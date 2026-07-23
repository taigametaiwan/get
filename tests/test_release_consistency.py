from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_VERSION = "4.4.24"
BUILD_TAG = "4.4.24-XOILAC-3-CONCURRENT-150-180-FULL-CATALOG"


class ReleaseConsistencyTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_core_versions_match_release_without_manifest_dependency(self) -> None:
        self.assertIn(f'VERSION = "{BUILD_TAG}"', self.read("main.py"))
        self.assertIn(f'VERSION = "{BUILD_TAG}"', self.read("merger.py"))

    def test_release_manifest_matches_when_present(self) -> None:
        manifest_path = ROOT / "RELEASE_MANIFEST.json"
        if not manifest_path.is_file():
            # Manifest chỉ là tài liệu phát hành. Thiếu manifest không được làm dừng crawler.
            return
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["package_version"], PACKAGE_VERSION)
        self.assertEqual(manifest["build_tag"], BUILD_TAG)
        self.assertEqual(manifest["components"]["main.py"], BUILD_TAG)
        self.assertEqual(manifest["components"]["merger.py"], BUILD_TAG)

    def test_workflow_has_current_identity_and_non_cancelling_concurrency(self) -> None:
        workflow = self.read(".github/workflows/update.yml")
        self.assertTrue(workflow.startswith("name: Quet 6 nguon v4.4.24"))
        self.assertIn('git commit -m "Update live streams v4.4.24', workflow)
        self.assertIn('cron: "*/30 * * * *"', workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("PHAOHOA_PLACEHOLDER_USE_MATCH_PAGE", workflow)
        self.assertNotIn("XOILAC_MAX_MATCHES", workflow)
        self.assertIn('XOILAC_SCAN_PAST_MINUTES: "150"', workflow)
        self.assertIn('XOILAC_SCAN_FUTURE_MINUTES: "180"', workflow)
        self.assertIn('XOILAC_MATCH_CONCURRENCY: "3"', workflow)
        self.assertNotIn("v4.4.18", workflow)
        self.assertEqual(workflow.count('XOILAC_NAVIGATION_TIMEOUT: "35"'), 1)

    def test_xoilac_is_unlimited_and_403_candidates_are_not_publishable(self) -> None:
        source = self.read("sources/xoilac.py")
        self.assertIn('VERSION = "4.4.24-XOILAC-3-WORKERS-150-180-FULL-CATALOG"', source)
        self.assertIn(
            'parser.add_argument("--max-matches", type=int, default=int(os.getenv("XOILAC_MAX_MATCHES", "0")))',
            source,
        )
        self.assertIn('entry.classification = "signed_runner_blocked"', source)
        block = source[source.index('entry.classification = "signed_runner_blocked"'):]
        self.assertIn("entry.publishable = False", block[:600])
        self.assertIn('os.getenv("XOILAC_SCAN_FUTURE_MINUTES", "180")', source)
        self.assertIn('os.getenv("XOILAC_MATCH_CONCURRENCY", "3")', source)
        self.assertIn('target_page.on("request", request_callback)', source)
        self.assertNotIn('context.on("request", collector.on_request)', source)
        self.assertIn("scan_targets_concurrently", source)

    def test_phaohoa_uses_only_safe_loopback_placeholder(self) -> None:
        source = self.read("sources/phaohoa.py")
        self.assertIn('SCANNER_VERSION = "4.4.20-PHAOHOA-SAFE-PLACEHOLDER-MULTISOURCE"', source)
        self.assertIn("PLACEHOLDER_USE_MATCH_PAGE = False", source)
        self.assertIn('"http://127.0.0.1:9/__phaohoa_metadata__"', source)

    def test_all_six_source_files_exist(self) -> None:
        for relative in (
            "sources/chuoichien.py",
            "sources/luongson.py",
            "sources/gavang.py",
            "sources/xoilac.py",
            "sources/colatv.py",
            "sources/phaohoa.py",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
