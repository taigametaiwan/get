import unittest
from unittest.mock import AsyncMock, patch

import main
from sources import chuoichien, luongson, xoilac


class DomainFailoverTests(unittest.TestCase):
    def test_new_domains_are_routed(self) -> None:
        routed = main.route_urls([
            "https://live04.chuoichientv.me/live/123/test-vs-test",
            "https://catbee.io/truc-tiep/a-vs-b",
            "https://malaysiandigest.com/truc-tiep/a-vs-b-luc-1200-ngay-22-07-2026/",
        ])
        self.assertEqual(len(routed["chuoichien"]), 1)
        self.assertEqual(len(routed["luongson"]), 1)
        self.assertEqual(len(routed["xoilac"]), 1)

    def test_default_domain_priority(self) -> None:
        self.assertEqual(chuoichien.HOME_URLS[0], "https://live04.chuoichientv.me/")
        self.assertIn("https://live03.chuoichientv.me/", chuoichien.HOME_URLS)
        self.assertEqual(luongson.HOME_URLS[0], "https://catbee.io/")
        self.assertIn("https://hygenie.io/", luongson.HOME_URLS)
        self.assertEqual(xoilac.HOME_URLS[0], "https://xoilacz.io/")
        self.assertIn("https://malaysiandigest.com/", xoilac.HOME_URLS)


class AdaptiveScanTests(unittest.TestCase):
    def test_chuoichien_wait_policy(self) -> None:
        self.assertEqual(
            chuoichien.effective_stream_wait_seconds({"minutes_to_kickoff": 100}),
            min(chuoichien.STREAM_WAIT_SECONDS, chuoichien.UPCOMING_FAR_WAIT_SECONDS),
        )
        self.assertEqual(
            chuoichien.effective_stream_wait_seconds({"minutes_to_kickoff": 20}),
            min(chuoichien.STREAM_WAIT_SECONDS, chuoichien.UPCOMING_NEAR_WAIT_SECONDS),
        )
        self.assertEqual(
            chuoichien.effective_stream_wait_seconds({"minutes_to_kickoff": -10}),
            chuoichien.STREAM_WAIT_SECONDS,
        )

    def test_luongson_quality_click_policy(self) -> None:
        self.assertFalse(
            luongson.should_probe_quality_buttons(
                {"minutes_to_kickoff": luongson.UPCOMING_FAR_THRESHOLD_MINUTES + 1},
                has_candidate=False,
            )
        )
        self.assertTrue(
            luongson.should_probe_quality_buttons(
                {"minutes_to_kickoff": luongson.UPCOMING_FAR_THRESHOLD_MINUTES + 1},
                has_candidate=True,
            )
        )
        self.assertTrue(
            luongson.should_probe_quality_buttons({"minutes_to_kickoff": 0}, False)
        )


class AsyncFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_chuoichien_uses_second_domain_when_first_is_empty(self) -> None:
        with patch.object(
            chuoichien,
            "collect_home_links",
            new=AsyncMock(side_effect=[[], [{"url": "https://live03.chuoichientv.me/live/1/a-vs-b"}]]),
        ) as mocked:
            links = await chuoichien.collect_home_links_with_failover(object())
        self.assertEqual(len(links), 1)
        self.assertEqual(mocked.await_count, 2)

    async def test_luongson_uses_second_domain_when_first_is_not_football(self) -> None:
        with patch.object(
            luongson,
            "collect_home_links",
            new=AsyncMock(side_effect=[[], [{"url": "https://catbee.io/truc-tiep/a-vs-b"}]]),
        ) as mocked:
            links = await luongson.collect_home_links_with_failover(object())
        self.assertEqual(len(links), 1)
        self.assertEqual(mocked.await_count, 2)


