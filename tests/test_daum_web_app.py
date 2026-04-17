import json
import unittest

from daum_trends_web.app import DaumTrendsApp
from daum_trends_web.scraper import TrendItem, TrendSnapshot


class _FakeCache:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_snapshot(self, force_refresh=False):
        return self.snapshot


def _call_wsgi_app(app, path):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(
        app(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": path,
                "QUERY_STRING": "",
            },
            start_response,
        )
    )
    return captured["status"], captured["headers"], body


class DaumWebAppTest(unittest.TestCase):
    def setUp(self):
        snapshot = TrendSnapshot(
            source_url="https://m.daum.net/",
            updated_at_label="오늘 20:39 기준",
            retrieved_at="2026-04-17T20:43:58+09:00",
            notice="테스트 공지",
            items=[
                TrendItem(
                    rank=1,
                    keyword="붉은 진주",
                    status="동일",
                    url="https://m.search.daum.net/search?q=test",
                )
            ],
        )
        self.app = DaumTrendsApp(cache=_FakeCache(snapshot))

    def test_healthz(self):
        status, headers, body = _call_wsgi_app(self.app, "/healthz")
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(body.decode("utf-8")), {"ok": True})

    def test_api_trends(self):
        status, _, body = _call_wsgi_app(self.app, "/api/trends")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload["item_count"], 1)
        self.assertEqual(payload["items"][0]["keyword"], "붉은 진주")


if __name__ == "__main__":
    unittest.main()

