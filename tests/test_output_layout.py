from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main as orchestrator
from sources import chuoichien, colatv, gavang, luongson, xoilac


class OutputLayoutTests(unittest.TestCase):
    def test_source_configs_keep_old_filenames_as_root_temporary_files(self) -> None:
        expected = {
            "chuoichien": ("chuoichien_live.m3u", "chuoichien_live_pipe.m3u", "chuoichien_live_vlc.m3u"),
            "luongson": ("hygenie_live.m3u", "hygenie_live_pipe.m3u", "hygenie_live_vlc.m3u"),
            "gavang": ("gavang_live.m3u", "gavang_live_pipe.m3u", "gavang_live_vlc.m3u"),
            "xoilac": ("xoilac_live.m3u", "xoilac_live_pipe.m3u", "xoilac_live_vlc.m3u"),
            "colatv": ("colatv_live.m3u", "colatv_live_pipe.m3u", "colatv_live_vlc.m3u"),
        }
        for key, (universal, pipe, vlc) in expected.items():
            config = orchestrator.SOURCES[key]
            self.assertEqual(config.universal.parent, orchestrator.ROOT)
            self.assertEqual(config.universal.name, universal)
            self.assertEqual(config.pipe.name, pipe)
            self.assertEqual(config.vlc.name, vlc)

    def test_ensure_source_playlists_creates_three_empty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = orchestrator.SourceConfig(
                key="demo",
                label="Demo",
                script=root / "demo.py",
                universal=root / "demo" / "old_name.m3u",
                pipe=root / "demo" / "old_name_pipe.m3u",
                vlc=root / "demo" / "old_name_vlc.m3u",
                debug=root / "demo.json",
                host_markers=("demo.test",),
            )
            orchestrator.ensure_source_playlists(config)
            for path in orchestrator.source_playlist_paths(config):
                self.assertEqual(path.read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_force_empty_replaces_stale_source_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = orchestrator.SourceConfig(
                key="demo",
                label="Demo",
                script=root / "demo.py",
                universal=root / "demo" / "old_name.m3u",
                pipe=root / "demo" / "old_name_pipe.m3u",
                vlc=root / "demo" / "old_name_vlc.m3u",
                debug=root / "demo.json",
                host_markers=("demo.test",),
            )
            orchestrator.ensure_source_playlists(config)
            config.universal.write_text("#EXTM3U\nhttps://stale.test/live.m3u8\n", encoding="utf-8")
            orchestrator.ensure_source_playlists(config, force_empty=True)
            self.assertEqual(config.universal.read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_source_writers_create_configured_temporary_files(self) -> None:
        modules = (chuoichien, luongson, gavang, xoilac, colatv)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for module in modules:
                with self.subTest(module=module.__name__):
                    source_name = module.__name__.rsplit(".", 1)[-1]
                    universal = root / Path(module.OUTPUT_M3U).name
                    pipe_path = root / Path(module.OUTPUT_PIPE_M3U).name
                    vlc_path = root / Path(module.OUTPUT_VLC_M3U).name
                    debug = root / f"{source_name}.json"
                    with patch.object(module, "OUTPUT_M3U", universal), \
                         patch.object(module, "OUTPUT_PIPE_M3U", pipe_path), \
                         patch.object(module, "OUTPUT_VLC_M3U", vlc_path), \
                         patch.object(module, "OUTPUT_DEBUG", debug):
                        module.write_outputs([])
                    for path in (universal, pipe_path, vlc_path):
                        self.assertEqual(path.read_text(encoding="utf-8"), "#EXTM3U\n")

    def test_git_history_lookup_uses_repository_relative_playlist_path(self) -> None:
        calls = []

        class Completed:
            returncode = 1
            stdout = ""

        def fake_run(command, **_kwargs):
            calls.append(command)
            return Completed()

        with patch.object(gavang.subprocess, "run", side_effect=fake_run):
            gavang.load_previous_playlist_streams(gavang.OUTPUT_M3U)

        flattened = [item for command in calls for item in command]
        self.assertIn("HEAD~1:gavang_live.m3u", flattened)
        self.assertIn("HEAD~1:gavang/gavang_live.m3u", flattened)
        self.assertIn("HEAD~2:gavang_live.m3u", flattened)
        self.assertIn("HEAD~2:gavang/gavang_live.m3u", flattened)


if __name__ == "__main__":
    unittest.main()
