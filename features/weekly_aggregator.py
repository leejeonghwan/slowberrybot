"""
Step 4: Feature Store - 주간 집계 (v3)
──────────────────────────────────────
이슈(policy_domain) × 타깃(ORG/PERSON) × 위원회 × 주간 단위로 신호 feature를 계산.
Pi5 로컬에서 SQLite 쿼리로 처리. LLM 호출 없음.

v3 개선:
- 교차오염 방지: ENTITY_DOMAIN_MAP으로 기관-도메인 정합성 검증
  (예: "검찰"은 "법과질서" 소관이므로 "교통→검찰" 조합 차단)
- clause_tag의 policy_domain을 issue_id로 직접 사용
- clause_entity의 ORG/PERSON을 target_entity로 사용
- clause_issue 테이블 의존 제거 (아직 미구현이므로)
- 전체 기간 집계 지원 (aggregate_all)
- 진행 보고 notify_fn
"""
import json
import math
import sqlite3
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

logger = logging.getLogger(__name__)


# ── 기관→소관 도메인 매핑 (교차오염 방지) ──
# 주요 기관/부처가 어떤 정책 도메인에 속하는지 정의.
# 매핑에 없는 엔티티(인물 등)는 모든 도메인과 조합 허용.
ENTITY_DOMAIN_MAP = {
    # 법과질서
    "검찰": {"법과질서"},
    "경찰청": {"법과질서"},
    "법무부": {"법과질서", "시민권/자유"},
    # 국방/안보
    "국방부": {"국방"},
    "국정원": {"국방", "외교"},
    "국가정보원": {"국방", "외교"},
    # 경제/재정
    "기획재정부": {"거시경제", "금융"},
    "국세청": {"거시경제"},
    "관세청": {"무역", "거시경제"},
    "한국은행": {"금융", "거시경제"},
    # 금융
    "금융위원회": {"금융"},
    "금융감독원": {"금융"},
    # 산업/통상/에너지
    "산업통상자원부": {"에너지", "무역", "거시경제"},
    # 교육
    "교육부": {"교육"},
    # 보건의료
    "보건복지부": {"보건의료", "복지"},
    "건강보험공단": {"보건의료"},
    # 복지
    "국민연금": {"복지"},
    # 환경
    "환경부": {"환경"},
    # 노동
    "고용노동부": {"노동/고용"},
    # 국토/교통/주거
    "국토교통부": {"교통", "주거", "토지/수자원"},
    # 농림/수산
    "농림축산식품부": {"농업/식품"},
    "해양수산부": {"농업/식품"},
    # 과학기술/ICT
    "과학기술정보통신부": {"과학기술"},
    # 문화/방송
    "문화체육관광부": {"문화/여가"},
    "방송통신위원회": {"문화/여가", "과학기술"},
    # 행정/정부운영
    "행정안전부": {"정부운영"},
    "감사원": {"정부운영", "법과질서"},
    "국민권익위원회": {"정부운영"},
    "공정거래위원회": {"거시경제", "금융"},
    # 여성/가족
    "여성가족부": {"복지", "시민권/자유"},
    # 외교
    "외교부": {"외교"},
    "통일부": {"외교", "국방"},
    # 보훈
    "국가보훈부": {"국방", "복지"},
    # 중소기업
    "중소벤처기업부": {"거시경제", "무역"},
    # 이민
    "법무부출입국": {"이민"},
}


def _is_valid_domain_target(domain: str, target: str) -> bool:
    """
    도메인×타깃 조합이 유효한지 검증.
    - 매핑에 있는 기관이면: 해당 도메인이 소관 목록에 있어야 통과
    - 매핑에 없으면 (인물, 미등록 기관): 모든 도메인 허용
    - _none, _unknown 등 특수 타깃: 모든 도메인 허용
    """
    if target.startswith("_") or not target:
        return True
    if domain.startswith("_") or not domain:
        return True
    valid_domains = ENTITY_DOMAIN_MAP.get(target)
    if valid_domains is None:
        return True  # 매핑에 없는 엔티티는 제한 안 함
    return domain in valid_domains


