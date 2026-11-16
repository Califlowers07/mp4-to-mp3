#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MP4 → MP3 音频提取工具

用法：
  1) 双击 / 直接运行（不带参数）打开图形界面：
         python mp4_to_mp3.py
  2) 命令行转换单个文件：
         python mp4_to_mp3.py 视频.mp4
  3) 批量转换一个文件夹：
         python mp4_to_mp3.py D:\\视频 --outdir D:\\音频 --bitrate 320
  4) 把文件拖到“拖放MP4转MP3.bat”上。

依赖：Python 3.8+ 和 ffmpeg（含 ffprobe）。脚本会自动在 PATH、
      C:\\Program Files\\ffmpeg\\bin、脚本所在目录和 FFMPEG_HOME 中查找。
"""

from __future__ import annotations

import argparse
import glob as globmod
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:  # 命令行模式不依赖 tkinter；没有安装时仍可正常转换。
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - 取决于运行环境
    tk = None
    filedialog = messagebox = ttk = None

APP_NAME = "MP4 转 MP3"
VERSION = "1.0"

# 支持的视频扩展名（主要针对 mp4，顺带兼容常见的其他容器）
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".flv",
    ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".rmvb",
}

DONE = "done"
SKIPPED = "skipped"
FAILED = "failed"
CANCELLED = "cancelled"
STATUS_STYLE = {
    DONE: ("完成", "ok"),
    SKIPPED: ("跳过", "muted"),
    FAILED: ("失败", "fail"),
    CANCELLED: ("已取消", "muted"),
}

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

FFMPEG = None
FFPROBE = None


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Options:
    """一次转换任务的参数。"""

    bitrate: str = "192k"
    track: int = 0
    overwrite: bool = False

    def __post_init__(self) -> None:
        if self.track < 0:
            raise ValueError("音轨编号不能为负数。")


@dataclass(frozen=True)
class Job:
    """一个源文件及其输出路径。"""

    src: Path
    dst: Path


@dataclass(frozen=True)
class Result:
    """单个文件的转换结果。"""

    status: str
    message: str


@dataclass
class Summary:
    """一批转换任务的计数。"""

    done: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: int = 0

    def add(self, status: str) -> None:
        if status == DONE:
            self.done += 1
        elif status == SKIPPED:
            self.skipped += 1
        elif status == CANCELLED:
            self.cancelled += 1
        else:
            self.failed += 1

    def text(self) -> str:
        text = f"成功 {self.done}，跳过 {self.skipped}，失败 {self.failed}"
        if self.cancelled:
            text += f"，取消 {self.cancelled}"
        return text

    @property
    def exit_code(self) -> int:
        return 0 if self.failed == 0 and self.cancelled == 0 else 1


class ToolMissing(RuntimeError):
    """找不到 ffmpeg / ffprobe。"""


# ---------------------------------------------------------------------------
# 工具查找 / ffprobe
# ---------------------------------------------------------------------------

def _find_exe(name: str) -> str | None:
    """在 PATH、常见安装目录、脚本目录中查找可执行文件。"""
    exe = f"{name}.exe" if os.name == "nt" else name

    found = shutil.which(name)
    if found:
        return found

    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    home = os.environ.get("FFMPEG_HOME", "").strip()
    if home:
        candidates += [Path(home) / exe, Path(home) / "bin" / exe]
    candidates += [
        here / exe,
        here / "ffmpeg" / "bin" / exe,
        Path(r"C:\Program Files\ffmpeg\bin") / exe,
        Path(r"C:\Program Files (x86)\ffmpeg\bin") / exe,
        Path(r"C:\ffmpeg\bin") / exe,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def ensure_tools() -> tuple[str, str]:
    """返回 (ffmpeg, ffprobe) 路径，找不到时抛出 ToolMissing。"""
    global FFMPEG, FFPROBE
    if FFMPEG is None:
        FFMPEG = _find_exe("ffmpeg")
    if FFPROBE is None:
        FFPROBE = _find_exe("ffprobe")
    if not FFMPEG or not FFPROBE:
        raise ToolMissing(
            "没有找到 ffmpeg / ffprobe。\n\n"
            "请先安装 ffmpeg，并确保 ffmpeg.exe 在 PATH 中，或者放在：\n"
            "  C:\\Program Files\\ffmpeg\\bin\\\n\n"
            "安装提示（任选其一）：\n"
            "  · winget install Gyan.FFmpeg\n"
            "  · 从 https://www.gyan.dev/ffmpeg/builds/ 下载并解压到上述目录\n"
            "也可以设置环境变量 FFMPEG_HOME 指向 ffmpeg 的 bin 目录。"
        )
    return FFMPEG, FFPROBE


def _run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )


def probe_duration(ffprobe: str, src: Path) -> float:
    """读取媒体总时长（秒），失败时返回 0。"""
    result = _run_capture([
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1",
        str(src),
    ])
    try:
        value = float(result.stdout.strip())
        return value if value > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def probe_audio(ffprobe: str, src: Path, track: int = 0) -> str | None:
    """返回第 track 条音轨的编码名；没有音轨时返回 None。"""
    result = _run_capture([
        ffprobe, "-v", "error",
        "-select_streams", f"a:{track}",
        "-show_entries", "stream=codec_name",
        "-of", "csv=p=0",
        str(src),
    ])
    codec = result.stdout.strip() if result.returncode == 0 else ""
    return codec or None


# ---------------------------------------------------------------------------
# 转换核心
# ---------------------------------------------------------------------------

def normalize_bitrate(value: str) -> str:
    """把 '192' / '192k' / '192K' 统一成 ffmpeg 需要的 '192k'。"""
    text = str(value).strip().lower()
    if text.endswith("k"):
        text = text[:-1]
    if not text.isdigit():
        raise ValueError("码率请填写数字，例如 192（单位 kbps）。")
    number = int(text)
    if not 32 <= number <= 320:
        raise ValueError("MP3 码率应在 32–320 kbps 之间。")
    return f"{number}k"


def parse_ff_time(text: str) -> float | None:
    """解析 ffmpeg -progress 输出的 HH:MM:SS.ffffff。"""
    match = re.match(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$", text.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _terminate_process(proc: subprocess.Popen) -> None:
    """尽量结束 ffmpeg，避免 Ctrl+C 后留下后台进程。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait()
        except OSError:
            pass


