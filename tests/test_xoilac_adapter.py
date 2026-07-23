from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

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

    def test_signed_runner_403_is_candidate_only_and_retried(self) -> None:
        entry = xoilac.StreamCapture(
            url="https://live.example/channel.flv?wsSecret=abc&wsABSTime=1893456000",
            player_type="7",
            status=403,
        )
        xoilac.classify_stream(entry)
        self.assertFalse(entry.publishable)
        self.assertEqual(entry.classification, "signed_runner_blocked")
        row = entry.as_dict()
        xoilac.annotate_multisource_playability(row)
        self.assertEqual(row["playability"], "candidate-403")
        self.assertFalse(row["observed_active"])
        stop, reason, blocked = xoilac.evaluate_token_attempt(
            [entry],
            ["https://xlz.livepingscorex.com/ajax/chanel/type/7/link/channel43/off-tvc"],
            600,
        )
        self.assertFalse(stop)
        self.assertEqual(reason, "signed-403-refresh")
        self.assertEqual(blocked, 1)

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

    def test_default_max_matches_is_unlimited(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch("sys.argv", ["xoilac.py"]):
            args = xoilac.parse_args()
        self.assertEqual(args.max_matches, 0)
        self.assertEqual(args.past_minutes, 150)
        self.assertEqual(args.future_minutes, 180)
        self.assertEqual(args.match_concurrency, 3)


    def test_zero_max_matches_keeps_all_ranked_candidates(self) -> None:
        now = datetime(2026, 7, 23, 1, 30, tzinfo=xoilac.VN_TZ)
        candidates = [
            {"url": f"https://example.test/truc-tiep/team-{i}-vs-team-b-luc-{(i%24):02d}00-ngay-23-07-2026/", "live_hint": False}
            for i in range(25)
        ]
        selected, _counts = xoilac.rank_scan_candidates(
            candidates, past_minutes=1500, future_minutes=1500, max_matches=0, now=now
        )
        self.assertEqual(len(selected), 25)

    def test_live_and_started_matches_are_ranked_before_upcoming(self) -> None:
        tz = ZoneInfo("Asia/Ho_Chi_Minh")
        now = datetime(2026, 7, 23, 2, 0, tzinfo=tz)
        candidates = [
            {"url": "https://xoilacz.io/truc-tiep/upcoming-vs-one-luc-0210-ngay-23-07-2026/", "live_hint": False},
            {"url": "https://xoilacz.io/truc-tiep/started-vs-one-luc-0100-ngay-23-07-2026/", "live_hint": False},
            {"url": "https://xoilacz.io/truc-tiep/live-vs-one-luc-0030-ngay-23-07-2026/", "live_hint": True},
            {"url": "https://xoilacz.io/truc-tiep/far-vs-one-luc-0400-ngay-23-07-2026/", "live_hint": False},
        ]
        selected, counts = xoilac.rank_scan_candidates(
            candidates, past_minutes=150, future_minutes=240, max_matches=20, now=now
        )
        self.assertIn("live-vs-one", selected[0])
        self.assertIn("started-vs-one", selected[1])
        self.assertIn("upcoming-vs-one", selected[2])
        self.assertEqual(counts["live"], 1)
        self.assertEqual(counts["started"], 1)

    def test_scan_window_keeps_minus_150_and_plus_180_boundaries(self) -> None:
        now = datetime(2026, 7, 23, 12, 0, tzinfo=xoilac.VN_TZ)
        candidates = [
            {"url": "https://xoilacz.io/truc-tiep/past-in-vs-rival-luc-0930-ngay-23-07-2026/", "live_hint": False},
            {"url": "https://xoilacz.io/truc-tiep/past-out-vs-rival-luc-0929-ngay-23-07-2026/", "live_hint": False},
            {"url": "https://xoilacz.io/truc-tiep/future-in-vs-rival-luc-1500-ngay-23-07-2026/", "live_hint": False},
            {"url": "https://xoilacz.io/truc-tiep/future-out-vs-rival-luc-1501-ngay-23-07-2026/", "live_hint": False},
        ]
        selected, _counts = xoilac.rank_scan_candidates(
            candidates, past_minutes=150, future_minutes=180, max_matches=0, now=now
        )
        joined = "\n".join(selected)
        self.assertIn("past-in-vs-rival", joined)
        self.assertIn("future-in-vs-rival", joined)
        self.assertNotIn("past-out-vs-rival", joined)
        self.assertNotIn("future-out-vs-rival", joined)

    def test_candidate_403_is_not_written_to_main_playlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outputs = [root / name for name in (
                "xoilac_live.m3u", "xoilac_live_pipe.m3u", "xoilac_live_vlc.m3u", "xoilac_debug.json"
            )]
            result = {
                "source": "xoilac",
                "input_url": "https://xoilacz.io/truc-tiep/a-vs-b-luc-2030-ngay-22-07-2026/",
                "final_url": "https://xoilacz.io/truc-tiep/a-vs-b-luc-2030-ngay-22-07-2026/",
                "match_name": "A vs B",
                "streams": [{
                    "url": "https://live.example/a.flv?wsSecret=x&wsABSTime=1893456000",
                    "kind": "flv",
                    "status": 403,
                    "verified": False,
                    "probe_ok": False,
                    "has_secret": True,
                    "publishable": False,
                    "classification": "signed_runner_blocked",
                    "placeholder_suspected": False,
                }],
                "sources": [],
            }
            with patch.object(xoilac, "OUTPUT_M3U", outputs[0]), \
                 patch.object(xoilac, "OUTPUT_PIPE_M3U", outputs[1]), \
                 patch.object(xoilac, "OUTPUT_VLC_M3U", outputs[2]), \
                 patch.object(xoilac, "OUTPUT_DEBUG", outputs[3]), \
                 patch.dict("os.environ", {"XOILAC_WRITE_AUDIT_M3U": "0"}, clear=False):
                matches, links = xoilac.write_outputs([result])
            self.assertEqual((matches, links), (0, 0))
            self.assertEqual(outputs[0].read_text(encoding="utf-8"), "#EXTM3U\n")
            payload = json.loads(outputs[3].read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["candidate_403"], 1)

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
                            "status": 200,
                            "verified": True,
                            "probe_ok": True,
                            "has_secret": True,
                            "publishable": True,
                            "classification": "signed",
                            "placeholder_suspected": False,
                            "playability": "verified",
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
            self.assertEqual(stream["playability"], "verified")


class XoilacConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_workers_limit_peak_and_preserve_result_order(self) -> None:
        args = SimpleNamespace(match_concurrency=3)
        targets = [f"https://example.test/truc-tiep/match-{index}/" for index in range(8)]
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def fake_scan(_context, target, _args, index, total):
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.01 * (9 - index))
            async with lock:
                active -= 1
            return {"target": target, "index": index, "total": total}

        with patch.object(xoilac, "scan_match", side_effect=fake_scan):
            rows = await xoilac.scan_targets_concurrently(object(), targets, args)

        self.assertEqual(peak, 3)
        self.assertEqual([row["target"] for row in rows], targets)
        self.assertEqual([row["index"] for row in rows], list(range(1, 9)))


    async def test_browser_verified_206_skips_urllib_reprobe(self) -> None:
        entry = xoilac.StreamCapture(
            url="https://live.example/channel.flv?wsSecret=abc&wsABSTime=1893456000",
            kind="flv",
            status=206,
            content_type="video/x-flv",
            verified=True,
        )
        with patch.object(xoilac, "probe_stream_sync") as probe:
            await xoilac.verify_streams([entry], timeout=5)
        probe.assert_not_called()
        self.assertTrue(entry.probe_ok)
        self.assertTrue(entry.publishable)
        self.assertEqual(entry.classification, "signed")

    async def test_one_worker_fallback_is_supported(self) -> None:
        args = SimpleNamespace(match_concurrency=1)
        targets = ["a", "b", "c"]
        active = 0
        peak = 0

        async def fake_scan(_context, target, _args, index, total):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return {"target": target, "index": index, "total": total}

        with patch.object(xoilac, "scan_match", side_effect=fake_scan):
            rows = await xoilac.scan_targets_concurrently(object(), targets, args)
        self.assertEqual(peak, 1)
        self.assertEqual([row["target"] for row in rows], targets)


if __name__ == "__main__":
    unittest.main()
