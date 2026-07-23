from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import main as orchestrator
from sources import colatv


TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MATCH_URL = (
    "https://colatv77.live/truc-tiep/"
    "bologna-fc-1909-vs-heidenheimer-luc-2100-ngay-22-07-2026-l6kegi82xrnbv75"
)
STREAM_URL = "https://live05.meung.app/live/75915087.m3u8"


class ColaTVAdapterTests(unittest.TestCase):
    def test_route_colatv_domain(self) -> None:
        routed = orchestrator.route_urls([MATCH_URL])
        self.assertEqual(routed["colatv"], [MATCH_URL])
        self.assertTrue(all(not values for key, values in routed.items() if key != "colatv"))

    def test_parse_name_time_and_date_from_colatv_url(self) -> None:
        name, time_value, blv = colatv.derive_match_info(MATCH_URL)
        self.assertEqual(name, "Bologna FC 1909 vs Heidenheimer")
        self.assertEqual(time_value, "21:00")
        self.assertEqual(colatv.extract_date(MATCH_URL), "22/07")
        self.assertEqual(blv, "")

    def test_sample_meung_m3u8_is_direct_stream(self) -> None:
        self.assertTrue(colatv.is_direct_stream_url(STREAM_URL))
        self.assertEqual(colatv.stream_kind(STREAM_URL), "m3u8")

    def test_full_match_referer_is_preserved(self) -> None:
        self.assertEqual(colatv.normalize_playback_referer(MATCH_URL), MATCH_URL)

    def test_filter_uses_schedule_embedded_in_url(self) -> None:
        rows = [{"url": MATCH_URL, "raw_title": "", "raw_time": "", "card_text": ""}]
        kept, stats = colatv.filter_links_by_scan_window(
            rows,
            now=datetime(2026, 7, 22, 20, 0, tzinfo=TZ),
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["time"], "21:00")
        self.assertEqual(kept[0]["date"], "22/07")
        self.assertEqual(kept[0]["minutes_to_kickoff"], 60)
        self.assertEqual(stats["window"], 1)

    def test_output_keeps_referer_origin_and_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(colatv, "OUTPUT_M3U", root / "colatv_live.m3u"), \
                 patch.object(colatv, "OUTPUT_PIPE_M3U", root / "colatv_live_pipe.m3u"), \
                 patch.object(colatv, "OUTPUT_VLC_M3U", root / "colatv_live_vlc.m3u"), \
                 patch.object(colatv, "OUTPUT_DEBUG", root / "colatv_debug.json"):
                matches, links = colatv.write_outputs([{
                    "url": MATCH_URL,
                    "match_name": "Bologna FC 1909 vs Heidenheimer",
                    "time": "21:00",
                    "date": "22/07",
                    "logo": "https://colatv77.live/favicon.ico",
                    "sport_group": "Bóng đá",
                    "streams": [{
                        "url": STREAM_URL,
                        "referer": MATCH_URL,
                        "origin": "https://colatv77.live",
                        "user_agent": colatv.UA,
                        "playability": "upcoming-pending",
                        "content_type": "application/vnd.apple.mpegurl",
                    }],
                }])
            self.assertEqual((matches, links), (1, 1))
            text = (root / "colatv_live.m3u").read_text(encoding="utf-8")
            self.assertNotIn("CHỜ PHÁT", text)
            self.assertIn("[21:00 22/07] Bologna FC 1909 vs Heidenheimer [M3U8]", text)
            self.assertIn(STREAM_URL, text)
            self.assertIn(f"#EXTVLCOPT:http-referrer={MATCH_URL}", text)
            self.assertIn("#EXTVLCOPT:http-origin=https://colatv77.live", text)
            self.assertIn('"Origin":"https://colatv77.live"', text)


if __name__ == "__main__":
    unittest.main()
