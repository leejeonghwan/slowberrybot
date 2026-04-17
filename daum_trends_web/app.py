"""배포용 WSGI 앱."""

from __future__ import annotations

import json
import os
from functools import partial
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Tuple
from urllib.parse import parse_qs

from daum_trends_web.scraper import (
    DEFAULT_STALE_IF_ERROR_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TTL_SECONDS,
    TrendCache,
    TrendDataUnavailable,
    fetch_trend_snapshot,
)

STATIC_ROOT = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8", "no-store"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8", "public, max-age=300"),
    "/static/config.js": (
        "config.js",
        "application/javascript; charset=utf-8",
        "public, max-age=60",
    ),
    "/static/app.js": (
        "app.js",
        "application/javascript; charset=utf-8",
        "public, max-age=300",
    ),
}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class DaumTrendsApp:
    def __init__(self, cache: TrendCache, static_root: Path = STATIC_ROOT) -> None:
        self.cache = cache
        self.static_root = static_root

    def __call__(self, environ: Dict[str, str], start_response: Callable):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        head_only = method == "HEAD"

        if method not in {"GET", "HEAD"}:
            return self._json_response(
                start_response,
                {"error": "허용되지 않는 메서드입니다."},
                "405 Method Not Allowed",
                head_only=head_only,
            )

        if path in STATIC_FILES:
            filename, content_type, cache_control = STATIC_FILES[path]
            return self._static_response(
                start_response,
                filename,
                content_type,
                cache_control,
                head_only=head_only,
            )

        if path == "/api/trends":
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
            force_refresh = query.get("force", ["0"])[0].lower() in {"1", "true", "yes"}
            try:
                snapshot = self.cache.get_snapshot(force_refresh=force_refresh)
            except TrendDataUnavailable as exc:
                return self._json_response(
                    start_response,
                    {
                        "error": "Daum 실시간 트렌드를 불러오지 못했습니다.",
                        "details": str(exc),
                    },
                    "502 Bad Gateway",
                    head_only=head_only,
                )

            return self._json_response(
                start_response,
                snapshot.to_dict(),
                "200 OK",
                head_only=head_only,
            )

        if path == "/healthz":
            return self._json_response(
                start_response,
                {"ok": True},
                "200 OK",
                head_only=head_only,
            )

        return self._json_response(
            start_response,
            {"error": "요청한 경로를 찾을 수 없습니다.", "path": path},
            "404 Not Found",
            head_only=head_only,
        )

    def _static_response(
        self,
        start_response: Callable,
        filename: str,
        content_type: str,
        cache_control: str,
        head_only: bool = False,
    ) -> Iterable[bytes]:
        path = self.static_root / filename
        if not path.exists():
            return self._json_response(
                start_response,
                {"error": "정적 파일을 찾을 수 없습니다.", "file": filename},
                "404 Not Found",
                head_only=head_only,
            )

        body = path.read_bytes()
        headers = [
            ("Content-Type", content_type),
            ("Cache-Control", cache_control),
            ("Content-Length", str(len(body))),
        ]
        start_response("200 OK", headers)
        return [] if head_only else [body]

    def _json_response(
        self,
        start_response: Callable,
        payload: Dict[str, object],
        status: str,
        head_only: bool = False,
    ) -> Iterable[bytes]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
        ]
        start_response(status, headers)
        return [] if head_only else [body]


def create_app(
    cache_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    stale_if_error_seconds: int = DEFAULT_STALE_IF_ERROR_SECONDS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    state_path: Path = Path("runtime/daum_trends_snapshot.json"),
) -> DaumTrendsApp:
    resolved_state_path = Path(state_path)
    fetcher = partial(fetch_trend_snapshot, timeout_seconds=timeout_seconds)
    cache = TrendCache(
        ttl_seconds=cache_ttl_seconds,
        stale_if_error_seconds=stale_if_error_seconds,
        state_path=resolved_state_path,
        fetcher=fetcher,
    )
    return DaumTrendsApp(cache=cache)


def create_app_from_env() -> DaumTrendsApp:
    return create_app(
        cache_ttl_seconds=_int_env("DAUM_TRENDS_CACHE_TTL", DEFAULT_TTL_SECONDS),
        stale_if_error_seconds=_int_env(
            "DAUM_TRENDS_STALE_IF_ERROR_SECONDS",
            DEFAULT_STALE_IF_ERROR_SECONDS,
        ),
        timeout_seconds=_int_env(
            "DAUM_TRENDS_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
        ),
        state_path=Path(
            os.environ.get(
                "DAUM_TRENDS_STATE_PATH",
                "runtime/daum_trends_snapshot.json",
            )
        ),
    )
