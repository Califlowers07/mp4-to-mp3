# -*- coding: utf-8 -*-
"""核心、发现规则与 CLI 的自动化测试。"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / ".tmp_tests"
TMP_ROOT.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))

import mp4_to_mp3 as m  # noqa: E402


def make_temp_dir(prefix: str) -> Path:
    """在测试专用目录里建临时目录。

    不使用 tempfile.mkdtemp：它在 Windows 上以 0o700 建目录，
    在当前文件沙箱下子进程无法写入。
    """
    path = TMP_ROOT / f"{prefix}{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return path


class TestHelpers(unittest.TestCase):
    def test_normalize_bitrate(self):
        self.assertEqual(m.normalize_bitrate("192"), "192k")
        self.assertEqual(m.normalize_bitrate("192k"), "192k")
        self.assertEqual(m.normalize_bitrate(" 320K "), "320k")
        for value in ("31", "321", "abc", "192.5", ""):
            with self.assertRaises(ValueError):
                m.normalize_bitrate(value)

    def test_parse_ff_time(self):
        self.assertEqual(m.parse_ff_time("00:00:01.500000"), 1.5)
        self.assertEqual(m.parse_ff_time("01:02:03.25"), 3723.25)
        self.assertIsNone(m.parse_ff_time("bad"))
        self.assertIsNone(m.parse_ff_time("1:2:3"))

    def test_human_size(self):
        self.assertEqual(m.human_size(0), "0 B")
        self.assertEqual(m.human_size(1023), "1023 B")
        self.assertEqual(m.human_size(1024), "1.0 KB")
        self.assertEqual(m.human_size(1024 * 1024), "1.0 MB")

    def test_options_reject_negative_track(self):
        with self.assertRaises(ValueError):
            m.Options(track=-1)

    def test_ffmpeg_command(self):
        cmd = m._ffmpeg_cmd(
            "ffmpeg.exe", Path("in.mp4"), Path("out.mp3"),
            m.Options(bitrate="320k", track=1),
        )
        self.assertIn("0:a:1", cmd)
        self.assertIn("320k", cmd)
        self.assertEqual(cmd[-1], "out.mp3")

    def test_summary(self):
        summary = m.Summary()
        for status in (m.DONE, m.SKIPPED, m.FAILED, m.CANCELLED):
            summary.add(status)
        self.assertEqual((summary.done, summary.skipped, summary.failed, summary.cancelled), (1, 1, 1, 1))
        self.assertIn("取消 1", summary.text())
        self.assertEqual(summary.exit_code, 1)


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.dir = make_temp_dir("discovery_")
        (self.dir / "a.mp4").write_bytes(b"x")
        (self.dir / "b.MP4").write_bytes(b"x")
        (self.dir / "note.txt").write_text("x")
        sub = self.dir / "sub"
        sub.mkdir()
        (sub / "c.mkv").write_bytes(b"x")
        (sub / "d.txt").write_text("x")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_collect_non_recursive(self):
        files, errors = m.collect_inputs([str(self.dir)])
        self.assertEqual({p.name for p in files}, {"a.mp4", "b.MP4"})
        self.assertEqual(errors, [])

    def test_collect_recursive(self):
        files, _ = m.collect_inputs([str(self.dir)], recursive=True)
        self.assertEqual({p.name for p in files}, {"a.mp4", "b.MP4", "c.mkv"})

    def test_collect_file_duplicate_and_errors(self):
        a = self.dir / "a.mp4"
        files, errors = m.collect_inputs([str(a), str(a), str(self.dir / "note.txt")])
        self.assertEqual(len(files), 1)
        self.assertEqual(len(errors), 1)

    def test_collect_missing(self):
        files, errors = m.collect_inputs([str(self.dir / "missing.mp4")])
        self.assertEqual(files, [])
        self.assertEqual(len(errors), 1)

    def test_plan_output_collisions(self):
        used: set[str] = set()
        src = Path("videos") / "同名.mp4"
        out = Path("out")
        first = m.plan_output(src, out, used)
        second = m.plan_output(src, out, used)
        third = m.plan_output(src, out, used)
        self.assertEqual(first.name, "同名.mp3")
        self.assertEqual(second.name, "同名 (2).mp3")
        self.assertEqual(third.name, "同名 (3).mp3")

    def test_plan_jobs_uses_outdir(self):
        jobs = m.plan_jobs([self.dir / "a.mp4"], self.dir / "out")
        self.assertEqual(jobs[0].dst, self.dir / "out" / "a.mp3")


class TestMedia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffmpeg, cls.ffprobe = m.ensure_tools()
        except m.ToolMissing as exc:
            raise unittest.SkipTest(str(exc))

        cls.dir = make_temp_dir("media_")
        cls.tone = cls.dir / "tone.mp4"
        cls.silent = cls.dir / "silent.mp4"
        cls.multi = cls.dir / "multi.mp4"
        cls.long = cls.dir / "long.mp4"

        cls._make(
            cls.tone,
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        )
        cls._make(
            cls.silent,
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=15",
            "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
        )
        cls._make(
            cls.multi,
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=15",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        )
        cls._make(
            cls.long,
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "20", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def _make(cls, path: Path, *args: str) -> None:
        cmd = [cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args, str(path)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"fixture failed: {result.stderr}")

    def _run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = m.run_cli(list(args))
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def _mean_volume(self, path: Path) -> float:
        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", result.stderr)
        if not match:
            self.fail(f"无法读取音量：{result.stderr[-400:]}")
        return float(match.group(1))

    def test_probe(self):
        self.assertEqual(m.probe_audio(self.ffprobe, self.tone), "aac")
        self.assertIsNone(m.probe_audio(self.ffprobe, self.silent))
        self.assertAlmostEqual(m.probe_duration(self.ffprobe, self.tone), 2.0, delta=0.2)
        self.assertIsNotNone(m.probe_audio(self.ffprobe, self.multi, 0))
        self.assertIsNotNone(m.probe_audio(self.ffprobe, self.multi, 1))

    def test_convert_success(self):
        dst = self.dir / "out" / "tone.mp3"
        result = m.convert_one(self.tone, dst, m.Options(bitrate="128k"))
        self.assertEqual(result.status, m.DONE)
        self.assertTrue(dst.is_file())
        self.assertFalse(dst.with_name(dst.name + ".part").exists())
        self.assertEqual(m.probe_audio(self.ffprobe, dst), "mp3")
        self.assertAlmostEqual(m.probe_duration(self.ffprobe, dst), 2.0, delta=0.2)

    def test_skip_and_overwrite(self):
        dst = self.dir / "out" / "skip.mp3"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"old")
        result = m.convert_one(self.tone, dst)
        self.assertEqual(result.status, m.SKIPPED)
        self.assertEqual(dst.read_bytes(), b"old")

        result = m.convert_one(self.tone, dst, m.Options(overwrite=True))
        self.assertEqual(result.status, m.DONE)
        self.assertNotEqual(dst.read_bytes(), b"old")

    def test_failure_cases(self):
        missing = m.convert_one(self.dir / "missing.mp4", self.dir / "x.mp3")
        self.assertEqual(missing.status, m.FAILED)
        self.assertIn("不存在", missing.message)

        silent = m.convert_one(self.silent, self.dir / "silent.mp3")
        self.assertEqual(silent.status, m.FAILED)
        self.assertIn("音频轨道", silent.message)

        bad_track = m.convert_one(self.tone, self.dir / "bad-track.mp3", m.Options(track=1))
        self.assertEqual(bad_track.status, m.FAILED)
        self.assertIn("音频轨道", bad_track.message)

        same = m.convert_one(self.tone, self.tone)
        self.assertEqual(same.status, m.FAILED)

    def test_track_selection(self):
        quiet = self.dir / "track0.mp3"
        loud = self.dir / "track1.mp3"
        self.assertEqual(m.convert_one(self.multi, quiet, m.Options(track=0)).status, m.DONE)
        self.assertEqual(m.convert_one(self.multi, loud, m.Options(track=1)).status, m.DONE)
        self.assertLess(self._mean_volume(quiet), -80)
        self.assertGreater(self._mean_volume(loud), -60)

    def test_cancel(self):
        import threading
        event = threading.Event()
        event.set()
        dst = self.dir / "cancelled.mp3"
        result = m.convert_one(self.long, dst, cancel_event=event)
        self.assertEqual(result.status, m.CANCELLED)
        self.assertFalse(dst.exists())
        self.assertFalse(dst.with_name(dst.name + ".part").exists())

    def test_cli_success_and_skip(self):
        outdir = self.dir / "cli-out"
        code, text, err = self._run_cli(str(self.tone), "-o", str(outdir), "-b", "128")
        self.assertEqual(code, 0, err)
        self.assertTrue((outdir / "tone.mp3").is_file())
        self.assertIn("成功 1", text)

        code, text, _ = self._run_cli(str(self.tone), "-o", str(outdir))
        self.assertEqual(code, 0)
        self.assertIn("跳过 1", text)

        code, text, _ = self._run_cli(str(self.tone), "-o", str(outdir), "-f")
        self.assertEqual(code, 0)
        self.assertIn("成功 1", text)

    def test_cli_failure_and_arguments(self):
        code, text, err = self._run_cli(str(self.silent), "-o", str(self.dir / "bad-out"))
        self.assertEqual(code, 1)
        self.assertIn("音频轨道", text + err)

        self.assertEqual(self._run_cli("-b", "999", str(self.tone))[0], 2)
        self.assertEqual(self._run_cli(str(self.dir / "none.mp4"))[0], 2)
        self.assertEqual(self._run_cli()[0], 2)

    def test_cli_track(self):
        outdir = self.dir / "track-out"
        code, _, err = self._run_cli(str(self.multi), "-o", str(outdir), "--track", "1")
        self.assertEqual(code, 0, err)
        self.assertGreater(self._mean_volume(outdir / "multi.mp3"), -60)
        self.assertEqual(self._run_cli(str(self.multi), "--track", "2")[0], 1)
        self.assertEqual(self._run_cli(str(self.multi), "--track", "-1")[0], 2)

    def test_cli_duplicate_names(self):
        root = self.dir / "dup"
        a = root / "a" / "同名.mp4"
        b = root / "b" / "同名.mp4"
        a.parent.mkdir(parents=True)
        b.parent.mkdir(parents=True)
        shutil.copyfile(self.tone, a)
        shutil.copyfile(self.tone, b)
        outdir = self.dir / "dup-out"
        code, _, err = self._run_cli(str(a), str(b), "-o", str(outdir))
        self.assertEqual(code, 0, err)
        self.assertEqual({p.name for p in outdir.glob("*.mp3")}, {"同名.mp3", "同名 (2).mp3"})

    def test_cli_recursive(self):
        root = self.dir / "tree"
        nested = root / "sub" / "nested"
        nested.mkdir(parents=True)
        shutil.copyfile(self.tone, nested / "one.mp4")
        outdir = self.dir / "tree-out"
        code, _, err = self._run_cli(str(root), "-r", "-o", str(outdir))
        self.assertEqual(code, 0, err)
        self.assertTrue((outdir / "one.mp3").is_file())

    def test_cli_version_and_help(self):
        for args, expected in ((["--version"], "1.0"), (["--help"], "--track")):
            result = subprocess.run(
                [sys.executable, str(ROOT / "mp4_to_mp3.py"), *args],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout)

    def test_main_dispatches_gui(self):
        with mock.patch.object(m, "run_gui", return_value=7) as gui:
            self.assertEqual(m.main([]), 7)
            gui.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
