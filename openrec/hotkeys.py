"""全局快捷键：独立线程 RegisterHotKey + Win32 消息轮询，避免与 Qt 事件过滤器冲突。"""
import ctypes
import ctypes.wintypes as wt
import time

from PySide6.QtCore import QObject, QThread, Signal

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

_VK_NAMES = {chr(c): c for c in range(ord("A"), ord("Z") + 1)}
_VK_NAMES.update({str(d): 0x30 + d for d in range(10)})
_VK_NAMES.update({f"F{i}": 0x6F + i for i in range(1, 13)})
_VK_NAMES.update({"SPACE": 0x20, "ESC": 0x1B, "ENTER": 0x0D, "TAB": 0x09,
                  "PRINTSCREEN": 0x2C, "PAUSE": 0x13})


def parse_combo(text: str):
    """'Ctrl+Alt+R' -> (mods, vk)；无法解析返回 None。"""
    mods = 0
    key = None
    for part in [p.strip().upper() for p in text.split("+") if p.strip()]:
        if part in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif part == "ALT":
            mods |= MOD_ALT
        elif part == "SHIFT":
            mods |= MOD_SHIFT
        elif part in ("WIN", "META"):
            mods |= MOD_WIN
        elif part in _VK_NAMES:
            key = _VK_NAMES[part]
        else:
            return None
    if key is None:
        return None
    return mods, key


def validate_bindings(start: str, pause: str) -> str | None:
    """校验两个全局快捷键；返回错误信息，None 表示可用。暂停可为空（禁用）。"""
    start = (start or "").strip()
    pause = (pause or "").strip()
    if not start:
        return "开始/停止快捷键不能为空。"
    for combo in (start, pause):
        if not combo:
            continue
        parsed = parse_combo(combo)
        if parsed is None:
            return f"无法识别快捷键：{combo}"
        if parsed[0] == 0:
            return f"快捷键需包含 Ctrl、Alt、Shift 或 Win：{combo}"
    if pause and start.upper() == pause.upper():
        return "开始/停止与暂停快捷键不能相同。"
    return None


class _HotkeyThread(QThread):
    triggered = Signal(str)
    failed = Signal(str)

    def __init__(self, bindings: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self._bindings = bindings
        self._ids: dict[int, str] = {}
        self._stop_flag = False

    def run(self):
        next_id = 0x4000
        for combo, action in self._bindings:
            parsed = parse_combo(combo)
            if not parsed:
                self.failed.emit(f"无法识别快捷键：{combo}")
                continue
            mods, vk = parsed
            next_id += 1
            if user32.RegisterHotKey(None, next_id, mods | MOD_NOREPEAT, vk):
                self._ids[next_id] = action
            else:
                self.failed.emit(f"快捷键被占用：{combo}")
        msg = wt.MSG()
        try:
            while not self._stop_flag:
                got = False
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001):
                    got = True
                    if msg.message == WM_HOTKEY:
                        action = self._ids.get(int(msg.wParam))
                        if action:
                            self.triggered.emit(action)
                if not got:
                    time.sleep(0.02)
        finally:
            for hid in list(self._ids):
                user32.UnregisterHotKey(None, hid)
            self._ids.clear()

    def stop(self):
        self._stop_flag = True


class HotkeyManager(QObject):
    triggered = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: _HotkeyThread | None = None

    def start(self, bindings: list[tuple[str, str]]):
        self.stop()
        self._thread = _HotkeyThread(bindings, self)
        self._thread.triggered.connect(self.triggered)
        self._thread.failed.connect(self.failed)
        self._thread.start()

    def stop(self):
        if self._thread:
            self._thread.stop()
            self._thread.wait(1500)
            self._thread = None
