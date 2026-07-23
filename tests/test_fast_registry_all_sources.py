from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sources import chuoichien, colatv, gavang, luongson, phaohoa, xoilac
from sources.fast_registry_support import identity_is_specific, normalize_key


class FastRegistryAllSourcesTests(unittest.TestCase):
    def test_generic_labels_are_not_registry_keys(self) -> None:
        for label in ("Xoilac", "Xôi Lạc TV", "Kênh 1", "Không rõ BLV", "ColaTV"):
            self.assertFalse(identity_is_specific(label), label)
        self.assertTrue(identity_is_specific("HD RIO"))
        self.assertTrue(identity_is_specific("Chuối Chao"))
        self.assertEqual(normalize_key("Tào Tháo"), "TAO THAO")

    def test_xoilac_rejects_generic_commentator_and_uses_only_live2_default(self) -> None:
        self.assertFalse(xoilac.registry_commentator_allowed("Xoilac"))
        self.assertFalse(xoilac.registry_commentator_allowed("Kênh 2"))
        self.assertTrue(xoilac.registry_commentator_allowed("HD RIO"))
        urls = xoilac.build_fast_channel_urls("channel16")
        self.assertEqual(urls, ["https://live2.streambylivepulse.com/live/channel16.flv"])
        self.assertFalse(any("pro2cdnlive" in url for url in urls))

    def test_chuoichien_builds_hls_and_flv_from_blv_slug(self) -> None:
        urls = chuoichien.build_fast_registry_urls("troctruhd")
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].endswith("/troctruhd/playlist.m3u8"))
        self.assertTrue(urls[1].endswith("/troctruhd.flv"))
        self.assertEqual(
            chuoichien.extract_fast_slug("https://live04.chuoichientv.me/live/1?blv=chuoichaohd"),
            "chuoichaohd",
        )

    def test_luongson_registry_filters_expired_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "registry.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "commentators": {
                    "KAKA": {
                        "urls": [
                            "https://hls.cdnfaster-a.live/live/KAKA/index.m3u8?expire=4102444800&sign=ok",
                            "https://hls.cdnfaster-a.live/live/KAKA/index.m3u8?expire=1000000000&sign=old",
                        ]
                    }
                },
            }), encoding="utf-8")
            with patch.object(luongson, "FAST_REGISTRY_PATH", path):
                urls = luongson.lookup_fast_registry_urls("KAKA")
            self.assertEqual(len(urls), 1)
            self.assertIn("expire=4102444800", urls[0])

    def test_phaohoa_registry_filters_expired_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "registry.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "commentators": {
                    "VAN MINH": {
                        "urls": [
                            "https://luong.phaohoa.live/live/phaohoa3/index.m3u8?expire=4102444800&sign=ok",
                            "https://luong.phaohoa.live/live/phaohoa3/index.m3u8?expire=2&sign=old",
                        ]
                    }
                },
            }), encoding="utf-8")
            with patch.object(phaohoa, "FAST_REGISTRY_PATH", path):
                urls = phaohoa.lookup_fast_registry_urls("Văn Minh")
            self.assertEqual(len(urls), 1)
            self.assertIn("expire=4102444800", urls[0])

    def test_gavang_stream_key_fast_path_prefers_hls_then_flv(self) -> None:
        url = "https://smorf.io/s8-live/test-fixture/jaiyq-khantengri-kazdiv1/?s8_live_stream_key=jaiyq-khantengri-kazdiv1"
        rows = gavang.derived_gavang_stream_candidates(url)
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(rows[-2]["url"].endswith("/jaiyq-khantengri-kazdiv1/index.m3u8"))
        self.assertTrue(rows[-1]["url"].endswith("/jaiyq-khantengri-kazdiv1.flv"))

    def test_colatv_does_not_learn_generic_source_label(self) -> None:
        self.assertFalse(colatv.registry_blv_allowed("ColaTV"))
        self.assertFalse(colatv.registry_blv_allowed("Kênh 1"))
        self.assertTrue(colatv.registry_blv_allowed("PEPSI"))


if __name__ == "__main__":
    unittest.main()