def _close_pipes(proc: subprocess.Popen) -> None:
    """关闭 Popen 的文本管道，避免 ResourceWarning 与句柄泄漏。"""
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _ffmpeg_cmd(ffmpeg: str, src: Path, dst: Path, options: Options) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner", "-nostdin", "-y",
        "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        "-i", str(src),
        "-map", f"0:a:{options.track}",
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", options.bitrate,
        "-id3v2_version", "3",
        "-map_metadata", "0",
        "-f", "mp3",
        str(dst),
    ]


def convert_one(
    src: Path,
    dst: Path,
    options: Options = Options(),
    *,
    on_progress=None,
    cancel_event: threading.Event | None = None,
) -> Result:
    """把视频中的一条音轨转换为 MP3。"""
    src = Path(src)
    dst = Path(dst)

    if not src.is_file():
        return Result(FAILED, "源文件不存在")
    if src.resolve() == dst.resolve():
        return Result(FAILED, "输出文件与源文件相同")
    if dst.exists() and not options.overwrite:
        return Result(SKIPPED, "目标 MP3 已存在")

    try:
        ffmpeg, ffprobe = ensure_tools()
    except ToolMissing as exc:
        return Result(FAILED, str(exc))

    if probe_audio(ffprobe, src, options.track) is None:
        return Result(FAILED, "文件里没有找到音频轨道")

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Result(FAILED, f"无法创建输出文件夹：{exc}")

    duration = probe_duration(ffprobe, src)
    tmp = dst.with_name(dst.name + ".part")
    _silent_unlink(tmp)

    try:
        proc = subprocess.Popen(
            _ffmpeg_cmd(ffmpeg, src, tmp, options),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            bufsize=1,
        )
    except OSError as exc:
        return Result(FAILED, f"无法启动 ffmpeg：{exc}")

    cancelled = False
    assert proc.stdout is not None

    try:
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                proc.terminate()
                break
            if on_progress is None or duration <= 0:
                continue
            if line.startswith("out_time="):
                seconds = parse_ff_time(line.split("=", 1)[1])
                if seconds is not None:
                    on_progress(max(0.0, min(seconds / duration, 0.999)))
            elif line.startswith("progress=end"):
                on_progress(1.0)
    except KeyboardInterrupt:
        _terminate_process(proc)
        _close_pipes(proc)
        _silent_unlink(tmp)
        raise

    if proc.stdout is not None:
        proc.stdout.close()

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except KeyboardInterrupt:
        _terminate_process(proc)
        _close_pipes(proc)
        _silent_unlink(tmp)
        raise

    stderr_text = ""
    if proc.stderr is not None:
        try:
            stderr_text = proc.stderr.read() or ""
        except (OSError, ValueError):
            stderr_text = ""
        finally:
            proc.stderr.close()

    if cancelled or (cancel_event is not None and cancel_event.is_set()):
        _silent_unlink(tmp)
        return Result(CANCELLED, "已取消")

    if proc.returncode != 0:
        _silent_unlink(tmp)
        detail = " ".join(stderr_text.strip().split())
        if len(detail) > 600:
            detail = detail[:600] + "…"
        return Result(FAILED, detail or f"ffmpeg 退出码 {proc.returncode}")

    if not tmp.is_file() or tmp.stat().st_size == 0:
        _silent_unlink(tmp)
        return Result(FAILED, "ffmpeg 没有生成有效文件")

    try:
        os.replace(tmp, dst)
    except OSError as exc:
        _silent_unlink(tmp)
        return Result(FAILED, f"无法保存输出文件：{exc}")

    if on_progress is not None:
        on_progress(1.0)
    return Result(DONE, human_size(dst.stat().st_size))


