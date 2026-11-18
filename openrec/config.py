"""应用配置：纯本地 JSON，不含任何账号或网络配置。"""
import json
import os
import sys
from dataclasses import dataclass, asdict, field
from typing import ClassVar

APP_NAME = "OpenRec"
APP_VERSION = "1.6.8"
if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
    _bundle = getattr(sys, "_MEIPASS", "")
    BIN_DIR = os.path.join(_bundle, "bin") if _bundle and os.path.isdir(os.path.join(_bundle, "bin")) \
        else os.path.join(ROOT, "bin")
    # 安装版可能装在 Program Files（只读）：配置与录像改放用户目录，
    # 源码运行时仍沿用项目目录，行为不变。
    _user_root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    DATA_DIR = os.path.join(_user_root, APP_NAME, "data")
    DEFAULT_OUTPUT_DIR = os.path.join(os.path.expanduser("~"), "Videos", APP_NAME)
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BIN_DIR = os.path.join(ROOT, "bin")
    DATA_DIR = os.path.join(ROOT, "data")
    DEFAULT_OUTPUT_DIR = os.path.join(ROOT, "recordings")
MIN_FREE_BYTES = 2 * 1024 ** 3   # 低于 2 GB 不允许开始录制；录制中到此阈值自动停止


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)


def icon_path() -> str:
    """应用图标：源码运行取 docs/，打包后取解包目录 docs/。"""
    bases = []
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        bases.append(os.path.join(bundle, "docs"))
    bases.append(os.path.join(ROOT, "docs"))
    for base in bases:
        path = os.path.join(base, "OpenRec.ico")
        if os.path.exists(path):
            return path
    return ""


def _migrate_legacy_settings():
    """安装版升级：把旧版留在程序目录的配置搬到用户目录。"""
    dst = os.path.join(DATA_DIR, "settings.json")
    if os.path.exists(dst) or not getattr(sys, "frozen", False):
        return
    legacy = os.path.join(ROOT, "data", "settings.json")
    if os.path.realpath(legacy) == os.path.realpath(dst) or not os.path.exists(legacy):
        return
    try:
        with open(legacy, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # 旧默认目录若不可写（例如装在 Program Files），改用用户的视频目录
        old_out = raw.get("output_dir")
        if old_out and not os.access(old_out, os.W_OK):
            raw["output_dir"] = DEFAULT_OUTPUT_DIR
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@dataclass
class Settings:
    source_mode: str = "full"          # full | region | window | audio
    monitor_id: str = ""               # mss 显示器的 unique_id，空 = 按序号/主屏
    monitor_index: int = -1             # 物理显示器序号（0 起）；-1 = 自动主屏
    monitor_all: bool = False           # True = 录制所有屏幕的拼接画面
    region: list = field(default_factory=lambda: [0, 0, 0, 0])
    window_title: str = ""
    fps: int = 30
    codec: str = "libx264"             # libx264 | h264_nvenc | h264_qsv | h264_amf
    quality: int = 23                  # CRF/量化值，越小越清晰
    mic_enabled: bool = True
    system_audio_enabled: bool = True
    mic_device: str = ""               # 空 = 系统默认麦克风
    camera_enabled: bool = False
    camera_index: int = 0
    camera_size: int = 320             # 摄像头叠加宽度（像素）
    cursor_enabled: bool = True
    rec_badge_enabled: bool = True      # 录制时在右上角叠加闪烁 REC 角标
    countdown: int = 3
    watermark_text: str = ""
    output_dir: str = DEFAULT_OUTPUT_DIR
    hotkey_start: str = "Ctrl+Alt+R"
    hotkey_pause: str = "Ctrl+Alt+P"
    share_port: int = 8080
    audio_only_format: str = "m4a"

    @classmethod
    def path(cls):
        return os.path.join(DATA_DIR, "settings.json")

    load_warning: ClassVar[str] = ""

    _ALLOWED: ClassVar[dict] = {
        "source_mode": {"full", "region", "window", "audio"},
        "codec": {"libx264", "h264_nvenc", "h264_qsv", "h264_amf"},
        "audio_only_format": {"m4a"},
    }
    _RANGES: ClassVar[dict] = {
        "fps": (5, 120), "quality": (1, 51), "camera_index": (0, 9),
        "camera_size": (120, 800), "countdown": (0, 10),
        "share_port": (1024, 65535), "monitor_index": (-1, 32),
    }

    @classmethod
    def _sanitize(cls, raw: dict) -> dict:
        """按默认值的类型校验配置，防止手改 JSON 导致启动崩溃或参数注入。"""
        defaults = cls()
        clean = {}
        for name in cls.__dataclass_fields__:
            if name not in raw:
                continue
            value = raw[name]
            default = getattr(defaults, name)
            if name == "region":
                if (isinstance(value, list) and len(value) == 4
                        and all(isinstance(v, int) and not isinstance(v, bool)
                                for v in value)):
                    clean[name] = value
                continue
            if isinstance(default, bool):
                if isinstance(value, bool):
                    clean[name] = value
                continue
            if isinstance(default, int):
                if isinstance(value, int) and not isinstance(value, bool):
                    lo, hi = cls._RANGES.get(name, (None, None))
                    if lo is not None:
                        value = min(max(value, lo), hi)
                    clean[name] = value
                continue
            if isinstance(default, str) and isinstance(value, str):
                if name in cls._ALLOWED and value not in cls._ALLOWED[name]:
                    continue
                if name == "watermark_text":
                    value = value[:64]
                elif name == "output_dir" and not value.strip():
                    continue
                else:
                    value = value[:1024]
                clean[name] = value
        return clean

    @classmethod
    def load(cls) -> "Settings":
        _ensure_dirs()
        _migrate_legacy_settings()
        cls.load_warning = ""
        try:
            with open(cls.path(), "r", encoding="utf-8") as f:
                raw = json.load(f)
            return cls(**cls._sanitize(raw))
        except FileNotFoundError:
            return cls()
        except Exception as exc:
            # 配置损坏：先备份再回退默认，避免用户设置被静默覆盖
            backup = cls.path() + ".broken"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(cls.path(), backup)
            except OSError:
                backup = ""
            cls.load_warning = (
                f"配置文件读取失败（{type(exc).__name__}），已使用默认设置"
                + (f"，原文件备份为 {os.path.basename(backup)}" if backup else "") + "。")
            return cls()

    def save(self):
        """原子写入：先写临时文件再替换，避免异常时留下半截 JSON。"""
        _ensure_dirs()
        tmp = self.path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path())


def ffmpeg_path() -> str:
    p = os.path.join(BIN_DIR, "ffmpeg.exe")
    return p if os.path.exists(p) else "ffmpeg"


def ffprobe_path() -> str:
    p = os.path.join(BIN_DIR, "ffprobe.exe")
    return p if os.path.exists(p) else "ffprobe"
