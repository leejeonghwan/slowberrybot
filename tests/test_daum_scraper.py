import unittest
from pathlib import Path

from daum_trends_web.scraper import TrendParseError, parse_trend_snapshot


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "daum_trend_sample.html"


class ParseTrendSnapshotTest(unittest.TestCase):
    def test_parse_fixture(self) -> None:
        snapshot = parse_trend_snapshot(FIXTURE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(snapshot.updated_at_label, "오늘 11:12 기준")
        self.assertTrue(snapshot.notice.startswith("실시간 트렌드 데이터는"))
        self.assertEqual(len(snapshot.items), 3)

        self.assertEqual(snapshot.items[0].rank, 1)
        self.assertEqual(snapshot.items[0].keyword, "김가네")
        self.assertEqual(snapshot.items[0].status, "하락")
        self.assertIn("m.search.daum.net", snapshot.items[0].url)

        self.assertEqual(snapshot.items[2].keyword, "베세토")
        self.assertEqual(snapshot.items[2].status, "신규")

    def test_raises_when_trend_layer_missing(self) -> None:
        with self.assertRaises(TrendParseError):
            parse_trend_snapshot("<html><body>missing</body></html>")


if __name__ == "__main__":
    unittest.main()