# ---------------------------------------------------------------------------
# 文件收集 / 输出路径规划
# ---------------------------------------------------------------------------

def _is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTS


def collect_inputs(raw_inputs: list[str], recursive: bool = False) -> tuple[list[Path], list[str]]:
    """把文件、文件夹、通配符统一展开成视频文件列表。"""
    files: list[Path] = []
    errors: list[str] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path.absolute()).lower()
        if key not in seen:
            seen.add(key)
            files.append(path)

    for raw in raw_inputs:
        item = Path(raw).expanduser()
        if item.is_dir():
            pattern = "**/*" if recursive else "*"
            found = sorted(p for p in item.glob(pattern) if _is_video(p))
            if not found:
                errors.append(f"文件夹里没有找到视频文件：{item}")
            for path in found:
                add(path)
        elif item.is_file():
            if item.suffix.lower() not in VIDEO_EXTS:
                errors.append(f"不是支持的视频格式：{item}")
            else:
                add(item)
        else:
            matches = [Path(m) for m in globmod.glob(raw, recursive=recursive)]
            matches = [m for m in matches if _is_video(m)]
            if matches:
                for path in matches:
                    add(path)
            else:
                errors.append(f"找不到文件或文件夹：{raw}")

    return files, errors


def plan_output(src: Path, outdir: Path | None, used: set[str]) -> Path:
    """决定输出路径；同一批里重名时自动加 (2)、(3)。"""
    dst = Path(outdir) / f"{src.stem}.mp3" if outdir is not None else src.with_suffix(".mp3")

    key = str(dst).lower()
    if key in used:
        index = 2
        while True:
            candidate = dst.with_name(f"{dst.stem} ({index}).mp3")
            if str(candidate).lower() not in used:
                dst = candidate
                break
            index += 1
    used.add(str(dst).lower())
    return dst


