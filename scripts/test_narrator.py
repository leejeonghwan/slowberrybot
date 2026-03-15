"""
narrator 진단 스크립트.
1위 신호에 대해 발언 수집 → LLM 호출 과정을 단계별로 출력.
"""
import json
import sqlite3
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

conn = sqlite3.connect(str(DB_PATH))

# 1. 1위 신호 확인
print("=" * 50)
print("STEP 1: 상위 신호")
print("=" * 50)
row = conn.execute("""
    SELECT signal_id, year_week, issue_id, target_entity, composite_score
    FROM signal ORDER BY composite_score DESC LIMIT 1
""").fetchone()

if not row:
    print("신호 없음!")
    sys.exit(1)

sig_id, year_week, issue, target, score = row
print(f"  signal_id: {sig_id}")
print(f"  {year_week} | {issue} → {target} | {score:.3f}")

# 2. 해당 주간 clause 수 확인
print()
print("=" * 50)
print("STEP 2: 해당 주간 clause 수")
print("=" * 50)

from datetime import datetime, timedelta
year, week = year_week.split("-W")
monday = datetime.strptime(f"{year}-W{int(week)}-1", "%G-W%V-%u")
sunday = monday + timedelta(days=6)
date_from = monday.strftime("%Y-%m-%d")
date_to = sunday.strftime("%Y-%m-%d")
print(f"  기간: {date_from} ~ {date_to}")

total_clauses = conn.execute("""
    SELECT COUNT(*)
    FROM clause c
    JOIN utterance u ON c.utterance_id = u.utterance_id
    JOIN meeting m ON u.meeting_id = m.meeting_id
    WHERE m.meeting_date BETWEEN ? AND ?
""", (date_from, date_to)).fetchone()[0]
print(f"  전체 clause: {total_clauses}")

# policy_domain 매칭
domain_count = conn.execute("""
    SELECT COUNT(*)
    FROM clause c
    JOIN utterance u ON c.utterance_id = u.utterance_id
    JOIN meeting m ON u.meeting_id = m.meeting_id
    JOIN clause_tag pd ON c.clause_id = pd.clause_id
        AND pd.axis = 'policy_domain' AND pd.value = ?
    WHERE m.meeting_date BETWEEN ? AND ?
      AND c.char_count > 30
""", (issue, date_from, date_to)).fetchone()[0]
print(f"  policy_domain='{issue}' 매칭: {domain_count}")

# entity 매칭
entity_count = conn.execute("""
    SELECT COUNT(*)
    FROM clause c
    JOIN utterance u ON c.utterance_id = u.utterance_id
    JOIN meeting m ON u.meeting_id = m.meeting_id
    JOIN clause_entity ce ON c.clause_id = ce.clause_id
        AND ce.entity_text = ?
    WHERE m.meeting_date BETWEEN ? AND ?
      AND c.char_count > 30
""", (target, date_from, date_to)).fetchone()[0]
print(f"  entity='{target}' 매칭: {entity_count}")

# 3. narrator 실행
print()
print("=" * 50)
print("STEP 3: narrator 실행")
print("=" * 50)

from explainer.narrator import Narrator

narrator = Narrator(notify_fn=lambda m: print(f"  [notify] {m}"))
print(f"  backend: {narrator.backend}")
print(f"  client: {type(narrator.client).__name__ if narrator.client else 'None'}")

if not narrator.client:
    print("  ⚠️ LLM 클라이언트 없음 — 폴백 모드로 실행됩니다!")

# 발언 수집
clauses = narrator._gather_clauses(issue, target, year_week)
print(f"  수집된 발언: {len(clauses)}건")

if clauses:
    print()
    print("  처음 3건:")
    for i, (cid, text, speaker, role, comm, act, party) in enumerate(clauses[:3], 1):
        print(f"    {i}. [{act or '?'}] {speaker}({party or '?'}/{role or '?'})")
        print(f"       \"{text[:100]}...\"")

# 4. LLM 호출 테스트
print()
print("=" * 50)
print("STEP 4: LLM 기사 생성")
print("=" * 50)

article = narrator.narrate_signal(sig_id)
print()
print("--- 기사 시작 ---")
print(article)
print("--- 기사 끝 ---")
print()
print(f"기사 길이: {len(article)}자")

narrator.close()
conn.close()
