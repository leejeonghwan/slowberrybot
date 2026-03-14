"""
Step 3-B: LLM 태거 (Haiku 배치)
- 규칙으로 처리 못하는 축: speech_act(비절차), stance, frame_type, response_mode, tone_conflict
- clause 단위 multi-label
- Pi5에서 Anthropic API 호출, rate limit 준수
- low-confidence는 abstain → review queue
"""
import json
import time
import sqlite3
import logging
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH, ANTHROPIC_API_KEY, LLM_MODEL_TAG, BATCH_SIZE

logger = logging.getLogger(__name__)

# Anthropic SDK가 없으면 requests로 대체
try:
    import anthropic
    HAS_SDK = True
except ImportError:
    import requests
    HAS_SDK = False
    logger.info("anthropic SDK 없음. requests로 대체합니다.")


TAGGING_PROMPT = """당신은 국회 회의록 분석 전문가입니다.
아래 국회 회의록 발언 절(clause)을 분석하여 JSON으로 태그를 부여하세요.

## 컨텍스트
- 회의 유형: {meeting_type}
- 위원회: {committee}
- 발언자: {speaker_name} ({speaker_role})
- 앞 발언: {prev_text}

## 분석 대상 절
{clause_text}

## 태그 축 (각각 하나 이상 선택 가능)

1. speech_act (복수 선택 가능):
   질문, 정보요구, 비판, 공격, 방어, 제안, 지지, 반대, 보고, 설명, 수사적질문, 호소, 자기홍보

2. target (비판/공격/요구의 대상, 없으면 null):
   구체적 대상명 (부처, 인물, 정당 등)

3. stance_on_issue (해당 이슈에 대한 입장):
   찬성 / 반대 / 조건부찬성 / 유보 / 입장없음

4. frame_type (이슈를 어떤 프레임으로 말하는가):
   피해구제 / 형사처벌 / 재정부담 / 시장안정 / 공정성 / 법적정당성 / 집행가능성 / 국가안보 / 해당없음

5. response_mode (답변자일 경우만):
   직답 / 부분답변 / 회피 / 검토약속 / 책임전가 / 수용 / 거부 / 해당없음

6. tone_conflict:
   협력 / 중립 / 긴장 / 갈등 / 적대

7. confidence (0.0~1.0): 전체 태깅에 대한 자기 확신도

## 출력 (JSON만, 설명 없이)
```json
{{
  "speech_act": ["..."],
  "target": "..." or null,
  "stance_on_issue": "...",
  "frame_type": "...",
  "response_mode": "...",
  "tone_conflict": "...",
  "confidence": 0.0
}}
```"""