class LuongSonStreamFailoverTests(unittest.TestCase):
    def test_duplicate_cards_are_grouped_and_variants_are_preserved(self) -> None:
        rows = [
            {
                "url": "https://catbee.io/truc-tiep/toluca-vs-pumas-unam/?blv=1",
                "raw_title": "Trực tiếp trận đấu Toluca vs Pumas U.N.A.M.",
                "card_text": "Đang diễn ra",
            },
            {
                "url": "https://catbee.io/truc-tiep/toluca-vs-pumas-unam/?blv=2",
                "raw_title": "toluca vs pumas unam",
                "card_text": "LIVE NOW",
            },
            {
                "url": "https://catbee.io/truc-tiep/tauranga-whai-vs-otago-nuggets/",
                "raw_title": "Tauranga Whai vs Otago Nuggets",
            },
        ]
        deduped = luongson.dedupe_home_matches(rows)
        self.assertEqual(len(deduped), 2)
        toluca = next(row for row in deduped if "toluca" in row["url"])
        self.assertEqual(toluca["_duplicate_card_count"], 2)
        self.assertEqual(len(toluca["_same_match_variants"]), 1)
        self.assertEqual(
            luongson.semantic_match_key(rows[0]),
            luongson.semantic_match_key(rows[1]),
        )

    def test_same_teams_different_kickoff_are_not_collapsed(self) -> None:
        rows = [
            {
                "url": "https://catbee.io/truc-tiep/a-vs-b-vao-luc-1000-22-07-2026/",
                "raw_title": "A vs B",
                "raw_time": "10:00",
                "date": "22/07/2026",
            },
            {
                "url": "https://catbee.io/truc-tiep/a-vs-b-vao-luc-1800-23-07-2026/",
                "raw_title": "A vs B",
                "raw_time": "18:00",
                "date": "23/07/2026",
            },
        ]
        self.assertEqual(len(luongson.dedupe_home_matches(rows)), 2)

    def test_far_upcoming_match_does_not_trigger_domain_failover(self) -> None:
        self.assertFalse(
            luongson.stream_failover_eligible(
                {"streams": [], "minutes_to_kickoff": luongson.DOMAIN_FAILOVER_NEAR_MINUTES + 1}
            )
        )
        self.assertTrue(
            luongson.stream_failover_eligible(
                {"streams": [], "minutes_to_kickoff": -10}
            )
        )


class LuongSonAsyncStreamFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_match_variant_success_keeps_primary_identity(self) -> None:
        primary_url = "https://catbee.io/truc-tiep/toluca-vs-pumas-unam/?blv=1"
        alternate_url = "https://catbee.io/truc-tiep/toluca-vs-pumas-unam/?blv=2"
        primary = {
            "url": primary_url,
            "raw_title": "Toluca vs Pumas UNAM",
            "minutes_to_kickoff": -5,
            "streams": [],
            "_fallback_target_index": 4,
            "_same_match_variants": [{
                "url": alternate_url,
                "raw_title": "Toluca vs Pumas U.N.A.M.",
            }],
        }

        async def fake_fetch(context, match, sem):
            row = dict(match)
            if row["url"] == alternate_url:
                row["streams"] = [{
                    "url": "https://cdn.example/toluca.flv",
                    "playability": "verified",
                }]
            else:
                row["streams"] = []
            row["match_name"] = "Toluca vs Pumas UNAM"
            return row

        with patch.object(luongson, "fetch_stream", new=fake_fetch):
            result = await luongson.fetch_stream_with_variants(
                object(), primary, __import__("asyncio").Semaphore(1)
            )

        self.assertEqual(result["url"], primary_url)
        self.assertEqual(result["selected_page_url"], alternate_url)
        self.assertEqual(result["_fallback_target_index"], 4)
        self.assertEqual(len(result["streams"]), 1)

    async def test_zero_stream_near_live_retries_matching_second_domain(self) -> None:
        primary_url = "https://catbee.io/truc-tiep/toluca-vs-pumas-unam/"
        fallback_url = "https://hygenie.io/truc-tiep/toluca-vs-pumas-unam/"
        results = [{
            "url": primary_url,
            "raw_title": "Toluca vs Pumas U.N.A.M.",
            "match_name": "Toluca vs Pumas U.N.A.M.",
            "card_text": "Đang diễn ra",
            "scan_window_reason": "unknown-time-live",
            "streams": [],
            "sport_group": "Bóng đá",
        }]
        fallback_cards = [{
            "url": fallback_url,
            "raw_title": "toluca vs pumas unam",
            "card_text": "LIVE NOW",
            "sport_group": "Bóng đá",
            "source_home_url": "https://hygenie.io/",
        }]

        async def fake_scan_batch(context, links, *, phase_label=""):
            row = dict(links[0])
            row["match_name"] = "Toluca vs Pumas UNAM"
            row["streams"] = [{
                "url": "https://cdn.example/toluca/index.m3u8",
                "playability": "verified",
            }]
            return [row]

        with patch.object(
            luongson,
            "HOME_URLS",
            ("https://catbee.io/", "https://hygenie.io/"),
        ), patch.object(
            luongson,
            "collect_home_links",
            new=AsyncMock(return_value=fallback_cards),
        ) as collect_mock, patch.object(
            luongson,
            "scan_match_batch",
            new=fake_scan_batch,
        ):
            updated = await luongson.retry_failed_matches_on_fallback_domains(
                object(), results, "https://catbee.io/"
            )

        self.assertEqual(collect_mock.await_count, 1)
        self.assertEqual(len(updated[0]["streams"]), 1)
        self.assertEqual(updated[0]["url"], primary_url)
        self.assertEqual(updated[0]["selected_page_url"], fallback_url)
        self.assertEqual(
            updated[0]["domain_failover"]["to_domain"],
            "https://hygenie.io/",
        )


if __name__ == "__main__":
    unittest.main()
