"""
회의록 → 꼭지 카드 생성기
──────────────────────────
회의록 하나를 LLM에게 읽혀서 독립 꼭지 카드로 변환.

2단계 파이프라인:
  1단계: 발언 목록 → 이슈별 분류 (JSON)
  2단계: 이슈별 발언 묶음 → 구조화된 카드

각 카드에는 제목, 핵심, 코멘트, 키워드, 인물, 기관, 회의 정보가 포함됨.
"""
import json
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
# 프롬프트
# ═══════════════════════════════════════

STAGE1_PROMPT = """아래는 국회 회의록 한 건에서 추출한 발언 목록입니다.
각 발언에는 번호, 화자, 소속, 역할이 표시되어 있습니다.

[회의 정보]
{meeting_info}

[발언 목록]
{clause_list}

위 발언들을 독립된 이슈 꼭지로 분류하세요.

규칙:
- 같은 쟁점을 다루는 발언을 하나의 꼭지로 묶으세요
- 하나의 꼭지에는 최소 3건 이상의 발언이 필요합니다
- 중복 발언(같은 내용이 반복되는 것)은 하나만 남기세요
- 한 발언이 여러 이슈를 언급하더라도, 핵심 쟁점 하나에만 배정하세요
- 어떤 꼭지에도 속하지 않는 발언은 dropped에 넣으세요

반드시 JSON만 출력하세요:
{{"issues": [{{"title": "이슈 핵심 키워드", "clause_ids": [1, 11, 13], "one_line": "이 이슈가 무엇인지 1문장"}}], "dropped": [5, 7]}}"""


STAGE2_PROMPT = """아래는 국회 회의록에서 하나의 이슈로 분류된 발언 묶음입니다.

[회의 정보]
{meeting_info}

[이슈]
{issue_title}: {issue_oneline}

[발언]
{clauses_text}

위 발언 묶음을 아래 형식으로 요약하세요.
반드시 발언 내용에 근거해야 하며, 추론이나 배경지식을 추가하지 마세요.

반드시 JSON만 출력하세요:
{{
  "title": "이슈 핵심 + 날짜·회의명 포함. 예) 검찰청 폐지안, 구속사건 처리 공백 우려 — 2025년 9월 25일 본회의",
  "summary": "무슨 일이 있었는지 2~3문장 요약",
  "comments": [
    {{"speaker": "홍길동", "party": "민주당", "role": "의원", "text": "의미 요약문"}},
    {{"speaker": "김철수", "party": "국민의힘", "role": "의원", "text": "의미 요약문"}}
  ],
  "keywords": ["키워드1", "키워드2", "키워드3"],
  "persons": ["홍길동", "김철수"],
  "orgs": ["금융감독원", "금융위원회"]
}}

코멘트 규칙:
- 원문의 의미를 훼손하지 않으면서 핵심만 간결하게 의미 요약
- 원문의 어투와 뉘앙스를 살리되 불필요한 반복·수식어는 제거
- 인용문 끝은 "~했다/~한다/~이다" 등 종결형으로 마무리
- 같은 사람이 여러 논점을 말했으면 별도 코멘트로 분리 가능
- 최소 2개, 평균 5개, 최대 20개
- 찬성·반대·정부 측 등 다양한 입장이 골고루 포함되도록
- 더불어민주당은 "민주당"으로 표기

인물/기관 규칙:
- persons: 코멘트에 등장하는 화자 + 발언에서 언급된 주요 인물
- orgs: 발언에서 언급된 주요 기관·조직명
- 날짜는 "2025년 9월 25일" 형식으로 표기"""


# ═══════════════════════════════════════
# 카드 생성기
# ═══════════════════════════════════════

