"""GitHub Pages용 정적 사이트를 생성한다."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict

from daum_trends_web.scraper import TrendSnapshot

STATIC_ROOT = Path(__file__).resolve().parent / "static"


def _pages_config() -> Dict[str, object]:
    return {
        "dataUrl": "./data/trends.json",
        "refreshLabel": "지금 다시 불러오기",
        "autoRefreshMs": 300000,
        "sectionNote": "GitHub Actions가 30분마다 새 데이터를 반영합니다.",
        "statusMessage": "배포된 최신 트렌드 스냅샷을 불러오는 중입니다.",
        "staleMessage": "마지막으로 배포된 데이터를 표시 중입니다.",
    }


def build_pages_site(snapshot: TrendSnapshot, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    static_dir = output_dir / "static"
    data_dir = output_dir / "data"

    if output_dir.exists():
        shutil.rmtree(output_dir)

    static_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(STATIC_ROOT / "index.html", output_dir / "index.html")
    shutil.copy2(STATIC_ROOT / "style.css", static_dir / "style.css")
    shutil.copy2(STATIC_ROOT / "app.js", static_dir / "app.js")

    (static_dir / "config.js").write_text(
        "window.DAUM_TRENDS_APP_CONFIG = "
        + json.dumps(_pages_config(), ensure_ascii=False, indent=2)
        + ";\n",
        encoding="utf-8",
    )
    (data_dir / "trends.json").write_text(
        json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return output_dir
