from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseConsistencyTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_release_manifest_and_core_versions_match(self) -> None:
        manifest = json.loads(self.read("RELEASE_MANIFEST.json"))
        self.assertEqual(manifest["package_version"], "4.4.21")
        self.assertIn('VERSION = "4.4.21-RELEASE-CONSISTENCY-GUARD"', self.read("main.py"))
        self.assertIn('VERSION = "4.4.21-RELEASE-CONSISTENCY-GUARD"', self.read("merger.py"))

    def test_workflow_has_current_identity_and_non_cancelling_concurrency(self) -> None:
        workflow = self.read(".github/workflows/update.yml")
        self.assertTrue(workflow.startswith("name: Quet 6 nguon v4.4.21"))
        self.assertIn('git commit -m "Update live streams v4.4.21', workflow)
        self.assertIn('cron: "*/30 * * * *"', workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("PHAOHOA_PLACEHOLDER_USE_MATCH_PAGE", workflow)
        self.assertNotIn("XOILAC_MAX_MATCHES", workflow)
        self.assertNotIn("v4.4.18", workflow)

    def test_xoilac_is_unlimited_and_403_candidates_are_not_publishable(self) -> None:
        source = self.read("sources/xoilac.py")
        self.assertIn('VERSION = "4.4.16-XOILAC-UNLIMITED-LIVE-PRIORITY-403-RETRY"', source)
        self.assertIn(
            'parser.add_argument("--max-matches", type=int, default=int(os.getenv("XOILAC_MAX_MATCHES", "0")))',
            source,
        )
        self.assertIn('entry.classification = "signed_runner_blocked"', source)
        block = source[source.index('entry.classification = "signed_runner_blocked"'):]
        self.assertIn("entry.publishable = False", block[:600])

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