class WeeklyAggregator:
    def __init__(self, notify_fn=None):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.notify = notify_fn or (lambda msg: logger.info(msg))

    def _year_week(self, date_str: str) -> str:
        """날짜 → 'YYYY-Www' 형식"""
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def _week_date_range(self, year_week: str) -> tuple:
        """ISO 주간 → (월요일, 일요일) 날짜 문자열"""
        year, week = year_week.split("-W")
        # ISO week 기준
        monday = datetime.strptime(f"{year}-W{int(week)}-1", "%G-W%V-%u")
        sunday = monday + timedelta(days=6)
        return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")

    def aggregate_week(self, year_week: str) -> dict:
        """
        특정 주간의 feature 집계.
        v2: clause_tag의 policy_domain → issue_id,
            clause_entity의 ORG/PERSON → target_entity
        """
        stats = {"rows_created": 0}

        date_from, date_to = self._week_date_range(year_week)

        # ── 1단계: 해당 주간 비절차 clause 목록 조회 ──
        clauses = self.conn.execute("""
            SELECT
                c.clause_id,
                m.committee_id,
                u.speaker_name,
                u.speaker_id,
                (SELECT party FROM member WHERE member_id = u.speaker_id) as party,
                u.speaker_role
            FROM clause c
            JOIN utterance u ON c.utterance_id = u.utterance_id
            JOIN meeting m ON u.meeting_id = m.meeting_id
            WHERE m.meeting_date BETWEEN ? AND ?
              AND u.is_procedural = 0
        """, (date_from, date_to)).fetchall()

        if not clauses:
            return stats

        clause_ids = [row[0] for row in clauses]
        clause_meta = {row[0]: row[1:] for row in clauses}  # clause_id → (committee, speaker, ...)

        # ── 2단계: clause별 policy_domain (= issue_id) 조회 ──
        placeholders = ",".join("?" * len(clause_ids))
        domain_rows = self.conn.execute(f"""
            SELECT clause_id, value
            FROM clause_tag
            WHERE clause_id IN ({placeholders})
              AND axis = 'policy_domain'
        """, clause_ids).fetchall()

        clause_domains = defaultdict(list)
        for cid, domain in domain_rows:
            clause_domains[cid].append(domain)

        # ── 3단계: clause별 target_entity (ORG/PERSON) 조회 ──
        entity_rows = self.conn.execute(f"""
            SELECT clause_id, entity_type, entity_text
            FROM clause_entity
            WHERE clause_id IN ({placeholders})
              AND entity_type IN ('ORG', 'PERSON')
        """, clause_ids).fetchall()

        clause_targets = defaultdict(list)
        for cid, etype, etext in entity_rows:
            clause_targets[cid].append(etext)

        # ── 4단계: clause별 speech_act, tone_conflict 조회 ──
        act_rows = self.conn.execute(f"""
            SELECT clause_id, axis, value
            FROM clause_tag
            WHERE clause_id IN ({placeholders})
              AND axis IN ('speech_act', 'tone_conflict', 'frame_type', 'response_mode')
        """, clause_ids).fetchall()

        clause_acts = defaultdict(dict)  # clause_id → {axis: value}
        for cid, axis, value in act_rows:
            clause_acts[cid][axis] = value

        # ── 5단계: 이슈 × 타깃 × 위원회 그룹핑 ──
        groups = defaultdict(lambda: {
            "mentions": 0,
            "speakers": set(),
            "parties": set(),
            "committees": set(),
            "roles": set(),
            "acts": Counter(),
            "frames": Counter(),
            "responses": Counter(),
            "tones": Counter(),
        })

        for cid in clause_ids:
            meta = clause_meta[cid]
            committee, speaker, speaker_id, party, role = meta
            domains = clause_domains.get(cid, ["_unknown"])
            targets = clause_targets.get(cid, ["_none"])
            acts = clause_acts.get(cid, {})

            # 도메인 × 타깃 조합 (v3: 교차오염 필터링)
            for domain in domains:
                for target in targets:
                    if not _is_valid_domain_target(domain, target):
                        continue  # 불합리한 조합 건너뛰기
                    key = (domain, target, committee or "_none")
                    g = groups[key]
                    g["mentions"] += 1
                    if speaker:
                        g["speakers"].add(speaker)
                    if party:
                        g["parties"].add(party)
                    if committee:
                        g["committees"].add(committee)
                    if role:
                        g["roles"].add(role)

                    if "speech_act" in acts:
                        g["acts"][acts["speech_act"]] += 1
                    if "frame_type" in acts:
                        g["frames"][acts["frame_type"]] += 1
                    if "response_mode" in acts:
                        g["responses"][acts["response_mode"]] += 1
                    if "tone_conflict" in acts:
                        g["tones"][acts["tone_conflict"]] += 1

        # ── 6단계: DB 적재 ──
        # 기존 해당 주간 데이터 삭제 (재집계 대비)
        self.conn.execute("DELETE FROM weekly_feature WHERE year_week = ?", (year_week,))

        for (issue_id, target, committee), g in groups.items():
            # spread entropy: 당 × 위원회 × 역할의 다양성
            spread = (self._entropy(g["parties"])
                      + self._entropy(g["committees"])
                      + self._entropy(g["roles"]))

            # pressure score: (비판+공격+수사적질문) / 전체 act
            pressure_acts = (g["acts"].get("비판", 0)
                             + g["acts"].get("공격", 0)
                             + g["acts"].get("수사적질문", 0))
            total_acts = sum(g["acts"].values()) or 1
            pressure = pressure_acts / total_acts

            self.conn.execute("""
                INSERT OR REPLACE INTO weekly_feature
                    (year_week, issue_id, target_entity, committee_id,
                     mention_count, speaker_count, party_count,
                     act_question, act_critique, act_attack, act_defend, act_propose, act_support,
                     pressure_score, spread_entropy, frame_dist_json, response_dist_json,
                     agenda_coupling)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                year_week, issue_id, target, committee,
                g["mentions"], len(g["speakers"]), len(g["parties"]),
                g["acts"].get("질문", 0) + g["acts"].get("수사적질문", 0),
                g["acts"].get("비판", 0),
                g["acts"].get("공격", 0),
                g["acts"].get("방어", 0),
                g["acts"].get("제안", 0),
                g["acts"].get("지지", 0),
                pressure, spread,
                json.dumps(dict(g["frames"]), ensure_ascii=False),
                json.dumps(dict(g["responses"]), ensure_ascii=False),
                0.0,  # agenda_coupling (추후 구현)
            ))
            stats["rows_created"] += 1

        self.conn.commit()
        logger.info(f"[{year_week}] feature 집계: {stats['rows_created']}건")
        return stats

    def _entropy(self, items) -> float:
        """Shannon entropy 계산"""
        if not items:
            return 0.0
        counts = Counter(items)
        total = sum(counts.values())
        ent = 0.0
        for c in counts.values():
            p = c / total
            if p > 0:
                ent -= p * math.log2(p)
        return ent

    def aggregate_range(self, start_date: str, end_date: str) -> dict:
        """날짜 범위에 대해 주간 단위로 반복 집계"""
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        total = {"weeks": 0, "rows_created": 0}

        # 전체 주간 목록 생성
        weeks = []
        current = start
        seen = set()
        while current <= end:
            yw = self._year_week(current.strftime("%Y-%m-%d"))
            if yw not in seen:
                weeks.append(yw)
                seen.add(yw)
            current += timedelta(weeks=1)

        self.notify(f"📊 **주간 집계 시작** ({len(weeks)}주)")
        start_time = time.time()

        for i, yw in enumerate(weeks, 1):
            stats = self.aggregate_week(yw)
            total["weeks"] += 1
            total["rows_created"] += stats["rows_created"]

            if i % 10 == 0:
                elapsed = time.time() - start_time
                self.notify(
                    f"📊 진행: {i}/{len(weeks)} ({i/len(weeks)*100:.0f}%) "
                    f"feature {total['rows_created']:,}개"
                )

        elapsed = time.time() - start_time
        self.notify(
            f"✅ **주간 집계 완료** ({elapsed:.0f}초)\n"
            f"{total['weeks']}주 / feature {total['rows_created']:,}개"
        )
        return total

    def aggregate_all(self) -> dict:
        """DB에 있는 전체 회의 기간 집계"""
        row = self.conn.execute(
            "SELECT MIN(meeting_date), MAX(meeting_date) FROM meeting WHERE meeting_date IS NOT NULL"
        ).fetchone()
        if not row or not row[0]:
            self.notify("❌ 회의 데이터 없음")
            return {"weeks": 0, "rows_created": 0}

        return self.aggregate_range(row[0], row[1])

    def get_stats(self) -> str:
        """집계 통계 조회"""
        total = self.conn.execute("SELECT COUNT(*) FROM weekly_feature").fetchone()[0]
        weeks = self.conn.execute("SELECT COUNT(DISTINCT year_week) FROM weekly_feature").fetchone()[0]
        issues = self.conn.execute("SELECT COUNT(DISTINCT issue_id) FROM weekly_feature").fetchone()[0]
        targets = self.conn.execute("SELECT COUNT(DISTINCT target_entity) FROM weekly_feature").fetchone()[0]

        lines = [
            f"📊 **집계 통계**",
            f"전체 feature: {total:,}개",
            f"주간: {weeks}주",
            f"이슈(도메인): {issues}개",
            f"타깃(엔티티): {targets}개",
        ]

        # 이슈별 상위
        top_issues = self.conn.execute("""
            SELECT issue_id, SUM(mention_count) as total
            FROM weekly_feature WHERE issue_id != '_unknown'
            GROUP BY issue_id ORDER BY total DESC LIMIT 10
        """).fetchall()
        if top_issues:
            lines.append(f"\n  [이슈 상위]")
            for issue, cnt in top_issues:
                lines.append(f"    {issue}: {cnt:,}")

        # 타깃별 상위
        top_targets = self.conn.execute("""
            SELECT target_entity, SUM(mention_count) as total
            FROM weekly_feature WHERE target_entity != '_none'
            GROUP BY target_entity ORDER BY total DESC LIMIT 10
        """).fetchall()
        if top_targets:
            lines.append(f"\n  [타깃 상위]")
            for target, cnt in top_targets:
                lines.append(f"    {target}: {cnt:,}")

        return "\n".join(lines)

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="주간 feature 집계")
    parser.add_argument("command", nargs="?", default="all",
                        choices=["all", "range", "week", "stats"],
                        help="실행 모드")
    parser.add_argument("--start", type=str, help="시작 날짜 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="종료 날짜 (YYYY-MM-DD)")
    parser.add_argument("--week", type=str, help="특정 주 (YYYY-Www)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    agg = WeeklyAggregator(notify_fn=lambda msg: print(msg))

    if args.command == "stats":
        print(agg.get_stats())
    elif args.command == "week" and args.week:
        stats = agg.aggregate_week(args.week)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    elif args.command == "range" and args.start and args.end:
        stats = agg.aggregate_range(args.start, args.end)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        stats = agg.aggregate_all()
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    agg.close()
