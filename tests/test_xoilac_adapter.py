from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sources import xoilac


class XoilacAdapterTests(unittest.TestCase):
    def test_canonical_match_url_removes_link_query_and_fragment(self) -> None:
        url = (
            "https://malaysiandigest.com/truc-tiep/a-vs-b-luc-2030-ngay-22-07-2026/"
            "link/2/?refresh=1#player"
        )
        self.assertEqual(
            xoilac.canonical_match_url(url),
            "https://malaysiandigest.com/truc-tiep/a-vs-b-luc-2030-ngay-22-07-2026/",
        )

    def test_metadata_is_derived_from_match_slug(self) -> None:
        metadata = xoilac.derive_match_metadata(
            "https://malaysiandigest.com/truc-tiep/arsenal-vs-chelsea-luc-2030-ngay-22-07-2026/"
        )
        self.assertEqual(metadata["name"], "Arsenal vs Chelsea")
        self.assertEqual(metadata["time"], "20:30")
        self.assertEqual(metadata["date"], "22/07/2026")
        self.assertEqual(metadata["home_name"], "Arsenal")
        self.assertEqual(metadata["away_name"], "Chelsea")

    def test_commentator_prefix_is_cleaned(self) -> None:
        self.assertEqual(xoilac.clean_commentator_label("BLV Gấu", 0), "Gấu")
        self.assertEqual(xoilac.clean_commentator_label("Bình luận viên: Một", 1), "Một")

    def test_flv_library_is_not_treated_as_stream(self) -> None:
        self.assertFalse(xoilac.is_media_candidate("https://cdn.example/flv.min.js"))
        self.assertTrue(xoilac.is_media_candidate("https://cdn.example/live.flv?wsSecret=abc"))

    def test_type8_unsigned_placeholder_is_rejected(self) -> None:
        entry = xoilac.StreamCapture(
            url="https://live2.streambylivepulse.com/banner.flv",
            player_type="8",
            status=200,
            probe_ok=True,
        )
        xoilac.classify_stream(entry)
        self.assertTrue(entry.placeholder_suspected)
        self.assertFalse(entry.publishable)
        self.assertEqual(entry.classification, "placeholder_or_ad")

    def test_signed_runner_403_is_kept_for_client(self) -> None:
        entry = xoilac.StreamCapture(
            url="https://live.example/channel.flv?wsSecret=abc&wsABSTime=1893456000",
            player_type="7",
            status=403,
        )
        xoilac.classify_stream(entry)
        self.assertTrue(entry.publishable)
        self.assertEqual(entry.classification, "signed_runner_blocked")
        row = entry.as_dict()
        xoilac.annotate_multisource_playability(row)
        self.assertEqual(row["playability"], "browser-observed")
        self.assertTrue(row["observed_active"])

    def test_hex_signed_expiry_is_supported(self) -> None:
        parsed = xoilac.parse_signed_expiry(
            "https://live.example/a.flv?wsSecret=x&wsABSTime=70DBD880"
        )
        self.assertEqual(parsed["timestamp"], int("70DBD880", 16))

    def test_expired_signed_url_is_removed_at_output_time(self) -> None:
        row = {
            "url": "https://live.example/a.flv?wsSecret=x&wsABSTime=1",
            "has_secret": True,
            "publishable": True,
            "classification": "signed",
            "status": 200,
            "verified": True,
        }
        xoilac.refresh_output_classification(row)
        self.assertFalse(row["publishable"])
        self.assertEqual(row["classification"], "expired")
        self.assertEqual(row["playability"], "rejected")

    def test_write_outputs_matches_multisource_schema_and_keeps_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "xoilac_live.m3u"
            output_pipe = root / "xoilac_live_pipe.m3u"
            output_vlc = root / "xoilac_live_vlc.m3u"
            output_debug = root / "xoilac_debug.json"
            results = [
                {
                    "source": "xoilac",
                    "url": "https://malaysiandigest.com/truc-tiep/a-vs-b-luc-2030-ngay-22-07-2026/",
                    "input_url": "https://xoilacz.io/truc-tiep/a-vs-b-luc-2030-ngay-22-07-2026/",
                    "final_url": "https://malaysiandigest.com/truc-tiep/a-vs-b-luc-2030-ngay-22-07-2026/",
                    "kickoff_iso": "2026-07-22T20:30:00+07:00",
                    "minutes_to_kickoff": 30,
                    "scan_window_reason": "time-window",
                    "match_name": "A vs B",
                    "home_name": "A",
                    "away_name": "B",
                    "time": "20:30",
                    "date": "22/07/2026",
                    "league": "Bóng đá",
                    "home_logo": "https://img.example/a.png",
                    "away_logo": "https://img.example/b.png",
                    "sources": [{"commentator": "Một"}],
                    "streams": [
                        {
                            "url": "https://live.example/a.flv?wsSecret=abc&wsABSTime=1893456000",
                            "kind": "flv",
                            "referer": "https://malaysiandigest.com/truc-tiep/a/link/1/",
                            "origin": "https://malaysiandigest.com",
                            "user_agent": xoilac.UA,
                            "commentator": "Một",
                            "source_index": 0,
                            "status": 403,
                            "verified": False,
                            "probe_ok": False,
                            "has_secret": True,
                            "publishable": True,
                            "classification": "signed_runner_blocked",
                            "placeholder_suspected": False,
                            "playability": "browser-observed",
                            "observed_active": True,
                        }
                    ],
                }
            ]
            with patch.object(xoilac, "OUTPUT_M3U", output), \
                 patch.object(xoilac, "OUTPUT_PIPE_M3U", output_pipe), \
                 patch.object(xoilac, "OUTPUT_VLC_M3U", output_vlc), \
                 patch.object(xoilac, "OUTPUT_DEBUG", output_debug), \
                 patch.dict("os.environ", {"XOILAC_WRITE_AUDIT_M3U": "0"}, clear=False):
                matches, links = xoilac.write_outputs(results)

            self.assertEqual((matches, links), (1, 1))
            text = output.read_text(encoding="utf-8")
            self.assertIn('[20:30 22/07/2026] A vs B [BLV Một] [FLV]', text)
            self.assertIn('#EXTVLCOPT:http-referrer=', text)
            self.assertIn('#EXTHTTP:{', text)
            self.assertNotIn('|User-Agent=', text)
            payload = json.loads(output_debug.read_text(encoding="utf-8"))
            stream = payload["results"][0]["streams"][0]
            self.assertEqual(stream["playability"], "browser-observed")


if __name__ == "__main__":
    unittest.main()
