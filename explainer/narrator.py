"""
Step 6: LLM 설명 생성기
- 신호 탐지기가 올린 candidate signal의 evidence packet을 받아
- Sonnet/Opus로 맥락 재구성 + 설명 생성
- "무슨 일이 벌어졌는지, 왜 신호인지, 근거, 불확실성"
- 텔레그램 리포트용 마크다운 생성
"""
import json
import sqlite3
import logging
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH, ANTHROPIC_API_KEY, LLM_MODEL_EXPLAIN

logger = logging.getLogger(__name__)

try:
    import anthropic
    HAS_SDK = True
except ImportError:
    import requests
    HAS_SDK = False


EXPLAIN_PROMPT = """당신은 국회 정치 동향을 분석하는 전문 기자/분석가입니다.

아래 데이터는 국회 회의록 자동 분석 시스템이 탐지한 정치 신호입니다.
이 신호를 읽고, **텔레그램 메시지 형태**로 간결하고 날카로운 분석을 작성하세요.

## 신호 정보
- 탐지 주간: {year_week}
- 신호 유형: {signal_type}
- 이슈: {issue_id}
- 타깃: {target_entity}
- 종합 점수: {composite_score:.3f}

## 세부 점수
- 현저성(burst+지속): {salience:.2f}
- 압박 강도: {pressure:.2f}
- 제도 확산: {spread:.2f}
- 프레임 변화: {frame_shift:.2f}
- 응답 변화: {response_shift:.2f}
- 안건 연결: {agenda_coupling:.2f}

## 증거
{evidence_text}

## 작성 지침
1. **한 줄 요약**: 무슨 일이 벌어지고 있는가 (1문장)
2. **왜 신호인가**: 단순 언급 증가가 아닌, 구조적으로 무엇이 달라졌는가 (2-3문장)
3. **핵심 발언**: evidence에서 가장 중요한 발언 1-2개 직접 인용
4. **관련 법안/안건**: 실제 입법 이동과의 연결점
5. **향후 관전 포인트**: 다음에 무엇을 봐야 하는가 (1-2문장)
6. **불확실성**: 이 신호가 과장됐거나 오해일 가능성 (1문장)

## 형식
- 텔레그램용이므로 **마크다운** 사용
- 전체 500자 이내
- 이모지 최소화 (🔴 강한 신호 / 🟡 주의 / 🟢 참고 정도만)
"""


class Narrator:
    """신호 → 사람이 읽을 수 있는 설명 생성"""

    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))
        if HAS_SDK and ANTHROPIC_API_KEY:
            self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        else:
            self.client = None

    def explain_signal(self, signal_id: int) -> str:
        """단일 신호 설명 생성"""
        row = self.conn.execute(
            "SELECT * FROM signal WHERE signal_id = ?", (signal_id,)
        ).fetchone()
        if not row:
            return ""

        cols = [d[0] for d in self.conn.execute("SELECT * FROM signal LIMIT 0").description]
        signal = dict(zip(cols, row))

        # evidence 텍스트 구성
        evidence = json.loads(signal.get("evidence_json", "{}") or "{}")
        evidence_lines = []

        for clause in evidence.get("top_clauses", []):
            evidence_lines.append(
                f'- [{clause["act"]}] {clause["speaker"]}({clause["role"]}): "{clause["text"][:150]}"'
            )

        for agenda in evidence.get("related_agendas", []):
            evidence_lines.append(
                f'- 관련 안건: {agenda["title"]} (상태: {agenda["status"]})'
            )

        evidence_text = "\n".join(evidence_lines) if evidence_lines else "증거 데이터 없음"

        # 강도 이모지
        score = signal.get("composite_score", 0)
        if score > 0.5:
            strength = "🔴"
        elif score > 0.3:
            strength = "🟡"
        else:
            strength = "🟢"

        prompt = EXPLAIN_PROMPT.format(
            year_week=signal.get("year_week", ""),
            signal_type=signal.get("signal_type", ""),
            issue_id=signal.get("issue_id", ""),
            target_entity=signal.get("target_entity", ""),
            composite_score=signal.get("composite_score", 0),
            salience=signal.get("salience", 0),
            pressure=signal.get("pressure", 0),
            spread=signal.get("spread", 0),
            frame_shift=signal.get("frame_shift", 0),
            response_shift=signal.get("response_shift", 0),
            agenda_coupling=signal.get("agenda_coupling", 0),
            evidence_text=evidence_text,
        )

        explanation = self._call_llm(prompt)
        if not explanation:
            # LLM 없으면 템플릿 기반 폴백
            explanation = self._fallback_explain(signal, evidence, strength)

        # DB 업데이트
        self.conn.execute(
            "UPDATE signal SET explanation = ? WHERE signal_id = ?",
            (explanation, signal_id)
        )
        self.conn.commit()

        return f"{strength} {explanation}"

    def _fallback_explain(self, signal: dict, evidence: dict, emoji: str) -> str:
        """LLM 없을 때 템플릿 기반 설명"""
        lines = [
            f"**{signal.get('issue_id', '미상')}** → {signal.get('target_entity', '미상')}",
            f"신호 유형: {signal.get('signal_type', '')} | 점수: {signal.get('composite_score', 0):.3f}",
            "",
        ]

        # 대표 발언
        for clause in evidence.get("top_clauses", [])[:2]:
            lines.append(f'> {clause["speaker"]}: "{clause["text"][:100]}..."')

        # 관련 안건
        for agenda in evidence.get("related_agendas", [])[:2]:
            lines.append(f'📋 {agenda["title"]} ({agenda["status"]})')

        return "\n".join(lines)

    def _call_llm(self, prompt: str) -> str | None:
        if not self.client and not ANTHROPIC_API_KEY:
            return None

        try:
            if HAS_SDK:
                response = self.client.messages.create(
                    model=LLM_MODEL_EXPLAIN,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.content[0].text
            else:
                import requests as req
                resp = req.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": LLM_MODEL_EXPLAIN,
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=60,
                )
                data = resp.json()
                return data["content"][0]["text"]
        except Exception as e:
            logger.error(f"LLM 설명 생성 실패: {e}")
            return None

    def generate_weekly_report(self, year_week: str, top_n: int = 5) -> str:
        """주간 리포트 생성"""
        signals = self.conn.execute("""
            SELECT signal_id, signal_type, issue_id, target_entity, composite_score
            FROM signal
            WHERE year_week = ?
            ORDER BY composite_score DESC
            LIMIT ?
        """, (year_week, top_n)).fetchall()

        if not signals:
            return f"📊 **{year_week} 주간 보고**: 탐지된 신호 없음"

        report_lines = [
            f"📊 **국회 동향 주간 보고 | {year_week}**",
            f"탐지 신호: {len(signals)}건",
            "─" * 20,
            "",
        ]

        for i, (sig_id, sig_type, issue, target, score) in enumerate(signals, 1):
            explanation = self.explain_signal(sig_id)
            report_lines.append(f"**{i}.** {explanation}")
            report_lines.append("")

        report_lines.append("─" * 20)
        report_lines.append("_자동 생성 | 국회 회의록 신호 탐지 시스템_")

        return "\n".join(report_lines)

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    narrator = Narrator()
    from datetime import datetime
    now = datetime.now()
    yw = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    report = narrator.generate_weekly_report(yw)
    print(report)
    narrator.close()