class LLMTagger:
    """Haiku 기반 clause 태거"""

    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))
        if HAS_SDK and ANTHROPIC_API_KEY:
            self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        else:
            self.client = None

    def _call_llm(self, prompt: str) -> dict | None:
        """Anthropic API 호출 → JSON 파싱"""
        if not self.client and not ANTHROPIC_API_KEY:
            logger.warning("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
            return None

        try:
            if HAS_SDK:
                response = self.client.messages.create(
                    model=LLM_MODEL_TAG,
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = response.content[0].text
            else:
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": LLM_MODEL_TAG,
                        "max_tokens": 512,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=30,
                )
                data = resp.json()
                text = data["content"][0]["text"]

            # JSON 추출
            text = text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            return json.loads(text.strip())

        except Exception as e:
            logger.error(f"LLM 호출 실패: {e}")
            return None

    def tag_clause(self, clause_id: int) -> dict | None:
        """단일 clause 태깅"""
        # 컨텍스트 조회
        row = self.conn.execute("""
            SELECT c.text, u.speaker_name, u.speaker_role,
                   m.meeting_type, m.committee_id,
                   (SELECT c2.text FROM clause c2
                    JOIN utterance u2 ON c2.utterance_id = u2.utterance_id
                    WHERE u2.meeting_id = m.meeting_id
                      AND (u2.sequence_no < u.sequence_no
                           OR (u2.sequence_no = u.sequence_no AND c2.sequence_no < c.sequence_no))
                    ORDER BY u2.sequence_no DESC, c2.sequence_no DESC LIMIT 1) as prev_text
            FROM clause c
            JOIN utterance u ON c.utterance_id = u.utterance_id
            JOIN meeting m ON u.meeting_id = m.meeting_id
            WHERE c.clause_id = ?
        """, (clause_id,)).fetchone()

        if not row:
            return None

        clause_text, speaker_name, speaker_role, meeting_type, committee, prev_text = row

        prompt = TAGGING_PROMPT.format(
            meeting_type=meeting_type or "미상",
            committee=committee or "미상",
            speaker_name=speaker_name,
            speaker_role=speaker_role or "미상",
            prev_text=(prev_text or "없음")[:200],
            clause_text=clause_text,
        )

        result = self._call_llm(prompt)
        if not result:
            return None

        # DB 적재
        confidence = result.get("confidence", 0.5)

        # low-confidence는 abstain
        if confidence < 0.4:
            self.conn.execute("""
                INSERT OR IGNORE INTO clause_tag (clause_id, axis, value, confidence, tagger)
                VALUES (?, 'review_status', 'needs_review', ?, 'haiku')
            """, (clause_id, confidence))
            self.conn.commit()
            return {"status": "abstain", "confidence": confidence}

        tag_mappings = [
            ("speech_act", result.get("speech_act", [])),
            ("stance_on_issue", [result.get("stance_on_issue")] if result.get("stance_on_issue") else []),
            ("frame_type", [result.get("frame_type")] if result.get("frame_type") and result.get("frame_type") != "해당없음" else []),
            ("response_mode", [result.get("response_mode")] if result.get("response_mode") and result.get("response_mode") != "해당없음" else []),
            ("tone_conflict", [result.get("tone_conflict")] if result.get("tone_conflict") else []),
        ]

        if result.get("target"):
            self._upsert_entity(clause_id, "TARGET", result["target"])

        for axis, values in tag_mappings:
            if isinstance(values, str):
                values = [values]
            for val in values:
                if val:
                    self.conn.execute("""
                        INSERT OR IGNORE INTO clause_tag
                            (clause_id, axis, value, confidence, tagger)
                        VALUES (?, ?, ?, ?, 'haiku')
                    """, (clause_id, axis, val, confidence))

        self.conn.commit()
        return result

    def _upsert_entity(self, clause_id, entity_type, entity_text):
        self.conn.execute("""
            INSERT OR IGNORE INTO clause_entity (clause_id, entity_type, entity_text)
            VALUES (?, ?, ?)
        """, (clause_id, entity_type, entity_text))

    def tag_batch(self, limit: int = None) -> dict:
        """규칙 태거가 처리 못한 비절차 clause를 배치 태깅"""
        limit = limit or BATCH_SIZE

        # 아직 haiku 태깅 안 된 비절차 clause
        rows = self.conn.execute("""
            SELECT DISTINCT c.clause_id
            FROM clause c
            JOIN utterance u ON c.utterance_id = u.utterance_id
            LEFT JOIN clause_tag ct ON c.clause_id = ct.clause_id AND ct.tagger = 'haiku'
            WHERE u.is_procedural = 0
              AND ct.tag_id IS NULL
              AND c.char_count > 20
            LIMIT ?
        """, (limit,)).fetchall()

        stats = {"total": len(rows), "tagged": 0, "abstained": 0, "failed": 0}
        logger.info(f"LLM 배치 태깅 시작: {len(rows)}건")

        for i, (clause_id,) in enumerate(rows):
            result = self.tag_clause(clause_id)
            if result is None:
                stats["failed"] += 1
            elif result.get("status") == "abstain":
                stats["abstained"] += 1
            else:
                stats["tagged"] += 1

            # rate limit: Haiku는 넉넉하지만 Pi5 부담 방지
            if (i + 1) % 10 == 0:
                logger.info(f"  진행: {i+1}/{len(rows)} (tagged={stats['tagged']})")
                time.sleep(0.5)

        logger.info(f"LLM 배치 태깅 완료: {stats}")
        return stats

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tagger = LLMTagger()
    stats = tagger.tag_batch(limit=10)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    tagger.close()
