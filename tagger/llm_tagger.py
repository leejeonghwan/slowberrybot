"""
Step 3-B: LLM 태거 (Solar Pro / Haiku)
──────────────────────────────────────
- 규칙 태거가 못 잡는 축: speech_act, stance, frame_type, response_mode, tone_conflict
- clause 배치 처리 (5건씩 묶어서 1회 호출 → 비용 절감)
- Upstage Solar Pro (기본) 또는 Anthropic Haiku 선택 가능
- low-confidence는 abstain → review queue
- Pi5에서 실행, 진행 보고 지원

v2 개선:
- Solar Pro 지원 (OpenAI 호환 API)
- 배치 모드 (5건씩 → API 호출 80% 감소)
- notify_fn으로 텔레그램 진행 보고
- rate limit 자동 조절
"""
import json
import time
import sqlite3
import logging
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    DB_PATH, BATCH_SIZE,
    ANTHROPIC_API_KEY, LLM_MODEL_TAG,
    UPSTAGE_API_KEY, UPSTAGE_BASE_URL, UPSTAGE_MODEL,
    LLM_BACKEND,
)

logger = logging.getLogger(__name__)

# ── API 클라이언트 로드 ──
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

# requests fallback
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# ── 배치 태깅 프롬프트 ──
BATCH_TAGGING_PROMPT = """당신은 국회 회의록 분석 전문가입니다.
아래 국회 발언 절(clause) {count}건을 분석하여 각각 JSON으로 태그를 부여하세요.

## 태그 축

1. speech_act (하나 선택):
   질문 / 비판 / 공격 / 방어 / 제안 / 지지 / 반대 / 보고 / 설명 / 수사적질문 / 수락 / 정보제공 / 의례 / 서술

2. target (비판/공격/요구의 대상, 없으면 null):
   구체적 대상명 (부처명, 인물, 정당 등)

3. stance (이슈에 대한 입장):
   찬성 / 반대 / 조건부찬성 / 유보 / 없음

4. frame_type (프레임):
   피해구제 / 형사처벌 / 재정부담 / 시장안정 / 공정성 / 법적정당성 / 집행가능성 / 국가안보 / 없음

5. response_mode (답변자인 경우만):
   직답 / 회피 / 검토약속 / 수용 / 거부 / 없음

6. tone:
   협력 / 중립 / 긴장 / 갈등 / 적대

## 발언 절 목록

{clauses_block}

## 출력 형식 (JSON 배열만, 설명 없이)
```json
[
  {{"id": 1, "speech_act": "...", "target": null, "stance": "없음", "frame_type": "없음", "response_mode": "없음", "tone": "중립"}},
  ...
]
```"""


# ── 단건 태깅 프롬프트 (소규모/테스트용) ──
SINGLE_TAGGING_PROMPT = """당신은 국회 회의록 분석 전문가입니다.
아래 발언 절을 분석하여 JSON으로 태그를 부여하세요.

## 컨텍스트
- 회의: {meeting_type} / {committee}
- 발언자: {speaker_name} ({speaker_role})

## 발언 절
{clause_text}

## 태그 축
1. speech_act: 질문/비판/공격/방어/제안/지지/반대/보고/설명/수사적질문/수락/정보제공/의례/서술
2. target: 대상명 또는 null
3. stance: 찬성/반대/조건부찬성/유보/없음
4. frame_type: 피해구제/형사처벌/재정부담/시장안정/공정성/법적정당성/집행가능성/국가안보/없음
5. response_mode: 직답/회피/검토약속/수용/거부/없음
6. tone: 협력/중립/긴장/갈등/적대

## 출력 (JSON만)
```json
{{"speech_act": "...", "target": null, "stance": "없음", "frame_type": "없음", "response_mode": "없음", "tone": "중립"}}
```"""