def plan_jobs(files: list[Path], outdir: Path | None = None) -> list[Job]:
    """为一批源文件规划输出路径，供 CLI 和 GUI 共用。"""
    used: set[str] = set()
    return [Job(src, plan_output(src, outdir, used)) for src in files]


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是整数")
    if number < 0:
        raise argparse.ArgumentTypeError("不能为负数")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mp4_to_mp3.py",
        description="从 MP4 视频中提取音频并保存为 MP3。不带参数运行会打开图形界面。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python mp4_to_mp3.py 视频.mp4\n"
            "  python mp4_to_mp3.py D:\\视频 -o D:\\音频 -b 320\n"
            "  python mp4_to_mp3.py D:\\视频 -r -o D:\\音频\n"
            "  python mp4_to_mp3.py *.mp4 -f\n"
        ),
    )
    parser.add_argument("inputs", nargs="*", help="视频文件、文件夹或通配符（支持多个）")
    parser.add_argument("-o", "--outdir", help="输出文件夹；默认与源文件同目录")
    parser.add_argument("-b", "--bitrate", default="192k",
                        help="MP3 码率，32–320 kbps（默认 192k）")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="递归扫描子文件夹")
    parser.add_argument("-f", "--force", action="store_true",
                        help="覆盖已经存在的 MP3")
    parser.add_argument("--track", type=_non_negative_int, default=0, metavar="N",
                        help="使用第 N 条音轨，从 0 开始（默认 0）")
    parser.add_argument("--gui", action="store_true", help="打开图形界面")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def run_cli(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui:
        return run_gui()

    if not args.inputs:
        parser.print_help()
        return 2

    try:
        bitrate = normalize_bitrate(args.bitrate)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        ffmpeg, _ = ensure_tools()
    except ToolMissing as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    files, errors = collect_inputs(args.inputs, recursive=args.recursive)
    for message in errors:
        print(f"提示：{message}", file=sys.stderr)
    if not files:
        print("没有可转换的视频文件。", file=sys.stderr)
        return 2

    outdir = Path(args.outdir).expanduser() if args.outdir else None
    if outdir is not None:
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"错误：无法创建输出文件夹：{exc}", file=sys.stderr)
            return 2

    jobs = plan_jobs(files, outdir)
    options = Options(bitrate=bitrate, track=args.track, overwrite=args.force)

    print(f"{APP_NAME}  v{VERSION}")
    print(f"ffmpeg：{ffmpeg}")
    print(f"共 {len(jobs)} 个文件，码率 {bitrate}"
          + (f"，输出到 {outdir}" if outdir else "，输出到源文件夹"))
    print("-" * 60)

    summary = Summary()
    started_at = time.perf_counter()

    try:
        for index, job in enumerate(jobs, 1):
            print(f"[{index}/{len(jobs)}] {job.src.name}  →  {job.dst.name}")
            last_percent = -1

            def on_progress(fraction: float) -> None:
                nonlocal last_percent
                percent = int(fraction * 100)
                if sys.stdout.isatty() and percent != last_percent:
                    print(f"\r    转换中 {percent:3d}%", end="", flush=True)
                    last_percent = percent

            result = convert_one(
                job.src, job.dst, options,
                on_progress=on_progress if sys.stdout.isatty() else None,
            )
            if sys.stdout.isatty():
                print("\r" + " " * 20 + "\r", end="")

            summary.add(result.status)
            if result.status == DONE:
                print(f"    完成  {result.message}")
            elif result.status == SKIPPED:
                print(f"    跳过：{result.message}（加 -f 可覆盖）")
            elif result.status == CANCELLED:
                print("    已取消")
            else:
                print(f"    失败：{result.message}")
    except KeyboardInterrupt:
        print("\n用户中断，已停止。")
        return 130

    elapsed = time.perf_counter() - started_at
    print("-" * 60)
    print(f"结束：{summary.text()}，用时 {elapsed:.1f} 秒")
    return summary.exit_code


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------

def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _set_default_fonts() -> None:
    try:
        from tkinter import font as tkfont
        for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(font_name).configure(family="Microsoft YaHei UI", size=10)
    except Exception:
        pass


