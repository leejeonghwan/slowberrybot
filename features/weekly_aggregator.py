"""
Step 4: Feature Store - 주간 집계 (v2)
──────────────────────────────────────
이슈(policy_domain) × 타깃(ORG/PERSON) × 위원회 × 주간 단위로 신호 feature를 계산.
Pi5 로컬에서 SQLite 쿼리로 처리. LLM 호출 없음.

v2 개선:
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

            # 도메인 × 타깃 모든 조합으로 그룹핑
            for domain in domains:
                for target in targets:
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