class CardGenerator:
    """회의록 하나를 꼭지 카드 묶음으로 변환"""

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

    # ── 회의록에서 발언 추출 ──

    def _get_meeting_info(self, meeting_id: str, conn: sqlite3.Connection) -> dict:
        row = conn.execute("""
            SELECT meeting_id, committee_id, meeting_date, meeting_type, era
            FROM meeting WHERE meeting_id = ?
        """, (meeting_id,)).fetchone()
        if not row:
            return {}
        return {
            "meeting_id": row[0],
            "committee": row[1] or "",
            "date": row[2] or "",
            "type": row[3] or "",
            "era": row[4] or "",
        }

    def _get_clauses(self, meeting_id: str, conn: sqlite3.Connection,
                     min_chars: int = 100) -> list[dict]:
        """회의록에서 실질적 발언(100자+) 추출, 중복 제거"""
        rows = conn.execute("""
            SELECT c.clause_id, c.text, u.speaker_name, u.speaker_role,
                   (SELECT party FROM member WHERE member_id = u.speaker_id) as party
            FROM clause c
            JOIN utterance u ON c.utterance_id = u.utterance_id
            WHERE u.meeting_id = ?
              AND c.char_count >= ?
            ORDER BY c.clause_id
        """, (meeting_id, min_chars)).fetchall()

        # 중복 제거 (같은 text가 여러 번)
        seen_texts = set()
        clauses = []
        for cid, text, speaker, role, party in rows:
            text_key = text[:100]  # 앞 100자로 중복 판단
            if text_key in seen_texts:
                continue
            seen_texts.add(text_key)
            clauses.append({
                "id": len(clauses) + 1,  # 1부터 순번
                "clause_id": cid,
                "text": text,
                "speaker": speaker or "?",
                "party": (party or "").replace("더불어민주당", "민주당"),
                "role": role or "",
            })

        return clauses

    def _format_clause_list(self, clauses: list[dict]) -> str:
        """1단계용 발언 목록 텍스트"""
        lines = []
        for c in clauses:
            label = c["speaker"]
            if c["party"]:
                label += f"({c['party']})"
            if c["role"]:
                label += f"[{c['role']}]"
            # 발언은 앞 200자만 (분류에는 충분)
            text_preview = c["text"][:200]
            if len(c["text"]) > 200:
                text_preview += "…"
            lines.append(f"[{c['id']}] {label}: {text_preview}")
        return "\n".join(lines)

    def _format_clauses_for_issue(self, clauses: list[dict],
                                   clause_ids: list[int]) -> str:
        """2단계용 이슈별 발언 전문"""
        lines = []
        for c in clauses:
            if c["id"] in clause_ids:
                label = c["speaker"]
                if c["party"]:
                    label += f"({c['party']})"
                if c["role"]:
                    label += f"[{c['role']}]"
                lines.append(f"--- {label} ---")
                lines.append(c["text"])
                lines.append("")
        return "\n".join(lines)

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

    # ── 메인 파이프라인 ──

    def generate_cards(self, meeting_id: str) -> list[dict]:
        """회의록 1건 → 꼭지 카드 리스트"""
        conn = sqlite3.connect(str(DB_PATH))
        try:
            # 1. 데이터 수집
            info = self._get_meeting_info(meeting_id, conn)
            if not info:
                logger.error(f"회의 없음: {meeting_id}")
                return []

            clauses = self._get_clauses(meeting_id, conn)
            if len(clauses) < 3:
                logger.info(f"발언 부족 ({len(clauses)}건): {meeting_id}")
                return []

            meeting_info_str = self._format_meeting_info(info)
            self.notify_fn(
                f"📄 {meeting_info_str} — {len(clauses)}건 발언"
            )

            # 2. 1단계: 이슈 분류
            clause_list_text = self._format_clause_list(clauses)
            prompt1 = STAGE1_PROMPT.format(
                meeting_info=meeting_info_str,
                clause_list=clause_list_text,
            )
            resp1 = self._call_llm(prompt1, max_tokens=1024)
            classification = self._parse_json(resp1)

            if not classification or "issues" not in classification:
                logger.error(f"1단계 분류 실패: {meeting_id}")
                return []

            issues = classification["issues"]
            self.notify_fn(f"  → {len(issues)}개 이슈 분류됨")

            # 3. 2단계: 이슈별 카드 생성
            cards = []
            for issue in issues:
                clause_ids = issue.get("clause_ids", [])
                if len(clause_ids) < 2:
                    continue

                clauses_text = self._format_clauses_for_issue(clauses, clause_ids)
                prompt2 = STAGE2_PROMPT.format(
                    meeting_info=meeting_info_str,
                    issue_title=issue.get("title", ""),
                    issue_oneline=issue.get("one_line", ""),
                    clauses_text=clauses_text,
                )
                resp2 = self._call_llm(prompt2, max_tokens=2048)
                card = self._parse_json(resp2)

                if card and "title" in card:
                    # 메타데이터 추가
                    card["meeting_id"] = meeting_id
                    card["meeting_date"] = info.get("date", "")
                    card["committee"] = info.get("committee", "")
                    card["source_clause_ids"] = [
                        c["clause_id"] for c in clauses
                        if c["id"] in clause_ids
                    ]
                    cards.append(card)
                    self.notify_fn(f"  ✅ {card['title'][:40]}...")

                time.sleep(0.5)  # rate limit

            return cards

        finally:
            conn.close()

    def generate_batch(self, meeting_ids: list[str] = None,
                       limit: int = 0) -> dict:
        """여러 회의록을 배치 처리.
        meeting_ids 없으면 아직 카드가 없는 회의를 자동 선택.
        """
        conn = sqlite3.connect(str(DB_PATH))
        self._ensure_table(conn)

        if not meeting_ids:
            # 아직 카드가 없는 회의 중 clause가 있는 것
            rows = conn.execute("""
                SELECT DISTINCT u.meeting_id
                FROM utterance u
                JOIN clause c ON u.utterance_id = c.utterance_id
                WHERE c.char_count >= 100
                  AND u.meeting_id NOT IN (
                      SELECT DISTINCT meeting_id FROM card
                  )
                ORDER BY u.meeting_id
            """).fetchall()
            meeting_ids = [r[0] for r in rows]

        if limit:
            meeting_ids = meeting_ids[:limit]

        conn.close()

        stats = {"meetings": 0, "cards": 0, "errors": 0}
        total = len(meeting_ids)

        for i, mid in enumerate(meeting_ids, 1):
            try:
                cards = self.generate_cards(mid)
                if cards:
                    self._save_cards(cards)
                    stats["meetings"] += 1
                    stats["cards"] += len(cards)
                if i % 10 == 0:
                    self.notify_fn(
                        f"📊 진행: {i}/{total} "
                        f"({stats['cards']}개 카드)"
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
                title TEXT,
                summary TEXT,
                comments TEXT,    -- JSON array
                keywords TEXT,    -- JSON array
                persons TEXT,     -- JSON array
                orgs TEXT,        -- JSON array
                source_clause_ids TEXT,  -- JSON array
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
                 title, summary, comments, keywords,
                 persons, orgs, source_clause_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                card_id,
                card.get("meeting_id", ""),
                card.get("meeting_date", ""),
                card.get("committee", ""),
                card.get("title", ""),
                card.get("summary", ""),
                json.dumps(card.get("comments", []), ensure_ascii=False),
                json.dumps(card.get("keywords", []), ensure_ascii=False),
                json.dumps(card.get("persons", []), ensure_ascii=False),
                json.dumps(card.get("orgs", []), ensure_ascii=False),
                json.dumps(card.get("source_clause_ids", []),
                           ensure_ascii=False),
            ))
        conn.commit()
        conn.close()

    def close(self):
        pass