class LLMTagger:
    """LLM 기반 clause 태거 (Solar Pro / Haiku)"""

    def __init__(self, notify_fn=None):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.notify = notify_fn or (lambda msg: logger.info(msg))
        self.backend = LLM_BACKEND
        self.client = None
        self._init_client()

    def _init_client(self):
        """LLM 클라이언트 초기화"""
        if self.backend == "solar":
            if HAS_OPENAI and UPSTAGE_API_KEY:
                self.client = OpenAI(
                    api_key=UPSTAGE_API_KEY,
                    base_url=UPSTAGE_BASE_URL,
                )
                logger.info(f"Solar Pro 클라이언트 초기화 (모델: {UPSTAGE_MODEL})")
            elif HAS_REQUESTS and UPSTAGE_API_KEY:
                self.client = "requests"  # requests fallback
                logger.info("Solar Pro (requests fallback)")
            else:
                logger.warning("Upstage API 키 또는 openai 패키지 없음")
        elif self.backend == "anthropic":
            if HAS_ANTHROPIC and ANTHROPIC_API_KEY:
                self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                logger.info(f"Anthropic 클라이언트 초기화 (모델: {LLM_MODEL_TAG})")
            else:
                logger.warning("Anthropic API 키 또는 SDK 없음")

    def _call_solar(self, prompt: str) -> str | None:
        """Solar Pro API 호출"""
        try:
            if isinstance(self.client, OpenAI):
                response = self.client.chat.completions.create(
                    model=UPSTAGE_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1024,
                    temperature=0.1,
                )
                return response.choices[0].message.content
            elif self.client == "requests":
                resp = requests.post(
                    f"{UPSTAGE_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {UPSTAGE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": UPSTAGE_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1024,
                        "temperature": 0.1,
                    },
                    timeout=30,
                )
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Solar API 오류: {e}")
            return None

    def _call_anthropic(self, prompt: str) -> str | None:
        """Anthropic API 호출"""
        try:
            response = self.client.messages.create(
                model=LLM_MODEL_TAG,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except Exception as e:
            logger.error(f"Anthropic API 오류: {e}")
            return None

    def _call_llm(self, prompt: str) -> str | None:
        """백엔드에 따라 API 호출"""
        if not self.client:
            logger.warning("LLM 클라이언트가 초기화되지 않았습니다.")
            return None

        if self.backend == "solar":
            return self._call_solar(prompt)
        else:
            return self._call_anthropic(prompt)

    def _parse_json(self, text: str) -> dict | list | None:
        """LLM 응답에서 JSON 추출"""
        if not text:
            return None
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            logger.error(f"JSON 파싱 실패: {text[:200]}")
            return None

    def tag_batch_clauses(self, clause_ids: list[int]) -> dict:
        """
        절 배치 태깅 (5건씩 묶어서 1회 호출).
        Returns: {"tagged": N, "failed": N}
        """
        # 절 데이터 조회
        placeholders = ",".join("?" for _ in clause_ids)
        rows = self.conn.execute(f"""
            SELECT c.clause_id, c.text,
                   u.speaker_name, u.speaker_role,
                   m.meeting_type, m.committee_id
            FROM clause c
            JOIN utterance u ON c.utterance_id = u.utterance_id
            JOIN meeting m ON u.meeting_id = m.meeting_id
            WHERE c.clause_id IN ({placeholders})
        """, clause_ids).fetchall()

        if not rows:
            return {"tagged": 0, "failed": len(clause_ids)}

        # 프롬프트 구성
        clauses_block = ""
        id_map = {}
        for i, (cid, text, speaker, role, mtype, comm) in enumerate(rows, 1):
            clauses_block += f"\n[{i}] ({speaker or '?'}, {role or '?'}) {text[:300]}\n"
            id_map[i] = cid

        prompt = BATCH_TAGGING_PROMPT.format(
            count=len(rows),
            clauses_block=clauses_block,
        )

        # API 호출
        raw = self._call_llm(prompt)
        results = self._parse_json(raw)

        if not results or not isinstance(results, list):
            return {"tagged": 0, "failed": len(rows)}

        # DB 적재
        tagged = 0
        for item in results:
            idx = item.get("id")
            clause_id = id_map.get(idx)
            if not clause_id:
                continue

            self._save_tags(clause_id, item)
            tagged += 1

        self.conn.commit()
        return {"tagged": tagged, "failed": len(rows) - tagged}

    def _save_tags(self, clause_id: int, result: dict):
        """태깅 결과를 DB에 저장"""
        tag_pairs = [
            ("speech_act", result.get("speech_act")),
            ("stance_on_issue", result.get("stance")),
            ("frame_type", result.get("frame_type")),
            ("response_mode", result.get("response_mode")),
            ("tone_conflict", result.get("tone")),
        ]

        for axis, value in tag_pairs:
            if value and value not in ("없음", "null", None, "해당없음"):
                # speech_act가 리스트인 경우
                if isinstance(value, list):
                    for v in value:
                        self._insert_tag(clause_id, axis, v)
                else:
                    self._insert_tag(clause_id, axis, value)

        # target → entity
        target = result.get("target")
        if target and target not in ("null", None, "없음"):
            self.conn.execute("""
                INSERT OR IGNORE INTO clause_entity (clause_id, entity_type, entity_text)
                VALUES (?, 'TARGET', ?)
            """, (clause_id, target))

    def _insert_tag(self, clause_id: int, axis: str, value: str):
        self.conn.execute("""
            INSERT OR IGNORE INTO clause_tag
                (clause_id, axis, value, confidence, tagger)
            VALUES (?, ?, ?, 0.7, 'solar')
        """, (clause_id, axis, value))

    def tag_batch(self, limit: int = None, notify_fn=None) -> dict:
        """
        규칙 태거가 못 잡은 비절차 clause를 배치 태깅.
        5건씩 묶어서 호출 → API 비용 80% 절감.
        """
        _notify = notify_fn or self.notify
        limit = limit or BATCH_SIZE
        batch_size = 5  # 한 번에 묶는 절 수

        # LLM 태깅 안 된 비절차 clause (20자 이상)
        rows = self.conn.execute("""
            SELECT DISTINCT c.clause_id
            FROM clause c
            JOIN utterance u ON c.utterance_id = u.utterance_id
            LEFT JOIN clause_tag ct ON c.clause_id = ct.clause_id
                AND ct.tagger IN ('solar', 'haiku')
            WHERE u.is_procedural = 0
              AND ct.tag_id IS NULL
              AND c.char_count > 20
            ORDER BY RANDOM()
            LIMIT ?
        """, (limit,)).fetchall()

        total = len(rows)
        clause_ids = [r[0] for r in rows]

        _notify(
            f"🤖 **LLM 태깅 시작** ({self.backend})\n"
            f"대상: {total:,}건 / 배치: {batch_size}건씩"
        )

        stats = {"total": total, "tagged": 0, "failed": 0, "api_calls": 0}
        start_time = time.time()

        for i in range(0, total, batch_size):
            batch = clause_ids[i:i + batch_size]
            result = self.tag_batch_clauses(batch)
            stats["tagged"] += result["tagged"]
            stats["failed"] += result["failed"]
            stats["api_calls"] += 1

            # 진행 보고 (50회 호출마다)
            if stats["api_calls"] % 50 == 0:
                elapsed = time.time() - start_time
                done = i + len(batch)
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                _notify(
                    f"🤖 진행: {done}/{total} "
                    f"({done/total*100:.0f}%) "
                    f"태그 {stats['tagged']:,}건 "
                    f"[{rate:.1f}절/초, ~{eta/60:.0f}분 남음]"
                )

            # rate limit
            time.sleep(0.3)

        elapsed = time.time() - start_time
        _notify(
            f"✅ **LLM 태깅 완료** ({elapsed/60:.1f}분)\n"
            f"API: {stats['api_calls']:,}회 호출\n"
            f"태그: {stats['tagged']:,}건 / "
            f"실패: {stats['failed']:,}건"
        )

        return stats

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # 테스트: 50건만
    tagger = LLMTagger(notify_fn=lambda m: print(m))
    stats = tagger.tag_batch(limit=50)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    tagger.close()
