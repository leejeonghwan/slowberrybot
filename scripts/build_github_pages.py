#!/usr/bin/env python3
"""GitHub Pages 배포용 정적 사이트를 생성한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from daum_trends_web.pages_build import build_pages_site
from daum_trends_web.scraper import TrendSnapshot, fetch_trend_snapshot


def _load_snapshot(input_json: str | None, timeout_seconds: int) -> TrendSnapshot:
    if input_json:
        payload = json.loads(Path(input_json).read_text(encoding="utf-8"))
        return TrendSnapshot.from_dict(payload)
    return fetch_trend_snapshot(timeout_seconds=timeout_seconds)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build static site for GitHub Pages")
    parser.add_argument(
        "--output-dir",
        default="site",
        help="정적 사이트를 생성할 디렉터리",
    )
    parser.add_argument(
        "--input-json",
        help="오프라인 빌드를 위한 trends.json 경로",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Daum 요청 타임아웃(초)",
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    snapshot = _load_snapshot(args.input_json, timeout_seconds=args.timeout)
    output_dir = build_pages_site(snapshot, Path(args.output_dir))
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

