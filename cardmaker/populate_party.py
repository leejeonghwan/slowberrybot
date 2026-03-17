"""
utterance.speaker_party 일괄 채우기
───────────────────────────────────
member 테이블의 (name → party) 매핑으로
utterance.speaker_party가 NULL인 행을 업데이트.

사용법:
  python cardmaker/populate_party.py          # dry-run (업데이트 건수만 표시)
  python cardmaker/populate_party.py --apply  # 실제 적용
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH


def build_name_party_map(conn: sqlite3.Connection) -> dict[str, str]:
    """member 테이블에서 이름→정당 매핑 생성.
    동명이인이 있으면 최신(마지막) 레코드 우선."""
    rows = conn.execute("""
        SELECT name, party FROM member
        WHERE name IS NOT NULL AND name != ''
          AND party IS NOT NULL AND party != ''
    """).fetchall()

    mapping = {}
    for name, party in rows:
        mapping[name.strip()] = party.strip()

    return mapping


def populate(apply: bool = False):
    conn = sqlite3.connect(str(DB_PATH))

    # 1) 매핑 구축
    name_party = build_name_party_map(conn)
    print(f"📊 member 테이블: {len(name_party)}명 매핑")

    if not name_party:
        print("⚠️  member 테이블이 비어있습니다. 먼저 의원 정보를 수집하세요:")
        print("   python -m collector.fetch members")
        conn.close()
        return

    # 2) speaker_party가 NULL인 utterance 조회
    nulls = conn.execute("""
        SELECT COUNT(DISTINCT speaker_name)
        FROM utterance
        WHERE (speaker_party IS NULL OR speaker_party = '')
          AND speaker_name IS NOT NULL AND speaker_name != ''
    """).fetchone()[0]
    print(f"📊 speaker_party 미입력 화자: {nulls}명")

    # 3) 매칭 확인
    null_speakers = conn.execute("""
        SELECT DISTINCT speaker_name
        FROM utterance
        WHERE (speaker_party IS NULL OR speaker_party = '')
          AND speaker_name IS NOT NULL AND speaker_name != ''
    """).fetchall()

    matched = 0
    unmatched = []
    for (name,) in null_speakers:
        if name.strip() in name_party:
            matched += 1
        else:
            unmatched.append(name.strip())

    print(f"✅ 매칭 가능: {matched}명")
    print(f"❌ 매칭 불가: {len(unmatched)}명")

    if unmatched and len(unmatched) <= 30:
        print(f"   미매칭: {', '.join(unmatched[:30])}")

    # 4) 업데이트
    if apply:
        updated = 0
        for name, party in name_party.items():
            cur = conn.execute("""
                UPDATE utterance
                SET speaker_party = ?
                WHERE speaker_name = ?
                  AND (speaker_party IS NULL OR speaker_party = '')
            """, (party, name))
            updated += cur.rowcount

        conn.commit()
        print(f"\n💾 {updated}건 utterance 업데이트 완료")
    else:
        print(f"\n🔍 dry-run 모드. 실제 적용: python cardmaker/populate_party.py --apply")

    conn.close()


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    populate(apply=apply)
