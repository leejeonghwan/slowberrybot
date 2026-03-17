"""카드 DB → JSON 내보내기 + 정적 HTML 생성"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "web"


def export_json(db_path: str = None, output: str = None) -> list[dict]:
    """card 테이블에서 전체 카드를 JSON으로 내보내기"""
    db = db_path or str(DB_PATH)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT c.card_id, c.meeting_id, c.meeting_date, c.committee,
               c.chunk_title, c.title, c.summary, c.quotes, c.keywords,
               c.persons, c.orgs, c.utterance_count, c.created_at,
               m.meeting_type
        FROM card c
        LEFT JOIN meeting m ON c.meeting_id = m.meeting_id
        ORDER BY c.meeting_date DESC, c.card_id
    """).fetchall()
    conn.close()

    cards = []
    for r in rows:
        card = dict(r)
        # JSON 문자열 필드를 파싱
        for field in ("quotes", "keywords", "persons", "orgs"):
            val = card.get(field, "[]")
            try:
                card[field] = json.loads(val) if val else []
            except (json.JSONDecodeError, TypeError):
                card[field] = []
        # meeting_type 기본값
        if not card.get("meeting_type"):
            card["meeting_type"] = "전체회의"
        cards.append(card)

    out_path = output or str(OUTPUT_DIR / "cards.json")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)

    print(f"✅ {len(cards)}개 카드 → {out_path}")
    return cards


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else None
    export_json(output=out)