class App:
    """图形界面。把类放在模块级，便于测试和复用。"""

    def __init__(self, root, ffmpeg_path: str) -> None:
        self.root = root
        self.ffmpeg_path = ffmpeg_path
        self.items: dict[str, Path] = {}
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.worker: threading.Thread | None = None

        self.out_mode = tk.StringVar(value="source")
        self.out_dir = tk.StringVar()
        self.bitrate = tk.StringVar(value="192")
        self.overwrite = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="就绪")
        self.count = tk.StringVar(value="共 0 个文件")

        self.root.title(f"{APP_NAME} · 音频提取工具  v{VERSION}")
        self.root.geometry("820x640")
        self.root.minsize(700, 560)
        _set_default_fonts()

        self._build()
        self._set_busy(False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)
        self.log(f"就绪 · ffmpeg: {ffmpeg_path}")
        self.log("添加视频后点击“开始转换”；输出 MP3 默认放在源文件旁边。")

    # -- 界面 ----------------------------------------------------------
    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x")
        self.btn_add = ttk.Button(toolbar, text="添加文件…", command=self.add_files)
        self.btn_add.pack(side="left")
        self.btn_folder = ttk.Button(toolbar, text="添加文件夹…", command=self.add_folder)
        self.btn_folder.pack(side="left", padx=(6, 0))
        self.btn_remove = ttk.Button(toolbar, text="移除选中", command=self.remove_selected)
        self.btn_remove.pack(side="left", padx=(6, 0))
        self.btn_clear = ttk.Button(toolbar, text="清空列表", command=self.clear_all)
        self.btn_clear.pack(side="left", padx=(6, 0))
        ttk.Label(toolbar, textvariable=self.count).pack(side="right")

        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True, pady=(10, 0))

        self.tree = ttk.Treeview(
            list_frame,
            columns=("file", "status"),
            show="headings",
            selectmode="extended",
            height=8,
        )
        self.tree.heading("file", text="文件")
        self.tree.heading("status", text="状态")
        self.tree.column("file", width=600, anchor="w")
        self.tree.column("status", width=110, anchor="center", stretch=False)
        self.tree.tag_configure("ok", foreground="#1a7f37")
        self.tree.tag_configure("fail", foreground="#c62828")
        self.tree.tag_configure("run", foreground="#1565c0")
        self.tree.tag_configure("muted", foreground="#888888")
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        options = ttk.LabelFrame(outer, text="输出设置", padding=10)
        options.pack(fill="x", pady=(10, 0))

        row1 = ttk.Frame(options)
        row1.pack(fill="x")
        self.row1 = row1
        ttk.Label(row1, text="输出位置：").pack(side="left")
        self.radio_source = ttk.Radiobutton(
            row1, text="源文件所在文件夹", variable=self.out_mode,
            value="source", command=self._sync_outdir,
        )
        self.radio_source.pack(side="left")
        self.radio_custom = ttk.Radiobutton(
            row1, text="指定文件夹", variable=self.out_mode,
            value="custom", command=self._sync_outdir,
        )
        self.radio_custom.pack(side="left", padx=(16, 0))
        self.chk_overwrite = ttk.Checkbutton(
            row1, text="覆盖已存在的 MP3", variable=self.overwrite,
        )
        self.chk_overwrite.pack(side="right")

        row2 = ttk.Frame(options)
        row2.pack(fill="x", pady=(8, 0))
        self.row2 = row2
        self.entry_out = ttk.Entry(row2, textvariable=self.out_dir)
        self.entry_out.pack(side="left", fill="x", expand=True)
        self.btn_browse = ttk.Button(row2, text="浏览…", command=self.choose_outdir)
        self.btn_browse.pack(side="left", padx=(6, 0))

        row3 = ttk.Frame(options)
        row3.pack(fill="x", pady=(8, 0))
        ttk.Label(row3, text="MP3 码率：").pack(side="left")
        self.combo_bitrate = ttk.Combobox(
            row3, textvariable=self.bitrate, values=("128", "192", "256", "320"),
            width=6, state="readonly",
        )
        self.combo_bitrate.pack(side="left")
        ttk.Label(row3, text="kbps　（192 适合日常，320 音质最好）").pack(
            side="left", padx=(6, 0)
        )

        action = ttk.Frame(outer)
        action.pack(fill="x", pady=(12, 0))
        self.btn_start = ttk.Button(action, text="开始转换", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(action, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 10))
        self.progress = ttk.Progressbar(action, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(action, textvariable=self.status, width=24).pack(
            side="left", padx=(10, 0)
        )

        log_frame = ttk.LabelFrame(outer, text="日志", padding=6)
        log_frame.pack(fill="both", expand=False, pady=(10, 0))
        self.log_text = tk.Text(
            log_frame, height=7, wrap="word", state="disabled",
            relief="solid", borderwidth=1,
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def _sync_outdir(self) -> None:
        custom = self.out_mode.get() == "custom"
        if custom:
            self.row2.pack(fill="x", pady=(8, 0), after=self.row1)
            state = "normal" if not self.running else "disabled"
        else:
            self.row2.pack_forget()
            state = "disabled"
        self.entry_out.configure(state=state)
        self.btn_browse.configure(state=state)

    def _set_busy(self, busy: bool) -> None:
        self.running = busy
        state = "disabled" if busy else "normal"
        for widget in (
            self.btn_add, self.btn_folder, self.btn_remove, self.btn_clear,
            self.radio_source, self.radio_custom, self.chk_overwrite,
        ):
            widget.configure(state=state)
        self.combo_bitrate.configure(state="disabled" if busy else "readonly")
        self.btn_start.configure(state="disabled" if busy else "normal")
        self.btn_stop.configure(state="normal" if busy else "disabled")
        self._sync_outdir()

    # -- 列表管理 ------------------------------------------------------
    def _add_paths(self, paths) -> None:
        added = 0
        existing = {str(p).lower() for p in self.items.values()}
        for path in paths:
            path = Path(path)
            if not _is_video(path):
                continue
            key = str(path).lower()
            if key in existing:
                continue
            existing.add(key)
            iid = self.tree.insert("", "end", values=(str(path), "等待"))
            self.items[iid] = path
            added += 1
        self.count.set(f"共 {len(self.items)} 个文件")
        if added:
            self.log(f"已添加 {added} 个文件。")
        elif paths:
            self.log("没有添加新文件（可能重复或格式不支持）。")

    def add_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="选择视频文件",
            filetypes=[
                ("视频文件", "*.mp4 *.m4v *.mov *.mkv *.avi *.webm *.flv *.wmv *.ts *.m2ts"),
                ("所有文件", "*.*"),
            ],
        )
        if chosen:
            self._add_paths(chosen)

    def add_folder(self) -> None:
        chosen = filedialog.askdirectory(title="选择文件夹（包含子文件夹）")
        if not chosen:
            return
        self.log(f"正在扫描：{chosen}")
        self.root.update_idletasks()
        self._add_paths(sorted(p for p in Path(chosen).rglob("*") if _is_video(p)))

    def remove_selected(self) -> None:
        if self.running:
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self.count.set(f"共 {len(self.items)} 个文件")

    def clear_all(self) -> None:
        if self.running:
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear()
        self.count.set("共 0 个文件")
        self.progress["value"] = 0
        self.status.set("就绪")

    def choose_outdir(self) -> None:
        chosen = filedialog.askdirectory(title="选择输出文件夹")
        if chosen:
            self.out_dir.set(chosen)
            self.out_mode.set("custom")
            self._sync_outdir()

    # -- 转换流程 ------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        if not self.items:
            messagebox.showinfo(APP_NAME, "请先添加要转换的视频文件。")
            return

        outdir = None
        if self.out_mode.get() == "custom":
            raw = self.out_dir.get().strip()
            if not raw:
                messagebox.showwarning(APP_NAME, "请选择输出文件夹。")
                return
            outdir = Path(raw)
            try:
                outdir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"无法创建输出文件夹：\n{exc}")
                return

        try:
            bitrate = normalize_bitrate(self.bitrate.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self._set_busy(True)
        self.cancel.clear()
        self.progress["value"] = 0
        self.status.set("准备中…")
        for iid in self.items:
            self.tree.set(iid, "status", "等待")
            self.tree.item(iid, tags=())

        self.log("-" * 56)
        self.log(f"开始转换 {len(self.items)} 个文件 · {bitrate}"
                 + (f" · 输出到 {outdir}" if outdir else " · 输出到源文件夹"))

        items = list(self.items.items())
        options = Options(bitrate=bitrate, track=0, overwrite=self.overwrite.get())
        self.worker = threading.Thread(
            target=self._work, args=(items, outdir, options), daemon=True,
        )
        self.worker.start()

    def _work(self, items, outdir: Path | None, options: Options) -> None:
        total = len(items)
        jobs = plan_jobs([src for _, src in items], outdir)
        summary = Summary()

        for index, ((iid, src), job) in enumerate(zip(items, jobs)):
            if self.cancel.is_set():
                summary.add(CANCELLED)
                self.events.put(("status", iid, "已取消", "muted"))
                self.events.put(("log", f"[{index + 1}/{total}] 取消 {src.name}"))
                self.events.put(("progress", (index + 1) / total * 100))
                continue

            self.events.put(("status", iid, "转换中…", "run"))

            def on_progress(fraction: float, i: int = index) -> None:
                self.events.put(("progress", (i + fraction) / total * 100))

            result = convert_one(
                job.src, job.dst, options,
                on_progress=on_progress, cancel_event=self.cancel,
            )
            summary.add(result.status)
            label, tag = STATUS_STYLE[result.status]
            self.events.put(("status", iid, label, tag))

            if result.status == DONE:
                self.events.put(("log", f"[{index + 1}/{total}] 完成 {src.name} → "
                                          f"{job.dst.name}（{result.message}）"))
            elif result.status == SKIPPED:
                self.events.put(("log", f"[{index + 1}/{total}] 跳过 {src.name}：{result.message}"))
            elif result.status == CANCELLED:
                self.events.put(("log", f"[{index + 1}/{total}] 取消 {src.name}"))
            else:
                self.events.put(("log", f"[{index + 1}/{total}] 失败 {src.name}：{result.message}"))

            self.events.put(("progress", (index + 1) / total * 100))

        self.events.put(("done", summary.text()))

    def stop(self) -> None:
        if not self.running:
            return
        self.cancel.set()
        self.status.set("正在停止…")
        self.btn_stop.configure(state="disabled")
        self.log("正在停止，请稍候…")

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "status":
                    _, iid, text, tag = event
                    if self.tree.exists(iid):
                        self.tree.set(iid, "status", text)
                        self.tree.item(iid, tags=(tag,) if tag else ())
                elif kind == "progress":
                    self.progress["value"] = event[1]
                elif kind == "log":
                    self.log(event[1])
                elif kind == "done":
                    self._finish(event[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish(self, summary: str) -> None:
        self._set_busy(False)
        self.status.set(summary)
        self.log(f"全部结束：{summary}")

    def log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S  ") + message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.running:
            messagebox.showwarning(
                APP_NAME,
                "正在转换，请先点击「停止」，等待当前文件结束后再关闭。",
            )
            return
        self.root.destroy()


def run_gui() -> int:
    if tk is None:
        print("错误：当前 Python 缺少 tkinter，无法打开图形界面。", file=sys.stderr)
        return 2

    _enable_dpi_awareness()

    try:
        ffmpeg, _ = ensure_tools()
    except ToolMissing as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, str(exc))
        root.destroy()
        return 2

    root = tk.Tk()
    App(root, ffmpeg)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return run_gui()
    try:
        return run_cli(argv)
    except ToolMissing as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
