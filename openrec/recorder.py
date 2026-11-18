"""录制核心：屏幕/区域/窗口采集、麦克风与系统声混音、FFmpeg 编码、暂停恢复、剪辑与修复。

全部本地执行，不依赖任何网络服务。
"""
import ctypes
import ctypes.wintypes as wt
import glob
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import mss

from .capture import DxcamSource, MssSource, dxgi_output_for
from .config import ffmpeg_path, ffprobe_path, Settings, MIN_FREE_BYTES

log = logging.getLogger("openrec.recorder")

# WAV 的 RIFF 头只用 32 位记录长度，单文件上限 4 GiB（48 kHz 双声道约 6.2 小时）。
# 留出余量，超过就换下一个分片，收尾时按顺序拼接。
WAV_SEGMENT_BYTES = 3_800_000_000

# ---------------------------------------------------------------- Windows API
user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

DWMWA_EXTENDED_FRAME_BOUNDS = 9
VK = {"R": 0x52, "P": 0x50}


@dataclass
class WindowInfo:
    hwnd: int
    title: str


def list_windows() -> list[WindowInfo]:
    """枚举可见顶层窗口。"""
    result = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if title and title not in ("Program Manager", "Default IME"):
            result.append(WindowInfo(int(hwnd), title))
        return True

    user32.EnumWindows(_cb, 0)
    return result


def find_window(title: str) -> int:
    for w in list_windows():
        if w.title == title:
            return w.hwnd
    return 0


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """返回窗口在屏幕上的物理像素矩形 (x, y, w, h)。"""
    rect = wt.RECT()
    ok = False
    try:
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                     ctypes.byref(rect), ctypes.sizeof(rect))
        ok = True
    except Exception:
        ok = False
    if not ok or rect.right <= rect.left:
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return 0, 0, 0, 0
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _encoder_works(name: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=black:s=640x360:d=0.2", "-c:v", name, "-f", "null", "-"],
            capture_output=True, timeout=6, creationflags=0x08000000)
        return proc.returncode == 0
    except Exception:
        return False


def available_encoders() -> set[str]:
    """返回本机真正可用的编码器（编译支持 + 硬件自检通过）。"""
    try:
        out = subprocess.run([ffmpeg_path(), "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=0x08000000)
        names = set()
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].startswith("V"):
                names.add(parts[1])
    except Exception:
        names = set()
    usable = {"libx264"}
    for hw in ("h264_nvenc", "h264_qsv", "h264_amf"):
        if hw in names and _encoder_works(hw):
            usable.add(hw)
    return usable


def probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            [ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _even(v: int) -> int:
    return max(2, v - (v % 2))


_DDAGRAB_OK: tuple[bool, float] | None = None   # (可用, 探测时刻)


def ddagrab_available() -> bool:
    """检测 FFmpeg 的 Desktop Duplication 硬件采集是否可用。

    成功结果长期缓存；失败只保留 30 秒——桌面锁屏时探测会失败，
    解锁后应尽快恢复硬件通路，而不是整个进程都退化为 mss。
    """
    global _DDAGRAB_OK
    now = time.monotonic()
    if _DDAGRAB_OK is not None:
        ok, ts = _DDAGRAB_OK
        if ok or now - ts < 30:
            return ok
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "ddagrab=output_idx=0:framerate=30",
             "-t", "0.4", "-c:v", "h264_nvenc", "-preset", "p5",
             "-f", "null", "-"],
            capture_output=True, timeout=10, creationflags=0x08000000)
        ok = proc.returncode == 0
    except Exception:
        ok = False
    _DDAGRAB_OK = (ok, time.monotonic())
    return ok


def free_bytes(path: str) -> int:
    """目录所在磁盘的可用字节数；目录尚不存在时取最近的已存在上级目录。"""
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free


def estimate_bitrate(codec: str, quality: int, width: int, height: int,
                     fps: int, with_audio: bool) -> float:
    """按画质、分辨率、帧率与音频开关粗略估算录制码率（bps）。"""
    base = {18: 8.0, 23: 4.0, 28: 2.2}.get(int(quality), 4.0) * 1_000_000
    scale = max(1, width * height) / (1920 * 1080) * max(1, fps) / 30.0
    factor = {"h264_nvenc": 1.0, "h264_qsv": 1.05, "h264_amf": 1.15}.get(codec, 1.0)
    return base * scale * factor + (192_000 if with_audio else 0)


def _replace_with_retry(src: str, dst: str, timeout: float = 3.0):
    """Windows 下 FFmpeg 退出后文件句柄可能短暂未释放，重试替换。"""
    deadline = time.time() + timeout
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.time() >= deadline:
                raise
            time.sleep(0.1)


def virtual_monitor() -> dict:
    """所有物理屏幕的拼接区域（mss 的虚拟合并屏，含屏幕间空隙）。"""
    with mss.mss() as sct:
        m = sct.monitors[0]
    return {
        "index": -1, "left": m["left"], "top": m["top"],
        "width": m["width"], "height": m["height"],
        "is_primary": False, "name": "所有屏幕",
        "unique_id": "__all__", "virtual": True,
    }


def list_monitors() -> list[dict]:
    """枚举物理显示器（不含 mss 的虚拟合并屏）。"""
    with mss.mss() as sct:
        raw = sct.monitors
    mons = raw[1:] if len(raw) > 1 else raw
    result = []
    for i, m in enumerate(mons):
        result.append({
            "index": i,
            "left": m["left"], "top": m["top"],
            "width": m["width"], "height": m["height"],
            "is_primary": bool(m.get("is_primary")),
            "name": m.get("name", ""),
            "unique_id": m.get("unique_id", ""),
            "virtual": False,
        })
    return result


def pick_monitor(settings: Settings) -> dict | None:
    """优先「所有屏幕」，其次 unique_id、序号，最后回退主屏。"""
    if settings.monitor_all:
        return virtual_monitor()
    monitors = list_monitors()
    if not monitors:
        return None
    if settings.monitor_id:
        for m in monitors:
            if m["unique_id"] and m["unique_id"] == settings.monitor_id:
                return m
    if 0 <= settings.monitor_index < len(monitors):
        return monitors[settings.monitor_index]
    return next((m for m in monitors if m["is_primary"]), monitors[0])


def monitor_label(m: dict, with_index: bool = True) -> str:
    if m.get("virtual"):
        tag = "所有屏幕（拼接）"
    elif m["is_primary"]:
        tag = "主屏"
    else:
        tag = f"显示器 {m['index'] + 1}" if with_index else "显示器"
    return f"{tag} · {m['width']}×{m['height']} @{m['left']},{m['top']}"


