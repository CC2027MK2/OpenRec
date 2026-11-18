"""OpenRec 纯函数与关键逻辑单测：python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openrec import config
from openrec.hotkeys import parse_combo, validate_bindings, MOD_CONTROL, MOD_ALT
from openrec.recorder import (
    _even, estimate_bitrate, Recorder, WAV_SEGMENT_BYTES,
    unique_path, unique_title,
)
from openrec.share import ShareServer, _fmt_size


class HotkeyTest(unittest.TestCase):
    def test_parse_ok(self):
        self.assertEqual(parse_combo("Ctrl+Alt+R"), (MOD_CONTROL | MOD_ALT, ord("R")))
        self.assertEqual(parse_combo("ctrl+f5"), (MOD_CONTROL, 0x6F + 5))

    def test_parse_bad(self):
        self.assertEqual(parse_combo("R"), (0, ord("R")))   # 无修饰键，由 validate 拒绝
        self.assertIsNone(parse_combo("Hyper+R"))
        self.assertIsNone(parse_combo("Ctrl"))

    def test_validate(self):
        self.assertIsNone(validate_bindings("Ctrl+Alt+R", "Ctrl+Alt+P"))
        self.assertIsNone(validate_bindings("Ctrl+Alt+R", ""))
        self.assertIsNotNone(validate_bindings("", "Ctrl+Alt+P"))
        self.assertIsNotNone(validate_bindings("R", "Ctrl+Alt+P"))
        self.assertIsNotNone(validate_bindings("Ctrl+Alt+R", "Ctrl+Alt+R"))


class RecorderHelperTest(unittest.TestCase):
    def test_even(self):
        self.assertEqual(_even(0), 2)
        self.assertEqual(_even(1), 2)
        self.assertEqual(_even(1920), 1920)
        self.assertEqual(_even(1921), 1920)

    def test_estimate_scales(self):
        base = estimate_bitrate("libx264", 23, 1920, 1080, 30, False)
        high = estimate_bitrate("libx264", 23, 1920, 1080, 60, False)
        loud = estimate_bitrate("libx264", 23, 1920, 1080, 30, True)
        self.assertAlmostEqual(high, base * 2, places=3)
        self.assertGreater(loud, base)

    def test_mix_length_and_clip(self):
        import numpy as np
        a = np.full((480, 1), 0.2, dtype=np.float32)
        b = np.full((240, 1), -0.1, dtype=np.float32)
        out = Recorder._mix(a, b)
        self.assertEqual(out.shape, (240, 2))     # 取较短长度，单声道补成双声道
        self.assertEqual(int(out[0, 0]), int(0.1 * 32767))
        loud = Recorder._mix(np.full((10, 1), 0.9), np.full((10, 1), 0.9))
        self.assertTrue((loud <= 32767).all())     # 削顶保护
        self.assertIsNone(Recorder._mix(None, None))

    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a_clip.mp4")
            self.assertEqual(unique_path(p), p)
            open(p, "wb").close()
            second = os.path.join(d, "a_clip_2.mp4")
            self.assertEqual(unique_path(p), second)
            open(second, "wb").close()
            self.assertEqual(unique_path(p), os.path.join(d, "a_clip_3.mp4"))

    def test_unique_title(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(unique_title(d, "录制_1"), "录制_1")
            for ext in (".mp4", ".mkv", ".wav", ".m4a"):
                open(os.path.join(d, "录制_1" + ext), "wb").close()
            self.assertEqual(unique_title(d, "录制_1"), "录制_1_2")

    def test_wav_segment_below_limit(self):
        self.assertLess(WAV_SEGMENT_BYTES, 4 * 1024 ** 3)

    def test_audio_part_naming(self):
        import threading
        rec = Recorder.__new__(Recorder)
        rec._out_dir = tempfile.mkdtemp()
        rec._title = "unit"
        rec._audio_parts = [os.path.join(rec._out_dir, ".unit.wav")]
        rec._lock = threading.Lock()
        p2 = rec._next_audio_part()
        p3 = rec._next_audio_part()
        self.assertTrue(p2.endswith(".unit.part2.wav"))
        self.assertTrue(p3.endswith(".unit.part3.wav"))


class ShareTest(unittest.TestCase):
    def test_token_length(self):
        srv = ShareServer(tempfile.gettempdir())
        self.assertEqual(len(srv.token), 8)
        self.assertTrue(srv.token.isalnum())

    def test_lockout(self):
        srv = ShareServer(tempfile.gettempdir())
        ip = "192.0.2.10"
        for _ in range(srv.MAX_FAILS):
            srv.note_failure(ip)
        self.assertTrue(srv.is_locked(ip))
        srv.clear_failures(ip)
        self.assertFalse(srv.is_locked(ip))

    def test_fmt_size(self):
        self.assertEqual(_fmt_size(512), "512B")
        self.assertEqual(_fmt_size(1536), "1.5KB")

    def test_non_ascii_token(self):
        """非 ASCII 口令不能抛异常，且必须计入失败次数/触发锁定。"""
        srv = ShareServer(tempfile.gettempdir())
        ip = "192.0.2.30"
        self.assertFalse(srv.check_token(ip, "中文口令"))
        self.assertEqual(srv._fails.get(ip), 1)
        for _ in range(srv.MAX_FAILS - 1):
            self.assertFalse(srv.check_token(ip, "中文口令"))
        self.assertTrue(srv.is_locked(ip))
        self.assertFalse(srv.check_token(ip, srv.token))   # 锁定期间正确口令也拒绝
        srv.clear_failures(ip)
        self.assertTrue(srv.check_token(ip, srv.token))

    def test_fail_entries_expire(self):
        srv = ShareServer(tempfile.gettempdir())
        srv._fails["10.0.0.9"] = 2
        srv._fail_ts["10.0.0.9"] = time.monotonic() - srv.FAIL_MEMORY_SECONDS - 1
        srv.note_failure("10.0.0.8")
        self.assertNotIn("10.0.0.9", srv._fails)
        self.assertIn("10.0.0.8", srv._fails)


class ShareHttpTest(unittest.TestCase):
    """分享 HTTP 层集成测试：锁定、白名单、路径穿越、Range。"""

    @classmethod
    def setUpClass(cls):
        import http.client
        from openrec.share import lan_ip
        cls.root = tempfile.mkdtemp(prefix="sharetest_")
        with open(os.path.join(cls.root, "ok.mp4"), "wb") as f:
            f.write(b"V" * 4096)
        with open(os.path.join(cls.root, "secret.txt"), "wb") as f:
            f.write(b"SECRET")
        with open(os.path.join(cls.root, ".hidden.mp4"), "wb") as f:
            f.write(b"HIDDEN")
        cls.srv = ShareServer(cls.root)
        cls.srv.start(19080)
        cls.host = lan_ip()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _get(self, path, headers=None):
        import http.client
        conn = http.client.HTTPConnection(self.host, self.srv.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_no_token_403(self):
        self.assertEqual(self._get("/")[0], 403)

    def test_non_ascii_token_403(self):
        self.assertEqual(self._get("/?k=%E4%B8%AD%E6%96%87")[0], 403)

    def test_download_extension_whitelist(self):
        self.assertEqual(self._get(f"/f/secret.txt?k={self.srv.token}")[0], 404)

    def test_hidden_file_404(self):
        """以 . 开头的隐藏文件不在列表展示，也不允许凭口令直接下载。"""
        self.assertEqual(self._get(f"/f/.hidden.mp4?k={self.srv.token}")[0], 404)

    def test_download_range_ok(self):
        code, body = self._get(f"/f/ok.mp4?k={self.srv.token}",
                               {"Range": "bytes=0-99"})
        self.assertEqual(code, 206)
        self.assertEqual(len(body), 100)

    def test_huge_range_416(self):
        code, _ = self._get(f"/f/ok.mp4?k={self.srv.token}",
                            {"Range": "bytes=" + "9" * 5000 + "-"})
        self.assertEqual(code, 416)

    def test_path_traversal_400(self):
        self.assertEqual(self._get(f"/f/..%2fsecret.txt?k={self.srv.token}")[0], 400)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(config, "DATA_DIR", self.tmp.name),
            mock.patch.object(config, "DEFAULT_OUTPUT_DIR", self.tmp.name),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_roundtrip(self):
        s = config.Settings(fps=45, codec="h264_nvenc", output_dir=self.tmp.name)
        s.save()
        self.assertFalse(os.path.exists(config.Settings.path() + ".tmp"))
        loaded = config.Settings.load()
        self.assertEqual(loaded.fps, 45)
        self.assertEqual(loaded.codec, "h264_nvenc")

    def test_corrupt_backup(self):
        path = config.Settings.path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        loaded = config.Settings.load()
        self.assertEqual(loaded.fps, 30)              # 回退默认值
        self.assertTrue(config.Settings.load_warning)
        self.assertTrue(os.path.exists(path + ".broken"))

    def test_unknown_keys_ignored(self):
        path = config.Settings.path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fps": 24, "legacy_option": 1}, f)
        self.assertEqual(config.Settings.load().fps, 24)

    def test_invalid_types_fall_back(self):
        path = config.Settings.path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fps": "abc", "codec": "evil", "region": ["a"],
                       "watermark_text": "x" * 200,
                       "audio_only_format": "mp3", "share_port": 99999}, f)
        s = config.Settings.load()
        self.assertEqual(s.fps, 30)                 # 类型错误回退默认
        self.assertEqual(s.codec, "libx264")        # 非白名单回退默认
        self.assertEqual(s.region, [0, 0, 0, 0])
        self.assertEqual(len(s.watermark_text), 64)  # 长度上限
        self.assertEqual(s.audio_only_format, "m4a")
        self.assertEqual(s.share_port, 65535)       # 越界被夹到上限


if __name__ == "__main__":
    unittest.main()
