"""
태깅 품질 평가 스크립트
───────────────────────
55만 절의 태깅 커버리지와 품질을 분석.

분석 내용:
1. 축(axis)별 커버리지
2. speech_act 미태깅 절 샘플 분석 → 패턴 발견
3. policy_domain 커버리지와 미태깅 분석
4. tone_conflict 분포
5. evidence_type 활용도

Pi5에서 실행: python scripts/evaluate_quality.py
"""
import sqlite3
import sys
import random
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH


def evaluate():
    conn = sqlite3.connect(str(DB_PATH))

    print("=" * 60)
    print("📋 태깅 품질 평가 리포트")
    print("=" * 60)

    # ── 1. 기본 통계 ──
    total_clauses = conn.execute("SELECT COUNT(*) FROM clause").fetchone()[0]
    total_tags = conn.execute("SELECT COUNT(*) FROM clause_tag").fetchone()[0]
    total_entities = conn.execute("SELECT COUNT(*) FROM clause_entity").fetchone()[0]

    print(f"\n전체 절: {total_clauses:,}개")
    print(f"전체 태그: {total_tags:,}개")
    print(f"전체 엔티티: {total_entities:,}개")
    print(f"절당 평균 태그: {total_tags/max(total_clauses,1):.2f}개")

    # ── 2. 축별 커버리지 ──
    axes = conn.execute("""
        SELECT axis, COUNT(DISTINCT clause_id), COUNT(*)
        FROM clause_tag
        GROUP BY axis
    """).fetchall()

    print(f"\n── 축(axis)별 커버리지 ──")
    axis_coverage = {}
    for axis, unique_clauses, tag_count in axes:
        coverage = unique_clauses / max(total_clauses, 1) * 100
        axis_coverage[axis] = coverage
        print(f"  {axis}: {unique_clauses:,}절 ({coverage:.1f}%) / {tag_count:,}태그")

    # ── 3. speech_act 분석 ──
    print(f"\n── speech_act 상세 분석 ──")

    # speech_act 값별 분포
    act_dist = conn.execute("""
        SELECT value, COUNT(*) FROM clause_tag
        WHERE axis = 'speech_act'
        GROUP BY value ORDER BY COUNT(*) DESC
    """).fetchall()

    total_speech_acts = sum(c for _, c in act_dist)
    print(f"  태깅된 절: {total_speech_acts:,}개")
    for act, cnt in act_dist:
        pct = cnt / max(total_speech_acts, 1) * 100
        print(f"    {act}: {cnt:,}개 ({pct:.1f}%)")

    # 미태깅 절 샘플 분석
    untagged_speech = conn.execute("""
        SELECT c.clause_id, c.text
        FROM clause c
        WHERE c.clause_id NOT IN (
            SELECT clause_id FROM clause_tag WHERE axis = 'speech_act'
        )
        AND c.char_count > 20
        ORDER BY RANDOM()
        LIMIT 100
    """).fetchall()

    print(f"\n── speech_act 미태깅 절 샘플 (100개) ──")
    print(f"  총 미태깅: {total_clauses - total_speech_acts:,}개 ({(1 - total_speech_acts/max(total_clauses,1))*100:.1f}%)")

    # 패턴 분류
    patterns = Counter()
    sample_texts = {}
    for cid, text in untagged_speech:
        text = text.strip()
        if len(text) < 10:
            patterns["짧은 절 (<10자)"] += 1
        elif text.endswith(("습니다.", "입니다.", "합니다.")):
            patterns["서술형 종결 (-습니다)"] += 1
            if "서술형 종결" not in sample_texts:
                sample_texts["서술형 종결"] = text[:100]
        elif "?" in text or text.endswith(("까?", "요?", "지?")):
            patterns["의문형 (미분류 질문)"] += 1
            if "의문형" not in sample_texts:
                sample_texts["의문형"] = text[:100]
        elif text.endswith(("시오.", "십시오.", "시오")):
            patterns["명령/요청형 (-시오)"] += 1
            if "명령/요청형" not in sample_texts:
                sample_texts["명령/요청형"] = text[:100]
        elif text.endswith(("겠습니다.", "겠습니다")):
            patterns["의지/예정형 (-겠습니다)"] += 1
            if "의지/예정형" not in sample_texts:
                sample_texts["의지/예정형"] = text[:100]
        elif any(w in text for w in ["인사", "감사", "수고", "존경"]):
            patterns["의례적 발언"] += 1
        elif any(w in text for w in ["예산", "세입", "세출", "결산"]):
            patterns["예산/결산 관련"] += 1
        elif any(w in text for w in ["위원장", "의장", "간사"]):
            patterns["호칭/지칭"] += 1
        else:
            patterns["기타 미분류"] += 1
            if "기타" not in sample_texts:
                sample_texts["기타"] = text[:100]

    print(f"\n  미태깅 절 패턴 분포 (샘플 100개 기준):")
    for pattern, cnt in patterns.most_common():
        print(f"    {pattern}: {cnt}개")

    print(f"\n  미태깅 절 예시:")
    for ptype, text in sample_texts.items():
        print(f"    [{ptype}] {text}")

    # ── 4. policy_domain 분석 ──
    print(f"\n── policy_domain 상세 분석 ──")
    domain_dist = conn.execute("""
        SELECT value, COUNT(*) FROM clause_tag
        WHERE axis = 'policy_domain'
        GROUP BY value ORDER BY COUNT(*) DESC
    """).fetchall()

    total_domains = sum(c for _, c in domain_dist)
    print(f"  태깅된 절: {total_domains:,}개 ({total_domains/max(total_clauses,1)*100:.1f}%)")
    for domain, cnt in domain_dist[:15]:
        pct = cnt / max(total_domains, 1) * 100
        print(f"    {domain}: {cnt:,}개 ({pct:.1f}%)")

    # ── 5. tone_conflict 분석 ──
    print(f"\n── tone_conflict 분포 ──")
    tone_dist = conn.execute("""
        SELECT value, COUNT(*) FROM clause_tag
        WHERE axis = 'tone_conflict'
        GROUP BY value ORDER BY COUNT(*) DESC
    """).fetchall()
    total_tones = sum(c for _, c in tone_dist)
    print(f"  태깅된 절: {total_tones:,}개 ({total_tones/max(total_clauses,1)*100:.1f}%)")
    for tone, cnt in tone_dist:
        pct = cnt / max(total_tones, 1) * 100
        print(f"    {tone}: {cnt:,}개 ({pct:.1f}%)")

    # ── 6. 절 길이 vs 태깅 관계 ──
    print(f"\n── 절 길이 vs 태깅 관계 ──")
    length_analysis = conn.execute("""
        SELECT
            CASE
                WHEN c.char_count < 10 THEN '~10자'
                WHEN c.char_count < 30 THEN '10~30자'
                WHEN c.char_count < 60 THEN '30~60자'
                WHEN c.char_count < 100 THEN '60~100자'
                ELSE '100자+'
            END as length_group,
            COUNT(*) as total,
            COUNT(DISTINCT ct.clause_id) as tagged
        FROM clause c
        LEFT JOIN clause_tag ct ON c.clause_id = ct.clause_id
        GROUP BY length_group
        ORDER BY MIN(c.char_count)
    """).fetchall()
    for group, total, tagged in length_analysis:
        pct = tagged / max(total, 1) * 100
        print(f"  {group}: {total:,}절 중 {tagged:,}절 태깅 ({pct:.1f}%)")

    # ── 7. speaker_role vs speech_act ──
    print(f"\n── speaker_role별 speech_act 커버리지 ──")
    role_coverage = conn.execute("""
        SELECT u.speaker_role,
               COUNT(DISTINCT c.clause_id) as total_clauses,
               COUNT(DISTINCT ct.clause_id) as tagged_clauses
        FROM clause c
        JOIN utterance u ON c.utterance_id = u.utterance_id
        LEFT JOIN clause_tag ct ON c.clause_id = ct.clause_id AND ct.axis = 'speech_act'
        GROUP BY u.speaker_role
        ORDER BY COUNT(DISTINCT c.clause_id) DESC
    """).fetchall()
    for role, total, tagged in role_coverage[:10]:
        pct = tagged / max(total, 1) * 100
        role_str = role or "(없음)"
        print(f"  {role_str}: {total:,}절 중 {tagged:,}절 ({pct:.1f}%)")

    # ── 8. 종합 품질 점수 ──
    print(f"\n{'='*60}")
    print(f"📊 종합 품질 평가")

    # 점수 계산 (각 축 커버리지 가중 평균)
    weights = {
        "policy_domain": 0.35,
        "speech_act": 0.30,
        "tone_conflict": 0.15,
        "institutional_context": 0.10,
        "speaker_role": 0.10,
    }
    score = 0
    for axis, weight in weights.items():
        cov = axis_coverage.get(axis, 0)
        score += cov * weight
        print(f"  {axis}: {cov:.1f}% × {weight} = {cov*weight:.1f}")

    print(f"\n  종합 점수: {score:.1f}/100")

    if score < 30:
        grade = "D (개선 필요)"
    elif score < 50:
        grade = "C (보통)"
    elif score < 70:
        grade = "B (양호)"
    else:
        grade = "A (우수)"
    print(f"  등급: {grade}")

    print(f"\n💡 개선 권장:")
    if axis_coverage.get("speech_act", 0) < 50:
        print(f"  1. speech_act 커버리지 확대 (현재 {axis_coverage.get('speech_act',0):.1f}%)")
        print(f"     → 서술형 종결어(-습니다, -입니다)에 대한 규칙 추가")
    if axis_coverage.get("policy_domain", 0) < 50:
        print(f"  2. policy_domain 키워드 사전 확대 (현재 {axis_coverage.get('policy_domain',0):.1f}%)")
        print(f"     → 미태깅 절의 주요 키워드 빈도 분석 필요")

    conn.close()


if __name__ == "__main__":
    evaluate()
