"""
특정 신호의 관련 발언 원문을 텍스트로 출력.
사용법:
  python scripts/dump_clauses.py              # 1위 신호
  python scripts/dump_clauses.py 21444        # 특정 signal_id
  python scripts/dump_clauses.py --week 2025-W39 --issue 금융 --target 금융감독원
"""
import sqlite3
import sys
import argparse
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

conn = sqlite3.connect(str(DB_PATH))


def get_signal(signal_id=None):
    if signal_id:
        row = conn.execute(
            "SELECT signal_id, year_week, issue_id, target_entity, composite_score "
            "FROM signal WHERE signal_id = ?", (signal_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT signal_id, year_week, issue_id, target_entity, composite_score "
            "FROM signal ORDER BY composite_score DESC LIMIT 1"
        ).fetchone()
    return row


def gather_clauses(issue, target, year_week):
    year, week = year_week.split("-W")
    monday = datetime.strptime(f"{year}-W{int(week)}-1", "%G-W%V-%u")
    sunday = monday + timedelta(days=6)
    date_from = monday.strftime("%Y-%m-%d")
    date_to = sunday.strftime("%Y-%m-%d")

    # policy_domain 매칭 (최대 40건)
    domain_rows = conn.execute("""
        SELECT c.clause_id, c.text, u.speaker_name, u.speaker_role,
               m.committee_id, m.meeting_date,
               (SELECT value FROM clause_tag
                WHERE clause_id = c.clause_id AND axis = 'speech_act'
                LIMIT 1) as act,
               (SELECT party FROM member WHERE member_id = u.speaker_id) as party
        FROM clause c
        JOIN utterance u ON c.utterance_id = u.utterance_id
        JOIN meeting m ON u.meeting_id = m.meeting_id
        JOIN clause_tag pd ON c.clause_id = pd.clause_id
            AND pd.axis = 'policy_domain' AND pd.value = ?
        WHERE m.meeting_date BETWEEN ? AND ?
          AND c.char_count > 30
        ORDER BY
            CASE WHEN EXISTS (
                SELECT 1 FROM clause_tag sa
                WHERE sa.clause_id = c.clause_id
                  AND sa.axis = 'speech_act'
                  AND sa.value IN ('비판', '공격', '수사적질문', '질문', '제안')
            ) THEN 0 ELSE 1 END,
            c.char_count DESC
        LIMIT 40
    """, (issue, date_from, date_to)).fetchall()

    # entity 매칭 (최대 20건)
    seen = {r[0] for r in domain_rows}
    entity_rows = conn.execute("""
        SELECT c.clause_id, c.text, u.speaker_name, u.speaker_role,
               m.committee_id, m.meeting_date,
               (SELECT value FROM clause_tag
                WHERE clause_id = c.clause_id AND axis = 'speech_act'
                LIMIT 1) as act,
               (SELECT party FROM member WHERE member_id = u.speaker_id) as party
        FROM clause c
        JOIN utterance u ON c.utterance_id = u.utterance_id
        JOIN meeting m ON u.meeting_id = m.meeting_id
        JOIN clause_entity ce ON c.clause_id = ce.clause_id
            AND ce.entity_text = ?
        WHERE m.meeting_date BETWEEN ? AND ?
          AND c.char_count > 30
        ORDER BY c.char_count DESC
        LIMIT 20
    """, (target, date_from, date_to)).fetchall()

    all_rows = list(domain_rows)
    for r in entity_rows:
        if r[0] not in seen:
            all_rows.append(r)
            seen.add(r[0])

    return all_rows, date_from, date_to


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("signal_id", nargs="?", type=int)
    parser.add_argument("--week", type=str)
    parser.add_argument("--issue", type=str)
    parser.add_argument("--target", type=str)
    args = parser.parse_args()

    if args.week and args.issue and args.target:
        year_week, issue, target = args.week, args.issue, args.target
        sig_id, score = None, None
    else:
        sig = get_signal(args.signal_id)
        if not sig:
            print("신호 없음")
            return
        sig_id, year_week, issue, target, score = sig

    clauses, date_from, date_to = gather_clauses(issue, target, year_week)

    # 출력
    print(f"{'='*60}")
    print(f"신호: {issue} → {target} ({year_week})")
    if score:
        print(f"signal_id: {sig_id} | score: {score:.3f}")
    print(f"기간: {date_from} ~ {date_to}")
    print(f"수집 발언: {len(clauses)}건")
    print(f"{'='*60}")
    print()

    for i, (cid, text, speaker, role, committee, mdate, act, party) in enumerate(clauses, 1):
        speaker_info = speaker or "?"
        if party:
            speaker_info += f" ({party})"
        if role:
            speaker_info += f" [{role}]"

        act_str = f" <{act}>" if act else ""
        print(f"--- {i}. {speaker_info}{act_str} | {committee} | {mdate} ---")
        print(text)
        print()

    conn.close()


if __name__ == "__main__":
    main()
