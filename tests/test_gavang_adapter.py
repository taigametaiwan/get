from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main as orchestrator
from sources import gavang


MATCH_URL = (
    "https://smorf.io/s8-live/2436/marielhamn-lahti-finveik/"
    "?s8_live_fixture_id=2436&"
    "s8_live_stream_key=marielhamn-lahti-finveik&"
    "s8_auto_sound=1"
)

DALIAN_URL = (
    "https://smorf.io/s8-live/2448/dalian-beijing-chnfa/"
    "?s8_live_fixture_id=2448&"
    "s8_live_stream_key=dalian-beijing-chnfa&"
    "s8_auto_sound=1"
)

QUEENSLAND_URL = (
    "https://smorf.io/s8-live/2449/queensland-perth-ausffa/"
    "?s8_live_fixture_id=2449&"
    "s8_live_stream_key=queensland-perth-ausffa&"
    "s8_auto_sound=1"
)


class GavangAdapterTests(unittest.TestCase):
    def test_route_smorf_to_gavang(self):
        routed = orchestrator.route_urls([MATCH_URL])
        self.assertEqual(routed["gavang"], [MATCH_URL])
        self.assertEqual(routed["chuoichien"], [])
        self.assertEqual(routed["luongson"], [])

    def test_extract_fixture_id(self):
        self.assertEqual(gavang.match_id_from_url(MATCH_URL), "2436")

    def test_extract_stream_key_from_query(self):
        self.assertEqual(
            gavang.extract_gavang_stream_key(MATCH_URL),
            "marielhamn-lahti-finveik",
        )

    def test_extract_stream_key_from_path_fallback(self):
        value = "https://smorf.io/s8-live/999/team-a-vs-team-b/"
        self.assertEqual(gavang.extract_gavang_stream_key(value), "team-a-vs-team-b")

    def test_reject_unsafe_stream_key(self):
        value = (
            "https://smorf.io/s8-live/999/test/"
            "?s8_live_stream_key=../../secret"
        )
        # Query không an toàn bị bỏ; slug an toàn vẫn là fallback.
        self.assertEqual(gavang.extract_gavang_stream_key(value), "test")

    def test_derived_flv_candidate_has_required_headers(self):
        rows = gavang.derived_gavang_stream_candidates(MATCH_URL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["url"],
            "https://flv.lauthaitv.cc/live/marielhamn-lahti-finveik.flv",
        )
        self.assertEqual(rows[0]["referer"], MATCH_URL)
        self.assertEqual(rows[0]["origin"], "https://smorf.io")
        self.assertEqual(rows[0]["source"], "derived/s8_live_stream_key")

    def test_unknown_time_stream_key_is_kept_for_direct_probe(self):
        rows = [{
            "url": DALIAN_URL,
            "raw_title": "Dalian Kewei vs Beijing Guoan",
            "raw_time": "",
            "card_text": "Dalian Kewei vs Beijing Guoan",
        }]
        kept, stats = gavang.filter_links_by_scan_window(rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["scan_window_reason"], "unknown-time-derived-probe")
        self.assertTrue(kept[0]["derived_probe_only"])
        self.assertEqual(stats["unknown_key_probe"], 1)
        self.assertEqual(stats["unknown"], 0)

    def test_unknown_time_without_stream_key_is_still_rejected(self):
        rows = [{
            "url": "https://smorf.io/news/not-a-fixture/",
            "raw_title": "A vs B",
            "raw_time": "",
            "card_text": "A vs B",
        }]
        kept, stats = gavang.filter_links_by_scan_window(rows)
        self.assertEqual(kept, [])
        self.assertEqual(stats["unknown"], 1)

    def test_flv_http_headers_survive_streaming_body_timeout(self):
        class FakeHeaders(dict):
            def items(self):
                return super().items()

        class FakeResponse:
            status = 200
            headers = FakeHeaders({
                "Content-Type": "video/x-flv",
                "X-Has-Token": "123",
            })
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def getcode(self):
                return 200
            def geturl(self):
                return "https://flv.lauthaitv.cc/live/dalian-beijing-chnfa.flv"
            def read1(self, _size):
                raise TimeoutError("live body keeps streaming")

        with patch.object(gavang.urllib.request, "urlopen", side_effect=lambda *_a, **_k: FakeResponse()) as urlopen:
            result = gavang.probe_stream_sync(
                "https://flv.lauthaitv.cc/live/dalian-beijing-chnfa.flv",
                gavang.UA,
                DALIAN_URL,
                "https://smorf.io",
                timeout=3,
            )
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(result["playable"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["content_type"], "video/x-flv")
        self.assertIn("live chunked", result["detail"])

    def test_dead_flv_timeout_is_not_retried_twice(self):
        with patch.object(
            gavang.urllib.request,
            "urlopen",
            side_effect=TimeoutError("inactive live socket"),
        ) as urlopen:
            result = gavang.probe_stream_sync(
                "https://flv.lauthaitv.cc/live/not-started.flv",
                gavang.UA,
                QUEENSLAND_URL,
                "https://smorf.io",
                timeout=3,
            )
        self.assertEqual(urlopen.call_count, 1)
        self.assertFalse(result["playable"])
        self.assertEqual(result["status"], 0)

    def test_shared_player_url_is_removed_across_distinct_fixtures(self):
        shared = "https://live-bong.s3.ap-southeast-1.amazonaws.com/player/master.m3u8"
        unique = "https://flv.lauthaitv.cc/live/dalian-beijing-chnfa.flv"
        rows = [
            {"url": DALIAN_URL, "streams": [{"url": shared}, {"url": unique}]},
            {"url": MATCH_URL, "streams": [{"url": shared}]},
        ]
        removed = gavang.remove_cross_match_shared_streams(rows)
        self.assertEqual(removed, 2)
        self.assertEqual([item["url"] for item in rows[0]["streams"]], [unique])
        self.assertEqual(rows[1]["streams"], [])
        self.assertIn("nhiều fixture", rows[1]["rejected_streams"][0]["reject_reason"])

    def test_blv_control_text_is_trimmed(self):
        self.assertEqual(
            gavang.normalize_blv_name(
                "NGƯỜI CHÈ TRẬN Đổi trận Bình luận Mô phỏng server 1"
            ),
            "NGƯỜI CHÈ",
        )

    def test_home_fixture_dedupe_keeps_rich_metadata(self):
        rows = [
            {
                "url": QUEENSLAND_URL,
                "raw_title": "queensland perth ausffa",
                "raw_time": "",
                "raw_blv": "",
                "card_text": "",
            },
            {
                "url": QUEENSLAND_URL.replace("&s8_auto_sound=1", ""),
                "raw_title": "Queensland Lions SC VS Perth Glory",
                "raw_time": "21/07/2026 16:30",
                "raw_blv": "NGƯỜI CHÈ TRẬN Đổi trận Bình luận Mô phỏng",
                "card_text": "Queensland Lions SC VS Perth Glory 16:30",
            },
            {
                "url": "https://smorf.io/s8-live/2449/queensland-perth-ausffa/",
                "raw_title": "Queensland Lions SC vs Perth Glory",
                "raw_time": "16:30",
                "raw_blv": "",
                "card_text": "",
            },
        ]
        merged, duplicates = gavang.dedupe_home_links(rows)
        self.assertEqual(duplicates, 2)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["raw_title"], "Queensland Lions SC VS Perth Glory")
        self.assertEqual(merged[0]["raw_time"], "21/07/2026 16:30")
        self.assertIn("NGƯỜI CHÈ", merged[0]["raw_blv"])
        self.assertIn("s8_live_stream_key", merged[0]["url"])

    def test_apply_detail_metadata_fixes_title_time_and_blv(self):
        match = {
            "url": QUEENSLAND_URL,
            "match_name": "queensland perth ausffa",
            "time": "",
            "date": "",
            "blv": "",
        }
        metadata = {
            "title": "Queensland Lions SC VS Perth Glory",
            "time_candidates": [
                {
                    "value": "2026-07-21T09:30:00Z",
                    "score": 100,
                    "source": "json-ld/startDate",
                }
            ],
            "blv": "NGƯỜI CHÈ TRẬN Đổi trận Bình luận Mô phỏng",
        }
        changes = gavang.apply_basic_match_metadata(match, metadata)
        self.assertEqual(match["match_name"], "Queensland Lions SC VS Perth Glory")
        self.assertEqual(match["time"], "16:30")
        self.assertEqual(match["date"], "21/07")
        self.assertEqual(match["blv"], "NGƯỜI CHÈ")
        self.assertEqual(changes["time"], "16:30")

    def test_playlist_display_uses_enriched_gavang_metadata(self):
        result = {
            "url": QUEENSLAND_URL,
            "match_name": "Queensland Lions SC VS Perth Glory",
            "time": "16:30",
            "date": "21/07",
            "blv": "NGƯỜI CHÈ",
            "sport_group": "Bóng đá",
            "streams": [{
                "url": "https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv",
                "referer": QUEENSLAND_URL,
                "origin": "https://smorf.io",
                "user_agent": gavang.UA,
                "playability": "verified",
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ("gavang_live.m3u", "gavang_live_pipe.m3u", "gavang_live_vlc.m3u")]
            debug = Path(tmp) / "gavang_debug.json"
            with patch.object(gavang, "OUTPUT_M3U", paths[0]), \
                 patch.object(gavang, "OUTPUT_PIPE_M3U", paths[1]), \
                 patch.object(gavang, "OUTPUT_VLC_M3U", paths[2]), \
                 patch.object(gavang, "OUTPUT_DEBUG", debug):
                count_links, count_matches = gavang.write_outputs([result])
            text = paths[0].read_text(encoding="utf-8")
        self.assertEqual((count_links, count_matches), (1, 1))
        self.assertIn("[16:30 21/07] Queensland Lions SC VS Perth Glory", text)
        self.assertIn("[BLV NGƯỜI CHÈ] [FLV]", text)

    def test_nested_jsonld_logo_object_is_normalized(self):
        value = {"contentUrl": {"url": "/media/team/queensland.png"}}
        self.assertEqual(
            gavang.normalize_logo_url(value, QUEENSLAND_URL),
            "https://smorf.io/media/team/queensland.png",
        )
        self.assertEqual(gavang.normalize_logo_url("[object Object]", QUEENSLAND_URL), "")

    def test_playlist_never_writes_object_object_and_uses_source_fallback(self):
        result = {
            "url": QUEENSLAND_URL,
            "match_name": "Queensland Lions SC VS Perth Glory",
            "time": "16:30",
            "date": "21/07",
            "blv": "",
            "sport_group": "Bóng đá",
            "logo": {"url": "[object Object]"},
            "source_logo": "https://smorf.io/favicon.ico",
            "streams": [{
                "url": "https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv",
                "referer": QUEENSLAND_URL,
                "origin": "https://smorf.io",
                "user_agent": gavang.UA,
                "playability": "verified",
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ("gavang_live.m3u", "gavang_live_pipe.m3u", "gavang_live_vlc.m3u")]
            debug = Path(tmp) / "gavang_debug.json"
            with patch.object(gavang, "OUTPUT_M3U", paths[0]), \
                 patch.object(gavang, "OUTPUT_PIPE_M3U", paths[1]), \
                 patch.object(gavang, "OUTPUT_VLC_M3U", paths[2]), \
                 patch.object(gavang, "OUTPUT_DEBUG", debug):
                gavang.write_outputs([result])
            playlist = paths[0].read_text(encoding="utf-8")
            payload = __import__("json").loads(debug.read_text(encoding="utf-8"))
        self.assertNotIn("[object Object]", playlist)
        self.assertIn('tvg-logo="https://smorf.io/favicon.ico"', playlist)
        self.assertTrue(payload[0]["logo_is_fallback"])

    def test_playlist_headers_keep_origin(self):
        header = gavang.header_json("UA", MATCH_URL, "https://smorf.io")
        self.assertIn('"Origin":"https://smorf.io"', header)
        pipe = gavang.android_stream_url(
            "https://flv.lauthaitv.cc/live/test.flv",
            "UA",
            MATCH_URL,
            "https://smorf.io",
        )
        self.assertIn("Origin=https://smorf.io", pipe)
        self.assertIn("Referer=https://smorf.io/s8-live/2436/", pipe)


class _NoPageContext:
    async def new_page(self):
        raise AssertionError("Không được mở tab Chromium khi FLV dựng đã xác minh")


class _ProbeContext(_NoPageContext):
    async def cookies(self, _urls):
        return []


class GavangFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_dalian_fixture_real_probe_path_keeps_flv(self):
        match = {
            "url": DALIAN_URL,
            "raw_title": "Dalian Kewei vs Beijing Guoan",
            "raw_time": "",
            "derived_probe_only": True,
            "scan_window_reason": "unknown-time-derived-probe",
            "sport_group": "Bóng đá",
        }

        class FakeHeaders(dict):
            def items(self):
                return super().items()

        class FakeResponse:
            status = 200
            headers = FakeHeaders({"Content-Type": "video/x-flv", "X-Has-Token": "2136995574"})
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def getcode(self): return 200
            def geturl(self): return "https://flv.lauthaitv.cc/live/dalian-beijing-chnfa.flv"
            def read1(self, _size): raise TimeoutError("live stream")

        discover = AsyncMock(return_value=0)
        enrich = AsyncMock(return_value=None)
        with patch.object(gavang.urllib.request, "urlopen", side_effect=lambda *_a, **_k: FakeResponse()), \
             patch.object(gavang, "discover_http_candidates", new=discover), \
             patch.object(gavang, "enrich_verified_match_metadata", new=enrich):
            gavang.PROBE_CACHE.clear()
            result = await gavang.fetch_stream(_ProbeContext(), match, asyncio.Semaphore(1))

        discover.assert_not_awaited()
        enrich.assert_awaited_once()
        self.assertEqual(result.get("scan_decision"), "derived-flv-fast-path")
        self.assertEqual(
            result["streams"][0]["url"],
            "https://flv.lauthaitv.cc/live/dalian-beijing-chnfa.flv",
        )
        self.assertEqual(result["streams"][0]["probe"]["status"], 200)

    async def test_verified_derived_flv_skips_browser(self):
        match = {
            "url": MATCH_URL,
            "raw_title": "Marielhamn vs Lahti",
            "raw_time": "",
            "minutes_to_kickoff": 0,
            "sport_group": "Bóng đá",
        }
        observed = {}

        async def fake_finalize(_context, stream_map, _match, **_kwargs):
            observed.update(stream_map)
            entry = next(iter(stream_map.values())).copy()
            entry["playability"] = "verified"
            return [entry], []

        discover = AsyncMock(return_value=0)
        enrich = AsyncMock(return_value=None)
        with patch.object(gavang, "discover_http_candidates", new=discover), \
             patch.object(gavang, "finalize_stream_map", new=fake_finalize), \
             patch.object(gavang, "enrich_verified_match_metadata", new=enrich):
            result = await gavang.fetch_stream(
                _NoPageContext(), match, asyncio.Semaphore(1)
            )

        discover.assert_not_awaited()
        enrich.assert_awaited_once()
        self.assertEqual(result.get("scan_decision"), "derived-flv-fast-path")
        self.assertEqual(len(result.get("streams") or []), 1)
        entry = next(iter(observed.values()))
        self.assertEqual(entry["referer"], MATCH_URL)
        self.assertEqual(entry["origin"], "https://smorf.io")
        self.assertIn("derived/s8_live_stream_key", entry["sources"])

    async def test_unknown_time_probe_miss_is_kept_as_pending_without_browser(self):
        match = {
            "url": DALIAN_URL,
            "raw_title": "Dalian Kewei vs Beijing Guoan",
            "raw_time": "",
            "derived_probe_only": True,
            "scan_window_reason": "unknown-time-derived-probe",
            "sport_group": "Bóng đá",
        }
        discover = AsyncMock(return_value=99)

        async def fake_finalize(_context, _stream_map, _match, **_kwargs):
            return [], [{"url": "https://flv.lauthaitv.cc/live/dalian-beijing-chnfa.flv"}]

        enrich = AsyncMock(return_value=None)
        with patch.object(gavang, "discover_http_candidates", new=discover), \
             patch.object(gavang, "finalize_stream_map", new=fake_finalize), \
             patch.object(gavang, "enrich_verified_match_metadata", new=enrich):
            result = await gavang.fetch_stream(
                _NoPageContext(), match, asyncio.Semaphore(1)
            )

        discover.assert_not_awaited()
        enrich.assert_awaited_once()
        self.assertEqual(result.get("scan_decision"), "derived-pending-only")
        self.assertEqual(len(result.get("streams") or []), 1)
        self.assertEqual(result["streams"][0]["playability"], "upcoming-pending")
        self.assertTrue(result["streams"][0]["derived_pending"])
        self.assertEqual(
            result["streams"][0]["url"],
            "https://flv.lauthaitv.cc/live/dalian-beijing-chnfa.flv",
        )

    def test_known_fixture_outside_scan_window_is_not_kept_pending(self):
        match = {
            "url": DALIAN_URL,
            "minutes_to_kickoff": 300,
            "scan_window_reason": "too-early",
        }
        pending = gavang.build_derived_pending_streams(
            match, gavang.derived_gavang_stream_candidates(DALIAN_URL), []
        )
        self.assertEqual(pending, [])

    def test_pending_playlist_is_labeled_waiting(self):
        result = {
            "url": QUEENSLAND_URL,
            "match_name": "Queensland Lions SC VS Perth Glory",
            "time": "16:30",
            "date": "21/07",
            "sport_group": "Bóng đá",
            "streams": [{
                "url": "https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv",
                "referer": QUEENSLAND_URL,
                "origin": "https://smorf.io",
                "user_agent": gavang.UA,
                "playability": "upcoming-pending",
                "derived_pending": True,
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ("gavang_live.m3u", "gavang_live_pipe.m3u", "gavang_live_vlc.m3u")]
            debug = Path(tmp) / "gavang_debug.json"
            with patch.object(gavang, "OUTPUT_M3U", paths[0]), \
                 patch.object(gavang, "OUTPUT_PIPE_M3U", paths[1]), \
                 patch.object(gavang, "OUTPUT_VLC_M3U", paths[2]), \
                 patch.object(gavang, "OUTPUT_DEBUG", debug):
                gavang.write_outputs([result])
            text = paths[0].read_text(encoding="utf-8")
        self.assertIn("[CHỜ PHÁT] [16:30 21/07] Queensland Lions SC VS Perth Glory [FLV]", text)
        self.assertIn("queensland-perth-ausffa.flv", text)



    def test_slug_fallback_removes_league_code_and_title_cases_teams(self):
        url = "https://smorf.io/s8-live/2471/buncheon-anyang-kork1/?s8_live_stream_key=buncheon-anyang-kork1"
        self.assertEqual(gavang.clean_match_name("", url), "Bucheon FC 1995 VS FC Anyang")


    def test_slug_aliases_repair_observed_bad_team_names(self):
        cases = {
            "camw-sinw-affw": "Cambodia Women VS Singapore Women",
            "cincinati-vancouver-mls": "FC Cincinnati VS Vancouver Whitecaps",
            "lagalaxy-stlouis-mls": "LA Galaxy VS St. Louis City SC",
            "tot-mkdons-friendly": "Tottenham Hotspur VS MK Dons",
        }
        for stream_key, expected in cases.items():
            with self.subTest(stream_key=stream_key):
                url = f"https://smorf.io/s8-live/999/{stream_key}/?s8_live_stream_key={stream_key}"
                self.assertEqual(gavang.clean_match_name("", url), expected)


    def test_abbreviated_stream_key_accepts_correct_full_title(self):
        cases = [
            ("camw-sinw-affw", "Cambodia Women VS Singapore Women"),
            ("lagalaxy-stlouis-mls", "LA Galaxy VS St. Louis City SC"),
            ("tot-mkdons-friendly", "Tottenham Hotspur VS MK Dons"),
        ]
        for stream_key, title in cases:
            with self.subTest(stream_key=stream_key):
                url = f"https://smorf.io/s8-live/999/{stream_key}/?s8_live_stream_key={stream_key}"
                confidence = gavang.title_stream_key_confidence(title, url)
                self.assertFalse(confidence["contradictory"])
                self.assertGreaterEqual(confidence["match_count"], 2)

    def test_exact_fixture_metadata_precedes_weak_dom_metadata(self):
        url = "https://smorf.io/s8-live/2449/queensland-perth-ausffa/?s8_live_stream_key=queensland-perth-ausffa"
        metadata = {
            "title": "Queensland VS Perth",
            "time_text": "",
            "time_candidates": [],
            "blv": "",
            "logos": [],
            "logo_candidates": [],
        }
        exact = {
            "title": "AUS FFA Cup - Queensland Lions SC VS Perth Glory",
            "time_candidates": [{"value": "2026-07-22T16:30:00+07:00", "score": 134, "source": "exact-fixture-script/date+time"}],
            "blv": "NGƯỜI CHÈ",
            "logos": ["https://cdn.example/queensland.png"],
        }
        merged = gavang.merge_exact_fixture_script_metadata(metadata, exact, url)
        self.assertIn("Queensland Lions SC VS Perth Glory", merged["title"])
        self.assertEqual(merged["time_candidates"][0]["value"], "2026-07-22T16:30:00+07:00")
        self.assertEqual(merged["blv"], "NGƯỜI CHÈ")
        self.assertEqual(merged["logos"][0], "https://cdn.example/queensland.png")

    def test_pending_playlist_date_only_marks_missing_time_explicitly(self):
        result = {
            "url": QUEENSLAND_URL,
            "match_name": "Queensland Lions SC VS Perth Glory",
            "time": "",
            "date": "22/07",
            "sport_group": "Bóng đá",
            "streams": [{
                "url": "https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv",
                "referer": QUEENSLAND_URL,
                "origin": "https://smorf.io",
                "user_agent": gavang.UA,
                "playability": "upcoming-pending",
                "derived_pending": True,
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ("gavang_live.m3u", "gavang_live_pipe.m3u", "gavang_live_vlc.m3u")]
            debug = Path(tmp) / "gavang_debug.json"
            with patch.object(gavang, "OUTPUT_M3U", paths[0]), \
                 patch.object(gavang, "OUTPUT_PIPE_M3U", paths[1]), \
                 patch.object(gavang, "OUTPUT_VLC_M3U", paths[2]), \
                 patch.object(gavang, "OUTPUT_DEBUG", debug):
                gavang.write_outputs([result])
            text = paths[0].read_text(encoding="utf-8")
        self.assertIn("[CHỜ PHÁT] [CHƯA CÓ GIỜ 22/07] Queensland Lions SC VS Perth Glory [FLV]", text)

    def test_detail_title_date_is_moved_to_date_metadata(self):
        match = {
            "url": "https://smorf.io/s8-live/2457/ararat-shamrock-c1qual/?s8_live_stream_key=ararat-shamrock-c1qual",
            "match_name": "Ararat VS Shamrock",
        }
        metadata = {
            "title": "UEFA Champions League (qual) - Ararat-Armenia FC VS Shamrock Rovers - 22-07",
            "time_candidates": [],
            "time_text": "",
            "blv": "",
        }
        gavang.apply_basic_match_metadata(match, metadata)
        self.assertEqual(match["date"], "22/07")
        self.assertNotIn("22-07", match["match_name"])

    def test_contradictory_home_title_is_downgraded_not_dropped(self):
        row = {
            "url": QUEENSLAND_URL,
            "raw_title": "Jeju SK FC vs Gangwon Football Club",
            "match_name": "Jeju SK FC vs Gangwon Football Club",
            "raw_blv": "NGƯỜI TIỀN SỬ",
            "logo": "https://example.test/jeju.png",
            "streams": [{"url": "https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv", "playability": "verified"}],
        }
        confidence = gavang.sanitize_gavang_match_metadata(row, stage="test")
        self.assertTrue(confidence["contradictory"])
        self.assertEqual(row["match_name"], "Queensland VS Perth")
        self.assertEqual(len(row["streams"]), 1)
        self.assertEqual(row["raw_blv"], "")
        self.assertTrue(row["metadata_warnings"])

    def test_unrelated_detail_metadata_does_not_overwrite_or_remove_stream(self):
        match = {
            "url": QUEENSLAND_URL,
            "match_name": "Queensland VS Perth",
            "time": "", "date": "", "blv": "",
            "streams": [{"url": "https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv", "playability": "verified"}],
        }
        metadata = {
            "title": "Jeju SK FC vs Gangwon Football Club",
            "time_candidates": [],
            "blv": "NGƯỜI TIỀN SỬ",
        }
        gavang.apply_basic_match_metadata(match, metadata)
        self.assertEqual(match["match_name"], "Queensland VS Perth")
        self.assertEqual(match["blv"], "")
        self.assertEqual(len(match["streams"]), 1)
        self.assertTrue(match["metadata_warnings"])


if __name__ == "__main__":
    unittest.main()
