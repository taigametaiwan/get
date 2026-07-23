from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from merger import SourceFiles, merge_sources

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


class WindowedCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 7, 20, 7, 0, tzinfo=TZ)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_source(self, rows: list[dict]) -> SourceFiles:
        universal = self.root / "colatv.m3u"
        universal.write_text("#EXTM3U\n", encoding="utf-8")
        pipe = self.root / "colatv_pipe.m3u"
        pipe.write_text("#EXTM3U\n", encoding="utf-8")
        vlc = self.root / "colatv_vlc.m3u"
        vlc.write_text("#EXTM3U\n", encoding="utf-8")
        debug = self.root / "colatv_debug.json"
        debug.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return SourceFiles("colatv", "ColaTV", universal, pipe, vlc, debug)

    def test_catalog_metadata_only_never_enters_verified_only_playlist(self) -> None:
        rows = [
            {"url": "https://colatv77.live/truc-tiep/old-vs-team", "match_name": "Old VS Team", "time": "04:29", "date": "20/07", "listed_in_playlist": True, "catalog_only": True},
            {"url": "https://colatv77.live/truc-tiep/past-edge-vs-team", "match_name": "Past Edge VS Team", "time": "04:30", "date": "20/07", "listed_in_playlist": True, "catalog_only": True},
            {"url": "https://colatv77.live/truc-tiep/future-edge-vs-team", "match_name": "Future Edge VS Team", "time": "10:00", "date": "20/07", "listed_in_playlist": True, "catalog_only": True},
            {"url": "https://colatv77.live/truc-tiep/far-vs-team", "match_name": "Far VS Team", "time": "10:01", "date": "20/07", "listed_in_playlist": True, "catalog_only": True},
        ]
        report = merge_sources(self.root, [self.make_source(rows)], now=self.now, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 0)
        self.assertEqual(report["metadata_reference_count"], 4)
        self.assertFalse(report["channels"])
        self.assertEqual((self.root / "all_live.m3u").read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_catalog_without_time_is_not_displayed_as_metadata_only(self) -> None:
        rows = [{"url": "https://colatv77.live/truc-tiep/no-time-vs-team", "match_name": "No Time VS Team", "listed_in_playlist": True, "catalog_only": True}]
        report = merge_sources(self.root, [self.make_source(rows)], now=self.now, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 0)


if __name__ == "__main__":
    unittest.main()
