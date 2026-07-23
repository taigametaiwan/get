from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from merger import SourceFiles, merge_sources

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

class AllCardsCatalogTests(unittest.TestCase):
    def make_source(self, root: Path, key: str = "chuoichien") -> SourceFiles:
        for name in ("src.m3u", "pipe.m3u", "vlc.m3u"):
            (root / name).write_text("#EXTM3U\n", encoding="utf-8")
        return SourceFiles(key, "Chuối Chiên", root/"src.m3u", root/"pipe.m3u", root/"vlc.m3u", root/"debug.json")

    def test_catalog_card_without_stream_stays_in_debug_not_main_playlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.make_source(root)
            source.debug.write_text(json.dumps([{
                "url": "https://example.com/truc-tiep/a-vs-b", "match_name": "A VS B",
                "time": "13:00", "date": "23/07/2026", "listed_in_playlist": True,
                "streams": []
            }]), encoding="utf-8")
            report = merge_sources(root, [source], now=datetime(2026,7,23,12,0,tzinfo=TZ), preserve_on_empty=False)
            self.assertEqual(report["selected_count"], 0)
            self.assertEqual(report["metadata_reference_count"], 1)
            self.assertTrue(report["policy"]["verified_only"])
            self.assertEqual((root / "all_live.m3u").read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_debug_row_without_catalog_flag_does_not_change_old_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.make_source(root)
            source.debug.write_text(json.dumps([{
                "url": "https://example.com/truc-tiep/a-vs-b", "match_name": "A VS B",
                "time": "20:30", "date": "23/07/2026", "streams": []
            }]), encoding="utf-8")
            report = merge_sources(root, [source], now=datetime(2026,7,23,12,0,tzinfo=TZ), preserve_on_empty=False)
            self.assertEqual(report["selected_count"], 0)

    def test_real_stream_replaces_catalog_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = self.make_source(root)
            stream = "https://cdn.example/a.m3u8"
            source.universal.write_text(f'#EXTM3U\n#EXTINF:-1 tvg-id="a",[13:00 23/07] A VS B [M3U8]\n{stream}\n', encoding="utf-8")
            source.debug.write_text(json.dumps([{
                "url": "https://example.com/truc-tiep/a-vs-b", "match_name": "A VS B",
                "time": "13:00", "date": "23/07/2026", "listed_in_playlist": True,
                "streams": [{"url": stream, "playability": "verified", "http_status": 200}]
            }]), encoding="utf-8")
            report = merge_sources(root, [source], now=datetime(2026,7,23,12,0,tzinfo=TZ), preserve_on_empty=False)
            self.assertEqual(report["selected_count"], 1)
            self.assertEqual(report["channels"][0]["url"], stream)
            self.assertEqual(report["channels"][0]["entry_mode"], "stream")

    def test_xoilac_102_homepage_cards_remain_in_debug_but_not_fake_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root, key="xoilac")
            rows = [
                {
                    "source": "xoilac",
                    "url": f"https://xoilacz.io/truc-tiep/team-{index}-vs-rival-luc-1300-ngay-23-07-2026/",
                    "match_name": f"Team {index} VS Rival",
                    "time": "13:00",
                    "date": "23/07/2026",
                    "listed_in_playlist": True,
                    "catalog_only": True,
                    "streams": [],
                }
                for index in range(102)
            ]
            source.debug.write_text(json.dumps({"results": rows}), encoding="utf-8")
            report = merge_sources(
                root,
                [source],
                now=datetime(2026, 7, 23, 12, 0, tzinfo=TZ),
                preserve_on_empty=False,
            )
            self.assertEqual(report["selected_count"], 0)
            self.assertEqual(report["metadata_reference_count"], 102)
            self.assertFalse(report["channels"])
            self.assertNotIn("/__multisource_metadata__/", (root / "all_live.m3u").read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
