# -*- coding: utf-8 -*-
"""图形界面的冒烟与流程测试（需要桌面环境，无桌面时自动跳过）。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / ".tmp_tests"
TMP_ROOT.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))

import mp4_to_mp3 as m  # noqa: E402


def make_temp_dir(prefix: str) -> Path:
    """使用默认目录权限，避开 tempfile 的 0o700 目录在沙箱中不可写的问题。"""
    path = TMP_ROOT / f"{prefix}{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return path


@unittest.skipUnless(m.tk is not None, "tkinter 不可用")
class TestGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg, _ = m.ensure_tools()
        except m.ToolMissing as exc:
            raise unittest.SkipTest(str(exc))
        cls.dir = make_temp_dir("gui_")
        cls.video = cls.dir / "gui.mp4"
        cmd = [
            cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.video),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        try:
            self.root = m.tk.Tk()
        except Exception as exc:  # 无显示环境
            self.skipTest(f"无法创建 Tk 窗口：{exc}")
        self.root.withdraw()
        self.app = m.App(self.root, self.ffmpeg)

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _pump_until_done(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while self.app.running and time.time() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.root.update()

    def _add(self, *paths: Path) -> None:
        with mock.patch.object(
            m.filedialog, "askopenfilenames", return_value=tuple(str(p) for p in paths)
        ):
            self.app.add_files()

    def test_add_convert_and_status(self):
        dst = self.video.with_suffix(".mp3")
        dst.unlink(missing_ok=True)
        self._add(self.video)
        self.assertEqual(len(self.app.items), 1)

        self.app.start()
        self._pump_until_done()

        self.assertFalse(self.app.running)
        self.assertTrue(dst.is_file())
        iid = next(iter(self.app.items))
        self.assertEqual(self.app.tree.item(iid, "values")[1], "完成")
        self.assertEqual(self.app.status.get(), "成功 1，跳过 0，失败 0")
        self.assertEqual(float(self.app.progress["value"]), 100.0)

    def test_custom_outdir(self):
        src = self.dir / "custom-src.mp4"
        shutil.copyfile(self.video, src)
        outdir = self.dir / "custom-out"
        self.app.out_mode.set("custom")
        self.app.out_dir.set(str(outdir))
        self.app._sync_outdir()
        self.assertEqual(self.app.row2.winfo_manager(), "pack")
        self.assertEqual(str(self.app.entry_out.cget("state")), "normal")

        self._add(src)
        self.app.start()
        self._pump_until_done()
        self.assertTrue((outdir / "custom-src.mp3").is_file())

    def test_busy_controls(self):
        self.app._set_busy(True)
        self.assertTrue(self.app.running)
        self.assertTrue(self.app.btn_add.instate(["disabled"]))
        self.assertTrue(self.app.btn_stop.instate(["!disabled"]))
        self.assertEqual(str(self.app.combo_bitrate.cget("state")), "disabled")

        self.app._set_busy(False)
        self.assertFalse(self.app.running)
        self.assertTrue(self.app.btn_add.instate(["!disabled"]))
        self.assertTrue(self.app.btn_start.instate(["!disabled"]))
        self.assertEqual(str(self.app.combo_bitrate.cget("state")), "readonly")

    def test_add_duplicate_remove_clear(self):
        other = self.dir / "other.mp4"
        shutil.copyfile(self.video, other)
        self._add(self.video, self.video, other)
        self.assertEqual(len(self.app.items), 2)

        first = next(iter(self.app.items))
        self.app.tree.selection_set(first)
        self.app.remove_selected()
        self.assertEqual(len(self.app.items), 1)

        self.app.clear_all()
        self.assertEqual(len(self.app.items), 0)
        self.assertEqual(self.app.count.get(), "共 0 个文件")


if __name__ == "__main__":
    unittest.main()
