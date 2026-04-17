"""Daum 실시간 트렌드 로컬 개발 서버."""

from __future__ import annotations

import argparse
import logging
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from daum_trends_web.app import create_app

logger = logging.getLogger(__name__)


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class LoggingWSGIRequestHandler(WSGIRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        logger.info("%s - %s", self.client_address[0], fmt % args)


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
    parser.add_argument(
        "--stale-if-error",
        type=int,
        default=21600,
        help="원본 호출 실패 시 마지막 성공 데이터를 허용할 최대 시간(초)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Daum 요청 타임아웃(초)",
    )
    parser.add_argument(
        "--state-path",
        default="runtime/daum_trends_snapshot.json",
        help="마지막 성공 응답을 저장할 JSON 경로",
    )
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    args = build_argument_parser().parse_args()

    app = create_app(
        cache_ttl_seconds=args.cache_ttl,
        stale_if_error_seconds=args.stale_if_error,
        timeout_seconds=args.timeout,
        state_path=args.state_path,
    )
    server = make_server(
        args.host,
        args.port,
        app,
        server_class=ThreadingWSGIServer,
        handler_class=LoggingWSGIRequestHandler,
    )

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
