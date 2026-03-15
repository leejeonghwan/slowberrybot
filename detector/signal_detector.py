"""
Step 5: 신호 탐지기
- 주간 feature 시계열에서 이상 신호를 탐지
- 5개 탐지 채널: burst / pressure_growth / frame_shift / response_shift / diffusion
- Pi5 로컬 처리. LLM 호출 없음.
- 종합 점수 계산 → evidence packet 조립
"""
import json
import math
import sqlite3
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

logger = logging.getLogger(__name__)


class SignalDetector:
    # 탐지 임계값 (v2: 8,223→~2,000 목표)
    COMPOSITE_THRESHOLD = 0.25  # 종합 점수 하한 (이전 0.15)
    BURST_ZSCORE = 2.5          # mention burst z-score 기준 (이전 2.0)
    PRESSURE_GROWTH = 0.3       # 주간 pressure 증가율 기준
    FRAME_DIVERGENCE = 0.5      # frame 분포 JS divergence 기준
    RESPONSE_SHIFT_THRESHOLD = 0.3
    SPREAD_GROWTH = 0.5         # entropy 증가율

    # _unknown 이슈 및 노이즈 엔티티 필터
    ISSUE_BLACKLIST = {"_unknown", "_none", ""}
    TARGET_BLACKLIST = {
        "_none", "", "지금", "현재", "최근", "우선", "다만",
        "그래서", "따라서", "그런데", "그러나", "하지만",
        "국회", "정부", "우리", "이것", "그것",
    }

    # 종합 점수 가중치
    WEIGHTS = {
        "salience": 0.15,
        "pressure": 0.25,
        "spread": 0.15,
        "frame_shift": 0.15,
        "response_shift": 0.15,
        "agenda_coupling": 0.10,
        "novelty": 0.05,
    }

    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))

    def detect_week(self, year_week: str, lookback_weeks: int = 8) -> list[dict]:
        """특정 주간의 신호 탐지"""
        signals = []

        # 현재 주와 과거 주 feature 조회
        current = self._get_features(year_week)
        if not current:
            return signals

        # lookback 주간 시계열
        history = self._get_history(year_week, lookback_weeks)

        for key, feat in current.items():
            issue_id, target, committee = key

            # 노이즈 필터: _unknown 이슈, 블랙리스트 타깃 건너뛰기
            if issue_id in self.ISSUE_BLACKLIST:
                continue
            if target in self.TARGET_BLACKLIST:
                continue

            hist = history.get(key, [])
            if len(hist) < 2:
                continue  # 히스토리 부족

            # ── 1. Salience (burst + persistence) ──
            mention_series = [h["mention_count"] for h in hist]
            current_mentions = feat["mention_count"]
            burst_z = self._zscore(current_mentions, mention_series)
            persistence = sum(1 for m in mention_series[-4:] if m > 0) / 4
            salience = min(1.0, (max(0, burst_z) / 4) * 0.7 + persistence * 0.3)

            # ── 2. Pressure growth ──
            pressure_series = [h["pressure_score"] for h in hist]
            current_pressure = feat["pressure_score"]
            if pressure_series and pressure_series[-1] > 0:
                pressure_growth = (current_pressure - pressure_series[-1]) / max(pressure_series[-1], 0.01)
            else:
                pressure_growth = current_pressure
            pressure = min(1.0, max(0, pressure_growth))

            # ── 3. Institutional spread ──
            spread_series = [h["spread_entropy"] for h in hist]
            current_spread = feat["spread_entropy"]
            if spread_series and max(spread_series) > 0:
                spread_growth = (current_spread - max(spread_series)) / max(max(spread_series), 0.01)
            else:
                spread_growth = 0
            spread = min(1.0, max(0, spread_growth))

            # ── 4. Frame shift (JS divergence) ──
            current_frames = json.loads(feat.get("frame_dist_json", "{}") or "{}")
            prev_frames = json.loads(hist[-1].get("frame_dist_json", "{}") or "{}") if hist else {}
            frame_shift = self._js_divergence(prev_frames, current_frames)

            # ── 5. Response shift ──
            current_resp = json.loads(feat.get("response_dist_json", "{}") or "{}")
            prev_resp = json.loads(hist[-1].get("response_dist_json", "{}") or "{}") if hist else {}
            response_shift = self._js_divergence(prev_resp, current_resp)

            # ── 6. Agenda coupling ──
            agenda_coupling = feat.get("agenda_coupling", 0)

            # ── 7. Novelty (이슈가 처음 등장했는가) ──
            novelty = 1.0 if len(hist) == 0 else 0.0

            # ── 종합 점수 ──
            scores = {
                "salience": salience,
                "pressure": pressure,
                "spread": spread,
                "frame_shift": min(1.0, frame_shift),
                "response_shift": min(1.0, response_shift),
                "agenda_coupling": agenda_coupling,
                "novelty": novelty,
            }
            composite = sum(scores[k] * self.WEIGHTS[k] for k in self.WEIGHTS)

            # 신호 유형 결정
            signal_type = self._classify_signal(scores)

            if composite > self.COMPOSITE_THRESHOLD or burst_z > self.BURST_ZSCORE:
                # evidence packet 조립
                evidence = self._build_evidence(key, year_week)

                signal = {
                    "year_week": year_week,
                    "signal_type": signal_type,
                    "issue_id": issue_id,
                    "target_entity": target,
                    **scores,
                    "composite_score": round(composite, 4),
                    "evidence_json": json.dumps(evidence, ensure_ascii=False),
                }
                signals.append(signal)

                # DB 적재
                self._save_signal(signal)

        # 점수 내림차순 정렬
        signals.sort(key=lambda s: s["composite_score"], reverse=True)
        logger.info(f"[{year_week}] {len(signals)}개 신호 탐지")
        return signals

    def _classify_signal(self, scores: dict) -> str:
        """가장 강한 채널로 신호 유형 분류"""
        channel_scores = {
            "burst": scores["salience"],
            "pressure_growth": scores["pressure"],
            "diffusion": scores["spread"],
            "frame_shift": scores["frame_shift"],
            "response_shift": scores["response_shift"],
        }
        return max(channel_scores, key=channel_scores.get)

    def _zscore(self, value: float, series: list[float]) -> float:
        if len(series) < 2:
            return 0.0
        mean = sum(series) / len(series)
        variance = sum((x - mean) ** 2 for x in series) / len(series)
        std = math.sqrt(variance) if variance > 0 else 1.0
        return (value - mean) / std

    def _js_divergence(self, p_dist: dict, q_dist: dict) -> float:
        """Jensen-Shannon Divergence"""
        all_keys = set(list(p_dist.keys()) + list(q_dist.keys()))
        if not all_keys:
            return 0.0

        p_total = sum(p_dist.values()) or 1
        q_total = sum(q_dist.values()) or 1

        divergence = 0.0
        for key in all_keys:
            p = (p_dist.get(key, 0) / p_total) + 1e-10
            q = (q_dist.get(key, 0) / q_total) + 1e-10
            m = (p + q) / 2
            divergence += 0.5 * (p * math.log2(p / m) + q * math.log2(q / m))

        return divergence

    def _build_evidence(self, key: tuple, year_week: str) -> dict:
        """신호의 증거 패킷 조립"""
        issue_id, target, committee = key

        # 대표 발언 추출 (가장 강한 speech_act)
        top_clauses = self.conn.execute("""
            SELECT c.text, u.speaker_name, u.speaker_role, ct.value as act
            FROM clause c
            JOIN utterance u ON c.utterance_id = u.utterance_id
            JOIN meeting m ON u.meeting_id = m.meeting_id
            JOIN clause_tag ct ON c.clause_id = ct.clause_id AND ct.axis = 'speech_act'
            LEFT JOIN clause_issue ci ON c.clause_id = ci.clause_id
            WHERE ci.issue_id = ?
              AND ct.value IN ('비판', '공격', '제안', '수사적질문')
            ORDER BY c.char_count DESC
            LIMIT 5
        """, (issue_id,)).fetchall()

        # 관련 법안/안건
        agendas = self.conn.execute("""
            SELECT DISTINCT a.agenda_id, a.title, a.status
            FROM clause_agenda ca
            JOIN agenda a ON ca.agenda_id = a.agenda_id
            JOIN clause c ON ca.clause_id = c.clause_id
            JOIN clause_issue ci ON c.clause_id = ci.clause_id
            WHERE ci.issue_id = ?
            LIMIT 5
        """, (issue_id,)).fetchall()

        return {
            "top_clauses": [
                {"text": t, "speaker": s, "role": r, "act": a}
                for t, s, r, a in top_clauses
            ],
            "related_agendas": [
                {"id": aid, "title": title, "status": status}
                for aid, title, status in agendas
            ],
        }

    def _save_signal(self, signal: dict):
        self.conn.execute("""
            INSERT INTO signal
                (year_week, signal_type, issue_id, target_entity,
                 salience, pressure, spread, frame_shift, response_shift,
                 agenda_coupling, novelty, composite_score, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal["year_week"], signal["signal_type"],
            signal["issue_id"], signal["target_entity"],
            signal["salience"], signal["pressure"], signal["spread"],
            signal["frame_shift"], signal["response_shift"],
            signal["agenda_coupling"], signal["novelty"],
            signal["composite_score"], signal["evidence_json"],
        ))
        self.conn.commit()

    def _get_features(self, year_week: str) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM weekly_feature WHERE year_week = ?", (year_week,)
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM weekly_feature LIMIT 0").description]
        result = {}
        for row in rows:
            d = dict(zip(cols, row))
            key = (d["issue_id"], d["target_entity"], d["committee_id"])
            result[key] = d
        return result

    def _get_history(self, year_week: str, lookback: int) -> dict:
        # lookback 주간의 week 목록 생성
        year, week = year_week.split("-W")
        weeks = []
        dt = datetime.strptime(f"{year}-W{int(week)}-1", "%G-W%V-%u")
        for i in range(1, lookback + 1):
            prev = dt - timedelta(weeks=i)
            iso = prev.isocalendar()
            weeks.append(f"{iso[0]}-W{iso[1]:02d}")

        if not weeks:
            return {}

        placeholders = ",".join("?" for _ in weeks)
        rows = self.conn.execute(f"""
            SELECT * FROM weekly_feature WHERE year_week IN ({placeholders})
            ORDER BY year_week
        """, weeks).fetchall()

        cols = [d[0] for d in self.conn.execute("SELECT * FROM weekly_feature LIMIT 0").description]
        history = defaultdict(list)
        for row in rows:
            d = dict(zip(cols, row))
            key = (d["issue_id"], d["target_entity"], d["committee_id"])
            history[key].append(d)

        return dict(history)

    def get_top_signals(self, year_week: str, limit: int = 10) -> list[dict]:
        """특정 주의 상위 신호 조회"""
        rows = self.conn.execute("""
            SELECT * FROM signal
            WHERE year_week = ?
            ORDER BY composite_score DESC
            LIMIT ?
        """, (year_week, limit)).fetchall()
        cols = [d[0] for d in self.conn.execute("SELECT * FROM signal LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    detector = SignalDetector()
    # 현재 주 탐지
    now = datetime.now()
    yw = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    signals = detector.detect_week(yw)
    for s in signals[:5]:
        print(f"  [{s['signal_type']}] {s['issue_id']} → {s['target_entity']}: {s['composite_score']:.3f}")
    detector.close()