# ═══════════════════════════════════════
# CLI
# ═══════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    gen = CardGenerator(notify_fn=lambda m: print(m))

    if len(sys.argv) > 1:
        mid = sys.argv[1]
        cards = gen.generate_cards(mid)
    else:
        # 기본: 1건만 테스트
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("""
            SELECT u.meeting_id, COUNT(*) as cnt
            FROM utterance u
            JOIN clause c ON u.utterance_id = c.utterance_id
            WHERE c.char_count >= 100
            GROUP BY u.meeting_id
            HAVING cnt >= 10
            ORDER BY RANDOM()
            LIMIT 1
        """).fetchone()
        conn.close()

        if row:
            mid = row[0]
            print(f"\n랜덤 회의 선택: {mid} ({row[1]}건 발언)\n")
            cards = gen.generate_cards(mid)
        else:
            print("적합한 회의가 없습니다")
            sys.exit(1)

    # 결과 출력
    print(f"\n{'='*60}")
    print(f"생성된 카드: {len(cards)}개")
    print(f"{'='*60}\n")

    for card in cards:
        print(f"제목: {card.get('title', '')}")
        print(f"핵심: {card.get('summary', '')}")
        print(f"코멘트:")
        for c in card.get("comments", []):
            party = c.get("party", "")
            role = c.get("role", "")
            label = c["speaker"]
            if party:
                label += f"({party}"
                if role:
                    label += f" {role}"
                label += ")"
            elif role:
                label += f"({role})"
            print(f"  - {label}: \"{c['text']}\"")
        print(f"키워드: {', '.join(card.get('keywords', []))}")
        print(f"인물: {', '.join(card.get('persons', []))}")
        print(f"기관: {', '.join(card.get('orgs', []))}")
        print(f"회의: {card.get('committee', '')} {card.get('meeting_date', '')}")
        print()
