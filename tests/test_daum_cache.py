import unittest

from daum_trends_web.scraper import TrendCache, TrendFetchError, TrendItem, TrendSnapshot


class TrendCacheTest(unittest.TestCase):
    def test_returns_stale_snapshot_when_refresh_fails(self):
        calls = {"count": 0}

        def fetcher():
            calls["count"] += 1
            if calls["count"] == 1:
                return TrendSnapshot(
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
            raise TrendFetchError("임시 네트워크 장애")

        cache = TrendCache(
            ttl_seconds=0,
            stale_if_error_seconds=21600,
            fetcher=fetcher,
        )

        fresh = cache.get_snapshot()
        stale = cache.get_snapshot()

        self.assertFalse(fresh.stale)
        self.assertTrue(stale.stale)
        self.assertIn("임시 네트워크 장애", stale.warning)


if __name__ == "__main__":
    unittest.main()
