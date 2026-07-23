from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import main as orchestrator
from sources import phaohoa

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MATCH_URL = "https://phaohoa1.live/truc-tiep/malisheva-vs-hibernian-23-07-2026-776573"
SIMPLE_URL = "https://phaohoa1.live/truc-tiep/tho-nhi-ki-w-vs-canada-w"
STREAM_URL = "https://cdn.example.test/live/phaohoa/index.m3u8"


class PhaoHoaAdapterTests(unittest.TestCase):
    def test_route_phaohoa_domain(self) -> None:
        routed = orchestrator.route_urls([MATCH_URL])
        self.assertEqual(routed["phaohoa"], [MATCH_URL])
        self.assertTrue(all(not values for key, values in routed.items() if key != "phaohoa"))

    def test_parse_dated_slug_name_and_date(self) -> None:
        name, time_value, blv = phaohoa.derive_match_info(MATCH_URL)
        self.assertEqual(name, "Malisheva vs Hibernian")
        self.assertEqual(time_value, "")
        self.assertEqual(phaohoa.extract_date(MATCH_URL), "23/07")
        self.assertEqual(blv, "")

    def test_card_time_and_date_drive_scan_window(self) -> None:
        rows = [{
            "url": SIMPLE_URL,
            "raw_title": "Thổ Nhĩ Kì (W) VS Canada (W)",
            "raw_time": "15:00 - 23-07",
            "card_text": "15:00 - 23-07 Bóng chuyền Nations League Thổ Nhĩ Kì (W) VS Sắp diễn ra Canada (W) KaKa",
        }]
        kept, stats = phaohoa.filter_links_by_scan_window(
            rows, now=datetime(2026, 7, 23, 14, 0, tzinfo=TZ)
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["time"], "15:00")
        self.assertEqual(kept[0]["date"], "23/07")
        self.assertEqual(kept[0]["minutes_to_kickoff"], 60)
        self.assertEqual(stats["window"], 1)

    def test_noisy_card_extracts_exact_team_names(self) -> None:
        card = (
            "15:00 - 23-07 Bóng chuyền Nations League "
            "Thổ Nhĩ Kì (W) VS Sắp diễn ra Canada (W) KaKa"
        )
        identity = phaohoa.extract_card_identity(SIMPLE_URL, card, card)
        self.assertEqual(identity["match_name"], "Thổ Nhĩ Kì (W) VS Canada (W)")
        self.assertEqual(identity["blv"], "KaKa")

    def test_output_keeps_headers_and_fallback_logo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(phaohoa, "OUTPUT_M3U", root / "phaohoa_live.m3u"), \
                 patch.object(phaohoa, "OUTPUT_PIPE_M3U", root / "phaohoa_live_pipe.m3u"), \
                 patch.object(phaohoa, "OUTPUT_VLC_M3U", root / "phaohoa_live_vlc.m3u"), \
                 patch.object(phaohoa, "OUTPUT_DEBUG", root / "phaohoa_debug.json"):
                matches, links, metadata_only = phaohoa.write_outputs([{
                    "url": MATCH_URL,
                    "match_name": "Malisheva vs Hibernian",
                    "time": "21:30",
                    "date": "23/07",
                    "logo": "",
                    "sport_group": "Bóng đá",
                    "streams": [{
                        "url": STREAM_URL,
                        "referer": MATCH_URL,
                        "origin": "https://phaohoa1.live",
                        "user_agent": phaohoa.UA,
                        "playability": "upcoming-pending",
                        "content_type": "application/vnd.apple.mpegurl",
                    }],
                }])
            self.assertEqual((matches, links, metadata_only), (1, 1, 0))
            text = (root / "phaohoa_live.m3u").read_text(encoding="utf-8")
            self.assertNotIn("CHỜ PHÁT", text)
            self.assertIn("[21:30 23/07] Malisheva vs Hibernian [M3U8]", text)
            self.assertIn(STREAM_URL, text)
            self.assertIn(f"#EXTVLCOPT:http-referrer={MATCH_URL}", text)
            self.assertIn("#EXTVLCOPT:http-origin=https://phaohoa1.live", text)
            self.assertIn('tvg-logo="https://phaohoa1.live/favicon.ico"', text)


    def test_all_discovered_matches_are_written_without_stream(self) -> None:
        rows = []
        for index in range(25):
            rows.append({
                "url": f"https://phaohoa1.live/truc-tiep/doi-{index}-vs-doi-{index + 1}",
                "raw_title": f"Đội {index} VS Đội {index + 1}",
                "raw_time": "20:00 - 23-07",
                "card_text": f"20:00 - 23-07 Bóng đá Đội {index} VS Sắp diễn ra Đội {index + 1} BLV {index}",
                "blv": f"BLV {index}",
                "home_logo": f"https://cdn.example/{index}-home.png",
                "away_logo": f"https://cdn.example/{index}-away.png",
                "sport_group": "Bóng đá",
                "streams": [],
            })
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(phaohoa, "OUTPUT_M3U", root / "phaohoa_live.m3u"), \
                 patch.object(phaohoa, "OUTPUT_PIPE_M3U", root / "phaohoa_live_pipe.m3u"), \
                 patch.object(phaohoa, "OUTPUT_VLC_M3U", root / "phaohoa_live_vlc.m3u"), \
                 patch.object(phaohoa, "OUTPUT_DEBUG", root / "phaohoa_debug.json"):
                stats = phaohoa.write_outputs(rows)
            content = (root / "phaohoa_live.m3u").read_text(encoding="utf-8")
            self.assertEqual(stats, (25, 0, 25))
            self.assertEqual(content.count("#EXTINF:"), 25)
            self.assertEqual(content.count('phaohoa-entry="metadata-only"'), 25)
            self.assertEqual(content.count("http://127.0.0.1:9/__phaohoa_metadata__/"), 25)
            self.assertNotIn("\nhttps://phaohoa1.live/truc-tiep/", content)

    def test_card_identity_keeps_names_logos_and_blv(self) -> None:
        row = {
            "url": SIMPLE_URL,
            "raw_title": "Thổ Nhĩ Kì (W) VS Canada (W)",
            "raw_time": "15:00 - 23-07",
            "card_text": "15:00 - 23-07 Bóng chuyền Nations League Thổ Nhĩ Kì (W) VS Sắp diễn ra Canada (W) KaKa",
            "home_logo": "https://cdn.example/home.png",
            "away_logo": "https://cdn.example/away.png",
            "streams": [],
        }
        phaohoa.hydrate_discovered_match_metadata(row, now=datetime(2026, 7, 23, 12, 0, tzinfo=TZ))
        self.assertEqual(row["match_name"], "Thổ Nhĩ Kì (W) VS Canada (W)")
        self.assertEqual(row["blv"], "KaKa")
        self.assertEqual(row["time"], "15:00")
        self.assertEqual(row["date"], "23/07")
        self.assertEqual(row["home_logo"], "https://cdn.example/home.png")
        self.assertEqual(row["away_logo"], "https://cdn.example/away.png")


if __name__ == "__main__":
    unittest.main()
