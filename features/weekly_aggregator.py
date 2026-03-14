"""
Step 4: Feature Store - 주간 집계
이슈 × 타깃 × 위원회 × 주간 단위로 신호 feature를 계산.
Pi5 로컬에서 SQLite 쿼리로 처리. LLM 호출 없음.
"""
import json
import math
import sqlite3
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

logger = logging.getLogger(__name__)


class WeeklyAggregator:
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))

    def _year_week(self, date_str: str) -> str:
        """날짜 → 'YYYY-Www' 형식"""
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def aggregate_week(self, year_week: str) -> dict:
        """특정 주간의 feature 집계"""
        stats = {"rows_created": 0}

        # 해당 주간 회의 목록
        # ISO week → 날짜 범위 계산
        year, week = year_week.split("-W")
        monday = datetime.strptime(f"{year}-W{int(week)}-1", "%Y-W%W-%w")
        if monday.year < int(year):
            monday = datetime.strptime(f"{year}-W{int(week)}-1", "%G-W%V-%u")
        sunday = monday + timedelta(days=6)

        date_from = monday.strftime("%Y-%m-%d")
        date_to = sunday.strftime("%Y-%m-%d")

        # 해당 주간 clause + tag 데이터 조회
        rows = self.conn.execute("""
            SELECT
                ci.issue_id,
                ce.entity_text as target_entity,
                m.committee_id,
                ct.axis, ct.value,
                u.speaker_name, u.speaker_id,
                (SELECT party FROM member WHERE member_id = u.speaker_id) as party,
                u.speaker_role
            FROM clause c
            JOIN utterance u ON c.utterance_id = u.utterance_id
            JOIN meeting m ON u.meeting_id = m.meeting_id
            LEFT JOIN clause_tag ct ON c.clause_id = ct.clause_id
            LEFT JOIN clause_issue ci ON c.clause_id = ci.clause_id
            LEFT JOIN clause_entity ce ON c.clause_id = ce.clause_id AND ce.entity_type = 'TARGET'
            WHERE m.meeting_date BETWEEN ? AND ?
              AND u.is_procedural = 0
        """, (date_from, date_to)).fetchall()

        if not rows:
            logger.info(f"[{year_week}] 데이터 없음")
            return stats

        # 이슈 × 타깃 × 위원회 단위로 집계
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
            "agendas": set(),
        })

        for (issue_id, target, committee, axis, value,
             speaker, speaker_id, party, role) in rows:
            # 이슈가 없는 clause는 '_unknown'으로
            key = (issue_id or "_unknown", target or "_none", committee or "_none")
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

            # 축별 분포 집계
            if axis == "speech_act":
                g["acts"][value] += 1
            elif axis == "frame_type":
                g["frames"][value] += 1
            elif axis == "response_mode":
                g["responses"][value] += 1
            elif axis == "tone_conflict":
                g["tones"][value] += 1

        # DB 적재
        for (issue_id, target, committee), g in groups.items():
            # spread entropy: 당 × 위원회 × 역할의 다양성
            spread = self._entropy(g["parties"]) + self._entropy(g["committees"]) + self._entropy(g["roles"])

            # pressure score: (비판+공격+요구) / 전체 act
            pressure_acts = g["acts"].get("비판", 0) + g["acts"].get("공격", 0) + g["acts"].get("정보요구", 0)
            total_acts = sum(g["acts"].values()) or 1
            pressure = pressure_acts / total_acts

            # agenda coupling은 별도 조인 필요 (간략화)
            agenda_coupling = 0.0

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
                g["acts"].get("질문", 0) + g["acts"].get("정보요구", 0),
                g["acts"].get("비판", 0),
                g["acts"].get("공격", 0),
                g["acts"].get("방어", 0),
                g["acts"].get("제안", 0),
                g["acts"].get("지지", 0),
                pressure, spread,
                json.dumps(dict(g["frames"]), ensure_ascii=False),
                json.dumps(dict(g["responses"]), ensure_ascii=False),
                agenda_coupling,
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

        current = start
        while current <= end:
            yw = self._year_week(current.strftime("%Y-%m-%d"))
            stats = self.aggregate_week(yw)
            total["weeks"] += 1
            total["rows_created"] += stats["rows_created"]
            current += timedelta(weeks=1)

        logger.info(f"범위 집계 완료: {total}")
        return total

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agg = WeeklyAggregator()
    # 최근 4주 집계
    end = datetime.now()
    start = end - timedelta(weeks=4)
    stats = agg.aggregate_range(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    agg.close()
