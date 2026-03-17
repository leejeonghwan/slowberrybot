"""
회의록 → 꼭지 카드 생성기 v2
──────────────────────────────
회의록 하나를 안건(청크) 단위로 쪼개고, 각 청크를 LLM으로 요약.

파이프라인:
  1. 회의록에서 utterance 추출 (절차 발언 = 안건 경계)
  2. 절차 발언 경계로 청크 분리
  3. 각 청크의 실질 발언을 LLM에 보내서 카드 생성

LLM은 분류가 아니라 요약만 하므로 호출 횟수 = 청크 수.
"""
import json
import re
import time
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    DB_PATH,
    UPSTAGE_API_KEY, UPSTAGE_BASE_URL, UPSTAGE_MODEL,
    ANTHROPIC_API_KEY, LLM_MODEL_TAG, LLM_BACKEND,
)

logger = logging.getLogger(__name__)

# ── API 클라이언트 ──
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


# ═══════════════════════════════════════
# 프롬프트 (1단계: 청크 → 카드)
# ═══════════════════════════════════════

CARD_PROMPT = """아래는 국회 회의록 한 안건에 대한 발언 묶음입니다.

[회의 정보]
{meeting_info}

[안건 제목]
{chunk_title}

[발언]
{utterances_text}

위 발언을 분석해서 독립된 쟁점별로 나누고, 각 쟁점을 별도 카드로 만드세요.
쟁점이 1개면 카드 1개, 3개면 카드 3개를 만듭니다.
반드시 발언 내용에 근거해야 하며, 추론이나 배경지식을 추가하지 마세요.
발언 내용 전체가 실질적 쟁점 없이 절차적이거나 의례적이면, {{"skip": true}}만 반환하세요.

반드시 JSON 배열만 출력하세요:
[
  {{
    "title": "쟁점 핵심 + 날짜·회의명",
    "summary": "상황 재구성 3~5문장",
    "quotes": [
      {{"speaker": "화자명", "party": "소속", "role": "직위", "quote": "원문 발췌 2~4문장"}},
      {{"speaker": "화자명", "party": "소속", "role": "직위", "quote": "원문 발췌 2~4문장"}}
    ],
    "keywords": ["키워드1", "키워드2"],
    "persons": ["인물1", "인물2"],
    "orgs": ["기관1", "기관2"]
  }}
]

쟁점 분리 규칙:
- 같은 주제에 대한 찬반 공방은 하나의 쟁점 (분리하지 않음)
- 서로 다른 주제는 별도 쟁점으로 분리
- 판단이 어려우면 합치기보다 분리하는 쪽으로

summary 규칙:
- 단순 나열이 아니라 상황 재구성. "누가 뭘 했고 → 상대가 어떻게 반박했고 → 결과가 어떻게 됐다" 흐름을 서술
- 3~5문장. 이 요약만 읽어도 무슨 일이 있었는지 이해할 수 있어야 함

quotes 규칙:
- quote: 원문에서 핵심이 되는 2~4문장을 발췌. 반드시 주어·목적어를 포함하여
  quote만 읽어도 무슨 말인지 이해할 수 있어야 함.
  나쁜 예: "그거 안 됩니다" (뭐가? 왜?)
  좋은 예: "A 기관이 B를 해놓고도 C 조치를 안 취했다. 즉각 D를 해야 하는 것 아닌가"
  반드시 발언 원문에 실제로 있는 표현이어야 함. 프롬프트의 예시를 베끼지 말 것.
  원문 표현을 최대한 살리되, 불필요한 반복·추임새·호칭은 제거.
  문장이 길면 "…"으로 중략 가능.
- 같은 사람이 여러 논점을 말했으면 별도 quote로 분리
- 카드당 최소 3개, 평균 7~8개, 최대 20개
- 찬성·반대·정부 측 등 다양한 입장이 골고루 포함되도록
- 더불어민주당은 "민주당"으로 표기

인물/기관 규칙:
- persons: quotes에 등장하는 화자 + 발언에서 언급된 주요 인물
- orgs: 발언에서 언급된 주요 기관·조직명
- 날짜는 "2025년 9월 25일" 형식으로 표기"""


