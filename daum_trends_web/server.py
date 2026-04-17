"""Daum 실시간 트렌드를 보여주는 작은 웹 서버."""

from __future__ import annotations

import argparse
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict
from urllib.parse import parse_qs, urlparse

from daum_trends_web.scraper import TrendCache, TrendFetchError, TrendParseError

logger = logging.getLogger(__name__)

STATIC_ROOT = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class DaumTrendRequestHandler(BaseHTTPRequestHandler):
    cache = TrendCache()
    server_version = "DaumTrendServer/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            self._serve_static(filename, content_type)
            return

        if parsed.path == "/api/trends":
            self._serve_trends(parsed.query)
            return

        if parsed.path == "/healthz":
            self._send_json({"ok": True}, status=HTTPStatus.OK)
            return

        self._send_json(
            {"error": "요청한 경로를 찾을 수 없습니다.", "path": parsed.path},
            status=HTTPStatus.NOT_FOUND,
        )

    def log_message(self, fmt: str, *args: object) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = STATIC_ROOT / filename
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_trends(self, query_string: str) -> None:
        query = parse_qs(query_string)
        force_refresh = query.get("force", ["0"])[0].lower() in {"1", "true", "yes"}

        try:
            snapshot = self.cache.get_snapshot(force_refresh=force_refresh)
        except (TrendFetchError, TrendParseError) as exc:
            logger.warning("Trend API error: %s", exc)
            self._send_json(
                {
                    "error": "Daum 실시간 트렌드를 불러오지 못했습니다.",
                    "details": str(exc),
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
            return
        except Exception as exc:  # pragma: no cover
            logger.exception("Unexpected trend API error")
            self._send_json(
                {
                    "error": "서버에서 예기치 못한 문제가 발생했습니다.",
                    "details": str(exc),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self._send_json(snapshot.to_dict(), status=HTTPStatus.OK)

    def _send_json(self, payload: Dict[str, object], status: HTTPStatus) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daum 실시간 트렌드 웹 앱")
    parser.add_argument("--host", default="127.0.0.1", help="바인딩할 호스트")
    parser.add_argument("--port", type=int, default=8787, help="서버 포트")
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=60,
        help="Daum 호출 캐시 유지 시간(초)",
    )
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    args = build_argument_parser().parse_args()

    DaumTrendRequestHandler.cache = TrendCache(ttl_seconds=args.cache_ttl)
    server = ThreadingHTTPServer((args.host, args.port), DaumTrendRequestHandler)

    logger.info("Serving Daum trends app at http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

