"""OpenRec 主界面与交互。"""
import io
import os
import platform
import subprocess
import time

import qrcode
from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QSize, QRect
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction, QGuiApplication
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox, QTableWidget,
    QTableWidgetItem, QFileDialog, QMessageBox, QDialog, QDialogButtonBox,
    QLineEdit, QFormLayout, QSystemTrayIcon, QMenu, QInputDialog, QHeaderView,
    QProgressDialog, QTextEdit, QGroupBox, QAbstractItemView, QApplication, QToolTip,
)

from .config import (
    Settings, ffmpeg_path, MIN_FREE_BYTES, icon_path, APP_VERSION, DATA_DIR,
)
from .hotkeys import HotkeyManager, validate_bindings
from .overlays import RegionOverlay, CountdownOverlay, Whiteboard, RecIndicator
from .recorder import (
    Recorder, RecorderError, list_windows, available_encoders, probe_duration,
    trim_video, repair_video, list_monitors, pick_monitor, monitor_label,
    virtual_monitor, free_bytes, estimate_bitrate, ddagrab_available,
    unique_path,
)
from .share import ShareServer, lan_ip

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".gif"}
AUDIO_EXT = {".m4a", ".wav", ".mp3"}


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}TB"


def human_duration(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def human_span(sec: float) -> str:
    sec = int(max(0, sec or 0))
    if sec >= 3600:
        return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"
    if sec >= 60:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec} 秒"


