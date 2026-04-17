import json
import tempfile
import unittest
from pathlib import Path

from daum_trends_web.pages_build import build_pages_site
from daum_trends_web.scraper import TrendSnapshot


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "trends_snapshot.json"


class PagesBuildTest(unittest.TestCase):
    def test_build_pages_site(self) -> None:
        snapshot = TrendSnapshot.from_dict(
            json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = build_pages_site(snapshot, Path(tmp_dir) / "site")

            self.assertTrue((output_dir / "index.html").exists())
            self.assertTrue((output_dir / "static" / "app.js").exists())
            self.assertTrue((output_dir / "static" / "config.js").exists())
            self.assertTrue((output_dir / "data" / "trends.json").exists())
            self.assertTrue((output_dir / ".nojekyll").exists())

            config_js = (output_dir / "static" / "config.js").read_text(encoding="utf-8")
            self.assertIn("./data/trends.json", config_js)

            payload = json.loads(
                (output_dir / "data" / "trends.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["items"][0]["keyword"], "김가네")


if __name__ == "__main__":
    unittest.main()
