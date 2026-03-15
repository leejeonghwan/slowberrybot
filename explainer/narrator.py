"""
Step 6: Narrator (v2) — 신호 → 기사 초안 생성
─────────────────────────────────────────────
신호 탐지기가 "여기 뭔가 있다"고 알려주면,
narrator가 관련 발언을 모아서 "무슨 일이 있었는지"를 글로 쓴다.

핵심 원칙:
- 숫자를 보여주는 게 아니라, 사건을 서술한다.
- 신호 점수는 기사에 나오지 않는다. (찾는 도구일 뿐)
- 실제 발언을 인용한다. 누가 누구에게 뭐라고 했는지.
- Solar Pro 또는 Anthropic API 사용.
"""
import json
import sqlite3
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    DB_PATH, ANTHROPIC_API_KEY, LLM_MODEL_EXPLAIN,
    UPSTAGE_API_KEY, UPSTAGE_BASE_URL, UPSTAGE_MODEL,
    LLM_BACKEND,
)

logger = logging.getLogger(__name__)

# ── API 클라이언트 ──
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


# ── 기사 생성 프롬프트 ──
NARRATE_PROMPT = """당신은 국회 출입 기자입니다.
아래는 {year_week_kr} 국회 회의록에서 추출한 발언들입니다.
이 발언들을 바탕으로 "이번 주 국회에서 {issue}을 둘러싸고 무슨 일이 있었는지"를
기사 형식으로 써주세요.

## 작성 원칙
- 리드(첫 문단)에서 핵심 사건/쟁점을 한 문장으로 요약
- 누가, 누구에게, 무엇을, 왜 말했는지를 서술
- 발언을 직접 인용("..." 형태)하되, 자연스럽게 맥락에 녹여서
- 여야 또는 질문자-답변자 간 대립 구도가 있으면 드러내기
- 수치나 점수는 쓰지 말 것 (데이터 보고서가 아님)
- 마지막 문단에서 향후 쟁점이나 관전 포인트 한 줄
- 분량: 800~1200자 (신문 단신~중간 기사)
- 톤: 객관적이되 날카롭게. 관료적 문장 금지.

## 배경 정보
- 정책 분야: {issue}
- 주요 타깃: {target}
- 관련 위원회: {committees}
- 기간: {date_range}

## 발언록 ({clause_count}건)

{clauses_text}

## 출력
기사 본문만 쓰세요. 제목, 메타데이터, 설명 없이 본문만."""


