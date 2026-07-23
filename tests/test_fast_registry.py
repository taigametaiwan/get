from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sources import xoilac as xoi
from sources import colatv as cola


class FastRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xoi = xoi
        cls.cola = cola

    def test_xoilac_live2_unsigned_not_rejected_by_hostname(self):
        e=self.xoi.StreamCapture(url='https://live2.streambylivepulse.com/live/channel37.flv', player_type='7')
        e.status=200; e.probe_ok=True; e.verified=True
        self.xoi.classify_stream(e)
        self.assertEqual(e.classification, 'verified_unsigned')
        self.assertTrue(e.publishable)
        self.assertFalse(e.placeholder_suspected)

    def test_xoilac_type8_still_rejected(self):
        e=self.xoi.StreamCapture(url='https://live2.streambylivepulse.com/live/channel37.flv', player_type='8')
        e.status=200; e.probe_ok=True; e.verified=True
        self.xoi.classify_stream(e)
        self.assertEqual(e.classification, 'placeholder_or_ad')
        self.assertFalse(e.publishable)

    def test_xoilac_probe_accepts_partial_content_flv(self):
        class Response:
            status = 206
            headers = {"Content-Type": "video/x-flv"}
            def getcode(self): return 206
            def read1(self, size): return b"FLV" + b"\x00" * max(0, size - 3)
            def __enter__(self): return self
            def __exit__(self, *args): return False
        entry = self.xoi.StreamCapture(url="https://live2.example/live/channel1.flv")
        with patch.object(self.xoi.urllib.request, "urlopen", return_value=Response()):
            ok, reason = self.xoi.probe_stream_sync(entry, timeout=3)
        self.assertTrue(ok)
        self.assertIn("HTTP 206", reason)

    def test_xoilac_templates(self):
        urls=self.xoi.build_fast_channel_urls('channel37')
        self.assertIn('https://live2.streambylivepulse.com/live/channel37.flv', urls)
        self.assertTrue(all('channel37' in u for u in urls))

    def test_colatv_registry_templates(self):
        old=self.cola.REGISTRY_PATH
        with tempfile.TemporaryDirectory() as td:
            self.cola.REGISTRY_PATH=Path(td)/'registry.json'
            self.cola.learn_stream_id('BLV PEPSI', '59444581', 'test')
            urls=self.cola.registry_candidate_urls('PEPSI')
            self.assertEqual(len(urls), 3)
            self.assertTrue(all('59444581' in u for u in urls))
        self.cola.REGISTRY_PATH=old

    def test_fast_registry_is_not_used_for_far_future_match(self):
        self.assertTrue(self.xoi.fast_registry_allowed(15))
        self.assertFalse(self.xoi.fast_registry_allowed(16))
        self.assertTrue(self.cola.fast_registry_allowed({"minutes_to_kickoff": 15}))
        self.assertFalse(self.cola.fast_registry_allowed({"minutes_to_kickoff": 16}))

    def test_colatv_extract_stream_id(self):
        self.assertEqual(self.cola.extract_registry_stream_id('https://live05.meung.app/live/59444581.flv'), '59444581')
        self.assertEqual(self.cola.extract_registry_stream_id('https://other.invalid/live/59444581.flv'), '')

if __name__ == '__main__':
    unittest.main()
