"""
카드 테이블의 committee 필드 일괄 수정
────────────────────────────────────────
배치 생성 후 "위원회", "소위원회" 등 불완전한 위원회명을
"보건복지위원회", "법제사법위원회" 등 정식 명칭으로 교체.

방법:
  card.meeting_id → meeting.committee_id → committee.committee_name

사용법:
  python cardmaker/fix_card_committee.py          # dry-run (확인만)
  python cardmaker/fix_card_committee.py --apply  # 실제 적용
"""
import sqlite3
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH


def fix_committee(apply: bool = False):
    conn = sqlite3.connect(str(DB_PATH))

    # 1) 현재 카드의 committee 분포 확인
    dist = conn.execute("""
        SELECT committee, COUNT(*) as cnt
        FROM card
        GROUP BY committee
        ORDER BY cnt DESC
    """).fetchall()
    print(f"📊 현재 카드 committee 분포 ({len(dist)}종):")
    for name, cnt in dist[:30]:
        flag = "⚠️" if len(name or "") <= 6 else "  "
        print(f"  {flag} {name or '(없음)'}: {cnt}개")

    # 2) meeting → committee 매핑 구축
    #    meeting.committee_id → committee.committee_name
    mapping = conn.execute("""
        SELECT m.meeting_id, c.committee_name
        FROM meeting m
        JOIN committee c ON m.committee_id = c.committee_id
        WHERE c.committee_name IS NOT NULL AND c.committee_name != ''
    """).fetchall()
    meeting_to_committee = {mid: cname for mid, cname in mapping}
    print(f"\n📊 meeting→committee 매핑: {len(meeting_to_committee)}건")

    # 3) 수정 대상 카드 찾기
    cards = conn.execute("""
        SELECT card_id, meeting_id, committee FROM card
    """).fetchall()

    updates = []  # (new_committee, card_id)
    for card_id, meeting_id, current_committee in cards:
        correct = meeting_to_committee.get(meeting_id)
        if correct and correct != current_committee:
            updates.append((correct, card_id))

    print(f"\n📊 수정 대상: {len(updates)}개 카드 (전체 {len(cards)}개 중)")

    # 수정 내용 미리보기 (변경 유형별)
    changes = Counter()
    for new_val, cid in updates:
        old = next((c for ci, mi, c in cards if ci == cid), "?")
        changes[f"{old} → {new_val}"] += 1

    if changes:
        print(f"\n📋 변경 내역:")
        for change, cnt in changes.most_common(30):
            print(f"  {change}: {cnt}건")

    # 4) 적용
    if apply and updates:
        conn.executemany("""
            UPDATE card SET committee = ? WHERE card_id = ?
        """, updates)
        conn.commit()
        print(f"\n💾 {len(updates)}개 카드 committee 수정 완료!")

        # 수정 후 분포 재확인
        dist2 = conn.execute("""
            SELECT committee, COUNT(*) as cnt
            FROM card
            GROUP BY committee
            ORDER BY cnt DESC
        """).fetchall()
        print(f"\n📊 수정 후 committee 분포:")
        for name, cnt in dist2[:20]:
            print(f"  {name}: {cnt}개")

    elif not apply:
        print(f"\n🔍 dry-run 모드. 실제 적용: python cardmaker/fix_card_committee.py --apply")

    conn.close()


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    fix_committee(apply=apply)
