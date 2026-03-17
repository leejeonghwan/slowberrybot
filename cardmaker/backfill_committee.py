"""
meeting.committee_id 일괄 채우기
────────────────────────────────
committee_id가 NULL인 meeting에 대해:
  1) committee 테이블의 committee_name으로 매핑 시도
  2) 해당 회의의 첫 utterance 텍스트에서 위원회명 추출
  3) 추출된 위원회명 → committee 테이블 매칭 → meeting.committee_id 업데이트

사용법:
  python cardmaker/backfill_committee.py          # dry-run
  python cardmaker/backfill_committee.py --apply  # 실제 적용
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

# 위원회명 추출 패턴들
COMMITTEE_PATTERNS = [
    # "법제사법위원회 제N차 전체회의"
    re.compile(r'([가-힣]+위원회)\s*제?\s*\d'),
    # "제N회 법제사법위원회"
    re.compile(r'제\s*\d+\s*회\s*([가-힣]+위원회)'),
    # "법제사법위원회를 개의"
    re.compile(r'([가-힣]+위원회)\s*(를|의|에서)'),
    # 단순 "OO위원회"
    re.compile(r'([가-힣]{2,}위원회)'),
]

# agenda_ids_json의 title에서 위원회명 추출
TITLE_COMMITTEE_PATTERN = re.compile(r'([가-힣]{2,}위원회)')


def extract_committee_from_text(text: str) -> str | None:
    """텍스트에서 위원회명 추출"""
    for pat in COMMITTEE_PATTERNS:
        m = pat.search(text)
        if m:
            name = m.group(1)
            # "소위원회" 등 너무 짧은 건 제외
            if len(name) >= 4:
                return name
    return None


def backfill(apply: bool = False):
    conn = sqlite3.connect(str(DB_PATH))

    # 1) committee 테이블에서 name→id 매핑
    try:
        rows = conn.execute("""
            SELECT committee_id, committee_name FROM committee
            WHERE committee_name IS NOT NULL
        """).fetchall()
        name_to_id = {name: cid for cid, name in rows}
        # 이름만으로도 매핑 가능하도록 역방향도 추가
        id_to_name = {cid: name for cid, name in rows}
        print(f"📊 committee 테이블: {len(name_to_id)}개 위원회")
    except Exception as e:
        print(f"⚠️ committee 테이블 없음: {e}")
        name_to_id = {}
        id_to_name = {}

    # 2) committee_id가 NULL인 meeting 조회
    null_meetings = conn.execute("""
        SELECT meeting_id, agenda_ids_json
        FROM meeting
        WHERE committee_id IS NULL OR committee_id = ''
    """).fetchall()
    print(f"📊 committee_id 미입력 회의: {len(null_meetings)}건")

    if not null_meetings:
        print("✅ 모든 회의에 committee_id가 있습니다!")
        conn.close()
        return

    # 3) 각 회의에 대해 위원회명 추출 시도
    found = 0
    not_found = 0
    results = []  # (meeting_id, committee_name, committee_id, source)

    for meeting_id, agenda_json in null_meetings:
        committee_name = None
        source = ""

        # 3-1) agenda_ids_json의 title에서 추출
        if agenda_json:
            import json
            try:
                agenda = json.loads(agenda_json)
                title = agenda.get("title", "")
                if title:
                    committee_name = extract_committee_from_text(title)
                    if committee_name:
                        source = "agenda_title"
            except (json.JSONDecodeError, AttributeError):
                pass

        # 3-2) 첫 utterance 텍스트에서 추출
        if not committee_name:
            first_utts = conn.execute("""
                SELECT raw_text FROM utterance
                WHERE meeting_id = ?
                ORDER BY sequence_no
                LIMIT 5
            """, (meeting_id,)).fetchall()

            for (text,) in first_utts:
                if text:
                    committee_name = extract_committee_from_text(text)
                    if committee_name:
                        source = "utterance"
                        break

        if committee_name:
            # committee 테이블에서 ID 매칭
            cid = name_to_id.get(committee_name, "")
            results.append((meeting_id, committee_name, cid, source))
            found += 1
        else:
            not_found += 1

    print(f"✅ 위원회명 추출 성공: {found}건")
    print(f"❌ 위원회명 추출 실패: {not_found}건")

    # 추출된 위원회명 분포
    from collections import Counter
    name_counts = Counter(r[1] for r in results)
    print(f"\n📊 위원회별 분포 (상위 20):")
    for name, cnt in name_counts.most_common(20):
        cid = name_to_id.get(name, "❌없음")
        print(f"  {name}: {cnt}건 (ID: {cid})")

    # 4) 업데이트
    if apply and results:
        updated = 0
        # committee 테이블에 없는 위원회는 추가
        new_committees = set()
        for _, cname, cid, _ in results:
            if not cid and cname not in new_committees:
                # committee_id = committee_name으로 사용 (코드가 없으므로)
                conn.execute("""
                    INSERT OR IGNORE INTO committee
                        (committee_id, committee_name, committee_type)
                    VALUES (?, ?, ?)
                """, (cname, cname, "상임위"))
                new_committees.add(cname)
                name_to_id[cname] = cname

        # meeting.committee_id 업데이트
        for meeting_id, cname, cid, _ in results:
            final_cid = cid or name_to_id.get(cname, cname)
            conn.execute("""
                UPDATE meeting SET committee_id = ?
                WHERE meeting_id = ? AND (committee_id IS NULL OR committee_id = '')
            """, (final_cid, meeting_id))
            updated += 1

        conn.commit()
        print(f"\n💾 {updated}건 meeting 업데이트 완료")
        if new_committees:
            print(f"   + {len(new_committees)}개 위원회 새로 추가: {', '.join(new_committees)}")
    elif not apply:
        print(f"\n🔍 dry-run 모드. 실제 적용: python cardmaker/backfill_committee.py --apply")

    conn.close()


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    backfill(apply=apply)