class Job(QThread):
    finished_ok = Signal(bool, object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.finished_ok.emit(True, self._fn())
        except Exception as exc:
            self.finished_ok.emit(False, str(exc))


class HotkeyEdit(QLineEdit):
    """点击后按下组合键即可录入全局快捷键，例如 Ctrl+Alt+R。"""

    _MOD_KEYS = {Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta}
    _NAMES = {
        Qt.Key_Space: "SPACE", Qt.Key_Escape: "ESC", Qt.Key_Return: "ENTER",
        Qt.Key_Enter: "ENTER", Qt.Key_Tab: "TAB", Qt.Key_Print: "PRINTSCREEN",
        Qt.Key_Pause: "PAUSE",
        **{getattr(Qt, f"Key_{chr(c)}"): chr(c) for c in range(ord("A"), ord("Z") + 1)},
        **{getattr(Qt, f"Key_{d}"): str(d) for d in range(10)},
        **{getattr(Qt, f"Key_F{i}"): f"F{i}" for i in range(1, 13)},
    }

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("点击后按下快捷键")
        self.setToolTip("点击输入框，按下 Ctrl / Alt / Shift / Win + 按键；Backspace 清除")
        self.setText(text)

    def keyPressEvent(self, e):
        if e.isAutoRepeat():
            return
        if e.key() in (Qt.Key_Backspace, Qt.Key_Delete):
            self.clear()
            return
        if e.key() == Qt.Key_Escape:
            self.clearFocus()
            return
        if e.key() in self._MOD_KEYS:
            return
        name = self._NAMES.get(e.key())
        if name is None:
            text = (e.text() or "").strip().upper()
            if len(text) == 1 and text in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
                name = text
        if name is None:
            return
        mods = e.modifiers()
        names = []
        if mods & Qt.ControlModifier:
            names.append("Ctrl")
        if mods & Qt.AltModifier:
            names.append("Alt")
        if mods & Qt.ShiftModifier:
            names.append("Shift")
        if mods & Qt.MetaModifier:
            names.append("Win")
        if not names:
            QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()),
                              "请至少包含 Ctrl、Alt、Shift 或 Win 之一", self)
            return
        self.setText("+".join(names + [name]))


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.s = settings
        self.recorder: Recorder | None = None
        self.share = ShareServer(settings.output_dir)
        self.whiteboard: Whiteboard | None = None
        self.rec_indicator = RecIndicator()
        self._countdown: CountdownOverlay | None = None
        self._region_dialog: RegionOverlay | None = None
        self._job: Job | None = None
        self._stop_job: Job | None = None
        self._probe_job: Job | None = None
        self._enc_job: Job | None = None
        self._encoders: set[str] | None = None   # None = 探测中
        self._dur_cache: dict[tuple, float] = {}
        self._stopping = False
        self._save_ctx: tuple | None = None
        self._save_progress = None
        self._quitting = False
        self._cleaned = False

        self.setWindowTitle(f"OpenRec · 本地录屏 v{APP_VERSION}")
        self.resize(1000, 760)
        self._build_ui()
        self._build_tray()
        self._setup_hotkeys()
        self._refresh_windows()
        self.refresh_library()
        self._start_encoder_probe()

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._est_timer = QTimer(self)
        self._est_timer.setInterval(3000)
        self._est_timer.timeout.connect(self._update_estimate)
        self._est_timer.start()
        self._update_estimate()
        QApplication.instance().aboutToQuit.connect(self._cleanup)
        QApplication.instance().screenAdded.connect(lambda *_: self._load_monitors())
        QApplication.instance().screenRemoved.connect(lambda *_: self._load_monitors())

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("OpenRec")
        title.setObjectName("title")
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setObjectName("status")
        head.addWidget(self.lbl_status)
        root.addLayout(head)

        # 录制源
        src_box = QGroupBox("录制源")
        src = QGridLayout(src_box)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("全屏", "full")
        self.cmb_mode.addItem("自定义区域", "region")
        self.cmb_mode.addItem("指定窗口", "window")
        self.cmb_mode.addItem("仅音频", "audio")
        idx = self.cmb_mode.findData(self.s.source_mode)
        self.cmb_mode.setCurrentIndex(max(0, idx))
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_change)
        self.btn_region = QPushButton("选择区域")
        self.btn_region.clicked.connect(self._pick_region)
        self.cmb_window = QComboBox()
        self.cmb_window.setMinimumWidth(320)
        self.cmb_monitor = QComboBox()
        self.cmb_monitor.setMinimumWidth(260)
        self.cmb_monitor.currentIndexChanged.connect(self._on_monitor_change)
        btn_refresh = QPushButton("刷新列表")
        btn_refresh.clicked.connect(self._refresh_sources)
        self.lbl_region = QLabel("")
        self.lbl_region.setObjectName("hint")
        self.lbl_monitor = QLabel("")
        self.lbl_monitor.setObjectName("hint")
        src.addWidget(QLabel("模式"), 0, 0)
        src.addWidget(self.cmb_mode, 0, 1)
        src.addWidget(self.btn_region, 0, 2)
        src.addWidget(self.cmb_window, 0, 3, 1, 2)
        src.addWidget(btn_refresh, 0, 5)
        src.addWidget(QLabel("屏幕"), 1, 0)
        src.addWidget(self.cmb_monitor, 1, 1, 1, 2)
        src.addWidget(self.lbl_monitor, 1, 3, 1, 3)
        src.addWidget(QLabel("窗口"), 2, 0)
        src.addWidget(self.lbl_region, 2, 1, 1, 5)
        self._load_monitors()
        root.addWidget(src_box)

        # 参数
        par_box = QGroupBox("参数")
        par = QVBoxLayout(par_box)
        par.setSpacing(9)
        self.spin_fps = QSpinBox(); self.spin_fps.setRange(5, 120); self.spin_fps.setValue(self.s.fps)
        self.spin_fps.setToolTip("最高 120 fps；实际帧率受采集方式与硬件性能限制")
        self.cmb_codec = QComboBox()
        # 先列出全部编码器，硬件探测放到后台线程，避免启动时界面卡住 1~2 秒
        for label, name in [("软件 H.264 (x264)", "libx264"),
                            ("NVIDIA NVENC", "h264_nvenc"),
                            ("Intel QSV", "h264_qsv"),
                            ("AMD AMF", "h264_amf")]:
            self.cmb_codec.addItem(label, name)
        ci = self.cmb_codec.findData(self.s.codec)
        self.cmb_codec.setCurrentIndex(max(0, ci))
        self.cmb_quality = QComboBox()
        for label, q in [("高（CRF 18）", 18), ("中（CRF 23）", 23), ("低（CRF 28）", 28)]:
            self.cmb_quality.addItem(label, q)
        qi = self.cmb_quality.findData(self.s.quality)
        self.cmb_quality.setCurrentIndex(max(0, qi))
        self.chk_mic = QCheckBox("麦克风"); self.chk_mic.setChecked(self.s.mic_enabled)
        self.cmb_mic = QComboBox(); self.cmb_mic.setMinimumWidth(220)
        self._load_mics()
        self.chk_sys = QCheckBox("系统声音"); self.chk_sys.setChecked(self.s.system_audio_enabled)
        self.chk_cam = QCheckBox("摄像头"); self.chk_cam.setChecked(self.s.camera_enabled)
        self.chk_cursor = QCheckBox("录制鼠标"); self.chk_cursor.setChecked(self.s.cursor_enabled)
        self.chk_badge = QCheckBox("REC 水印"); self.chk_badge.setChecked(self.s.rec_badge_enabled)
        self.spin_countdown = QSpinBox(); self.spin_countdown.setRange(0, 10)
        self.spin_countdown.setValue(self.s.countdown); self.spin_countdown.setSuffix(" 秒")
        # 第一行：画面参数，控件保持自然宽度，多余空间留白
        r1 = QHBoxLayout()
        r1.setSpacing(10)
        r1.addWidget(QLabel("帧率"))
        self.spin_fps.setFixedWidth(104)
        r1.addWidget(self.spin_fps)
        r1.addSpacing(22)
        r1.addWidget(QLabel("编码器"))
        self.cmb_codec.setMinimumWidth(190)
        r1.addWidget(self.cmb_codec)
        r1.addSpacing(22)
        r1.addWidget(QLabel("画质"))
        self.cmb_quality.setMinimumWidth(140)
        r1.addWidget(self.cmb_quality)
        r1.addStretch(1)
        par.addLayout(r1)

        # 第二行：音频输入
        r2 = QHBoxLayout()
        r2.setSpacing(10)
        r2.addWidget(self.chk_mic)
        r2.addWidget(self.cmb_mic)
        r2.addStretch(1)
        par.addLayout(r2)

        # 第三行：附加采集与倒计时
        r3 = QHBoxLayout()
        r3.setSpacing(18)
        r3.addWidget(self.chk_sys)
        r3.addWidget(self.chk_cam)
        r3.addWidget(self.chk_cursor)
        r3.addSpacing(22)
        r3.addWidget(QLabel("倒计时"))
        self.spin_countdown.setFixedWidth(104)
        r3.addWidget(self.spin_countdown)
        r3.addStretch(1)
        par.addLayout(r3)

        # 第四行：水印
        r4 = QHBoxLayout()
        r4.setSpacing(10)
        r4.addWidget(self.chk_badge)
        badge_hint = QLabel("录制时在画面右上角显示红点闪烁的 REC，成片保留")
        badge_hint.setObjectName("hint")
        r4.addWidget(badge_hint)
        r4.addStretch(1)
        par.addLayout(r4)

        self.lbl_estimate = QLabel("")
        self.lbl_estimate.setObjectName("hint")
        par.addWidget(self.lbl_estimate)
        root.addWidget(par_box)

        for sig in (self.spin_fps.valueChanged, self.cmb_codec.currentIndexChanged,
                    self.cmb_quality.currentIndexChanged,
                    self.cmb_monitor.currentIndexChanged, self.cmb_mode.currentIndexChanged,
                    self.chk_mic.toggled, self.chk_sys.toggled):
            sig.connect(lambda *_: self._update_estimate())

        # 控制区：按功能分类分组 —— 录制 / 标注分享 / 工具
        ctrl = QHBoxLayout()
        ctrl.setSpacing(24)

        grp_rec = QHBoxLayout()
        self.btn_start = QPushButton("开始录制")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.toggle_record)
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_pause.setEnabled(False)
        self.lbl_timer = QLabel("00:00:00")
        self.lbl_timer.setObjectName("timer")
        grp_rec.addWidget(self.btn_start, 0)
        grp_rec.addWidget(self.btn_pause, 0)
        grp_rec.addSpacing(12)
        grp_rec.addWidget(self.lbl_timer)

        grp_tools = QHBoxLayout()
        self.btn_board = QPushButton("白板标注")
        self.btn_board.clicked.connect(self.open_whiteboard)
        self.btn_share = QPushButton("局域网分享")
        self.btn_share.clicked.connect(self.toggle_share)
        grp_tools.addWidget(self.btn_board)
        grp_tools.addWidget(self.btn_share)

        grp_util = QHBoxLayout()
        self.btn_settings = QPushButton("设置")
        self.btn_settings.clicked.connect(self.open_settings)
        grp_util.addWidget(self.btn_settings)

        ctrl.addLayout(grp_rec)
        ctrl.addLayout(grp_tools)
        ctrl.addLayout(grp_util)
        ctrl.addStretch(1)
        self.lbl_hotkeys = QLabel("")
        self.lbl_hotkeys.setObjectName("hint")
        ctrl.addWidget(self.lbl_hotkeys)
        root.addLayout(ctrl)

        # 录制库
        lib_box = QGroupBox("录制库")
        lib = QVBoxLayout(lib_box)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["文件", "时长", "大小", "修改时间"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(lambda *_: self.play_selected())
        lib.addWidget(self.table)

        row = QHBoxLayout()
        # 录制库操作分类：查看 / 加工 / 文件管理
        grp_view = QHBoxLayout()
        grp_edit = QHBoxLayout()
        grp_file = QHBoxLayout()
        for text, fn, grp in [("播放", self.play_selected, grp_view),
                              ("剪辑", self.trim_selected, grp_edit),
                              ("修复", self.repair_selected, grp_edit),
                              ("重命名", self.rename_selected, grp_file),
                              ("删除", self.delete_selected, grp_file),
                              ("打开目录", self.open_folder, grp_file),
                              ("刷新", self.refresh_library, grp_file)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            grp.addWidget(b)
        for grp in (grp_view, grp_edit, grp_file):
            row.addLayout(grp)
            row.addSpacing(16)
        row.addStretch(1)
        self.lbl_lib = QLabel("")
        self.lbl_lib.setObjectName("hint")
        row.addWidget(self.lbl_lib)
        lib.addLayout(row)
        root.addWidget(lib_box, 1)

        self._on_mode_change()

    # ------------------------------------------------------------ 关于
    @staticmethod
    def _ffmpeg_version() -> str:
        """读取随包 FFmpeg 的实际版本，避免在界面里写死版本号。"""
        try:
            proc = subprocess.run(
                [ffmpeg_path(), "-version"], capture_output=True, text=True,
                timeout=3, creationflags=0x08000000)
            parts = (proc.stdout or "").splitlines()[0].split()
            return parts[2] if len(parts) > 2 else ""
        except Exception:
            return ""

    def _about_info(self) -> list[tuple[str, str]]:
        """按开源软件惯例列出的版本与归属信息。"""
        ff_ver = self._ffmpeg_version()
        ffmpeg = (f"FFmpeg {ff_ver}（随包分发，GPL-3.0）" if ff_ver
                  else "FFmpeg（随包分发，GPL-3.0）")
        return [
            ("软件", "OpenRec · 本地录屏"),
            ("版本", APP_VERSION),
            ("作者", "C.C."),
            ("版权", "Copyright © 2026 C.C."),
            ("许可证", "MIT License（详见程序目录 LICENSE）"),
            ("运行环境", f"Windows {platform.release()} · Python {platform.python_version()}"
                        f" · PySide6 {PYSIDE_VERSION}"),
            ("视频编码", ffmpeg),
            ("数据目录", DATA_DIR),
            ("默认保存", self.s.output_dir),
        ]

    def _build_about_section(self) -> QGroupBox:
        """设置对话框中的「关于」区块。"""
        info = QGroupBox("关于")
        lay = QVBoxLayout(info)
        grid = QGridLayout()
        for i, (key, value) in enumerate(self._about_info()):
            k = QLabel(key)
            k.setObjectName("hint")
            v = QLabel(value)
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(k, i, 0, Qt.AlignTop)
            grid.addWidget(v, i, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        text = QLabel(
            "<p><b>第三方组件与许可</b></p>"
            "<ul style='margin-left:16px'>"
            "<li>PySide6 / Qt for Python — LGPL-3.0-only / GPL-2.0-only / GPL-3.0-only（三选一）</li>"
            "<li>FFmpeg（gyan.dev essentials 构建）— GPL-3.0，全文见 LICENSES\\GPL-3.0.txt</li>"
            "<li>NumPy — BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0</li>"
            "<li>OpenCV（opencv-python-headless）— Apache-2.0</li>"
            "<li>mss、DXcam — MIT</li>"
            "<li>SoundCard、qrcode — BSD-3-Clause</li>"
            "<li>Pillow — MIT-CMU（HPND）</li>"
            "</ul>"
            "<p>随包 LICENSES\\ 目录提供 GPL-3.0 全文及 FFmpeg 版权与源码获取说明；"
            "本项目仅通过外部进程调用 FFmpeg，未修改其二进制。</p>")
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(text)

        row = QHBoxLayout()
        btn_copy = QPushButton("复制版本信息")
        btn_copy.clicked.connect(self._copy_version_info)
        row.addWidget(btn_copy)
        row.addStretch(1)
        lay.addLayout(row)
        return info

    def _copy_version_info(self):
        text = "\n".join(f"{k}：{v}" for k, v in self._about_info())
        QApplication.clipboard().setText(text)
        self.lbl_status.setText("版本信息已复制到剪贴板")

    def _build_tray(self):
        icon = QIcon(icon_path()) if icon_path() else QIcon()
        if icon.isNull():
            # 图标文件缺失时回退到内置绘制
            pix = QPixmap(64, 64)
            pix.fill(Qt.transparent)
            p = QPainter(pix)
            p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(QColor(44, 95, 168)); p.setPen(Qt.NoPen)
            p.drawEllipse(4, 4, 56, 56)
            p.setBrush(QColor(255, 255, 255))
            p.drawEllipse(20, 20, 24, 24)
            p.end()
            icon = QIcon(pix)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        act_show = QAction("显示主界面", self, triggered=self._show)
        act_start = QAction("开始/停止录制", self, triggered=self.toggle_record)
        act_pause = QAction("暂停/继续", self, triggered=self.toggle_pause)
        act_quit = QAction("退出", self, triggered=self._quit)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_start)
        menu.addAction(act_pause)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show() if reason == QSystemTrayIcon.DoubleClick else None)
        self.tray.setToolTip("OpenRec")
        self.tray.show()

    def _setup_hotkeys(self):
        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.failed.connect(lambda msg: self.lbl_status.setText(msg))
        self._register_hotkeys()

    def _register_hotkeys(self):
        bindings = [(self.s.hotkey_start, "toggle")]
        if (self.s.hotkey_pause or "").strip():
            bindings.append((self.s.hotkey_pause, "pause"))
        self.hotkeys.start(bindings)
        self._update_hotkey_hint()

    def _update_hotkey_hint(self):
        parts = []
        if (self.s.hotkey_start or "").strip():
            parts.append(f"{self.s.hotkey_start.strip()} 开始/停止")
        if (self.s.hotkey_pause or "").strip():
            parts.append(f"{self.s.hotkey_pause.strip()} 暂停/继续")
        self.lbl_hotkeys.setText("快捷键：" + " · ".join(parts) if parts else "快捷键：未设置")
        self.btn_start.setToolTip(f"录制/停止（{self.s.hotkey_start}）")

    def _on_hotkey(self, action):
        if action == "toggle":
            self.toggle_record()
        elif action == "pause":
            self.toggle_pause()

    def _on_mode_change(self, *_):
        mode = self.cmb_mode.currentData()
        self.btn_region.setEnabled(mode == "region")
        self.cmb_window.setEnabled(mode == "window")
        self._update_region_label()
        recording = self.recorder is not None
        self.cmb_mode.setEnabled(not recording)
        self.cmb_window.setEnabled(mode == "window" and not recording)
        self.cmb_monitor.setEnabled(mode in ("full", "region") and not recording)
        self.chk_badge.setEnabled(mode != "audio" and not recording)

    def _update_region_label(self):
        if self.s.region[2] > 1:
            x, y, w, h = self.s.region
            text = f"当前区域：{x},{y}  {w}×{h}"
            if not self.s.monitor_all:
                mon = pick_monitor(self.s)
                if mon and not self._region_inside_monitor(self.s.region, mon):
                    text += "（不在所选屏幕内）"
            self.lbl_region.setText(text)
        elif self.cmb_mode.currentData() == "region" and not self.s.monitor_all:
            self.lbl_region.setText("点击“选择区域”，选区将限定在所选屏幕内")
        else:
            self.lbl_region.setText("尚未选择区域，将录制下方所选屏幕")

    def _load_monitors(self):
        """填充显示器下拉框，首项为所有屏幕拼接，保留上次选择。"""
        current_id = self.s.monitor_id
        current_index = self.s.monitor_index
        current_all = self.s.monitor_all
        self.cmb_monitor.blockSignals(True)
        self.cmb_monitor.clear()
        vm = virtual_monitor()
        self.cmb_monitor.addItem(monitor_label(vm), {"all": True, "index": -1, "id": ""})
        monitors = list_monitors()
        for m in monitors:
            self.cmb_monitor.addItem(
                monitor_label(m), {"all": False, "index": m["index"], "id": m["unique_id"]})
        target = 0 if current_all else -1
        if target < 0 and current_id:
            for i, m in enumerate(monitors):
                if m["unique_id"] == current_id:
                    target = i + 1
                    break
        if target < 0 and 0 <= current_index < len(monitors):
            target = current_index + 1
        if target < 0 and monitors:
            target = next((i + 1 for i, m in enumerate(monitors) if m["is_primary"]), 1)
        if target >= 0:
            self.cmb_monitor.setCurrentIndex(target)
        self.cmb_monitor.blockSignals(False)
        self._on_monitor_change()

    def _on_monitor_change(self, *_):
        data = self.cmb_monitor.currentData()
        if data:
            self.s.monitor_all = data["all"]
            self.s.monitor_index = data["index"]
            self.s.monitor_id = data["id"]
        mon = pick_monitor(self.s)
        if mon:
            text = f"{mon['width']}×{mon['height']} · 共 {max(0, self.cmb_monitor.count() - 1)} 块屏幕"
            if self.s.monitor_all:
                text += " · 拼接分辨率大，建议降低帧率"
            self.lbl_monitor.setText(text)
        else:
            self.lbl_monitor.setText("未检测到显示器")
        self._update_region_label()

    def _selected_monitor_screen_rect(self) -> QRect | None:
        """把所选物理显示器映射为 Qt 逻辑矩形；所有屏幕/无法匹配时返回 None。

        Qt 的屏幕原点是逻辑坐标，mss 的显示器矩形是物理坐标。混合缩放时
        两者不能直接比较，这里改用「物理尺寸 + 同尺寸屏幕排序」匹配。
        """
        if self.s.monitor_all:
            return None
        mon = pick_monitor(self.s)
        if not mon:
            return None
        all_mons = [m for m in list_monitors() if not m.get("virtual")]
        same_mons = sorted(
            [m for m in all_mons
             if m["width"] == mon["width"] and m["height"] == mon["height"]],
            key=lambda m: (m["left"], m["top"]))
        rank = same_mons.index(mon) if mon in same_mons else 0
        screens = []
        for screen in QGuiApplication.screens():
            g = screen.geometry()
            dpr = screen.devicePixelRatio()
            if (abs(round(g.width() * dpr) - mon["width"]) <= 2
                    and abs(round(g.height() * dpr) - mon["height"]) <= 2):
                screens.append(screen)
        screens.sort(key=lambda s: (s.geometry().x(), s.geometry().y()))
        if rank < len(screens):
            return QRect(screens[rank].geometry())
        return None

    def _region_inside_monitor(self, region: list, mon: dict) -> bool:
        x, y, w, h = region
        return (mon["left"] <= x and mon["top"] <= y
                and x + w <= mon["left"] + mon["width"]
                and y + h <= mon["top"] + mon["height"])

    def _refresh_sources(self):
        self._refresh_windows()
        self._load_monitors()

    def _load_mics(self):
        self.cmb_mic.clear()
        self.cmb_mic.addItem("系统默认麦克风", "")
        try:
            import soundcard as sc
            for mic in sc.all_microphones(include_loopback=False):
                self.cmb_mic.addItem(mic.name, mic.id)
        except Exception:
            pass
        i = self.cmb_mic.findData(self.s.mic_device)
        self.cmb_mic.setCurrentIndex(max(0, i))

    def _refresh_windows(self):
        current = self.s.window_title
        self.cmb_window.clear()
        try:
            for w in list_windows():
                self.cmb_window.addItem(w.title, w.title)
        except Exception:
            pass
        if current:
            i = self.cmb_window.findData(current)
            if i >= 0:
                self.cmb_window.setCurrentIndex(i)

    # ------------------------------------------------------------ 编码器探测
    def _start_encoder_probe(self):
        """后台探测可用编码器与硬件采集通路，避免阻塞启动。"""
        self.lbl_status.setText("正在检测编码器…")
        self.btn_start.setEnabled(False)   # 探测完成前不允许开录，防止选中未验证的编码器
        self._enc_job = Job(lambda: (available_encoders(), ddagrab_available()), self)
        self._enc_job.finished_ok.connect(self._encoder_probe_done)
        self._enc_job.start()

    def _encoder_probe_done(self, ok, result):
        self._enc_job = None
        encs = set(result[0]) if ok and isinstance(result, tuple) else {"libx264"}
        self._encoders = encs
        current = self.cmb_codec.currentData()
        for name in [self.cmb_codec.itemData(i) for i in range(self.cmb_codec.count())]:
            if name not in encs and self.cmb_codec.count() > 1:
                idx = self.cmb_codec.findData(name)
                if idx >= 0:
                    self.cmb_codec.removeItem(idx)
        if self.cmb_codec.findData(current) < 0:
            self.cmb_codec.setCurrentIndex(0)
        if self.recorder is None:
            self.btn_start.setEnabled(True)
        self.lbl_status.setText(Settings.load_warning or "就绪")
        self._update_estimate()

    # ------------------------------------------------------------ 录制控制
    def toggle_record(self):
        if self.recorder or self._countdown is not None or self._stopping:
            self.stop_record()          # 倒计时中点击 = 取消
            return
        if self._encoders is None:
            self.lbl_status.setText("正在检测编码器，请稍候…")
            return
        self.start_record()

    def _collect_settings(self):
        s = self.s
        s.source_mode = self.cmb_mode.currentData()
        data = self.cmb_monitor.currentData()
        if data:
            s.monitor_all = data["all"]
            s.monitor_index = data["index"]
            s.monitor_id = data["id"]
        s.window_title = self.cmb_window.currentData() or ""
        s.fps = self.spin_fps.value()
        s.codec = self.cmb_codec.currentData() or "libx264"
        if self._encoders is not None and s.codec not in self._encoders:
            s.codec = "libx264"
        s.quality = self.cmb_quality.currentData() or 23
        s.mic_enabled = self.chk_mic.isChecked()
        s.system_audio_enabled = self.chk_sys.isChecked()
        s.mic_device = self.cmb_mic.currentData() or ""
        s.camera_enabled = self.chk_cam.isChecked()
        s.cursor_enabled = self.chk_cursor.isChecked()
        s.rec_badge_enabled = self.chk_badge.isChecked()
        s.countdown = self.spin_countdown.value()
        return s

    def start_record(self):
        if self.recorder or self._countdown is not None or self._stopping:
            return
        s = self._collect_settings()
        if s.source_mode == "region" and s.region[2] < 2:
            QMessageBox.warning(self, "提示", "请先点击“选择区域”划定录制范围。")
            return
        if s.source_mode == "region" and not s.monitor_all:
            mon = pick_monitor(s)
            if mon and not self._region_inside_monitor(s.region, mon):
                QMessageBox.warning(self, "提示", "当前区域不在所选屏幕内，请重新选择区域。")
                return
        if s.source_mode == "window" and not s.window_title:
            QMessageBox.warning(self, "提示", "请选择要录制的窗口。")
            return
        try:
            os.makedirs(s.output_dir, exist_ok=True)
            free = free_bytes(s.output_dir)
        except OSError as exc:
            QMessageBox.warning(self, "无法访问保存目录",
                                f"{s.output_dir}\n{exc}")
            return
        if free < MIN_FREE_BYTES:
            self._update_estimate()
            QMessageBox.warning(
                self, "空间不足",
                f"保存位置只剩 {human_size(free)}，低于 2 GB，无法开始录像。\n"
                "请清理磁盘或在“设置”中更换保存目录。")
            return
        if s.source_mode != "audio" and not (s.mic_enabled or s.system_audio_enabled):
            pass  # 允许无声录制
        if s.source_mode == "audio" and not (s.mic_enabled or s.system_audio_enabled):
            QMessageBox.warning(self, "提示",
                                "仅音频模式至少需要选择麦克风或系统声音。")
            return
        s.save()
        self._set_controls(recording=True)
        self.btn_pause.setEnabled(False)
        self.lbl_status.setText("准备录制…")

        if s.countdown > 0:
            self._countdown = CountdownOverlay(s.countdown)
            self._countdown.finished.connect(self._really_start)
            self._countdown.show()
        else:
            self._really_start()

    def _really_start(self):
        if self.recorder:
            return
        self._countdown = None
        try:
            self.recorder = Recorder(self.s)
            self.recorder.start()
        except Exception as exc:
            self.recorder = None
            self._set_controls(recording=False)
            self.lbl_status.setText("启动失败")
            QMessageBox.critical(self, "无法开始录制", str(exc))
            return
        if self.recorder.backend == "ddagrab":
            self.lbl_status.setText("录制中（高帧率）")
            self.btn_pause.setEnabled(False)
            self.btn_pause.setToolTip("高帧率采集模式不支持暂停")
        else:
            self.lbl_status.setText("录制中（DXGI 采集）" if self.recorder.backend == "dxcam" else "录制中")
            self.btn_pause.setEnabled(True)
            self.btn_pause.setToolTip("")
        self.btn_start.setText("停止录制")
        self.cmb_mode.setEnabled(False)
        self._update_indicator()

    def _update_indicator(self):
        """录制中在采集区域右上角显示实时闪烁的 REC 角标。"""
        rec = self.recorder
        if not rec or not self.s.rec_badge_enabled or rec.state != "recording":
            self.rec_indicator.hide_indicator()
            return
        bbox = rec.current_bbox()
        bw, _ = self.rec_indicator.badge_physical_size()
        self.rec_indicator.place_physical(bbox[0] + bbox[2] - bw - 24, bbox[1] + 24)
        self.rec_indicator.show_indicator()

    def stop_record(self):
        if self._stopping:
            return                      # 正在收尾，忽略重复触发
        if not self.recorder:
            if self._countdown is not None:
                self._countdown.cancel()
                self._countdown = None
                self._set_controls(recording=False)
                self.lbl_status.setText("就绪")
            return
        self.rec_indicator.hide_indicator()
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.lbl_status.setText("正在保存…")
        rec = self.recorder
        # 收尾要重封装整个文件：19 分钟 4K 约 6 GB，需十几秒。
        # 放到后台线程执行，否则主线程无消息循环，系统判定“未响应”。
        self._save_ctx = (rec.capture_warning, rec.low_space_warning,
                          self.lbl_timer.text())
        rec.capture_warning = None
        rec.low_space_warning = None
        self._stopping = True
        self._save_progress = QProgressDialog("正在保存录像…", "", 0, 0, self)
        self._save_progress.setCancelButton(None)
        self._save_progress.setWindowModality(Qt.WindowModal)
        self._save_progress.setMinimumDuration(0)
        self._save_progress.show()
        self._stop_job = Job(rec.stop, self)
        self._stop_job.finished_ok.connect(self._stop_done)
        self._stop_job.start()

    def _stop_done(self, ok, message):
        try:
            if self._save_progress:
                self._save_progress.close()
        except Exception:
            pass
        self._save_progress = None
        self._stop_job = None
        self._stopping = False
        rec, self.recorder = self.recorder, None
        if rec is not None:
            # 休眠抑制按线程记录，必须回到开启它的主线程清除
            try:
                rec.release_awake()
            except Exception:
                pass
        low, note, recorded_for = self._save_ctx or (None, None, "")
        self._save_ctx = None
        degraded = rec.degraded_save if rec else None
        pace = rec.pacing_note if rec else None
        path = message if ok else ""
        if not ok:
            if self.isVisible():
                QMessageBox.critical(self, "保存失败", str(message))
            else:
                self.tray.showMessage("保存失败", str(message),
                                      QSystemTrayIcon.Critical, 10000)
        self._set_controls(recording=False)
        self.lbl_status.setText("已保存" if path else "就绪")
        if self._quitting:
            # 退出流程：不再弹提示，清理后立即退出
            self._cleanup()
            self.tray.hide()
            QApplication.quit()
            return
        self.refresh_library()
        if path and not self._quitting:
            self.lbl_lib.setText("已保存：" + os.path.basename(path))
        if not path:
            return
        alerts = []
        if low:
            alerts.append(f"{low}\n中断前已录 {recorded_for}。")
        if note:
            alerts.append(f"{note}\n中断前已录 {recorded_for}。")
        if degraded:
            alerts.append(degraded)
        if pace:
            alerts.append(pace)
        if not alerts:
            return
        msg = "\n\n".join(alerts) + "\n\n已保存：\n" + os.path.basename(path)
        title = "硬盘已满，停止录像" if low else ("采集已中断" if note else "录像已保存")
        if self.isVisible():
            if low or note:
                QMessageBox.warning(self, title, msg)
            else:
                QMessageBox.information(self, title, msg)
        else:
            level = (QSystemTrayIcon.Warning if (low or note)
                     else QSystemTrayIcon.Information)
            self.tray.showMessage(title, msg, level, 8000)

    def toggle_pause(self):
        if not self.recorder:
            return
        if self.recorder.backend == "ddagrab":
            self.lbl_status.setText("高帧率采集模式不支持暂停")
            return
        if self.recorder.state == "recording":
            self.recorder.pause()
            self.btn_pause.setText("继续")
            self.lbl_status.setText("已暂停")
        elif self.recorder.state == "paused":
            self.recorder.resume()
            self.btn_pause.setText("暂停")
            self.lbl_status.setText("录制中")

    def _set_controls(self, recording: bool):
        self.btn_start.setEnabled(True)
        self.btn_start.setText("停止录制" if recording else "开始录制")
        self.btn_pause.setEnabled(recording)
        self.btn_pause.setText("暂停")
        self.btn_region.setEnabled(not recording)
        self.cmb_mode.setEnabled(not recording)
        self.cmb_monitor.setEnabled(not recording and self.cmb_mode.currentData() in ("full", "region"))
        self.chk_badge.setEnabled(not recording and self.cmb_mode.currentData() != "audio")
        self.btn_settings.setEnabled(not recording)
        # 录制期间禁用参数区，避免界面值被误改后与当前录像不一致
        for w in (self.spin_fps, self.cmb_codec, self.cmb_quality, self.spin_countdown,
                  self.chk_mic, self.cmb_mic, self.chk_sys, self.chk_cam,
                  self.chk_cursor):
            w.setEnabled(not recording)
        if not recording:
            self.lbl_timer.setText("00:00:00")
            self.btn_pause.setToolTip("")

    def _tick(self):
        if not self.recorder:
            return
        if self.recorder.low_space_warning:
            # 剩余空间到 2 GB：立刻优雅收尾，保住已录内容
            self.stop_record()
            return
        if self.recorder.capture_warning:
            # 采集持续失败（显示器休眠/断开等）：立即收尾并保住已录内容
            self.stop_record()
            return
        if self.recorder.error:
            # 编码/采集线程异常退出：主动收尾，避免界面一直显示「录制中」
            self.lbl_status.setText("录制异常，正在保存…")
            self.stop_record()
            return
        sec = self.recorder.elapsed
        self.lbl_timer.setText(f"{int(sec)//3600:02d}:{int(sec)%3600//60:02d}:{int(sec)%60:02d}")
        self._update_indicator()

    def _estimate_area(self) -> tuple[int, int]:
        if self.cmb_mode.currentData() == "region" and self.s.region[2] > 1:
            return self.s.region[2], self.s.region[3]
        mon = pick_monitor(self.s)
        if mon:
            return mon["width"], mon["height"]
        return 1920, 1080

    def _update_estimate(self):
        """显示磁盘剩余空间与按当前画质估算的可录时长。"""
        try:
            free = free_bytes(self.s.output_dir)
        except OSError:
            self.lbl_estimate.setText("无法读取保存目录所在磁盘的空间")
            return
        if free < MIN_FREE_BYTES:
            self.lbl_estimate.setText(
                f"剩余 {human_size(free)} · 空间不足（至少需要 2 GB），无法开始录像")
            return
        audio = self.chk_mic.isChecked() or self.chk_sys.isChecked()
        if self.cmb_mode.currentData() == "audio":
            bitrate = 200_000 if audio else 0
        else:
            w, h = self._estimate_area()
            bitrate = estimate_bitrate(self.cmb_codec.currentData() or "libx264",
                                       self.cmb_quality.currentData() or 23,
                                       w, h, self.spin_fps.value(), audio)
        if bitrate <= 0:
            self.lbl_estimate.setText(f"剩余 {human_size(free)} · 未启用音频，无法估算")
            return
        seconds = (free - MIN_FREE_BYTES) * 8 / bitrate
        note = ""
        if self.cmb_mode.currentData() in ("window",) or (
                self.cmb_monitor.currentData() or {}).get("all"):
            note = "；该模式为通用抓屏，实际帧率低于高帧率模式"
        self.lbl_estimate.setText(
            f"剩余 {human_size(free)} · 预计可录约 {human_span(seconds)}"
            f"（约 {bitrate / 1e6:.1f} Mbps，按当前画质估算）{note}")

    # ------------------------------------------------------------ 区域/白板/分享
    def _pick_region(self):
        self.hide()
        # 选中具体屏幕时，把选区限制在该屏幕内；选“所有屏幕”时允许跨屏
        self._region_dialog = RegionOverlay(self._selected_monitor_screen_rect())
        self._region_dialog.selected.connect(self._region_selected)
        self._region_dialog.cancelled.connect(self._region_cancelled)
        self._region_dialog.show()

    def _region_selected(self, x, y, w, h):
        self.s.region = [x, y, w, h]
        self.s.source_mode = "region"
        self.cmb_mode.setCurrentIndex(self.cmb_mode.findData("region"))
        self._update_region_label()
        self._update_estimate()
        self.show()

    def _region_cancelled(self):
        self.show()

    def open_whiteboard(self):
        if self.whiteboard and self.whiteboard.isVisible():
            self.whiteboard.raise_()
            return
        self.whiteboard = Whiteboard()
        self.whiteboard.closed.connect(lambda: setattr(self, "whiteboard", None))
        self.whiteboard.show()

    def toggle_share(self):
        if self.share.running:
            self.share.stop()
            self.btn_share.setText("局域网分享")
            self.lbl_status.setText("分享已关闭")
            return
        if QMessageBox.question(
                self, "开启局域网分享",
                "分享开启后，同一局域网内的设备可通过 8 位口令访问录像文件。\n"
                "请仅在可信网络（如家庭 Wi-Fi）下使用，不要在公司/公共网络长期开启。\n\n"
                "确定要开启分享吗？") != QMessageBox.Yes:
            return
        self.share.root = self.s.output_dir
        try:
            url = self.share.start(self.s.share_port)
        except Exception as exc:
            QMessageBox.critical(self, "分享失败", str(exc))
            return
        self.btn_share.setText("停止分享")
        self.lbl_status.setText("分享中：" + url)
        self._show_qr(url, self.share.token)

    def _show_qr(self, url: str, token: str):
        dlg = QDialog(self)
        dlg.setWindowTitle("局域网分享")
        lay = QVBoxLayout(dlg)
        tip = QLabel(f"手机与电脑连同一 Wi-Fi，扫码或访问：\n{url}\n访问口令：{token}")
        tip.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(tip)
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pix = QPixmap()
        pix.loadFromData(buf.getvalue())
        lab = QLabel()
        lab.setPixmap(pix.scaled(260, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lay.addWidget(lab, 0, Qt.AlignCenter)
        note = QLabel("仅限可信局域网；口令连续输错 5 次将临时锁定 1 分钟；关闭分享后立即失效。")
        note.setObjectName("hint")
        lay.addWidget(note)
        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn)
        dlg.exec()

    # ------------------------------------------------------------ 录制库
    def _selected_path(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else ""

    def refresh_library(self):
        self.table.setRowCount(0)
        self.share.root = self.s.output_dir
        try:
            files = [f for f in os.listdir(self.s.output_dir)
                     if os.path.splitext(f)[1].lower() in VIDEO_EXT | AUDIO_EXT
                     and not f.startswith(".")]
        except Exception:
            files = []
        try:
            files.sort(key=lambda f: os.path.getmtime(os.path.join(self.s.output_dir, f)),
                       reverse=True)
        except OSError:
            pass
        self._probe_targets = []
        for name in files[:200]:
            path = os.path.join(self.s.output_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = (path, int(st.st_mtime), st.st_size)
            dur = self._dur_cache.get(key)
            r = self.table.rowCount()
            self.table.insertRow(r)
            item = QTableWidgetItem(name)
            item.setData(Qt.UserRole, path)
            self.table.setItem(r, 0, item)
            self.table.setItem(r, 1, QTableWidgetItem(
                human_duration(dur) if dur is not None else "--:--"))
            self.table.setItem(r, 2, QTableWidgetItem(human_size(st.st_size)))
            self.table.setItem(r, 3, QTableWidgetItem(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))))
            if dur is None:
                self._probe_targets.append(key)
        self.lbl_lib.setText(f"{len(files)} 个文件")
        self._start_probe()

    def _start_probe(self):
        """后台读取缺失的时长：ffprobe 单次约 150 ms，同步跑会卡住界面。

        每批只处理少量文件，避免一次跑几十秒影响退出或录制。
        """
        if self._probe_job is not None or not getattr(self, "_probe_targets", None):
            return
        targets = self._probe_targets[:16]
        self._probe_targets = []
        self._probe_job = Job(
            lambda: [(k, probe_duration(k[0])) for k in targets], self)
        self._probe_job.finished_ok.connect(self._probe_done)
        self._probe_job.start()

    def _probe_done(self, ok, message):
        self._probe_job = None
        if ok:
            for key, dur in message:
                self._dur_cache[key] = dur
            self.refresh_library()      # 命中缓存后秒填时长

    def play_selected(self):
        path = self._selected_path()
        if not path:
            return
        try:
            os.startfile(path)
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", str(exc))

    def open_folder(self):
        os.makedirs(self.s.output_dir, exist_ok=True)
        os.startfile(self.s.output_dir)

    def rename_selected(self):
        path = self._selected_path()
        if not path:
            return
        name, ok = QInputDialog.getText(self, "重命名", "新文件名：", text=os.path.basename(path))
        if not ok or not name.strip():
            return
        ext = os.path.splitext(path)[1]
        base = name.strip()
        if ext and base.lower().endswith(ext.lower()):
            base = base[:-len(ext)].rstrip()
        invalid = set('\\/:*?"<>|')
        if (not base or base in (".", "..") or base != base.rstrip(". ")
                or any(ch in invalid or ord(ch) < 32 for ch in base)):
            QMessageBox.warning(self, "重命名失败",
                                "文件名不能包含 \\ / : * ? \" < > | 等字符，也不能以点或空格结尾。")
            return
        new = os.path.join(self.s.output_dir, base + ext)
        if os.path.dirname(os.path.abspath(new)) != os.path.abspath(self.s.output_dir):
            QMessageBox.warning(self, "重命名失败", "文件名不能包含路径。")
            return
        try:
            os.rename(path, new)
            self.refresh_library()
        except Exception as exc:
            QMessageBox.critical(self, "重命名失败", str(exc))

    def delete_selected(self):
        path = self._selected_path()
        if not path:
            return
        if QMessageBox.question(self, "删除", f"确定删除 {os.path.basename(path)} ？") != QMessageBox.Yes:
            return
        try:
            os.remove(path)
            self.refresh_library()
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))

    def trim_selected(self):
        path = self._selected_path()
        if not path:
            return
        duration = probe_duration(path)
        if duration <= 0:
            QMessageBox.warning(self, "提示", "无法读取该文件时长。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("剪辑")
        form = QFormLayout(dlg)
        start = QDoubleSpinBox(); start.setRange(0, duration); start.setSuffix(" 秒")
        end = QDoubleSpinBox(); end.setRange(0, duration); end.setValue(duration); end.setSuffix(" 秒")
        form.addRow("开始", start)
        form.addRow("结束", end)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() != QDialog.Accepted or end.value() <= start.value():
            return
        out = unique_path(os.path.splitext(path)[0] + "_clip.mp4")
        self._run_job(lambda: trim_video(path, start.value(), end.value(), out), "剪辑中…")

    def repair_selected(self):
        path = self._selected_path()
        if not path:
            return
        out = unique_path(os.path.splitext(path)[0] + "_fixed.mp4")
        self._run_job(lambda: repair_video(path, out), "修复中…")

    def _run_job(self, fn, title):
        # 编码任务无法中途安全终止，因此不显示会误导的“取消”按钮
        self._progress = QProgressDialog(title, "", 0, 0, self)
        self._progress.setCancelButton(None)
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.show()
        self._job = Job(fn, self)
        self._job.finished_ok.connect(self._job_done)
        self._job.start()

    def _job_done(self, ok, message):
        try:
            self._progress.close()
        except Exception:
            pass
        if ok:
            self.lbl_lib.setText("完成：" + os.path.basename(message))
            self.refresh_library()
        else:
            QMessageBox.critical(self, "处理失败", message)

    # ------------------------------------------------------------ 设置
    def open_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("设置")
        dlg.setMinimumWidth(640)
        lay = QVBoxLayout(dlg)
        form = QFormLayout()
        out_edit = QLineEdit(self.s.output_dir)
        browse = QPushButton("选择…")
        row = QHBoxLayout(); row.addWidget(out_edit); row.addWidget(browse)
        port = QSpinBox(); port.setRange(1024, 65535); port.setValue(self.s.share_port)
        mark = QLineEdit(self.s.watermark_text)
        mark.setMaxLength(64)
        mark.setPlaceholderText("留空表示不加水印（最多 64 字）")
        hk1 = HotkeyEdit(self.s.hotkey_start)
        hk2 = HotkeyEdit(self.s.hotkey_pause)
        cam_w = QSpinBox(); cam_w.setRange(120, 800); cam_w.setValue(self.s.camera_size); cam_w.setSuffix(" px")
        form.addRow("录制保存目录", row)
        form.addRow("分享端口", port)
        form.addRow("水印文字", mark)
        form.addRow("开始/停止快捷键", hk1)
        form.addRow("暂停快捷键", hk2)
        hk_note = QLabel("点击输入框后直接按下组合键即可录入；Backspace 清除，暂停快捷键留空即禁用。")
        hk_note.setObjectName("hint")
        form.addRow(hk_note)
        form.addRow("摄像头叠加宽度", cam_w)
        lay.addLayout(form)
        lay.addWidget(self._build_about_section())
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        def choose_dir():
            d = QFileDialog.getExistingDirectory(self, "选择目录", out_edit.text())
            if d:
                out_edit.setText(d)

        def try_accept():
            start_text = hk1.text().strip() or self.s.hotkey_start
            pause_text = hk2.text().strip()
            err = validate_bindings(start_text, pause_text)
            if err:
                QMessageBox.warning(dlg, "快捷键无效", err)
                return
            dlg.accept()

        btns.accepted.connect(try_accept)
        browse.clicked.connect(choose_dir)
        if dlg.exec() != QDialog.Accepted:
            return
        self.s.output_dir = out_edit.text().strip() or self.s.output_dir
        self.s.share_port = port.value()
        self.s.watermark_text = mark.text().strip()
        self.s.hotkey_start = hk1.text().strip() or self.s.hotkey_start
        self.s.hotkey_pause = hk2.text().strip()
        self.s.camera_size = cam_w.value()
        self.s.save()
        self.share.root = self.s.output_dir
        self._register_hotkeys()
        self.refresh_library()
        self._update_estimate()
        self._update_hotkey_hint()

    # ------------------------------------------------------------ 窗口生命周期
    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        self._quitting = True
        if self.recorder:
            # 收尾在后台 Job 里进行；_stop_done 检测到 _quitting 后再真正退出，
            # 避免在主线程同步等待导致界面「未响应」。
            self.stop_record()
            return
        if self._countdown is not None:
            try:
                self._countdown.close()
            except Exception:
                pass
            self._countdown = None
        self._cleanup()
        self.tray.hide()
        QApplication.quit()

    def _cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        try:
            if self.recorder is not None:
                self.recorder.release_awake()   # 兜底：退出时一定清掉休眠抑制
        except Exception:
            pass
        try:
            self.rec_indicator.hide_indicator()
        except Exception:
            pass
        try:
            if self.hotkeys:
                self.hotkeys.stop()
        except Exception:
            pass
        try:
            self.share.stop()
        except Exception:
            pass
        for job in (self._job, self._stop_job, self._probe_job, self._enc_job):
            try:
                if job is not None and job.isRunning():
                    job.wait(30000 if job is self._stop_job else 4000)
            except Exception:
                pass

    def _ask_close_action(self) -> str:
        """关闭按钮询问：退出 / 最小化到托盘 / 取消。"""
        box = QMessageBox(self)
        box.setWindowTitle("关闭 OpenRec")
        box.setIcon(QMessageBox.Question)
        if self.recorder:
            box.setText("正在录制。要停止录制并退出，还是最小化到托盘继续录制？")
        else:
            box.setText("要退出程序，还是最小化到托盘继续待命？")
        btn_quit = box.addButton("退出程序", QMessageBox.DestructiveRole)
        btn_tray = box.addButton("最小化到托盘", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(btn_tray)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_quit:
            return "quit"
        if clicked is btn_tray:
            return "tray"
        return "cancel"

    def closeEvent(self, e):
        if self._quitting:
            # 退出收尾尚未完成时忽略关闭，避免销毁仍在运行的 QThread
            if self.recorder or self._stopping:
                e.ignore()
                return
            self._cleanup()
            e.accept()
            return
        if not self.tray.isVisible():
            self._cleanup()
            e.accept()
            return
        e.ignore()
        action = self._ask_close_action()
        if action == "quit":
            self._quit()
        elif action == "tray":
            self.hide()
            self.tray.showMessage("OpenRec", "已最小化到托盘，快捷键仍然可用。",
                                  QSystemTrayIcon.Information, 2000)
