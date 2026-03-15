"""
신호 분석 스크립트
──────────────────
grind 완료 후 신호 점수 분포를 분석하고 적절한 임계값을 제안.
Pi5에서 실행: python scripts/analyze_signals.py

출력:
1. 전체 신호 수 및 주간 분포
2. composite_score 분포 (히스토그램)
3. signal_type별 분포
4. 임계값별 필터링 결과 (0.15 → 0.20 → 0.25 → 0.30)
5. 상위 30개 신호 요약
"""
import json
import sqlite3
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH


def analyze():
    conn = sqlite3.connect(str(DB_PATH))

    # ── 1. 기본 통계 ──
    total = conn.execute("SELECT COUNT(*) FROM signal").fetchone()[0]
    weeks = conn.execute("SELECT COUNT(DISTINCT year_week) FROM signal").fetchone()[0]
    issues = conn.execute("SELECT COUNT(DISTINCT issue_id) FROM signal").fetchone()[0]
    targets = conn.execute("SELECT COUNT(DISTINCT target_entity) FROM signal").fetchone()[0]

    print("=" * 60)
    print(f"📡 신호 분석 리포트")
    print("=" * 60)
    print(f"전체 신호: {total:,}개")
    print(f"주간: {weeks}주")
    print(f"이슈: {issues}개")
    print(f"타깃: {targets}개")
    print()

    if total == 0:
        print("❌ 신호 데이터 없음. grind를 먼저 실행하세요.")
        return

    # ── 2. composite_score 분포 ──
    scores = conn.execute(
        "SELECT composite_score FROM signal ORDER BY composite_score DESC"
    ).fetchall()
    scores = [s[0] for s in scores]

    print("── composite_score 분포 ──")
    buckets = {}
    for s in scores:
        bucket = round(s, 1)  # 0.1 단위
        buckets[bucket] = buckets.get(bucket, 0) + 1

    for bucket in sorted(buckets.keys(), reverse=True):
        count = buckets[bucket]
        bar = "█" * min(count // 5, 50)
        print(f"  {bucket:.1f}: {count:>5}개 {bar}")

    print()
    print(f"  평균: {sum(scores)/len(scores):.4f}")
    print(f"  중앙값: {sorted(scores)[len(scores)//2]:.4f}")
    print(f"  최대: {max(scores):.4f}")
    print(f"  최소: {min(scores):.4f}")
    print()

    # ── 3. 임계값별 필터링 ──
    print("── 임계값별 신호 수 ──")
    thresholds = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]
    for t in thresholds:
        count = sum(1 for s in scores if s >= t)
        marker = " ◀ 현재" if t == 0.15 else ""
        print(f"  >= {t:.2f}: {count:>5}개{marker}")

    print()

    # ── 4. signal_type 분포 ──
    type_rows = conn.execute(
        "SELECT signal_type, COUNT(*) FROM signal GROUP BY signal_type ORDER BY COUNT(*) DESC"
    ).fetchall()
    print("── signal_type 분포 ──")
    for stype, count in type_rows:
        pct = count / total * 100
        print(f"  {stype}: {count:>5}개 ({pct:.1f}%)")
    print()

    # ── 5. 이슈별 상위 ──
    issue_rows = conn.execute("""
        SELECT issue_id, COUNT(*), AVG(composite_score), MAX(composite_score)
        FROM signal WHERE issue_id != '_unknown'
        GROUP BY issue_id ORDER BY MAX(composite_score) DESC LIMIT 20
    """).fetchall()
    print("── 이슈별 상위 (max score 기준) ──")
    for issue, cnt, avg, mx in issue_rows:
        print(f"  {issue}: {cnt}건, avg={avg:.3f}, max={mx:.3f}")
    print()

    # ── 6. 타깃별 상위 ──
    target_rows = conn.execute("""
        SELECT target_entity, COUNT(*), AVG(composite_score), MAX(composite_score)
        FROM signal WHERE target_entity != '_none'
        GROUP BY target_entity ORDER BY MAX(composite_score) DESC LIMIT 20
    """).fetchall()
    print("── 타깃별 상위 (max score 기준) ──")
    for target, cnt, avg, mx in target_rows:
        print(f"  {target}: {cnt}건, avg={avg:.3f}, max={mx:.3f}")
    print()

    # ── 7. 상위 30개 신호 ──
    top = conn.execute("""
        SELECT year_week, signal_type, issue_id, target_entity,
               composite_score, salience, pressure, spread
        FROM signal
        ORDER BY composite_score DESC
        LIMIT 30
    """).fetchall()
    print("── 상위 30 신호 ──")
    print(f"  {'주간':<10} {'유형':<18} {'이슈':<15} {'타깃':<15} {'종합':>6} {'돌출':>6} {'압박':>6} {'확산':>6}")
    print("  " + "-" * 100)
    for yw, stype, issue, target, comp, sal, pres, spr in top:
        print(f"  {yw:<10} {stype:<18} {issue[:14]:<15} {target[:14]:<15} "
              f"{comp:>6.3f} {sal:>6.3f} {pres:>6.3f} {spr:>6.3f}")

    print()

    # ── 8. 주간별 신호 수 추이 ──
    week_dist = conn.execute("""
        SELECT year_week, COUNT(*), AVG(composite_score)
        FROM signal GROUP BY year_week ORDER BY year_week
    """).fetchall()
    print("── 주간별 신호 수 추이 ──")
    for yw, cnt, avg in week_dist:
        bar = "█" * min(cnt // 3, 40)
        print(f"  {yw}: {cnt:>4}개 (avg={avg:.3f}) {bar}")

    # ── 9. 권장 임계값 ──
    print()
    print("=" * 60)
    # 상위 5% 기준
    top_5pct_idx = max(1, int(len(scores) * 0.05))
    top_10pct_idx = max(1, int(len(scores) * 0.10))
    top_20pct_idx = max(1, int(len(scores) * 0.20))

    print(f"📊 임계값 권장:")
    print(f"  상위 5% 기준: >= {scores[top_5pct_idx-1]:.3f} ({top_5pct_idx}개)")
    print(f"  상위 10% 기준: >= {scores[top_10pct_idx-1]:.3f} ({top_10pct_idx}개)")
    print(f"  상위 20% 기준: >= {scores[top_20pct_idx-1]:.3f} ({top_20pct_idx}개)")
    print()
    print("💡 추천: 주간 평균 10~30개 수준이 적절합니다.")
    target_per_week = 20
    target_total = target_per_week * weeks
    if target_total < len(scores):
        threshold_idx = min(target_total, len(scores) - 1)
        print(f"   주당 ~{target_per_week}개 목표 → 임계값 >= {scores[threshold_idx]:.3f}")

    conn.close()


if __name__ == "__main__":
    analyze()