# ═══════════════════════════════════════
# 안건 경계 패턴 (절차 발언 기반)
# ═══════════════════════════════════════

BOUNDARY_PATTERNS = [
    re.compile(r'의사일정\s*(제\s*\d+\s*항|에\s*들어가)'),
    re.compile(r'(상정|회부)합니다'),
    re.compile(r'(다음은|그다음|다음으로)\s*(의사일정|안건)'),
    re.compile(r'법률안.*심사'),
    re.compile(r'대안을?\s*(상정|보고)'),
]

# 절차적이지만 안건 경계가 아닌 발언
SKIP_PATTERNS = [
    re.compile(r'^(예|네|알겠습니다|감사합니다|수고하셨습니다)\s*[.,]?\s*$'),
    re.compile(r'(개의|산회|폐회|정회|속개)\s*(하겠습니다|합니다|선포)'),
    re.compile(r'(가결|부결|의결)\s*(되었습니다|됐습니다)'),
    re.compile(r'(이의|재석위원)\s*(없|과반)'),
    re.compile(r'(찬성|반대)\s*(하여\s*주시기|투표하여)'),
]


# ═══════════════════════════════════════
# 카드 생성기
# ═══════════════════════════════════════

class CardGenerator:
    """회의록 → 안건 청크 → 꼭지 카드"""

    def __init__(self, backend=None, notify_fn=None):
        self.backend = backend or LLM_BACKEND
        self.notify_fn = notify_fn or (lambda m: None)
        self.client = None
        self._init_client()

    def _init_client(self):
        if self.backend == "solar":
            if HAS_OPENAI and UPSTAGE_API_KEY:
                self.client = OpenAI(
                    api_key=UPSTAGE_API_KEY,
                    base_url=UPSTAGE_BASE_URL,
                )
                logger.info("Solar Pro 클라이언트 초기화")
            elif HAS_REQUESTS and UPSTAGE_API_KEY:
                self.client = "requests"
                logger.info("Solar Pro (requests fallback)")
            else:
                logger.warning("Upstage API 키 또는 openai 패키지 없음")
        elif self.backend == "anthropic":
            if HAS_ANTHROPIC and ANTHROPIC_API_KEY:
                self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                logger.info("Anthropic 클라이언트 초기화")

    def _call_llm(self, prompt: str, max_tokens: int = 2048) -> str | None:
        if not self.client:
            return None
        try:
            if self.backend == "solar":
                if isinstance(self.client, OpenAI):
                    resp = self.client.chat.completions.create(
                        model=UPSTAGE_MODEL,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=max_tokens,
                        temperature=0.2,
                    )
                    return resp.choices[0].message.content
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
                            "max_tokens": max_tokens,
                            "temperature": 0.2,
                        },
                        timeout=60,
                    )
                    return resp.json()["choices"][0]["message"]["content"]
            elif self.backend == "anthropic":
                resp = self.client.messages.create(
                    model=LLM_MODEL_TAG,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.content[0].text
        except Exception as e:
            logger.error(f"LLM 호출 오류: {e}")
            return None

    def _parse_json(self, text: str) -> dict | list | None:
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
            logger.error(f"JSON 파싱 실패: {text[:300]}")
            return None

    # ── 데이터 추출 ──

    def _get_meeting_info(self, meeting_id: str, conn: sqlite3.Connection) -> dict:
        row = conn.execute("""
            SELECT meeting_id, committee_id, meeting_date, meeting_type
            FROM meeting WHERE meeting_id = ?
        """, (meeting_id,)).fetchone()
        if not row:
            return {}
        return {
            "meeting_id": row[0],
            "committee": row[1] or "",
            "date": row[2] or "",
            "type": row[3] or "",
        }

    def _get_utterances(self, meeting_id: str,
                        conn: sqlite3.Connection) -> list[dict]:
        """회의록의 모든 utterance를 순서대로 가져오기"""
        rows = conn.execute("""
            SELECT utterance_id, sequence_no, speaker_name, speaker_role,
                   speaker_party, raw_text, char_count, is_procedural
            FROM utterance
            WHERE meeting_id = ?
            ORDER BY sequence_no
        """, (meeting_id,)).fetchall()

        utts = []
        for uid, seq, name, role, party, text, chars, is_proc in rows:
            utts.append({
                "utterance_id": uid,
                "seq": seq,
                "speaker": name or "?",
                "role": role or "",
                "party": (party or "").replace("더불어민주당", "민주당"),
                "text": text or "",
                "char_count": chars or 0,
                "is_procedural": bool(is_proc),
            })
        return utts

    # ── 청크 분리 ──

    def _is_boundary(self, utt: dict) -> bool:
        """이 발언이 안건 경계(새 안건 시작)인가?"""
        if utt["is_procedural"]:
            text = utt["text"][:200]
            for pat in BOUNDARY_PATTERNS:
                if pat.search(text):
                    return True
        return False

    def _is_skippable(self, utt: dict) -> bool:
        """의례적/절차적 발언으로 건너뛸 것인가?"""
        if utt["char_count"] < 30:
            return True
        if utt["is_procedural"]:
            return True
        text = utt["text"].strip()
        for pat in SKIP_PATTERNS:
            if pat.search(text):
                return True
        return False

    def _extract_chunk_title(self, utt: dict) -> str:
        """경계 발언에서 안건 제목 추출 시도"""
        text = utt["text"]
        # "의사일정 제N항 OOO법 일부개정법률안" 패턴
        m = re.search(r'의사일정\s*제\s*\d+\s*항\s*(.+?)(?:을|를|에 대해|심사)',
                      text)
        if m:
            return m.group(1).strip()
        # "OOO법 일부개정법률안(대안)" 패턴
        m = re.search(r'([가-힣]+법[가-힣]*(?:안|대안))', text)
        if m:
            return m.group(1).strip()
        # 못 찾으면 발언 앞부분
        return text[:50].strip()

    def _split_into_chunks(self, utterances: list[dict]) -> list[dict]:
        """utterance 리스트를 안건 경계 기준으로 청크로 분리.
        경계가 없으면 화자 전환 기준으로 자동 분할."""
        chunks = []
        current_title = "(도입부)"
        current_utts = []

        for utt in utterances:
            if self._is_boundary(utt):
                # 이전 청크 저장
                if current_utts:
                    chunks.append({
                        "title": current_title,
                        "utterances": current_utts,
                    })
                # 새 청크 시작
                current_title = self._extract_chunk_title(utt)
                current_utts = []
            elif not self._is_skippable(utt):
                current_utts.append(utt)

        # 마지막 청크
        if current_utts:
            chunks.append({
                "title": current_title,
                "utterances": current_utts,
            })

        # 큰 청크 자동 분할 (안건 경계가 없는 회의 대응)
        MAX_UTT_PER_CHUNK = 150
        final_chunks = []
        for chunk in chunks:
            if len(chunk["utterances"]) <= MAX_UTT_PER_CHUNK:
                final_chunks.append(chunk)
            else:
                sub = self._split_large_chunk(chunk, MAX_UTT_PER_CHUNK)
                final_chunks.extend(sub)

        return final_chunks

    def _split_large_chunk(self, chunk: dict,
                           max_utts: int = 150) -> list[dict]:
        """큰 청크를 화자(위원) 전환 지점에서 분할.
        국회 회의: 위원A 질의→답변→위원B 질의→답변 패턴.
        새 위원이 발언을 시작하는 지점 = 자연스러운 주제 전환점.
        """
        utts = chunk["utterances"]
        # 위원(의원) 역할 화자 목록 수집
        member_roles = {"의원", "위원", "위원장", "간사"}

        # 위원 전환 지점 찾기: 이전 발언자와 다른 위원이 나오는 시점
        split_points = []
        last_member = None
        for i, u in enumerate(utts):
            role = u.get("role", "")
            is_member = any(r in role for r in member_roles)
            if is_member:
                if last_member and u["speaker"] != last_member and i > 0:
                    split_points.append(i)
                last_member = u["speaker"]

        if not split_points:
            # 위원 전환을 못 찾으면 단순 균등 분할
            return self._split_evenly(chunk, max_utts)

        # split_points에서 max_utts 간격에 가장 가까운 지점 선택
        sub_chunks = []
        start = 0
        target = max_utts

        for sp in split_points:
            if sp >= target and sp - start >= 30:
                sub_utts = utts[start:sp]
                sub_chunks.append({
                    "title": self._infer_sub_title(chunk["title"],
                                                    sub_utts, len(sub_chunks) + 1),
                    "utterances": sub_utts,
                })
                start = sp
                target = sp + max_utts

        # 나머지
        if start < len(utts):
            sub_utts = utts[start:]
            if sub_utts:
                sub_chunks.append({
                    "title": self._infer_sub_title(chunk["title"],
                                                    sub_utts, len(sub_chunks) + 1),
                    "utterances": sub_utts,
                })

        return sub_chunks if sub_chunks else [chunk]

    def _split_evenly(self, chunk: dict, max_utts: int) -> list[dict]:
        """위원 전환점을 못 찾을 때 균등 분할."""
        utts = chunk["utterances"]
        n_parts = (len(utts) + max_utts - 1) // max_utts
        part_size = len(utts) // n_parts

        sub_chunks = []
        for i in range(n_parts):
            start = i * part_size
            end = start + part_size if i < n_parts - 1 else len(utts)
            sub_utts = utts[start:end]
            if sub_utts:
                sub_chunks.append({
                    "title": self._infer_sub_title(chunk["title"],
                                                    sub_utts, i + 1),
                    "utterances": sub_utts,
                })
        return sub_chunks

    def _infer_sub_title(self, parent_title: str,
                          utts: list[dict], part_no: int) -> str:
        """분할된 서브 청크의 제목 생성.
        첫 발언 화자를 포함해서 맥락 제공."""
        first_speaker = utts[0]["speaker"] if utts else "?"
        last_speaker = utts[-1]["speaker"] if utts else "?"
        if first_speaker == last_speaker:
            return f"{parent_title} — {first_speaker} 외 질의 (파트 {part_no})"
        return f"{parent_title} — {first_speaker}~{last_speaker} 질의 (파트 {part_no})"

    # ── 포맷팅 ──

    def _format_meeting_info(self, info: dict) -> str:
        date_str = info.get("date", "")
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                date_str = f"{dt.year}년 {dt.month}월 {dt.day}일"
            except ValueError:
                pass
        committee = info.get("committee", "")
        mtype = info.get("type", "")
        return f"{date_str} {committee} {mtype}".strip()

    def _format_chunk_utterances(self, utterances: list[dict],
                                  max_chars: int = 15000) -> str:
        """청크 내 발언을 LLM 입력용 텍스트로 포맷.
        max_chars 초과 시 발언을 앞뒤에서 균등하게 잘라냄.
        """
        lines = []
        total_chars = 0

        for utt in utterances:
            label = utt["speaker"]
            if utt["party"]:
                label += f"({utt['party']})"
            if utt["role"]:
                label += f"[{utt['role']}]"

            text = utt["text"]
            entry = f"--- {label} ---\n{text}\n"

            if total_chars + len(entry) > max_chars:
                # 잘림 표시
                remaining = max_chars - total_chars - 100
                if remaining > 200:
                    lines.append(f"--- {label} ---")
                    lines.append(text[:remaining] + "\n[이하 생략]")
                else:
                    lines.append(f"[이하 {len(utterances) - len(lines)//3}건 생략]")
                break

            lines.append(entry)
            total_chars += len(entry)

        return "\n".join(lines)

    # ── 메인 파이프라인 ──

    def generate_cards(self, meeting_id: str) -> list[dict]:
        """회의록 1건 → 꼭지 카드 리스트"""
        conn = sqlite3.connect(str(DB_PATH))
        try:
            info = self._get_meeting_info(meeting_id, conn)
            if not info:
                logger.error(f"회의 없음: {meeting_id}")
                return []

            utterances = self._get_utterances(meeting_id, conn)
            if len(utterances) < 3:
                logger.info(f"발언 부족 ({len(utterances)}건): {meeting_id}")
                return []

            meeting_info_str = self._format_meeting_info(info)

            # 청크 분리
            chunks = self._split_into_chunks(utterances)
            self.notify_fn(
                f"📄 {meeting_info_str} — "
                f"{len(utterances)}건 발언 → {len(chunks)}개 청크"
            )

            if not chunks:
                # 경계를 못 찾으면 전체를 하나의 청크로
                substantive = [u for u in utterances
                               if not self._is_skippable(u)]
                if substantive:
                    chunks = [{"title": "(전체)", "utterances": substantive}]

            # 각 청크 → 카드 (쟁점별 복수 카드 가능)
            cards = []
            for i, chunk in enumerate(chunks, 1):
                utts = chunk["utterances"]
                if len(utts) < 2:
                    continue  # 발언 2건 미만은 건너뜀

                utt_text = self._format_chunk_utterances(utts)
                prompt = CARD_PROMPT.format(
                    meeting_info=meeting_info_str,
                    chunk_title=chunk["title"],
                    utterances_text=utt_text,
                )
                # 쟁점 분리 시 출력이 길어질 수 있으므로 max_tokens 확대
                resp = self._call_llm(prompt, max_tokens=4096)
                parsed = self._parse_json(resp)

                if not parsed:
                    continue

                # 배열 또는 단일 객체 모두 처리
                if isinstance(parsed, dict):
                    card_list = [parsed]
                elif isinstance(parsed, list):
                    card_list = parsed
                else:
                    continue

                for card in card_list:
                    if not isinstance(card, dict):
                        continue
                    if card.get("skip"):
                        self.notify_fn(
                            f"  ⏭ 청크 {i}: {chunk['title'][:30]} (건너뜀)")
                        continue
                    if "title" not in card:
                        continue

                    # 메타데이터 추가
                    card["meeting_id"] = meeting_id
                    card["meeting_date"] = info.get("date", "")
                    card["committee"] = info.get("committee", "")
                    card["chunk_title"] = chunk["title"]
                    card["utterance_ids"] = [u["utterance_id"] for u in utts]
                    card["utterance_count"] = len(utts)
                    cards.append(card)
                    self.notify_fn(f"  ✅ 청크 {i}: {card['title'][:50]}")

                time.sleep(0.5)  # rate limit

            return cards

        finally:
            conn.close()

    def generate_batch(self, meeting_ids: list[str] = None,
                       limit: int = 0) -> dict:
        """여러 회의록을 배치 처리."""
        conn = sqlite3.connect(str(DB_PATH))
        self._ensure_table(conn)

        if not meeting_ids:
            rows = conn.execute("""
                SELECT DISTINCT m.meeting_id
                FROM meeting m
                JOIN utterance u ON m.meeting_id = u.meeting_id
                WHERE u.is_procedural = 0
                  AND u.char_count >= 50
                  AND m.meeting_id NOT IN (
                      SELECT DISTINCT meeting_id FROM card
                  )
                GROUP BY m.meeting_id
                HAVING COUNT(*) >= 5
                ORDER BY m.meeting_date DESC
            """).fetchall()
            meeting_ids = [r[0] for r in rows]

        if limit:
            meeting_ids = meeting_ids[:limit]
        conn.close()

        stats = {"meetings": 0, "cards": 0, "errors": 0, "skipped": 0}
        total = len(meeting_ids)
        self.notify_fn(f"📊 배치 시작: {total}개 회의")

        for i, mid in enumerate(meeting_ids, 1):
            try:
                cards = self.generate_cards(mid)
                if cards:
                    self._save_cards(cards)
                    stats["meetings"] += 1
                    stats["cards"] += len(cards)
                else:
                    stats["skipped"] += 1
                if i % 10 == 0 or i == total:
                    self.notify_fn(
                        f"📊 진행: {i}/{total} — "
                        f"{stats['cards']}개 카드, {stats['errors']}개 오류"
                    )
            except Exception as e:
                stats["errors"] += 1
                logger.error(f"[cardmaker] {mid} 실패: {e}")

        return stats

    # ── DB 저장 ──

    def _ensure_table(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS card (
                card_id TEXT PRIMARY KEY,
                meeting_id TEXT,
                meeting_date TEXT,
                committee TEXT,
                chunk_title TEXT,
                title TEXT,
                summary TEXT,
                quotes TEXT,           -- JSON array
                keywords TEXT,         -- JSON array
                persons TEXT,          -- JSON array
                orgs TEXT,             -- JSON array
                utterance_ids TEXT,    -- JSON array
                utterance_count INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_meeting
            ON card(meeting_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_date
            ON card(meeting_date)
        """)
        conn.commit()

    def _save_cards(self, cards: list[dict]):
        conn = sqlite3.connect(str(DB_PATH))
        self._ensure_table(conn)
        for i, card in enumerate(cards, 1):
            card_id = f"{card['meeting_id']}_{i:02d}"
            conn.execute("""
                INSERT OR REPLACE INTO card
                (card_id, meeting_id, meeting_date, committee,
                 chunk_title, title, summary, quotes, keywords,
                 persons, orgs, utterance_ids, utterance_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                card_id,
                card.get("meeting_id", ""),
                card.get("meeting_date", ""),
                card.get("committee", ""),
                card.get("chunk_title", ""),
                card.get("title", ""),
                card.get("summary", ""),
                json.dumps(card.get("quotes", card.get("comments", [])), ensure_ascii=False),
                json.dumps(card.get("keywords", []), ensure_ascii=False),
                json.dumps(card.get("persons", []), ensure_ascii=False),
                json.dumps(card.get("orgs", []), ensure_ascii=False),
                json.dumps(card.get("utterance_ids", []), ensure_ascii=False),
                card.get("utterance_count", 0),
            ))
        conn.commit()
        conn.close()

    def close(self):
        pass


# ═══════════════════════════════════════
# CLI
# ═══════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    gen = CardGenerator(notify_fn=lambda m: print(m))

    save_to_db = "--no-save" not in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if args:
        mid = args[0]
        print(f"\n지정 회의: {mid}\n")
        cards = gen.generate_cards(mid)
    else:
        # 랜덤 회의 1건 테스트
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("""
            SELECT m.meeting_id, m.meeting_date, m.committee_id,
                   COUNT(*) as utt_count
            FROM meeting m
            JOIN utterance u ON m.meeting_id = u.meeting_id
            WHERE u.is_procedural = 0 AND u.char_count >= 50
            GROUP BY m.meeting_id
            HAVING utt_count >= 10
            ORDER BY RANDOM()
            LIMIT 1
        """).fetchone()
        conn.close()

        if row:
            mid = row[0]
            print(f"\n랜덤 회의: {mid} ({row[1]} {row[2]}, "
                  f"{row[3]}건 실질 발언)\n")
            cards = gen.generate_cards(mid)
        else:
            print("적합한 회의가 없습니다")
            sys.exit(1)

    # DB 저장
    if cards and save_to_db:
        gen._save_cards(cards)
        print(f"💾 {len(cards)}개 카드 DB 저장 완료")

    # 결과 출력
    print(f"\n{'='*60}")
    print(f"생성된 카드: {len(cards)}개")
    print(f"{'='*60}\n")

    for card in cards:
        print(f"제목: {card.get('title', '')}")
        print(f"핵심: {card.get('summary', '')}")
        print(f"발언:")
        for c in card.get("quotes", card.get("comments", [])):
            party = c.get("party", "")
            role = c.get("role", "")
            label = c["speaker"]
            if party:
                label += f"({party}"
                if role:
                    label += f" {role}"
                label += ")"
            elif role:
                label += f"[{role}]"
            quote = c.get("quote", c.get("text", ""))
            print(f"  - {label}: \"{quote}\"")
        print(f"키워드: {', '.join(card.get('keywords', []))}")
        print(f"인물: {', '.join(card.get('persons', []))}")
        print(f"기관: {', '.join(card.get('orgs', []))}")
        print(f"회의: {card.get('committee', '')} {card.get('meeting_date', '')}")
        print(f"발언: {card.get('utterance_count', 0)}건")
        print()