class Narrator:
    """신호 → 사람이 읽을 수 있는 기사 생성"""

    def __init__(self, notify_fn=None):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.notify = notify_fn or (lambda msg: logger.info(msg))
        self.backend = LLM_BACKEND
        self.client = None
        self._init_client()

    def _init_client(self):
        if self.backend == "solar" and HAS_OPENAI and UPSTAGE_API_KEY:
            self.client = OpenAI(
                api_key=UPSTAGE_API_KEY,
                base_url=UPSTAGE_BASE_URL,
            )
        elif HAS_ANTHROPIC and ANTHROPIC_API_KEY:
            self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            self.backend = "anthropic"

    # ──────────────────────────────────
    # 핵심: 신호 → 기사
    # ──────────────────────────────────

    def narrate_signal(self, signal_id: int) -> str:
        """신호 하나를 기사로 만든다."""
        signal = self._get_signal(signal_id)
        if not signal:
            return "신호를 찾을 수 없습니다."

        issue = signal["issue_id"]
        target = signal["target_entity"]
        year_week = signal["year_week"]

        # 1. 해당 주간의 관련 발언을 풍부하게 수집
        clauses = self._gather_clauses(issue, target, year_week)
        if not clauses:
            return f"[{year_week}] {issue}→{target}: 관련 발언을 찾을 수 없습니다."

        # 2. 발언을 텍스트로 정리
        clauses_text, committees = self._format_clauses(clauses)

        # 3. 주간 날짜 범위
        date_range = self._week_to_date_range(year_week)

        # 4. 주간 한글 표현
        year, week = year_week.split("-W")
        year_week_kr = f"{year}년 {int(week)}주차"

        # 5. LLM에 기사 작성 요청
        prompt = NARRATE_PROMPT.format(
            year_week_kr=year_week_kr,
            issue=issue,
            target=target,
            committees=", ".join(committees) if committees else "복수 위원회",
            date_range=date_range,
            clause_count=len(clauses),
            clauses_text=clauses_text,
        )

        article = self._call_llm(prompt)

        if not article:
            # LLM 실패 시 발언 요약으로 폴백
            article = self._fallback_narrate(signal, clauses)

        # DB에 저장
        self.conn.execute(
            "UPDATE signal SET explanation = ? WHERE signal_id = ?",
            (article, signal_id)
        )
        self.conn.commit()

        return article

    def _gather_clauses(self, issue: str, target: str, year_week: str) -> list:
        """
        신호와 관련된 발언을 넉넉히 수집.
        전략: issue(policy_domain) 매칭 + target(entity) 매칭,
              비판/공격/질문/제안 등 실질 발언 우선.
        """
        year, week = year_week.split("-W")
        monday = datetime.strptime(f"{year}-W{int(week)}-1", "%G-W%V-%u")
        sunday = monday + timedelta(days=6)
        date_from = monday.strftime("%Y-%m-%d")
        date_to = sunday.strftime("%Y-%m-%d")

        # A) policy_domain 매칭 + 실질 speech_act (최대 30건)
        domain_clauses = self.conn.execute("""
            SELECT c.clause_id, c.text, u.speaker_name, u.speaker_role,
                   m.committee_id,
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
            LIMIT 30
        """, (issue, date_from, date_to)).fetchall()

        # B) target entity 매칭 (최대 20건, 중복 제거)
        seen_ids = {r[0] for r in domain_clauses}
        target_clauses = self.conn.execute("""
            SELECT c.clause_id, c.text, u.speaker_name, u.speaker_role,
                   m.committee_id,
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

        # 합치기 (중복 제거)
        all_clauses = list(domain_clauses)
        for row in target_clauses:
            if row[0] not in seen_ids:
                all_clauses.append(row)
                seen_ids.add(row[0])

        return all_clauses

    def _format_clauses(self, clauses: list) -> tuple:
        """발언 목록을 LLM에 넘길 텍스트로 정리. (committees도 반환)"""
        lines = []
        committees = set()

        for i, (cid, text, speaker, role, committee, act, party) in enumerate(clauses, 1):
            if committee:
                committees.add(committee)

            # 발화자 정보
            speaker_info = speaker or "?"
            if party:
                speaker_info += f"/{party}"
            if role:
                speaker_info += f"/{role}"

            # 발화행위
            act_label = f"[{act}]" if act else ""

            lines.append(f"{i}. {speaker_info} {act_label}")
            lines.append(f"   \"{text[:400]}\"")
            lines.append("")

        return "\n".join(lines), committees

    def _week_to_date_range(self, year_week: str) -> str:
        year, week = year_week.split("-W")
        monday = datetime.strptime(f"{year}-W{int(week)}-1", "%G-W%V-%u")
        sunday = monday + timedelta(days=6)
        return f"{monday.strftime('%m/%d')}~{sunday.strftime('%m/%d')}"

    def _fallback_narrate(self, signal: dict, clauses: list) -> str:
        """LLM 없을 때 발언 기반 폴백"""
        issue = signal["issue_id"]
        target = signal["target_entity"]
        year_week = signal["year_week"]

        lines = [
            f"**{issue} — {target}** ({year_week})",
            "",
        ]

        # 비판/공격/질문 발언 우선 표시
        priority_acts = {"비판", "공격", "수사적질문", "질문", "제안"}
        shown = 0
        for cid, text, speaker, role, committee, act, party in clauses:
            if shown >= 5:
                break
            if act in priority_acts or shown < 3:
                label = f"{speaker}"
                if party:
                    label += f"({party})"
                lines.append(f"> {label}: \"{text[:150]}\"")
                lines.append("")
                shown += 1

        return "\n".join(lines)

    # ──────────────────────────────────
    # 주간 브리핑
    # ──────────────────────────────────

    def generate_weekly_report(self, year_week: str, top_n: int = 5) -> str:
        """주간 상위 신호 기사 모음"""
        signals = self.conn.execute("""
            SELECT signal_id, signal_type, issue_id, target_entity, composite_score
            FROM signal
            WHERE year_week = ?
            ORDER BY composite_score DESC
            LIMIT ?
        """, (year_week, top_n)).fetchall()

        if not signals:
            return f"**{year_week}**: 탐지된 신호 없음"

        year, week = year_week.split("-W")
        date_range = self._week_to_date_range(year_week)

        report_lines = [
            f"**국회 동향 브리핑 | {year}년 {int(week)}주차 ({date_range})**",
            "",
        ]

        for i, (sig_id, sig_type, issue, target, score) in enumerate(signals, 1):
            self.notify(f"📝 {i}/{len(signals)} 기사 생성 중: {issue}→{target}")
            article = self.narrate_signal(sig_id)
            report_lines.append(f"━━━ {i}. {issue} — {target} ━━━")
            report_lines.append("")
            report_lines.append(article)
            report_lines.append("")

            # API rate limit
            if i < len(signals):
                time.sleep(1)

        report_lines.append("━" * 30)
        report_lines.append("_국회 회의록 신호 탐지 시스템 자동 생성_")

        return "\n".join(report_lines)

    def narrate_top_signal(self) -> str:
        """전체 기간 1위 신호를 기사로."""
        row = self.conn.execute("""
            SELECT signal_id FROM signal
            ORDER BY composite_score DESC
            LIMIT 1
        """).fetchone()

        if not row:
            return "탐지된 신호가 없습니다."

        return self.narrate_signal(row[0])

    # ──────────────────────────────────
    # LLM 호출
    # ──────────────────────────────────

    def _call_llm(self, prompt: str) -> str | None:
        if not self.client:
            return None

        try:
            if self.backend == "solar":
                response = self.client.chat.completions.create(
                    model=UPSTAGE_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2048,
                    temperature=0.3,
                )
                return response.choices[0].message.content
            else:
                response = self.client.messages.create(
                    model=LLM_MODEL_EXPLAIN,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.content[0].text
        except Exception as e:
            logger.error(f"LLM 호출 실패: {e}")
            return None

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    narrator = Narrator(notify_fn=lambda m: print(m))

    import sys
    if len(sys.argv) > 1:
        # 특정 signal_id 지정
        sig_id = int(sys.argv[1])
        article = narrator.narrate_signal(sig_id)
    else:
        # 1위 신호
        article = narrator.narrate_top_signal()

    print(article)
    narrator.close()
