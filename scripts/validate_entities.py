"""
엔티티 교차 검증 스크립트
──────────────────────────
1. PERSON 엔티티 vs member 테이블 매칭
2. 미매칭 PERSON 상위 출현빈도 → 추가 블랙리스트 후보
3. ORG 엔티티 빈도 분석
4. 전체 엔티티 품질 보고서

Pi5에서 실행: python scripts/validate_entities.py
"""
import sqlite3
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH


def validate():
    conn = sqlite3.connect(str(DB_PATH))

    print("=" * 60)
    print("🔍 엔티티 교차 검증 리포트")
    print("=" * 60)

    # ── 1. 기본 통계 ──
    total_entities = conn.execute("SELECT COUNT(*) FROM clause_entity").fetchone()[0]
    person_count = conn.execute(
        "SELECT COUNT(*) FROM clause_entity WHERE entity_type='PERSON'"
    ).fetchone()[0]
    org_count = conn.execute(
        "SELECT COUNT(*) FROM clause_entity WHERE entity_type='ORG'"
    ).fetchone()[0]
    law_count = conn.execute(
        "SELECT COUNT(*) FROM clause_entity WHERE entity_type='LAW'"
    ).fetchone()[0]

    print(f"\n전체 엔티티: {total_entities:,}개")
    print(f"  PERSON: {person_count:,}개")
    print(f"  ORG: {org_count:,}개")
    print(f"  LAW: {law_count:,}개")
    print(f"  기타: {total_entities - person_count - org_count - law_count:,}개")

    # ── 2. member 테이블 ──
    members = conn.execute("SELECT member_id, name, party FROM member").fetchall()
    member_names = {row[1] for row in members}
    print(f"\nmember 테이블: {len(members)}명")

    # ── 3. PERSON 엔티티 빈도 ──
    person_rows = conn.execute("""
        SELECT entity_text, COUNT(*) as cnt
        FROM clause_entity
        WHERE entity_type = 'PERSON'
        GROUP BY entity_text
        ORDER BY cnt DESC
    """).fetchall()

    unique_persons = len(person_rows)
    print(f"\n고유 PERSON 엔티티: {unique_persons}개")

    # 매칭/미매칭 분류
    matched = []
    unmatched = []
    for name, cnt in person_rows:
        if name in member_names:
            matched.append((name, cnt))
        else:
            unmatched.append((name, cnt))

    matched_count = sum(c for _, c in matched)
    unmatched_count = sum(c for _, c in unmatched)

    print(f"\n── PERSON vs member 매칭 ──")
    print(f"  매칭: {len(matched)}명 / {matched_count:,}건 ({matched_count/max(person_count,1)*100:.1f}%)")
    print(f"  미매칭: {len(unmatched)}명 / {unmatched_count:,}건 ({unmatched_count/max(person_count,1)*100:.1f}%)")

    # ── 4. 매칭된 의원 상위 ──
    print(f"\n── 매칭 의원 상위 30 (가장 많이 언급된 의원) ──")
    for name, cnt in matched[:30]:
        # 당적 찾기
        party = next((p for mid, n, p in members if n == name), "?")
        print(f"  {name} ({party}): {cnt:,}건")

    # ── 5. 미매칭 PERSON 상위 (노이즈 후보) ──
    print(f"\n── 미매칭 PERSON 상위 50 (블랙리스트 후보) ──")
    for name, cnt in unmatched[:50]:
        # 2글자 이상 한글인지 체크
        is_korean_name = len(name) >= 2 and all('\uac00' <= c <= '\ud7a3' for c in name)
        marker = "👤" if is_korean_name and len(name) in (2, 3) else "⚠️"
        print(f"  {marker} {name}: {cnt:,}건")

    # ── 6. 미매칭 중 이름 형태 (2-3글자 한글) ──
    name_like = [(n, c) for n, c in unmatched
                 if len(n) in (2, 3) and all('\uac00' <= ch <= '\ud7a3' for ch in n)]
    noise_like = [(n, c) for n, c in unmatched
                  if not (len(n) in (2, 3) and all('\uac00' <= ch <= '\ud7a3' for ch in n))]

    print(f"\n── 미매칭 유형 분류 ──")
    print(f"  이름 형태 (2-3글자 한글): {len(name_like)}개 / {sum(c for _,c in name_like):,}건")
    print(f"    → 가능성: 전직 의원, 정부 인사, 증인 등")
    print(f"  비이름 형태: {len(noise_like)}개 / {sum(c for _,c in noise_like):,}건")
    print(f"    → 가능성: 노이즈 (블랙리스트 추가 대상)")

    # ── 7. 비이름 형태 상위 (확실한 노이즈) ──
    print(f"\n── 확실한 노이즈 후보 (비이름 형태 상위 30) ──")
    for name, cnt in noise_like[:30]:
        print(f"  ⚠️ \"{name}\": {cnt:,}건")

    # ── 8. ORG 엔티티 분석 ──
    org_rows = conn.execute("""
        SELECT entity_text, COUNT(*) as cnt
        FROM clause_entity
        WHERE entity_type = 'ORG'
        GROUP BY entity_text
        ORDER BY cnt DESC
    """).fetchall()

    print(f"\n── ORG 엔티티 상위 30 ──")
    for name, cnt in org_rows[:30]:
        print(f"  {name}: {cnt:,}건")

    # ── 9. 신호에 나타난 타깃 vs member 매칭 ──
    signal_targets = conn.execute("""
        SELECT target_entity, COUNT(*) as cnt,
               AVG(composite_score) as avg_score
        FROM signal
        GROUP BY target_entity
        ORDER BY cnt DESC
    """).fetchall()

    print(f"\n── 신호 타깃 중 의원 매칭 ──")
    signal_member_count = 0
    signal_non_member = 0
    for target, cnt, avg in signal_targets:
        if target in member_names:
            signal_member_count += 1
            if signal_member_count <= 15:
                party = next((p for mid, n, p in members if n == target), "?")
                print(f"  👤 {target} ({party}): {cnt}건 신호, avg={avg:.3f}")

    for target, cnt, avg in signal_targets:
        if target not in member_names:
            signal_non_member += 1

    print(f"\n  신호 타깃 중 의원: {signal_member_count}명")
    print(f"  신호 타깃 중 비의원: {signal_non_member}개 (기관/기타)")

    # ── 10. 요약 ──
    print(f"\n{'='*60}")
    print(f"📊 요약")
    print(f"  PERSON 정확도 (member 매칭률): {len(matched)}/{unique_persons} = {len(matched)/max(unique_persons,1)*100:.1f}%")
    print(f"  PERSON 커버리지 (건수 기준): {matched_count}/{person_count} = {matched_count/max(person_count,1)*100:.1f}%")
    print(f"  블랙리스트 추가 후보: {len(noise_like)}개")
    print(f"  추가 조사 필요 (이름 형태 미매칭): {len(name_like)}개")

    conn.close()


if __name__ == "__main__":
    validate()
