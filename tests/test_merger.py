from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import merger
from merger import SourceFiles, cleanup_intermediate_playlists, merge_sources

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def write_playlist(path: Path, rows: list[dict[str, str]], pipe: bool = False) -> None:
    lines = ["#EXTM3U"]
    for row in rows:
        lines.extend([
            f'#EXTINF:-1 tvg-id="{row["id"]}" tvg-name="{row["name"]}" group-title="{row.get("group", "Bóng đá")}",{row["name"]}',
            "#EXTVLCOPT:http-referrer=https://example.test/",
            "#EXTVLCOPT:http-user-agent=UA",
            '#EXTHTTP:{"User-Agent":"UA","Referer":"https://example.test/"}',
            row["url"] + ("|User-Agent=UA&Referer=https://example.test/" if pipe else ""),
        ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class MergerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 7, 20, 7, 0, tzinfo=TZ)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_source(self, key: str, label: str, rows: list[dict[str, str]], debug_rows: list[dict]) -> SourceFiles:
        universal = self.root / f"{key}.m3u"
        pipe = self.root / f"{key}_pipe.m3u"
        vlc = self.root / f"{key}_vlc.m3u"
        debug = self.root / f"{key}.json"
        write_playlist(universal, rows)
        write_playlist(pipe, rows, pipe=True)
        write_playlist(vlc, rows)
        debug.write_text(json.dumps(debug_rows, ensure_ascii=False), encoding="utf-8")
        return SourceFiles(key, label, universal, pipe, vlc, debug)

    def test_dedupe_and_quality_cap(self) -> None:
        match_name = "USA vs Poland - Nations League"
        rows_a = [
            {"id": "cc-1", "name": f"[08:00 20/07] {match_name} [BLV A] [FHD M3U8]", "url": "https://cdn/xhd/playlist.m3u8"},
            {"id": "cc-2", "name": f"[08:00 20/07] {match_name} [BLV A] [FHD FLV]", "url": "https://cdn/xhd.flv"},
            {"id": "cc-3", "name": f"[08:00 20/07] {match_name} [BLV A] [HD M3U8]", "url": "https://cdn/x/playlist.m3u8"},
        ]
        debug_a = [{
            "match_name": match_name, "date": "20/07/2026", "time": "08:00", "blv": "A",
            "streams": [
                {"url": "https://cdn/xhd/playlist.m3u8", "quality": "FHD", "playability": "verified", "http_status": 200},
                {"url": "https://cdn/xhd.flv", "quality": "FHD", "playability": "verified", "http_status": 200},
                {"url": "https://cdn/x/playlist.m3u8", "quality": "HD", "playability": "verified", "http_status": 200},
            ],
        }]
        rows_b = [{"id": "ls-1", "name": f"[20/07/2026 08:00] {match_name} [BLV A] [FHD M3U8]", "url": "https://cdn/xhd/playlist.m3u8"}]
        debug_b = [{"match_name": match_name, "date": "20/07/2026", "time": "08:00", "blv": "A", "streams": [{"url": "https://cdn/xhd/playlist.m3u8", "quality": "FHD", "playability": "verified"}]}]
        report = merge_sources(self.root, [self.make_source("cc", "CC", rows_a, debug_a), self.make_source("ls", "LS", rows_b, debug_b)], now=self.now, max_per_match=2, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 2)
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertEqual(content.count("https://cdn/xhd/playlist.m3u8"), 1)
        self.assertIn("https://cdn/x/playlist.m3u8", content)
        self.assertNotIn("https://cdn/xhd.flv", content)
        self.assertNotIn("|User-Agent=", content)
        self.assertFalse((self.root / "all_live_pipe.m3u").exists())
        self.assertFalse((self.root / "all_live_vlc.m3u").exists())
        self.assertIn('group-title="CC"', content)
        self.assertNotIn('group-title="Bóng đá"', content)

    def test_upcoming_pending_is_excluded_in_verified_only_mode(self) -> None:
        rows = [
            {"id": "a", "name": "Soon vs Team [FHD M3U8]", "url": "https://cdn/soon/playlist.m3u8"},
            {"id": "b", "name": "Far vs Team [FHD M3U8]", "url": "https://cdn/far/playlist.m3u8"},
        ]
        soon = self.now + timedelta(hours=3)
        far = self.now + timedelta(hours=3, minutes=1)
        debug = [
            {"match_name": "Soon vs Team", "kickoff_iso": soon.isoformat(), "streams": [{"url": "https://cdn/soon/playlist.m3u8", "quality": "FHD", "playability": "upcoming-pending"}]},
            {"match_name": "Far vs Team", "kickoff_iso": far.isoformat(), "streams": [{"url": "https://cdn/far/playlist.m3u8", "quality": "FHD", "playability": "upcoming-pending"}]},
        ]
        report = merge_sources(self.root, [self.make_source("cc", "CC", rows, debug)], now=self.now, max_per_match=2, upcoming_hours=4, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 0)
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertNotIn("/soon/", content)
        self.assertNotIn("/far/", content)


    def test_gavang_unknown_time_derived_pending_is_excluded(self) -> None:
        url = "https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv"
        rows = [{"id": "gavang-2449", "name": "[CHỜ PHÁT] Queensland VS Perth [FLV]", "url": url, "group": "Bóng đá"}]
        debug = [{
            "url": "https://smorf.io/s8-live/2449/queensland-perth-ausffa/",
            "match_name": "Queensland VS Perth",
            "scan_window_reason": "unknown-time-derived-probe",
            "streams": [{
                "url": url,
                "playability": "upcoming-pending",
                "derived_pending": True,
                "pending_reason": "current-home-stream-key-no-time",
            }],
        }]
        report = merge_sources(
            self.root,
            [self.make_source("gavang", "Gà Vàng", rows, debug)],
            now=self.now,
            preserve_on_empty=False,
        )
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertEqual(report["selected_count"], 0)
        self.assertNotIn(url, content)


    def test_gavang_uses_metadata_only_debug_row_from_other_source(self) -> None:
        gv_url = "https://flv.lauthaitv.cc/live/cincinati-vancouver-mls.flv"
        gv_rows = [{"id": "gv", "name": "[CHỜ PHÁT] Cincinati VS Vancouver [FLV]", "url": gv_url, "group": "Bóng đá"}]
        gv_debug = [{
            "url": "https://smorf.io/s8-live/3000/cincinati-vancouver-mls/",
            "match_name": "Cincinati VS Vancouver",
            "streams": [{
                "url": gv_url, "playability": "verified", "http_status": 200,
            }],
        }]
        # Nguồn đối chiếu chưa có player nên playlist rỗng, nhưng debug đã có lịch/logo.
        ls_debug = [{
            "match_name": "FC Cincinnati VS Vancouver Whitecaps",
            "date": "23/07/2026", "time": "07:30",
            "logo": "https://cdn.example/cincinnati.png",
            "streams": [],
        }]
        report = merge_sources(
            self.root,
            [
                self.make_source("luongson", "Lương Sơn", [], ls_debug),
                self.make_source("gavang", "Gà Vàng", gv_rows, gv_debug),
            ],
            now=datetime(2026, 7, 23, 6, 0, tzinfo=TZ),
            preserve_on_empty=False,
        )
        text = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertIn("[07:30 23/07] FC Cincinnati VS Vancouver Whitecaps [FLV]", text)
        self.assertIn('tvg-logo="https://cdn.example/cincinnati.png"', text)
        channel = next(row for row in report["channels"] if row["source"] == "gavang")
        self.assertEqual(channel["metadata_audit"], "enriched-soft")
        self.assertEqual(channel["metadata_enriched_from"], "luongson")
        self.assertGreaterEqual(report["metadata_reference_count"], 1)

    def test_gavang_pending_date_only_is_excluded(self) -> None:
        url = "https://flv.lauthaitv.cc/live/maitland-fremantle-ausffa.flv"
        rows = [{"id": "gv", "name": "[CHỜ PHÁT] [22/07] Maitland VS Fremantle [FLV]", "url": url, "group": "Bóng đá"}]
        debug = [{
            "match_name": "Maitland VS Fremantle", "date": "22/07", "time": "",
            "scan_window_reason": "unknown-time-derived-probe",
            "streams": [{
                "url": url, "playability": "upcoming-pending", "derived_pending": True,
                "pending_reason": "current-home-stream-key-no-time",
            }],
        }]
        merge_sources(
            self.root, [self.make_source("gavang", "Gà Vàng", rows, debug)],
            now=datetime(2026, 7, 22, 12, 0, tzinfo=TZ), preserve_on_empty=False,
        )
        text = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertNotIn(url, text)

    def test_gavang_pending_started_within_150_minutes_is_still_excluded(self) -> None:
        url = "https://flv.lauthaitv.cc/live/a-b-league.flv"
        kickoff = self.now - timedelta(minutes=149)
        rows = [{"id": "gv", "name": "[CHỜ PHÁT] A VS B [FLV]", "url": url, "group": "Bóng đá"}]
        debug = [{
            "match_name": "A VS B",
            "kickoff_iso": kickoff.isoformat(),
            "streams": [{"url": url, "playability": "upcoming-pending", "derived_pending": True}],
        }]
        report = merge_sources(
            self.root, [self.make_source("gavang", "Gà Vàng", rows, debug)],
            now=self.now, preserve_on_empty=False,
        )
        self.assertEqual(report["selected_count"], 0)

    def test_gavang_pending_older_than_150_minutes_is_removed(self) -> None:
        url = "https://flv.lauthaitv.cc/live/a-b-league.flv"
        kickoff = self.now - timedelta(minutes=151)
        rows = [{"id": "gv", "name": "[CHỜ PHÁT] A VS B [FLV]", "url": url, "group": "Bóng đá"}]
        debug = [{
            "match_name": "A VS B",
            "kickoff_iso": kickoff.isoformat(),
            "streams": [{"url": url, "playability": "upcoming-pending", "derived_pending": True}],
        }]
        report = merge_sources(
            self.root, [self.make_source("gavang", "Gà Vàng", rows, debug)],
            now=self.now, preserve_on_empty=False,
        )
        self.assertEqual(report["selected_count"], 0)


    def test_gavang_fuzzy_key_match_enriches_buncheon_from_bucheon(self) -> None:
        url = "https://flv.lauthaitv.cc/live/buncheon-anyang-kork1.flv"
        gavang_rows = [{"id": "gv", "name": "[CHỜ PHÁT] Buncheon VS Anyang [FLV]", "url": url, "group": "Bóng đá"}]
        gavang_debug = [{
            "match_name": "Buncheon VS Anyang",
            "date": "22/07",
            "streams": [{"url": url, "playability": "verified", "http_status": 200}],
        }]
        kickoff = self.now + timedelta(hours=2)
        ref_url = "https://cdn.example/bucheon-anyang.m3u8"
        ref_rows = [{"id": "ls", "name": "Bucheon FC 1995 VS FC Anyang [FHD M3U8]", "url": ref_url, "group": "Bóng đá"}]
        ref_debug = [{
            "match_name": "Bucheon FC 1995 VS FC Anyang",
            "kickoff_iso": kickoff.isoformat(),
            "streams": [{"url": ref_url, "playability": "verified", "quality": "FHD"}],
        }]
        report = merge_sources(
            self.root,
            [
                self.make_source("gavang", "Gà Vàng", gavang_rows, gavang_debug),
                self.make_source("luongson", "Lương Sơn", ref_rows, ref_debug),
            ],
            now=self.now,
            preserve_on_empty=False,
        )
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertNotIn("CHỜ PHÁT", content)
        self.assertIn(kickoff.strftime("%H:%M %d/%m"), content)
        self.assertEqual(report["gavang_metadata"]["enriched"], 1)

    def test_previous_fallback_is_rejected(self) -> None:
        rows = [{"id": "a", "name": "Dead vs Link [FLV]", "url": "https://cdn/dead.flv"}]
        debug = [{"match_name": "Dead vs Link", "streams": [{"url": "https://cdn/dead.flv", "playability": "previous-fallback"}]}]
        report = merge_sources(self.root, [self.make_source("cc", "CC", rows, debug)], now=self.now, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 0)
        self.assertEqual((self.root / "all_live.m3u").read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_different_commentators_are_kept(self) -> None:
        rows = [
            {"id": "a", "name": "A vs B [BLV Một] [FHD M3U8]", "url": "https://cdn/one/playlist.m3u8"},
            {"id": "b", "name": "A vs B [BLV Hai] [FHD M3U8]", "url": "https://cdn/two/playlist.m3u8"},
        ]
        debug = [
            {"match_name": "A vs B", "blv": "Một", "streams": [{"url": "https://cdn/one/playlist.m3u8", "quality": "FHD", "playability": "verified"}]},
            {"match_name": "A vs B", "blv": "Hai", "streams": [{"url": "https://cdn/two/playlist.m3u8", "quality": "FHD", "playability": "verified"}]},
        ]
        report = merge_sources(self.root, [self.make_source("cc", "CC", rows, debug)], now=self.now, max_per_match=1, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 2)

    def test_all_live_groups_channels_by_source(self) -> None:
        cc_rows = [{"id": "cc", "name": "C vs D [FHD M3U8]", "url": "https://cdn/cc/playlist.m3u8", "group": "Bóng đá"}]
        ls_rows = [{"id": "ls", "name": "A vs B [FHD M3U8]", "url": "https://cdn/ls/playlist.m3u8", "group": "Bóng đá"}]
        gv_rows = [{"id": "gv", "name": "E vs F [FHD M3U8]", "url": "https://cdn/gv/playlist.m3u8", "group": "Bóng đá"}]
        xl_rows = [{"id": "xl", "name": "G vs H [FHD FLV]", "url": "https://cdn/xl/live.flv?wsSecret=ok", "group": "Bóng đá"}]
        cola_rows = [{"id": "cola", "name": "I vs J [FHD M3U8]", "url": "https://live05.meung.app/live/75915087.m3u8", "group": "Bóng đá"}]
        cc_debug = [{"match_name": "C vs D", "streams": [{"url": "https://cdn/cc/playlist.m3u8", "quality": "FHD", "playability": "verified"}]}]
        ls_debug = [{"match_name": "A vs B", "streams": [{"url": "https://cdn/ls/playlist.m3u8", "quality": "FHD", "playability": "verified"}]}]
        gv_debug = [{"match_name": "E vs F", "streams": [{"url": "https://cdn/gv/playlist.m3u8", "quality": "FHD", "playability": "verified"}]}]
        xl_debug = [{"match_name": "G vs H", "streams": [{"url": "https://cdn/xl/live.flv?wsSecret=ok", "quality": "FHD", "playability": "verified"}]}]
        cola_debug = [{"match_name": "I vs J", "streams": [{"url": "https://live05.meung.app/live/75915087.m3u8", "quality": "FHD", "playability": "verified"}]}]
        report = merge_sources(
            self.root,
            [
                self.make_source("chuoichien", "Chuối Chiên", cc_rows, cc_debug),
                self.make_source("luongson", "Lương Sơn", ls_rows, ls_debug),
                self.make_source("gavang", "Gà Vàng", gv_rows, gv_debug),
                self.make_source("xoilac", "Xôi Lạc", xl_rows, xl_debug),
                self.make_source("colatv", "ColaTV", cola_rows, cola_debug),
            ],
            now=self.now,
            preserve_on_empty=False,
        )
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertEqual(content.count('group-title="Chuối Chiên"'), 1)
        self.assertEqual(content.count('group-title="Lương Sơn"'), 1)
        self.assertEqual(content.count('group-title="Gà Vàng"'), 1)
        self.assertEqual(content.count('group-title="Xôi Lạc"'), 1)
        self.assertEqual(content.count('group-title="ColaTV"'), 1)
        self.assertLess(content.index('group-title="Chuối Chiên"'), content.index('group-title="Lương Sơn"'))
        self.assertLess(content.index('group-title="Lương Sơn"'), content.index('group-title="Gà Vàng"'))
        self.assertLess(content.index('group-title="Gà Vàng"'), content.index('group-title="Xôi Lạc"'))
        self.assertLess(content.index('group-title="Xôi Lạc"'), content.index('group-title="ColaTV"'))
        self.assertEqual({row["group"] for row in report["channels"]}, {"Chuối Chiên", "Lương Sơn", "Gà Vàng", "Xôi Lạc", "ColaTV"})
        self.assertEqual({row["sport_group"] for row in report["channels"]}, {"Bóng đá"})

    def test_cleanup_leaves_only_all_live_m3u(self) -> None:
        (self.root / "all_live.m3u").write_text("#EXTM3U\n", encoding="utf-8")
        for name in ("chuoichien_live.m3u", "hygenie_live.m3u", "xoilac_live.m3u", "colatv_live.m3u", "all_live_pipe.m3u", "all_live_vlc.m3u"):
            (self.root / name).write_text("#EXTM3U\n", encoding="utf-8")
        legacy = self.root / "gavang" / "gavang_live.m3u"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("#EXTM3U\n", encoding="utf-8")
        removed = cleanup_intermediate_playlists(self.root)
        self.assertEqual(sorted(removed), sorted(["chuoichien_live.m3u", "hygenie_live.m3u", "xoilac_live.m3u", "colatv_live.m3u", "all_live_pipe.m3u", "all_live_vlc.m3u", "gavang/gavang_live.m3u"]))
        self.assertEqual([path.relative_to(self.root).as_posix() for path in self.root.rglob("*.m3u")], ["all_live.m3u"])
        self.assertFalse((self.root / "gavang").exists())

    def test_gavang_metadata_is_soft_enriched_from_matching_source(self) -> None:
        gv_url = "https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv"
        gv_rows = [{"id": "gavang-2449", "name": "Queensland VS Perth [FLV]", "url": gv_url, "group": "Bóng đá"}]
        gv_debug = [{
            "url": "https://smorf.io/s8-live/2449/queensland-perth-ausffa/",
            "match_name": "Queensland VS Perth", "blv": "NGƯỜI CHÈ",
            "streams": [{"url": gv_url, "quality": "", "playability": "verified", "http_status": 200}],
        }]
        ls_url = "https://cdn.example/queensland/index.m3u8"
        ls_rows = [{"id": "ls-qld", "name": "[21/07/2026 16:30] QUEENSLAND LIONS SC vs PERTH GLORY [M3U8]", "url": ls_url, "group": "Bóng đá"}]
        ls_debug = [{
            "match_name": "QUEENSLAND LIONS SC vs PERTH GLORY",
            "date": "21/07/2026", "time": "16:30",
            "streams": [{"url": ls_url, "quality": "", "playability": "verified", "http_status": 200}],
        }]
        report = merge_sources(
            self.root,
            [
                self.make_source("luongson", "Lương Sơn", ls_rows, ls_debug),
                self.make_source("gavang", "Gà Vàng", gv_rows, gv_debug),
            ],
            now=datetime(2026, 7, 21, 15, 0, tzinfo=TZ),
            max_per_match=2, preserve_on_empty=False,
        )
        text = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertIn(gv_url, text)
        self.assertIn('[16:30 21/07] QUEENSLAND LIONS SC vs PERTH GLORY [BLV NGƯỜI CHÈ] [FLV]', text)
        gv_channel = next(row for row in report["channels"] if row["source"] == "gavang")
        self.assertEqual(gv_channel["metadata_audit"], "enriched-soft")
        self.assertEqual(gv_channel["metadata_enriched_from"], "luongson")

    def test_gavang_metadata_mismatch_warns_but_keeps_verified_link(self) -> None:
        gv_url = "https://flv.lauthaitv.cc/live/unknown-alpha-beta.flv"
        gv_rows = [{"id": "gv", "name": "Unknown VS Alpha [FLV]", "url": gv_url, "group": "Bóng đá"}]
        gv_debug = [{
            "match_name": "Unknown VS Alpha",
            "streams": [{"url": gv_url, "playability": "verified", "http_status": 200}],
        }]
        report = merge_sources(
            self.root, [self.make_source("gavang", "Gà Vàng", gv_rows, gv_debug)],
            now=self.now, preserve_on_empty=False,
        )
        text = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertIn(gv_url, text)
        self.assertEqual(report["selected_count"], 1)
        self.assertEqual(report["channels"][0]["metadata_audit"], "warn-only")


    def test_gavang_fallback_logo_is_replaced_by_matching_team_logo(self):
        from merger import M3UBlock, enrich_gavang_logos_from_other_sources
        gavang_block = M3UBlock(
            source_key="gavang", source_label="Gà Vàng",
            extinf='#EXTINF:-1 tvg-logo="https://smorf.io/favicon.ico",Queensland VS Perth [FLV]',
            lines=['#EXTINF:-1 tvg-logo="https://smorf.io/favicon.ico",Queensland VS Perth [FLV]', 'https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv'],
            url_line='https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv',
            canonical_url='https://flv.lauthaitv.cc/live/queensland-perth-ausffa.flv',
            attributes={"tvg-logo": "https://smorf.io/favicon.ico"},
            display_name="Queensland VS Perth [FLV]",
            metadata={"logo_is_fallback": True},
        )
        reference = M3UBlock(
            source_key="luongson", source_label="Lương Sơn",
            extinf='#EXTINF:-1 tvg-logo="https://cdn.example/queensland.png",QUEENSLAND LIONS SC vs PERTH GLORY [M3U8]',
            lines=['#EXTINF:-1 tvg-logo="https://cdn.example/queensland.png",QUEENSLAND LIONS SC vs PERTH GLORY [M3U8]', 'https://cdn.example/live.m3u8'],
            url_line='https://cdn.example/live.m3u8', canonical_url='https://cdn.example/live.m3u8',
            attributes={"tvg-logo": "https://cdn.example/queensland.png"},
            display_name="QUEENSLAND LIONS SC vs PERTH GLORY [M3U8]", metadata={},
        )
        stats = enrich_gavang_logos_from_other_sources([gavang_block, reference])
        self.assertEqual(gavang_block.attributes["tvg-logo"], "https://cdn.example/queensland.png")
        self.assertIn('tvg-logo="https://cdn.example/queensland.png"', gavang_block.extinf)
        self.assertEqual(stats["team_logo"], 1)

    def test_phaohoa_headers_survive_merge(self) -> None:
        source = self.make_source(
            "phaohoa",
            "Pháo Hoa TV",
            [{
                "id": "phaohoa-1",
                "name": "[08:00 20/07] Malisheva vs Hibernian [M3U8]",
                "url": "https://cdn.example.test/phaohoa/index.m3u8",
                "group": "Bóng đá",
            }],
            [{
                "url": "https://phaohoa1.live/truc-tiep/malisheva-vs-hibernian-23-07-2026-776573",
                "match_name": "Malisheva vs Hibernian",
                "time": "08:00",
                "date": "20/07",
                "streams": [{
                    "url": "https://cdn.example.test/phaohoa/index.m3u8",
                    "playability": "verified",
                    "referer": "https://phaohoa1.live/truc-tiep/malisheva-vs-hibernian-23-07-2026-776573",
                    "origin": "https://phaohoa1.live",
                }],
            }],
        )
        # Replace the generic helper directives with the source-specific headers.
        source.universal.write_text(
            '#EXTM3U\n'
            '#EXTINF:-1 tvg-id="phaohoa-1" tvg-name="Malisheva vs Hibernian" group-title="Bóng đá",Malisheva vs Hibernian [M3U8]\n'
            '#EXTVLCOPT:http-referrer=https://phaohoa1.live/truc-tiep/malisheva-vs-hibernian-23-07-2026-776573\n'
            '#EXTVLCOPT:http-origin=https://phaohoa1.live\n'
            '#EXTHTTP:{"User-Agent":"UA","Referer":"https://phaohoa1.live/truc-tiep/malisheva-vs-hibernian-23-07-2026-776573","Origin":"https://phaohoa1.live"}\n'
            'https://cdn.example.test/phaohoa/index.m3u8\n',
            encoding="utf-8",
        )
        report = merge_sources(self.root, [source], now=self.now, preserve_on_empty=False)
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertIn('group-title="Pháo Hoa TV"', content)
        self.assertIn('#EXTVLCOPT:http-origin=https://phaohoa1.live', content)
        self.assertEqual(report["channels"][0]["source"], "phaohoa")

    def test_colatv_headers_survive_merge(self) -> None:
        url = "https://live05.meung.app/live/75915087.m3u8"
        source = self.make_source(
            "colatv",
            "ColaTV",
            [{
                "id": "cola-a",
                "name": "[08:00 20/07] Bologna FC 1909 vs Heidenheimer [M3U8]",
                "url": url,
                "group": "Bóng đá",
            }],
            [{
                "match_name": "Bologna FC 1909 vs Heidenheimer",
                "time": "08:00",
                "date": "20/07/2026",
                "streams": [{"url": url, "playability": "verified"}],
            }],
        )
        source.universal.write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="cola-a" group-title="Bóng đá",[08:00 20/07] Bologna FC 1909 vs Heidenheimer [M3U8]\n'
            "#EXTVLCOPT:http-referrer=https://colatv77.live/truc-tiep/bologna/\n"
            "#EXTVLCOPT:http-origin=https://colatv77.live\n"
            '#EXTHTTP:{"User-Agent":"UA","Referer":"https://colatv77.live/truc-tiep/bologna/","Origin":"https://colatv77.live"}\n'
            f"{url}\n",
            encoding="utf-8",
        )
        report = merge_sources(self.root, [source], now=self.now, preserve_on_empty=False)
        text = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertIn('group-title="ColaTV"', text)
        self.assertIn("#EXTVLCOPT:http-referrer=https://colatv77.live/truc-tiep/bologna/", text)
        self.assertIn("#EXTVLCOPT:http-origin=https://colatv77.live", text)
        self.assertIn('"Origin":"https://colatv77.live"', text)
        self.assertIn(url, text)
        self.assertEqual(report["channels"][0]["source"], "colatv")

    def test_xoilac_headers_survive_merge(self) -> None:
        url = "https://live.example/channel.flv?wsSecret=abc&wsABSTime=1893456000"
        source = self.make_source(
            "xoilac",
            "Xôi Lạc",
            [{
                "id": "xl-a",
                "name": "[08:00 20/07] A vs B [BLV Một] [FLV]",
                "url": url,
                "group": "Bóng đá",
            }],
            [{
                "match_name": "A vs B",
                "time": "08:00",
                "date": "20/07/2026",
                "streams": [{
                    "url": url,
                    "playability": "verified",
                    "classification": "signed",
                    "has_secret": True,
                }],
            }],
        )
        source.universal.write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="xl-a" group-title="Bóng đá",[08:00 20/07] A vs B [BLV Một] [FLV]\n'
            "#EXTVLCOPT:http-referrer=https://malaysiandigest.com/truc-tiep/a/\n"
            '#EXTHTTP:{"User-Agent":"UA","Referer":"https://malaysiandigest.com/truc-tiep/a/"}\n'
            f"{url}\n",
            encoding="utf-8",
        )
        report = merge_sources(self.root, [source], now=self.now, preserve_on_empty=False)
        text = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertIn('group-title="Xôi Lạc"', text)
        self.assertIn("#EXTVLCOPT:http-referrer=https://malaysiandigest.com/truc-tiep/a/", text)
        self.assertIn("#EXTHTTP:{", text)
        self.assertIn(url, text)
        self.assertEqual(report["channels"][0]["classification"], "signed")
        self.assertTrue(report["channels"][0]["has_secret"])


    def test_phaohoa_metadata_only_placeholder_is_excluded(self) -> None:
        page_url = "https://phaohoa1.live/truc-tiep/viet-nam-vs-thai-lan"
        placeholder_url = "http://127.0.0.1:9/__phaohoa_metadata__/viet-nam-vs-thai-lan.m3u8"
        universal = self.root / "phaohoa.m3u"
        universal.write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="phaohoa-test-1" tvg-name="[08:00 20/07] Việt Nam VS Thái Lan [BLV Chim Nhỏ]" '
            'group-title="Bóng chuyền" phaohoa-entry="metadata-only" '
            f'phaohoa-page-url="{page_url}" phaohoa-home-logo="https://cdn.example/vn.png" '
            'phaohoa-away-logo="https://cdn.example/th.png" phaohoa-blv="Chim Nhỏ" '
            'tvg-logo="https://cdn.example/vn.png",[08:00 20/07] Việt Nam VS Thái Lan [BLV Chim Nhỏ]\n'
            f"{placeholder_url}\n",
            encoding="utf-8",
        )
        debug = self.root / "phaohoa.json"
        debug.write_text(json.dumps([{
            "url": page_url,
            "match_name": "Việt Nam VS Thái Lan",
            "time": "08:00",
            "date": "20/07",
            "blv": "Chim Nhỏ",
            "home_logo": "https://cdn.example/vn.png",
            "away_logo": "https://cdn.example/th.png",
            "playlist_mode": "metadata-only",
            "listed_in_playlist": True,
            "scan_attempted": False,
            "streams": [],
        }], ensure_ascii=False), encoding="utf-8")
        source = SourceFiles(
            "phaohoa", "Pháo Hoa TV", universal,
            self.root / "phaohoa_pipe.m3u", self.root / "phaohoa_vlc.m3u", debug,
        )
        report = merge_sources(self.root, [source], now=self.now, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 0)
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertNotIn(placeholder_url, content)
        self.assertNotIn(page_url, content)

    def test_phaohoa_real_stream_replaces_metadata_only_page(self) -> None:
        page_url = "https://phaohoa1.live/truc-tiep/viet-nam-vs-thai-lan"
        placeholder_url = "http://127.0.0.1:9/__phaohoa_metadata__/viet-nam-vs-thai-lan.m3u8"
        stream_url = "https://cdn.example/live/vn-th.m3u8"
        universal = self.root / "phaohoa.m3u"
        universal.write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="phaohoa-test-1" tvg-name="[08:00 20/07] Việt Nam VS Thái Lan [BLV Chim Nhỏ]" group-title="Bóng chuyền" '
            f'phaohoa-entry="metadata-only" phaohoa-page-url="{page_url}",[08:00 20/07] Việt Nam VS Thái Lan [BLV Chim Nhỏ]\n{placeholder_url}\n'
            '#EXTINF:-1 tvg-id="phaohoa-test-1" tvg-name="[08:00 20/07] Việt Nam VS Thái Lan [BLV Chim Nhỏ]" group-title="Bóng chuyền" '
            f'phaohoa-entry="stream" phaohoa-page-url="{page_url}",[08:00 20/07] Việt Nam VS Thái Lan [BLV Chim Nhỏ] [M3U8]\n{stream_url}\n',
            encoding="utf-8",
        )
        debug = self.root / "phaohoa.json"
        debug.write_text(json.dumps([{
            "url": page_url, "match_name": "Việt Nam VS Thái Lan",
            "time": "08:00", "date": "20/07", "blv": "Chim Nhỏ",
            "playlist_mode": "stream", "listed_in_playlist": True,
            "streams": [{"url": stream_url, "playability": "verified", "http_status": 200}],
        }], ensure_ascii=False), encoding="utf-8")
        source = SourceFiles(
            "phaohoa", "Pháo Hoa TV", universal,
            self.root / "phaohoa_pipe.m3u", self.root / "phaohoa_vlc.m3u", debug,
        )
        report = merge_sources(self.root, [source], now=self.now, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 1)
        self.assertEqual(report["channels"][0]["url"], stream_url)
        content = (self.root / "all_live.m3u").read_text(encoding="utf-8")
        self.assertIn(stream_url, content)
        self.assertNotIn(f"\n{placeholder_url}\n", content)
        self.assertNotIn(f"\n{page_url}\n", content)
        self.assertTrue(any(row.get("url") == placeholder_url for row in report["dropped"]))



    def test_chuoichien_stream_urls_schema_keeps_both_verified_matches(self) -> None:
        rows = [
            {
                "id": "cc-1",
                "name": "[22:00 23/07] Alashkert vs CFR Cluj - UEFA Europa Conference League [BLV Chuối Chao] [FHD FLV]",
                "url": "https://gckc0525.edgemaxcdn.org/live/chuoichao.flv",
            },
            {
                "id": "cc-2",
                "name": "[23:00 23/07] Qarabag vs CSKA Sofia - UEFA Europa League [BLV Chuối To] [FHD FLV]",
                "url": "https://gckc0525.edgemaxcdn.org/live/chuoito.flv",
            },
        ]
        debug = [
            {
                "match_name": "Alashkert vs CFR Cluj - UEFA Europa Conference League",
                "date": "23/07", "time": "22:00", "blv": "Chuối Chao",
                "playability": "verified", "quality": "FHD",
                "stream_urls": [rows[0]["url"]],
            },
            {
                "match_name": "Qarabag vs CSKA Sofia - UEFA Europa League",
                "date": "23/07", "time": "23:00", "blv": "Chuối To",
                "playability": "verified", "quality": "FHD",
                "stream_urls": [rows[1]["url"]],
            },
        ]
        report = merge_sources(
            self.root,
            [self.make_source("chuoichien", "Chuối Chiên", rows, debug)],
            now=datetime(2026, 7, 23, 23, 41, tzinfo=TZ),
            preserve_on_empty=False,
        )
        self.assertEqual(report["selected_count"], 2)
        urls = {row["url"] for row in report["channels"]}
        self.assertEqual(urls, {rows[0]["url"], rows[1]["url"]})
        self.assertTrue(report["fresh_preservation_ok"])
        source = report["sources"][0]
        self.assertEqual(source["fresh_verified_blocks"], 2)
        self.assertEqual(source["fresh_selected"], 2)
        self.assertEqual(source["unresolved"], 0)

    def test_fresh_playlist_block_survives_missing_debug_stream_index(self) -> None:
        rows = [
            {
                "id": "cc-1",
                "name": "[22:00 23/07] Alashkert vs CFR Cluj - UEFA Europa Conference League [BLV Chuối Chao] [FHD FLV]",
                "url": "https://gckc0525.edgemaxcdn.org/live/chuoichao.flv",
            },
            {
                "id": "cc-2",
                "name": "[23:00 23/07] Qarabag vs CSKA Sofia - UEFA Europa League [BLV Chuối To] [FHD FLV]",
                "url": "https://gckc0525.edgemaxcdn.org/live/chuoito.flv",
            },
        ]
        # Mô phỏng artifact thực tế: debug vẫn có card nhưng một stream không được index.
        debug = [
            {
                "match_name": "Alashkert vs CFR Cluj - UEFA Europa Conference League",
                "date": "23/07", "time": "22:00", "blv": "Chuối Chao",
                "streams": [{"url": rows[0]["url"], "playability": "verified", "quality": "FHD", "http_status": 200}],
            },
            {
                "match_name": "Qarabag vs CSKA Sofia - UEFA Europa League",
                "date": "23/07", "time": "23:00", "blv": "Chuối To",
                "streams": [],
            },
        ]
        report = merge_sources(
            self.root,
            [self.make_source("chuoichien", "Chuối Chiên", rows, debug)],
            now=datetime(2026, 7, 23, 23, 41, tzinfo=TZ),
            preserve_on_empty=False,
        )
        self.assertEqual(report["selected_count"], 2)
        qarabag = next(row for row in report["channels"] if row["url"] == rows[1]["url"])
        self.assertTrue(qarabag["verified_by_source_playlist"])
        self.assertTrue(qarabag["metadata_link_fallback"])
        self.assertEqual(qarabag["metadata_link_reason"], "missing-debug-stream-index")
        source = report["sources"][0]
        self.assertEqual(source["metadata_link_fallbacks"], 1)
        self.assertEqual(source["fresh_selected"], 2)
        self.assertTrue(source["integrity_ok"])

    def test_fresh_same_url_suppresses_recovered_last_good(self) -> None:
        url = "https://gckc0525.edgemaxcdn.org/live/chuoichao.flv"
        kickoff = datetime(2026, 7, 23, 22, 0, tzinfo=TZ)
        (self.root / "all_live.m3u").write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="old" tvg-name="Old" group-title="Chuối Chiên",Old [FHD FLV]\n'
            f"{url}\n",
            encoding="utf-8",
        )
        (self.root / "all_live_debug.json").write_text(json.dumps({
            "generated_at": datetime(2026, 7, 23, 23, 0, tzinfo=TZ).isoformat(),
            "channels": [{
                "source": "chuoichien", "url": url, "playability": "verified",
                "quality": "FHD", "kickoff_iso": kickoff.isoformat(), "match_key": "old|",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        rows = [{
            "id": "fresh", "name": "[22:00 23/07] Alashkert vs CFR Cluj [BLV Chuối Chao] [FHD FLV]", "url": url,
        }]
        debug = [{
            "match_name": "Alashkert vs CFR Cluj", "date": "23/07", "time": "22:00", "blv": "Chuối Chao",
            "streams": [{"url": url, "playability": "verified", "quality": "FHD", "http_status": 200}],
        }]
        with patch.object(merger, "_probe_previous_block", return_value=(True, "HTTP 200; flv=True")):
            report = merge_sources(
                self.root,
                [self.make_source("chuoichien", "Chuối Chiên", rows, debug)],
                now=datetime(2026, 7, 23, 23, 41, tzinfo=TZ),
                preserve_on_empty=False,
            )
        self.assertEqual(report["selected_count"], 1)
        self.assertEqual(report["last_good_recovered_count"], 0)
        self.assertEqual(report["last_good_suppressed_by_fresh_count"], 1)
        self.assertFalse(report["channels"][0]["recovered_last_good"])
        self.assertTrue(any(row.get("reason") == "fresh-source-replaces-last-good" for row in report["last_good_audit"]))

    def test_previous_stream_without_kickoff_iso_uses_extinf_time_and_is_not_recovered_outside_window(self) -> None:
        url = "https://gckc0525.edgemaxcdn.org/live/chuoichao.flv"
        (self.root / "all_live.m3u").write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="old-cc" tvg-name="[22:00 23/07] Alashkert vs CFR Cluj [BLV Chuối Chao] [FHD FLV]" '
            'group-title="Chuối Chiên",[22:00 23/07] Alashkert vs CFR Cluj [BLV Chuối Chao] [FHD FLV]\n'
            f"{url}\n",
            encoding="utf-8",
        )
        # Mô phỏng artifact thực tế gây lỗi: debug có stream verified nhưng thiếu kickoff_iso.
        (self.root / "all_live_debug.json").write_text(
            json.dumps({
                "generated_at": datetime(2026, 7, 24, 0, 20, tzinfo=TZ).isoformat(),
                "channels": [{
                    "source": "chuoichien",
                    "url": url,
                    "playability": "verified",
                    "quality": "FHD",
                    "match_key": "alashkert vs cfr cluj|chuoi chao",
                }],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        now = datetime(2026, 7, 24, 0, 53, 40, tzinfo=TZ)  # -173.67 phút
        with patch.object(merger, "_probe_previous_block", return_value=(True, "HTTP 200; flv=True")) as probe:
            report = merge_sources(self.root, [], now=now, preserve_on_empty=False)
        probe.assert_not_called()
        self.assertEqual(report["selected_count"], 0)
        self.assertEqual(report["last_good_recovered_count"], 0)
        self.assertTrue(any(row.get("reason") == "outside-source-window" for row in report["last_good_audit"]))
        self.assertEqual((self.root / "all_live.m3u").read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_previous_verified_stream_is_recovered_only_after_successful_reprobe(self) -> None:
        url = "https://cdn.example/live/a.flv"
        kickoff = self.now - timedelta(minutes=15)
        (self.root / "all_live.m3u").write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="ls-a" tvg-name="[06:45 20/07] A VS B [FLV]" '
            'group-title="Lương Sơn",[06:45 20/07] A VS B [FLV]\n'
            "#EXTVLCOPT:http-referrer=https://catbee.io/\n"
            f"{url}\n",
            encoding="utf-8",
        )
        (self.root / "all_live_debug.json").write_text(
            json.dumps({
                "generated_at": (self.now - timedelta(minutes=30)).isoformat(),
                "channels": [{
                    "source": "luongson",
                    "url": url,
                    "playability": "verified",
                    "quality": "HD",
                    "kickoff_iso": kickoff.isoformat(),
                    "match_key": "a vs b|",
                }],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        with patch.object(merger, "_probe_previous_block", return_value=(True, "HTTP 200; flv=True")) as probe:
            report = merge_sources(self.root, [], now=self.now, preserve_on_empty=False)
        probe.assert_called_once()
        self.assertEqual(report["selected_count"], 1)
        self.assertEqual(report["last_good_recovered_count"], 1)
        self.assertTrue(report["channels"][0]["recovered_last_good"])
        self.assertIn(url, (self.root / "all_live.m3u").read_text(encoding="utf-8"))

    def test_previous_verified_stream_is_dropped_when_reprobe_fails(self) -> None:
        url = "https://cdn.example/live/a.m3u8"
        kickoff = self.now + timedelta(minutes=30)
        (self.root / "all_live.m3u").write_text(
            "#EXTM3U\n"
            '#EXTINF:-1 tvg-id="cola-a" tvg-name="[07:30 20/07] A VS B [M3U8]" '
            'group-title="ColaTV",[07:30 20/07] A VS B [M3U8]\n'
            f"{url}\n",
            encoding="utf-8",
        )
        (self.root / "all_live_debug.json").write_text(
            json.dumps({
                "generated_at": (self.now - timedelta(minutes=30)).isoformat(),
                "channels": [{
                    "source": "colatv",
                    "url": url,
                    "playability": "browser-observed",
                    "quality": "HD",
                    "kickoff_iso": kickoff.isoformat(),
                }],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        with patch.object(merger, "_probe_previous_block", return_value=(False, "HTTP 403")):
            report = merge_sources(self.root, [], now=self.now, preserve_on_empty=False)
        self.assertEqual(report["selected_count"], 0)
        self.assertEqual(report["last_good_recovered_count"], 0)
        self.assertEqual((self.root / "all_live.m3u").read_text(encoding="utf-8"), "#EXTM3U\n")
        self.assertTrue(any(row.get("reason") == "HTTP 403" for row in report["last_good_audit"]))



if __name__ == "__main__":
    unittest.main()
