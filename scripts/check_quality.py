"""
신호 퀄리티 점검 스크립트
─────────────────────────
상위 신호를 실제 회의록과 대조해서 품질을 검증.

검증 항목:
1. evidence 패킷에 대표 발언이 있는가?
2. 발언 내용이 이슈/타깃과 실제 관련 있는가?
3. 신호 유형(burst/pressure)이 데이터와 부합하는가?
4. 노이즈 신호 비율은?

Pi5에서 실행: python scripts/check_quality.py [N]
  N: 검사할 상위 신호 수 (기본 20)
"""
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH


def check(limit=20):
    conn = sqlite3.connect(str(DB_PATH))

    print("=" * 70)
    print(f"🔍 신호 퀄리티 점검 (상위 {limit}개)")
    print("=" * 70)

    # 상위 신호 조회
    signals = conn.execute("""
        SELECT signal_id, year_week, signal_type, issue_id, target_entity,
               composite_score, salience, pressure, spread,
               frame_shift, response_shift, evidence_json
        FROM signal
        ORDER BY composite_score DESC
        LIMIT ?
    """, (limit,)).fetchall()

    if not signals:
        print("❌ 신호 없음")
        return

    quality_scores = []
    issues_found = []

    for idx, row in enumerate(signals, 1):
        (sig_id, yw, stype, issue, target, comp, sal, pres, spr,
         fshift, rshift, evidence_raw) = row

        print(f"\n{'─'*70}")
        print(f"#{idx} [{yw}] {issue} → {target}")
        print(f"   유형: {stype} | 종합: {comp:.3f} | "
              f"돌출: {sal:.3f} | 압박: {pres:.3f} | 확산: {spr:.3f}")

        # ── 1. Evidence 패킷 확인 ──
        evidence = {}
        try:
            evidence = json.loads(evidence_raw) if evidence_raw else {}
        except json.JSONDecodeError:
            print("   ⚠️ evidence JSON 파싱 실패")
            issues_found.append(f"#{idx}: evidence JSON 오류")

        clauses = evidence.get("top_clauses", [])
        meetings = evidence.get("meetings", [])

        has_evidence = len(clauses) > 0
        has_meetings = len(meetings) > 0

        if has_evidence:
            print(f"   ✅ 대표 발언: {len(clauses)}건")
            for c in clauses[:3]:
                text = c.get("text", "")[:80]
                speaker = c.get("speaker", "?")
                act = c.get("act", "?")
                print(f"      [{act}] {speaker}: \"{text}...\"")
        else:
            print("   ❌ 대표 발언 없음 (evidence 비어있음)")
            issues_found.append(f"#{idx}: {yw} {issue}→{target} evidence 없음")

        if has_meetings:
            print(f"   ✅ 관련 회의: {len(meetings)}건")
            for m in meetings[:2]:
                print(f"      {m.get('date','')} {m.get('committee','')}: {m.get('title','')[:50]}")
        else:
            print("   ⚠️ 관련 회의 정보 없음")

        # ── 2. 이슈-발언 관련성 확인 ──
        relevance = "unknown"
        if clauses:
            # 발언 텍스트에 이슈 키워드가 포함되어 있는지 간이 확인
            from tagger.rule_tagger import POLICY_DOMAINS
            keywords = POLICY_DOMAINS.get(issue, [])
            relevant_count = 0
            for c in clauses:
                text = c.get("text", "")
                if any(kw in text for kw in keywords):
                    relevant_count += 1
            if relevant_count > 0:
                relevance = f"✅ 관련 ({relevant_count}/{len(clauses)})"
            else:
                relevance = f"⚠️ 키워드 불일치 (0/{len(clauses)})"
                issues_found.append(f"#{idx}: {issue} 키워드 미발견 in evidence")
        print(f"   이슈 관련성: {relevance}")

        # ── 3. 타깃-발언 관련성 확인 ──
        target_found = False
        for c in clauses:
            if target in c.get("text", "") or target in c.get("speaker", ""):
                target_found = True
                break
        if target_found:
            print(f"   타깃 관련성: ✅ \"{target}\" 발견")
        elif clauses:
            # entity 테이블에서 해당 주간에 target이 실제 있는지 확인
            year, week = yw.split("-W")
            monday = datetime.strptime(f"{year}-W{int(week)}-1", "%G-W%V-%u")
            sunday = monday + timedelta(days=6)
            entity_count = conn.execute("""
                SELECT COUNT(*)
                FROM clause_entity ce
                JOIN clause c ON ce.clause_id = c.clause_id
                JOIN utterance u ON c.utterance_id = u.utterance_id
                JOIN meeting m ON u.meeting_id = m.meeting_id
                WHERE ce.entity_text = ?
                  AND m.meeting_date BETWEEN ? AND ?
            """, (target, monday.strftime("%Y-%m-%d"),
                  sunday.strftime("%Y-%m-%d"))).fetchone()[0]
            if entity_count > 0:
                print(f"   타깃 관련성: 🟡 evidence에 없지만 DB에 {entity_count}건 존재")
            else:
                print(f"   타깃 관련성: ❌ \"{target}\" 해당 주간 미발견")
                issues_found.append(f"#{idx}: target \"{target}\" not found in {yw}")

        # ── 4. 채널 점수 합리성 확인 ──
        dominant_channel = max(
            [("salience", sal), ("pressure", pres), ("spread", spr),
             ("frame_shift", fshift), ("response_shift", rshift)],
            key=lambda x: x[1]
        )
        channel_match = (
            (stype == "burst" and dominant_channel[0] == "salience") or
            (stype == "pressure_growth" and dominant_channel[0] == "pressure") or
            (stype == "diffusion" and dominant_channel[0] == "spread") or
            (stype == "frame_shift" and dominant_channel[0] == "frame_shift") or
            (stype == "response_shift" and dominant_channel[0] == "response_shift")
        )
        if channel_match:
            print(f"   채널 일치: ✅ {stype} (최고: {dominant_channel[0]}={dominant_channel[1]:.3f})")
        else:
            print(f"   채널 일치: ⚠️ {stype} vs 최고: {dominant_channel[0]}={dominant_channel[1]:.3f}")

        # ── 5. 품질 점수 산정 ──
        q_score = 0
        if has_evidence:
            q_score += 30
        if has_meetings:
            q_score += 10
        if "✅" in relevance:
            q_score += 30
        if target_found:
            q_score += 20
        if channel_match:
            q_score += 10
        quality_scores.append(q_score)
        print(f"   📊 품질 점수: {q_score}/100")

    # ── 종합 리포트 ──
    print(f"\n{'='*70}")
    print(f"📊 종합 퀄리티 리포트")
    print(f"{'='*70}")

    avg_quality = sum(quality_scores) / len(quality_scores)
    good = sum(1 for q in quality_scores if q >= 70)
    medium = sum(1 for q in quality_scores if 40 <= q < 70)
    bad = sum(1 for q in quality_scores if q < 40)

    print(f"  검사 신호: {len(quality_scores)}개")
    print(f"  평균 품질: {avg_quality:.0f}/100")
    print(f"  양호 (>=70): {good}개 ({good/len(quality_scores)*100:.0f}%)")
    print(f"  보통 (40~69): {medium}개 ({medium/len(quality_scores)*100:.0f}%)")
    print(f"  미흡 (<40): {bad}개 ({bad/len(quality_scores)*100:.0f}%)")

    if avg_quality >= 70:
        grade = "A (프로덕션 준비 완료)"
    elif avg_quality >= 50:
        grade = "B (사용 가능, 개선 필요)"
    elif avg_quality >= 30:
        grade = "C (주의 필요)"
    else:
        grade = "D (심각한 개선 필요)"
    print(f"  등급: {grade}")

    if issues_found:
        print(f"\n  ⚠️ 발견된 문제 ({len(issues_found)}건):")
        for issue in issues_found[:15]:
            print(f"    • {issue}")

    print(f"\n💡 다음 단계:")
    if avg_quality >= 50:
        print("  → 주간 자동 업데이트 설정 가능")
        print("  → evidence 빈 신호는 LLM 보강으로 해결")
    else:
        print("  → evidence 빈 신호 원인 분석 필요")
        print("  → policy_domain 키워드 사전 확대 검토")

    conn.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    check(n)