def encoder_flags(codec: str, quality: int, fps: int = 30) -> list[str]:
    q = int(quality)
    if codec == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", str(q), "-b:v", "0", "-pix_fmt", "yuv420p"]
    if codec == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(q), "-pix_fmt", "nv12"]
    if codec == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
                "-qp_i", str(q), "-qp_p", str(q), "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(q),
            "-pix_fmt", "yuv420p", "-g", str(max(1, fps * 2))]


_BADGE_FONT = None


def _badge_font():
    global _BADGE_FONT
    if _BADGE_FONT is None:
        for name in ("msyhbd.ttc", "msyh.ttc", "arialbd.ttf"):
            try:
                _BADGE_FONT = ImageFont.truetype(f"C:/Windows/Fonts/{name}", 26)
                break
            except Exception:
                continue
        if _BADGE_FONT is None:
            _BADGE_FONT = ImageFont.load_default()
    return _BADGE_FONT


def make_badge_image(dot_on: bool) -> Image.Image:
    """绘制右上角 REC 角标：深色圆角底 + 红色圆点 + REC。"""
    font = _badge_font()
    dot_r, gap, pad_x, pad_y = 7, 10, 14, 9
    try:
        box = font.getbbox("REC")
    except Exception:
        box = (0, 0, 44, 20)
    tw, th = box[2] - box[0], box[3] - box[1]
    w = pad_x * 2 + dot_r * 2 + gap + tw
    h = max(th, dot_r * 2) + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=min(h // 3, 14),
                        fill=(0, 0, 0, 115))
    if dot_on:
        cx, cy = pad_x + dot_r, h // 2
        d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
                  fill=(255, 59, 48, 255))
    tx = pad_x + dot_r * 2 + gap
    d.text((tx + 1, h // 2 + 1), "REC", font=font, fill=(0, 0, 0, 150), anchor="lm")
    d.text((tx, h // 2), "REC", font=font, fill=(255, 59, 48, 255), anchor="lm")
    return img


def _keep_awake(enable: bool):
    """录制期间阻止系统休眠与显示器关闭，避免 BitBlt 采集失效。"""
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        flags = ES_CONTINUOUS
        if enable:
            flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


class RecorderError(RuntimeError):
    pass


class Recorder:
    """一个录制会话。线程模型：视频线程 + 音频线程 + FFmpeg 子进程。"""

    def __init__(self, settings: Settings, title: str | None = None):
        self.s = settings
        # 毫秒后缀：同一秒内重复启动也不会互删同名临时文件
        self._title = title or (time.strftime("录制_%Y%m%d_%H%M%S")
                                + f"_{int(time.time() * 1000) % 1000:03d}")
        self._out_dir = settings.output_dir
        os.makedirs(self._out_dir, exist_ok=True)
        # 同名成品已存在时改用 _2、_3…，避免覆盖旧录像
        self._title = unique_title(self._out_dir, self._title,
                                   settings.audio_only_format)
        self._video_tmp = os.path.join(self._out_dir, f".{self._title}.video.mkv")
        self._audio_tmp = os.path.join(self._out_dir, f".{self._title}.wav")
        self._final = os.path.join(self._out_dir, self._title + ".mp4")
        self._proc: subprocess.Popen | None = None
        self._video_thread: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None
        self._disk_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._recording = threading.Event()
        self._lock = threading.Lock()          # 保护音频分片/说明等共享字段
        self._stderr_buf = bytearray()         # FFmpeg stderr 尾部（由抽干线程写入）
        self._stderr_lock = threading.Lock()
        self._start_ts = 0.0
        self._start_mono = 0.0
        self._stop_ts = 0.0
        self._paused_total = 0.0
        self._pause_started = 0.0
        self._error: str | None = None
        self._audio_ok = False
        self._audio_error: str | None = None
        self._audio_parts: list[str] = []      # 音频分片（超过 4 GiB 自动切分）
        self._mux_started = False              # 本次是否已尝试合成 MP4
        self._awake_tid: int | None = None     # 开启休眠抑制的线程
        self._low_space_seen = False           # 磁盘线程置位后不再被界面清空
        self.frames = 0
        self.state = "idle"  # idle | recording | paused | stopping
        self.backend = "mss"  # mss | dxcam | ddagrab
        self.capture_warning: str | None = None
        self.low_space_warning: str | None = None
        self.degraded_save: str | None = None
        self.pacing_scale = 1.0      # 成片时间戳校正倍率
        self.pacing_note: str | None = None
        self._audio_offset = 0.0     # 音频整体后移量（对齐硬件采集的预热）
        self._bbox = self._resolve_bbox()

    # ------------------------------------------------------------ 采集区域
    def _resolve_bbox(self) -> tuple[int, int, int, int]:
        s = self.s
        if s.source_mode == "region":
            x, y, w, h = s.region
            if w > 1 and h > 1:
                return x, y, _even(w), _even(h)
        if s.source_mode == "window":
            hwnd = find_window(s.window_title)
            if hwnd:
                x, y, w, h = window_rect(hwnd)
                if w > 1 and h > 1:
                    return x, y, _even(w), _even(h)
        mon = pick_monitor(s)
        if mon is None:
            with mss.mss() as sct:
                mon = sct.monitors[0]
        return mon["left"], mon["top"], _even(mon["width"]), _even(mon["height"])

    def _current_bbox(self):
        if self.s.source_mode == "window":
            hwnd = find_window(self.s.window_title)
            if hwnd:
                x, y, w, h = window_rect(hwnd)
                if w > 1 and h > 1:
                    return x, y, _even(w), _even(h)
        return self._bbox

    def current_bbox(self) -> tuple[int, int, int, int]:
        """当前采集区域（物理像素），窗口模式下会跟随窗口移动。"""
        return self._current_bbox()

    # ------------------------------------------------------------ 编码参数
    def _ddagrab_output(self) -> int | None:
        """该显示器对应的 ddagrab 输出号；仅第一块显卡的输出可用。"""
        mon = pick_monitor(self.s)
        dg = dxgi_output_for(mon)
        if dg is None or dg[0] != 0:
            return None
        return dg[1]

    def _wants_ddagrab(self) -> bool:
        """单屏全屏/区域 + NVENC + 无摄像头/文字水印时走硬件桌面复制。"""
        s = self.s
        if s.source_mode not in ("full", "region") or s.monitor_all:
            return False
        if s.codec != "h264_nvenc":
            return False
        if s.camera_enabled or (s.watermark_text or "").strip():
            return False
        mon = pick_monitor(s)
        if not mon or mon.get("virtual"):
            return False
        if self._ddagrab_output() is None:
            return False
        if s.source_mode == "region":
            x, y, w, h = s.region
            if w < 2 or h < 2:
                return False
            if (x < mon["left"] or y < mon["top"]
                    or x + w > mon["left"] + mon["width"]
                    or y + h > mon["top"] + mon["height"]):
                return False
        return True

    def _wants_dxcam(self) -> bool:
        """单屏全屏/区域且能映射到 DXGI 输出时，用桌面复制替代 BitBlt 抓屏。

        叠加层（摄像头/水印/光标）在 Python 侧绘制，不受影响；
        旋转屏与「所有屏幕」仍走 mss，避免输出拓扑映射出错。
        """
        s = self.s
        if s.source_mode not in ("full", "region") or s.monitor_all:
            return False
        mon = pick_monitor(s)
        if not mon or mon.get("virtual"):
            return False
        try:
            if dxgi_output_for(mon) is None:
                return False
        except Exception:      # dxcam 未安装/初始化失败
            return False
        if s.source_mode == "region":
            x, y, w, h = s.region
            if w < 2 or h < 2:
                return False
            if (x < mon["left"] or y < mon["top"]
                    or x + w > mon["left"] + mon["width"]
                    or y + h > mon["top"] + mon["height"]):
                return False
        return True

    def _ddagrab_args(self) -> list[str]:
        s = self.s
        mon = pick_monitor(s) or {"left": 0, "top": 0, "width": 0, "height": 0}
        if s.source_mode == "region":
            x, y, w, h = s.region
            vw, vh = _even(w), _even(h)
            ox, oy = x - mon["left"], y - mon["top"]
        else:
            vw, vh = _even(mon["width"]), _even(mon["height"])
            ox, oy = 0, 0
        draw = "1" if s.cursor_enabled else "0"
        out_idx = self._ddagrab_output() or 0
        source = (f"ddagrab=output_idx={out_idx}:framerate={s.fps}:draw_mouse={draw}"
                  f":video_size={vw}x{vh}:offset_x={ox}:offset_y={oy}")
        return [
            "-hide_banner", "-loglevel", "error", "-stdin", "-y",
            "-f", "lavfi", "-i", source,
            "-c:v", "h264_nvenc", "-preset", "p3", "-rc", "vbr",
            "-cq", str(int(s.quality)), "-b:v", "0", "-threads", "0",
            "-f", "matroska", self._video_tmp,
        ]

    def _video_args(self, w: int, h: int) -> list[str]:
        s = self.s
        return [
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "bgra",
            "-video_size", f"{w}x{h}", "-framerate", str(s.fps),
            "-i", "pipe:0",
            *encoder_flags(s.codec, s.quality, s.fps),
            # x264 默认按核数开线程，2K/4K 下光帧缓冲就要 1.5 GB；
            # 实时录制只需 8 线程即可跑满 30/60 fps，内存降到 1/3。
            "-threads", "8" if s.codec == "libx264" else "0",
            # 关闭 B 帧：实时管道下可降低编码延迟、减少积压；
            # 同时保证收尾做时间轴校正（-itsscale）时 MP4 头部时长准确。
            "-bf", "0",
            "-f", "matroska", self._video_tmp,
        ]

    # ------------------------------------------------------------ 生命周期
    def _wait_first_output(self, timeout: float = 4.0):
        """等待实时采集真正开始落盘，避免把启动延迟算进视频时间轴。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RecorderError(self._read_stderr(self._proc) or "采集进程提前退出")
            try:
                # Matroska 会缓冲数秒才增长，文件出现即表示桌面复制已开始
                if os.path.exists(self._video_tmp):
                    return
            except OSError:
                pass
            time.sleep(0.05)
        raise RecorderError("高帧率采集启动超时")

    def start(self):
        if self.state != "idle":
            return
        self.capture_warning = None
        self.low_space_warning = None
        self.degraded_save = None
        self.pacing_scale = 1.0
        self.pacing_note = None
        self._audio_offset = 0.0
        self._stop_ts = 0.0
        self._paused_total = 0.0
        self._pause_started = 0.0
        self._low_space_seen = False
        self.frames = 0
        self.backend = "mss"
        self._audio_ok = False
        with self._stderr_lock:
            self._stderr_buf.clear()
        self._stop.clear()
        self._paused.clear()
        self._error = None
        self._audio_error = None
        self._audio_parts = []
        for stale in (self._video_tmp, self._audio_tmp):
            try:
                os.remove(stale)
            except OSError:
                pass
        for stale in glob.glob(os.path.join(
                self._out_dir, f".{self._title}.part*.wav")):
            try:
                os.remove(stale)
            except OSError:
                pass
        if self.s.source_mode == "audio":
            log.info("录制开始（仅音频）fps=%s", self.s.fps)
            self._start_ts = time.time()
            self._start_mono = time.monotonic()
            self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
            self._audio_thread.start()
        else:
            backends = []
            if self._wants_ddagrab() and ddagrab_available():
                backends.append("ddagrab")
            if self._wants_dxcam():
                backends.append("dxcam")
            backends.append("mss")   # 兜底：通用 BitBlt 采集
            creation = 0x08000000  # CREATE_NO_WINDOW
            last_error = None
            source = None
            for backend in backends:
                args = (self._ddagrab_args() if backend == "ddagrab"
                        else self._video_args(self._bbox[2], self._bbox[3]))
                proc = None
                src = None
                try:
                    proc = subprocess.Popen(
                        [ffmpeg_path(), *args],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, creationflags=creation)
                    self._proc = proc
                    self._start_stderr_drain(proc)
                    if backend == "ddagrab":
                        self._wait_first_output()
                    elif backend == "dxcam":
                        mon = pick_monitor(self.s)
                        origin = (mon["left"], mon["top"]) if mon else (0, 0)
                        dev, out = dxgi_output_for(mon) or (0, None)
                        src = DxcamSource(self._bbox, self.s.fps, origin=origin,
                                          device_idx=dev, output_idx=out)
                        if not src.wait_ready(2.0):
                            raise RecorderError("桌面复制没有输出画面")
                    else:
                        src = MssSource()
                        src.grab(self._bbox)   # 先抓一帧，尽早暴露采集失败
                    self.backend = backend
                    source = src
                    break
                except Exception as exc:
                    last_error = exc
                    if src is not None:
                        try:
                            src.close()
                        except Exception:
                            pass
                    if proc is not None:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=3)
                        except Exception:
                            pass
                    self._proc = None
            if self._proc is None:
                self.state = "idle"
                log.error("启动采集失败：%s", last_error)
                raise RecorderError(f"启动采集失败：{last_error}")
            log.info("录制开始 backend=%s mode=%s fps=%s codec=%s",
                     self.backend, self.s.source_mode, self.s.fps, self.s.codec)
            self._start_ts = time.time()
            self._start_mono = time.monotonic()
            if self.backend != "ddagrab":
                self._video_thread = threading.Thread(
                    target=self._video_loop, args=(source,), daemon=True)
                self._video_thread.start()
            self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
            self._audio_thread.start()
        self._disk_thread = threading.Thread(target=self._disk_loop, daemon=True)
        self._disk_thread.start()
        _keep_awake(True)
        self._awake_tid = threading.get_ident()   # 休眠状态按线程记录
        self._recording.set()
        self.state = "recording"

    def release_awake(self):
        """清除休眠抑制。Windows 按线程维护该状态，必须由开启它的线程调用。"""
        if self._awake_tid is None or threading.get_ident() != self._awake_tid:
            return
        _keep_awake(False)
        self._awake_tid = None

    def _note(self, text: str):
        """追加一条保存说明，供界面在收尾后提示用户。"""
        with self._lock:
            self.degraded_save = (f"{self.degraded_save}\n{text}"
                                  if self.degraded_save else text)

    @property
    def error(self) -> str | None:
        """录制线程/采集进程的异常信息（供界面轮询）。"""
        return self._error

    def pause(self):
        if self.backend == "ddagrab":
            return  # 硬件桌面复制是实时流，暂不支持暂停
        if self.state == "recording":
            self._paused.set()
            self._pause_started = time.time()
            self.state = "paused"

    def resume(self):
        if self.backend == "ddagrab":
            return
        if self.state == "paused":
            self._paused.clear()
            self._paused_total += time.time() - self._pause_started
            self.state = "recording"

    @property
    def elapsed(self) -> float:
        if not self._start_ts:
            return 0.0
        if self._stop_ts:
            end = self._stop_ts
        else:
            end = self._pause_started if self.state == "paused" else time.time()
        return max(0.0, end - self._start_ts - self._paused_total)

    def stop(self) -> str:
        if self.state == "idle":
            return ""
        if self.state == "paused":
            # 暂停中直接停止：先把最后一段暂停时长结算掉，避免成片被拉慢
            self._paused_total += time.time() - self._pause_started
            self._pause_started = 0.0
        self.state = "stopping"
        self._stop_ts = time.time()
        self.release_awake()
        self._stop.set()
        self._recording.clear()
        if self._video_thread:
            self._video_thread.join(timeout=8)
        if self._audio_thread:
            self._audio_thread.join(timeout=8)
        if self._disk_thread:
            self._disk_thread.join(timeout=3)
        if self._video_thread and self._video_thread.is_alive():
            # 写入线程未退出：_write_frame 已按 _stop 提前返回，这里仅记录
            log.warning("视频线程在 8 秒内未退出，继续收尾但跳过剩余帧")
        if self._proc:
            if self.backend == "ddagrab":
                # 实时采集用 FFmpeg 的 'q' 命令优雅收尾，保证 MKV 完整
                try:
                    if self._proc.stdin:
                        self._proc.stdin.write(b"q\n")
                        self._proc.stdin.flush()
                        self._proc.stdin.close()
                except Exception:
                    pass
            else:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                except Exception:
                    pass
            try:
                self._proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=5)   # 强杀后必须等进程真正退出
                except Exception:
                    pass
            for pipe in (self._proc.stdin, self._proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass
        self._proc = None

        if self._error:
            video_ok = (os.path.exists(self._video_tmp)
                        and os.path.getsize(self._video_tmp) > 1024)
            if not video_ok:
                self._cleanup_temps()
                self.state = "idle"
                raise RecorderError(self._error)
            # 已录内容仍然可用：保住它，把异常作为提示带回界面
            self._note(f"录制异常中断：{self._error}\n已保留中断前的录制内容。")

        if self.s.source_mode == "audio":
            try:
                final = self._encode_audio_only()
                log.info("录制完成（仅音频）时长=%.2fs 文件=%s", self.elapsed, final)
                return final
            finally:
                self.state = "idle"

        if not os.path.exists(self._video_tmp) or os.path.getsize(self._video_tmp) < 1024:
            self._cleanup_temps()
            self.state = "idle"
            if self.capture_warning:
                raise RecorderError(f"{self.capture_warning}；中断前未采集到有效画面。")
            raise RecorderError("视频编码失败：没有有效输出，可能是编码器不可用。")

        try:
            has_audio = (self._audio_ok and os.path.exists(self._audio_tmp)
                         and os.path.getsize(self._audio_tmp) > 1024)
            self._fix_timeline()
            if has_audio and self._space_for_mux():
                try:
                    self._mux_av()
                    result = self._final
                except RecorderError:
                    if not self._low_space_seen:
                        raise
                    result = self._finalize_plain()
            elif has_audio or self._low_space_seen:
                # 空间不足以再写出一个 MP4：保留原格式，确保录像不丢
                result = self._finalize_plain()
            else:
                result = self._materialize_video()
            if self._audio_error:
                # 音频异常不能静默降级：要么提示「只有异常前的部分」，要么明确无声音
                if has_audio:
                    self._note(f"音频中途异常：{self._audio_error}\n已保留异常前的声音。")
                else:
                    self._note(f"音频采集失败：{self._audio_error}\n该录像没有声音。")
            self._cleanup_temps()
            if self.capture_warning or self.degraded_save:
                log.warning("录制降级保存：%s", self.degraded_save or self.capture_warning)
            log.info("录制完成 backend=%s 时长=%.2fs 帧=%s 文件=%s",
                     self.backend, self.elapsed, self.frames, result)
            return result
        finally:
            # 无论成功或异常都复位状态；异常时保留临时文件，方便恢复
            self.state = "idle"

    def _fix_timeline(self):
        """编码吞吐低于设定帧率时成片会变短（加速），按真实时长校正时间戳。

        补重复帧只能缓解采集慢的情况；若瓶颈在编码器（软编/低端机器），
        补帧本身也要花编码时间，因此改为收尾时统一拉伸时间轴。
        """
        self.pacing_scale = 1.0
        self._audio_offset = 0.0
        if not os.path.exists(self._video_tmp):
            return
        real = self.elapsed
        video = probe_duration(self._video_tmp)
        if real < 1.0 or video < 0.5:
            return
        if self.backend == "ddagrab":
            # 桌面复制在 FFmpeg 初始化阶段就开始出帧，比计时起点早约 0.3 s；
            # 把音频整体后移同样时间，避免音画错位。
            pre = video - real
            if 0.05 < pre < 3.0:
                self._audio_offset = pre
            return
        # itsscale 只缩放帧间隔，最后一帧的显示时长仍是一帧的原值，
        # 因此按「(总时长-一帧)*倍率+一帧=真实时长」反解，避免尾部偏短。
        frame = 1.0 / max(1, int(self.s.fps))
        scale = ((real - frame) / (video - frame)
                 if video > frame * 1.5 else real / video)
        if 0.98 <= scale <= 1.02:
            return
        self.pacing_scale = max(0.1, min(100.0, scale))
        self.pacing_note = (
            f"编码性能不足（实际约 {self.frames / real:.0f} fps），"
            "已按真实时长校正时间轴，播放速度正常。")

    def _materialize_video(self) -> str:
        """无音频收尾：重封装为真正的 MP4；空间不足或失败时按 MKV 原样保存。

        直接把 Matroska 临时文件改名成 .mp4 会产生「扩展名与容器不符」的文件，
        浏览器、剪辑软件、硬件播放器按扩展名解析时会拒绝或出错。
        """
        if not self._space_for_mux():
            return self._finalize_plain()
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]
        if self.pacing_scale != 1.0:
            cmd += ["-itsscale", f"{self.pacing_scale:.6f}"]
        cmd += ["-i", self._video_tmp, "-c", "copy",
                "-movflags", "+faststart", self._final]
        try:
            proc = subprocess.run(cmd, capture_output=True, creationflags=0x08000000)
            if proc.returncode != 0 or not os.path.exists(self._final):
                raise RecorderError(
                    (proc.stderr or b"").decode("utf-8", "ignore")[-300:])
            return self._final
        except RecorderError as exc:
            self._note(f"MP4 重封装失败：{exc}\n已改为原格式保存。")
            return self._finalize_plain()
        except OSError as exc:
            self._note(f"MP4 重封装失败：{exc}\n已改为原格式保存。")
            return self._finalize_plain()

    def _space_for_mux(self) -> bool:
        """合成 MP4 需再写出与视频等大的文件，空间不够时改用原格式保存。

        探测失败按「空间不足」处理（失败关闭），避免把已满磁盘彻底写满。
        """
        try:
            video = os.path.getsize(self._video_tmp)
            audio = sum(os.path.getsize(p) for p in self._audio_files())
            free = shutil.disk_usage(self._out_dir).free
        except OSError:
            return False
        return free >= int(video * 1.05) + audio + 128 * 1024 * 1024

    def _finalize_plain(self) -> str:
        """不做二次合成，直接改名落盘（同盘改名不产生额外写入）。"""
        if self._mux_started:
            try:
                if os.path.exists(self._final):
                    os.remove(self._final)  # 仅清理本次合成失败留下的半成品
            except OSError:
                pass
        final = os.path.join(self._out_dir, self._title + ".mkv")
        _replace_with_retry(self._video_tmp, final)
        parts = self._audio_files() if self._audio_ok else []
        reason = "剩余空间不足以合成 MP4，" if self._low_space_seen else ""
        if parts:
            _replace_with_retry(parts[0],
                                os.path.join(self._out_dir, self._title + ".wav"))
            for i, part in enumerate(parts[1:], start=2):
                _replace_with_retry(
                    part, os.path.join(self._out_dir, f"{self._title}.part{i}.wav"))
            extra = "（音频为同名 .wav）" if len(parts) == 1 else "（音频为同名 .wav 及其 partN）"
            self._note(f"{reason}已按录制原格式保存：\n"
                       f"{os.path.basename(final)}{extra}。")
        else:
            self._note(f"已按录制原格式保存：\n{os.path.basename(final)}。")
        if self.pacing_scale != 1.0:
            # 原格式直存无法重封装，时间轴未校正，避免误导
            self.pacing_note = None
            self.degraded_save += "\n注意：编码性能不足，该文件可能比实际录制时长短。"
        return final

    def _disk_loop(self):
        """录制中每秒检查剩余空间，降到 2 GB 时主动收尾，避免写满导致整段作废。"""
        while not self._stop.is_set():
            if (self.backend == "ddagrab" and self._proc is not None
                    and self._proc.poll() is not None):
                err = self._read_stderr(self._proc)
                self._error = f"采集进程异常退出：{err or '未知原因'}"
                self._stop.set()
                self._recording.clear()
                return
            if not self._paused.is_set():
                try:
                    free = shutil.disk_usage(self._out_dir).free
                except OSError:
                    return
                if free <= MIN_FREE_BYTES:
                    self._low_space_seen = True
                    self.low_space_warning = (
                        f"硬盘已满（剩余 {free / 1024 ** 3:.1f} GB，低于 2 GB），已自动停止录像。")
                    self._stop.set()
                    self._recording.clear()
                    return
            time.sleep(1.0)

    # ------------------------------------------------------------ 视频线程
    def _grab_frame(self, source, bbox):
        """抓取一帧；采集短暂失败时重试，并在必要时重建采集对象。

        显示器休眠、分辨率切换、锁屏等会让采集连续失败。持续失败时
        记录 capture_warning 并请求停止采集，让已录内容正常封装而非作废。
        """
        fails = 0
        first_fail = 0.0
        while not self._stop.is_set():
            try:
                frame = source.grab(bbox)
                if frame is None:      # 桌面复制首帧之前
                    time.sleep(0.005)
                    continue
                return frame, source
            except Exception as exc:
                if not fails:
                    first_fail = time.monotonic()
                fails += 1
                if time.monotonic() - first_fail > 8.0:
                    self.capture_warning = f"屏幕采集中断：{exc}"
                    self._stop.set()
                    self._recording.clear()
                    return None, source
                if fails % 10 == 0:
                    try:
                        source.recover()
                    except Exception:
                        pass
                time.sleep(0.1)
        return None, source

    def _video_loop(self, source):
        cam = None
        try:
            if self.s.camera_enabled:
                try:
                    import cv2
                    cam = cv2.VideoCapture(self.s.camera_index, cv2.CAP_DSHOW)
                    cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                except Exception:
                    cam = None
            cursor_img = self._render_cursor()
            mark = self._render_watermark()
            fps = max(1, int(self.s.fps))
            interval = 1.0 / fps
            # FFmpeg 的 -video_size 在启动时固定，帧尺寸一旦变化，
            # 原始字节流会被按旧尺寸硬切，后续画面必然错位。
            enc_w, enc_h = self._bbox[2], self._bbox[3]
            anchor = self._start_mono or time.monotonic()
            paused_acc = 0.0
            pause_at = None
            while not self._stop.is_set():
                if self._paused.is_set():
                    if pause_at is None:
                        pause_at = time.monotonic()
                    time.sleep(0.02)
                    continue
                if pause_at is not None:
                    paused_acc += time.monotonic() - pause_at
                    pause_at = None

                # 以真实时间为准：FFmpeg 固定按 fps 解释每一帧，
                # 采集慢于设定帧率时必须补重复帧，否则成片会被“加速”。
                now = time.monotonic()
                target = int((now - anchor - paused_acc) * fps) + 1
                if self.frames >= target:
                    # 采集快于设定帧率：跳过本次抓屏，保持时间轴准确
                    time.sleep(min(interval, 0.02))
                    continue

                bbox = self._current_bbox()
                if bbox[2] != enc_w or bbox[3] != enc_h:
                    # 窗口被缩放：继续写入会让编码器错位，主动收尾保住已录内容
                    self.capture_warning = (
                        f"窗口尺寸从 {enc_w}×{enc_h} 变为 {bbox[2]}×{bbox[3]}，"
                        "已自动停止录制以保住已录内容。")
                    self._stop.set()
                    self._recording.clear()
                    break
                shot, source = self._grab_frame(source, bbox)
                if shot is None:
                    break
                need_overlay = (cam is not None or mark is not None
                                or (self.s.cursor_enabled and cursor_img is not None))
                if need_overlay:
                    if isinstance(shot, np.ndarray):
                        # DXcam 返回自有可写缓冲，直接在其上叠加，零额外拷贝
                        frame = shot
                    else:
                        frame = np.frombuffer(shot, dtype=np.uint8).reshape(
                            bbox[3], bbox[2], 4).copy()
                    if cam is not None:
                        frame = self._overlay_camera(frame, cam)
                    if mark is not None:
                        frame = self._overlay_rgba(frame, mark, right=12, top=None)
                    if self.s.cursor_enabled and cursor_img is not None:
                        frame = self._overlay_cursor(frame, cursor_img)
                else:
                    # 无叠加层时直接写采集缓冲（bytes 或 ndarray），省大块内存拷贝
                    frame = shot

                now2 = time.monotonic()
                target2 = int((now2 - anchor - paused_acc) * fps) + 1
                self._write_frame(frame)
                lag = max(0, target2 - self.frames)
                # 编码吞吐远低于设定帧率时不再补重复帧（越补越慢），
                # 收尾时按真实时长校正时间戳，保证播放速度正确。
                if lag > fps * 2:
                    continue
                for _ in range(min(lag, fps * 2)):
                    if self._stop.is_set() or self._paused.is_set():
                        break
                    self._write_frame(frame)
        except Exception as exc:  # 编码/写入异常：结束录制并上报
            if not self._stop.is_set():
                self._error = f"采集线程异常：{exc}"
            self._stop.set()
            self._recording.clear()
        finally:
            if cam is not None:
                try:
                    cam.release()
                except Exception:
                    pass
            try:
                source.close()
            except Exception:
                pass

    def _write_frame(self, frame):
        if self._stop.is_set():
            return          # 已请求停止：丢弃剩余帧，避免写已关闭的管道
        proc = self._proc
        if not proc or not proc.stdin:
            raise RecorderError("编码进程未启动")
        rc = proc.poll()
        if rc is not None:
            err = self._read_stderr(proc)
            raise RecorderError(f"FFmpeg 退出（{rc}）：{err}")
        # ndarray 用 memoryview 直接写管道，避免再生成一份整帧 bytes
        data = frame if isinstance(frame, (bytes, bytearray)) else memoryview(frame).cast("B")
        try:
            proc.stdin.write(data)
        except (ValueError, OSError) as exc:
            raise RecorderError(f"写入编码管道失败：{exc}") from exc
        self.frames += 1

    def _start_stderr_drain(self, proc):
        """持续抽干 FFmpeg stderr，避免管道写满导致编码进程卡死。"""
        def _drain():
            try:
                while True:
                    chunk = proc.stderr.read(4096)
                    if not chunk:
                        break
                    with self._stderr_lock:
                        self._stderr_buf.extend(chunk)
                        if len(self._stderr_buf) > 8192:
                            del self._stderr_buf[:-8192]
            except Exception:
                pass
        threading.Thread(target=_drain, daemon=True,
                         name="ffmpeg-stderr").start()

    def _read_stderr(self, proc=None) -> str:
        with self._stderr_lock:
            tail = bytes(self._stderr_buf[-400:])
        if tail:
            return tail.decode("utf-8", "ignore")
        # 抽干线程已接管管道；仅在进程确定退出（管道已关闭）时回退补读，避免阻塞
        if proc is None or proc.poll() is None or not proc.stderr:
            return ""
        try:
            return proc.stderr.read().decode("utf-8", "ignore")[-400:]
        except Exception:
            return ""

    # ------------------------------------------------------------ 叠加层
    @staticmethod
    def _render_cursor():
        img = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.polygon([(2, 2), (2, 18), (7, 14), (10, 21), (13, 20), (10, 13), (17, 13)],
                  fill=(255, 255, 255, 235), outline=(0, 0, 0, 255))
        return np.array(img)

    def _render_watermark(self):
        text = (self.s.watermark_text or "").strip()[:64]
        if not text:
            return None
        img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
        except Exception:
            font = ImageFont.load_default()
        box = d.textbbox((0, 0), text, font=font)
        w, h = box[2] - box[0] + 16, box[3] - box[1] + 12
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((8, 4), text, font=font, fill=(255, 255, 255, 210),
               stroke_width=2, stroke_fill=(0, 0, 0, 160))
        return np.array(img)

    @staticmethod
    def _overlay_rgba(frame: np.ndarray, rgba: np.ndarray, right=12, top=None) -> np.ndarray:
        h, w = frame.shape[:2]
        oh, ow = rgba.shape[:2]
        if oh >= h or ow >= w:
            return frame
        x = w - ow - right
        y = h - oh - 12 if top is None else top
        if x < 0 or y < 0:
            return frame          # 画面太小放不下叠加层：直接跳过，避免负切片报错
        roi = frame[y:y + oh, x:x + ow]
        alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
        # 叠加图是 RGBA，帧缓冲是 BGRA，需要交换 R/B 通道
        rgb = rgba[:, :, 2::-1].astype(np.float32)
        roi[:, :, :3] = (roi[:, :, :3].astype(np.float32) * (1 - alpha) + rgb * alpha).astype(np.uint8)
        return frame

    def _overlay_cursor(self, frame: np.ndarray, cursor: np.ndarray) -> np.ndarray:
        cx, cy = _cursor_pos()
        bbox = self._current_bbox()
        x = cx - bbox[0] - 2
        y = cy - bbox[1] - 2
        h, w = frame.shape[:2]
        oh, ow = cursor.shape[:2]
        if 0 <= x <= w - ow and 0 <= y <= h - oh:
            roi = frame[y:y + oh, x:x + ow]
            alpha = cursor[:, :, 3:4].astype(np.float32) / 255.0
            roi[:, :, :3] = (roi[:, :, :3].astype(np.float32) * (1 - alpha) +
                             cursor[:, :, :3].astype(np.float32) * alpha).astype(np.uint8)
        return frame

    def _overlay_camera(self, frame: np.ndarray, cam) -> np.ndarray:
        try:
            ok, img = cam.read()
            if not ok or img is None:
                return frame
            import cv2
            target_w = int(self.s.camera_size)
            h, w = img.shape[:2]
            target_h = int(h * target_w / w) if w else target_w
            img = cv2.resize(img, (target_w, target_h))
            img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
            fh, fw = frame.shape[:2]
            if target_h >= fh or target_w >= fw:
                return frame
            x = fw - target_w - 16
            y = fh - target_h - 16
            frame[y:y + target_h, x:x + target_w] = img
        except Exception:
            pass
        return frame

    # ------------------------------------------------------------ 音频线程
    def _audio_loop(self):
        com_ready = False
        try:
            # WASAPI 需要在线程内初始化 COM
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)
            com_ready = True
        except Exception:
            com_ready = False
        try:
            # 长时间录制时视频线程会占满 CPU，音频线程被饿死就会丢块
            # （soundcard 报 data discontinuity），导致成片声音比画面短。
            k32 = ctypes.windll.kernel32
            k32.GetCurrentThread.restype = ctypes.c_void_p
            k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            k32.SetThreadPriority(k32.GetCurrentThread(), 2)  # HIGHEST
        except Exception:
            pass
        wf = None
        try:
            if not (self.s.mic_enabled or self.s.system_audio_enabled):
                return      # 用户主动关闭了声音，不当作采集失败
            import soundcard as sc
            mic = None
            loop = None
            if self.s.mic_enabled:
                mic = (sc.get_microphone(self.s.mic_device, include_loopback=False)
                       if self.s.mic_device else sc.default_microphone())
            if self.s.system_audio_enabled:
                spk = sc.default_speaker()
                if spk is not None:
                    loop = sc.get_microphone(spk.name, include_loopback=True)
            if mic is None and loop is None:
                self._audio_error = "没有可用音频设备"
                return
            rate = 48000
            self._audio_parts = [self._audio_tmp]
            wf = self._open_wav(self._audio_tmp, rate)
            self._audio_ok = True
            mic_ctx = mic.recorder(samplerate=rate, blocksize=960) if mic else None
            loop_ctx = loop.recorder(samplerate=rate, blocksize=960) if loop else None
            try:
                if mic_ctx:
                    mic_ctx.__enter__()
                if loop_ctx:
                    loop_ctx.__enter__()
                # 音频设备启动比视频慢，开头补静音对齐时间轴
                anchor = self._start_mono or time.monotonic()
                written = 0
                seg_written = 0
                if self._start_mono:
                    delay = min(5.0, max(0.0, time.monotonic() - self._start_mono))
                    pad = int(delay * rate) * 4  # 双声道 16bit
                    if pad > 0:
                        wf.writeframes(b"\x00" * pad)
                    written = int(delay * rate)
                    seg_written = written * 4
                paused_acc = 0.0
                pause_at = None
                while not self._stop.is_set():
                    if self._paused.is_set():
                        if pause_at is None:
                            pause_at = time.monotonic()
                        time.sleep(0.02)
                        continue
                    if pause_at is not None:
                        paused_acc += time.monotonic() - pause_at
                        pause_at = None
                    a = mic_ctx.record(numframes=960) if mic_ctx else None
                    b = loop_ctx.record(numframes=960) if loop_ctx else None
                    mix = self._mix(a, b)
                    if mix is not None:
                        wf.writeframes(mix.tobytes())
                        written += len(mix)
                        seg_written += len(mix) * 4
                    # 采集丢块时按真实时间补静音，避免成片声音越来越滞后
                    # （19 分钟实测可差 39 秒）。留 100 ms 余量给设备缓冲，
                    # 避免把正常的采集延迟误判为丢块。
                    want = int((time.monotonic() - anchor - paused_acc) * rate)
                    gap = want - written
                    if gap > int(rate * 0.2):
                        pad = gap - int(rate * 0.1)
                        wf.writeframes(b"\x00" * pad * 4)
                        written += pad
                        seg_written += pad * 4
                    # WAV 头只有 32 位长度：到 3.8 GB 就换下一个分片，
                    # 否则 close() 会抛错并丢掉整段音频（约 6.2 小时处）。
                    if seg_written >= WAV_SEGMENT_BYTES:
                        wf.close()
                        wf = self._open_wav(self._next_audio_part(), rate)
                        seg_written = 0
            finally:
                if mic_ctx:
                    try:
                        mic_ctx.__exit__(None, None, None)
                    except Exception:
                        pass
                if loop_ctx:
                    try:
                        loop_ctx.__exit__(None, None, None)
                    except Exception:
                        pass
        except Exception as exc:
            self._audio_error = str(exc)
            if not self._has_audio_data():
                self._audio_ok = False
        finally:
            if wf is not None:
                try:
                    wf.close()
                except Exception as exc:
                    self._audio_error = self._audio_error or str(exc)
                    if not self._has_audio_data():
                        self._audio_ok = False
            if com_ready:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass

    @staticmethod
    def _open_wav(path: str, rate: int):
        wf = wave.open(path, "wb")
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        return wf

    def _next_audio_part(self) -> str:
        with self._lock:
            index = len(self._audio_parts) + 1
            path = os.path.join(self._out_dir, f".{self._title}.part{index}.wav")
            self._audio_parts.append(path)
            return path

    def _audio_files(self) -> list[str]:
        """按录制顺序返回有效的音频分片。"""
        with self._lock:
            parts = list(self._audio_parts) or [self._audio_tmp]
        result = []
        for p in parts:
            try:
                if os.path.exists(p) and os.path.getsize(p) > 44:
                    result.append(p)
            except OSError:
                pass
        return result

    def _has_audio_data(self) -> bool:
        with self._lock:
            parts = list(self._audio_parts) or [self._audio_tmp]
        for p in parts:
            try:
                if os.path.exists(p) and os.path.getsize(p) > 1024:
                    return True
            except OSError:
                pass
        return False

    @staticmethod
    def _mix(a, b):
        def to2(x):
            if x is None or len(x) == 0:
                return None
            x = np.asarray(x, dtype=np.float32)
            if x.ndim == 1:
                x = x[:, None]
            if x.shape[1] == 1:
                x = np.repeat(x, 2, axis=1)
            elif x.shape[1] > 2:
                x = x[:, :2]
            return x
        a2, b2 = to2(a), to2(b)
        if a2 is None and b2 is None:
            return None
        if a2 is None:
            mix = b2
        elif b2 is None:
            mix = a2
        else:
            n = min(len(a2), len(b2))
            mix = a2[:n] + b2[:n]
        mix = np.clip(mix, -1.0, 1.0)
        return (mix * 32767).astype(np.int16)

    # ------------------------------------------------------------ 收尾
    def _mux_av(self):
        parts = self._audio_files()
        if not parts:
            raise RecorderError("没有可用的音频数据")
        self._mux_started = True
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]
        if self.pacing_scale != 1.0:
            # 只缩放视频输入的时间戳，音频保持真实时长，音画依然对齐
            cmd += ["-itsscale", f"{self.pacing_scale:.6f}"]
        cmd += ["-i", self._video_tmp]
        for i, part in enumerate(parts):
            if i == 0 and self._audio_offset:
                # 偏移代表硬件采集预热，只需作用于第一片；每片都加会在段间插入空隙
                cmd += ["-itsoffset", f"{self._audio_offset:.3f}"]
            cmd += ["-i", part]
        if len(parts) == 1:
            cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", self._final]
        else:
            # 多个音频分片（超 4 GiB 自动切分）：先按顺序拼接再编码
            chain = "".join(f"[{i + 1}:a]" for i in range(len(parts)))
            cmd += ["-filter_complex",
                    f"{chain}concat=n={len(parts)}:v=0:a=1[a]",
                    "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", self._final]
        proc = subprocess.run(cmd, capture_output=True,
                              creationflags=0x08000000)
        if proc.returncode != 0 or not os.path.exists(self._final):
            err = proc.stderr.decode("utf-8", "ignore")[-300:]
            raise RecorderError(f"音视频合成失败：{err}")

    def _encode_audio_only(self) -> str:
        final = os.path.join(self._out_dir, self._title + "." + self.s.audio_only_format)
        parts = self._audio_files()
        if not (self._audio_ok and parts):
            self._cleanup_temps()
            raise RecorderError(f"音频采集失败：{self._audio_error or '无音频数据'}")
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]
        for part in parts:
            cmd += ["-i", part]
        if len(parts) > 1:
            chain = "".join(f"[{i}:a]" for i in range(len(parts)))
            cmd += ["-filter_complex",
                    f"{chain}concat=n={len(parts)}:v=0:a=1[a]", "-map", "[a]"]
        cmd += ["-c:a", "aac", "-b:a", "192k", final]
        proc = subprocess.run(cmd, capture_output=True,
                              creationflags=0x08000000)
        if proc.returncode != 0:
            # 编码失败：把全部分片改成可见文件名保留，避免隐藏分片长期占用磁盘
            first = os.path.join(self._out_dir, self._title + ".wav")
            _replace_with_retry(parts[0], first)
            for i, part in enumerate(parts[1:], start=2):
                try:
                    _replace_with_retry(
                        part, os.path.join(self._out_dir, f"{self._title}.part{i}.wav"))
                except Exception:
                    pass
            return first
        self._cleanup_temps()
        return final

    def _cleanup_temps(self):
        with self._lock:
            targets = [self._video_tmp, self._audio_tmp, *self._audio_parts]
        for p in targets:
            try:
                if os.path.exists(p) and p != self._final:
                    os.remove(p)
            except Exception:
                pass


# ---------------------------------------------------------------- 后处理工具
def unique_path(path: str) -> str:
    """目标文件已存在时追加 _2、_3…，避免静默覆盖已有文件。"""
    base, ext = os.path.splitext(path)
    candidate, n = path, 1
    while os.path.exists(candidate):
        n += 1
        candidate = f"{base}_{n}{ext}"
    return candidate


def unique_title(out_dir: str, title: str, audio_fmt: str = "m4a") -> str:
    """成品名冲突时追加 _2、_3…，避免覆盖同名旧录像（mp4/mkv/wav/音频）。"""
    exts = (".mp4", ".mkv", ".wav", "." + audio_fmt.lstrip(".").lower())
    candidate, n = title, 1
    while any(os.path.exists(os.path.join(out_dir, candidate + ext))
              for ext in exts):
        n += 1
        candidate = f"{title}_{n}"
    return candidate


def trim_video(src: str, start: float, end: float, dst: str) -> str:
    duration = max(0.2, end - start)
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", src,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dst]
    proc = subprocess.run(cmd, capture_output=True, creationflags=0x08000000)
    if proc.returncode != 0:
        raise RecorderError(proc.stderr.decode("utf-8", "ignore")[-400:])
    return dst


def repair_video(src: str, dst: str) -> str:
    attempts = [
        ["-err_detect", "ignore_err", "-i", src, "-c", "copy",
         "-movflags", "+faststart", dst],
        ["-fflags", "+genpts+discardcorrupt", "-i", src, "-c", "copy", dst],
        ["-err_detect", "ignore_err", "-i", src,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-movflags", "+faststart", dst],
    ]
    last = ""
    for args in attempts:
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args]
        proc = subprocess.run(cmd, capture_output=True, creationflags=0x08000000)
        if proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 1024:
            return dst
        last = proc.stderr.decode("utf-8", "ignore")[-400:]
    raise RecorderError(f"修复失败：{last}")
