"""OpenRec 启动入口。"""
import logging
import logging.handlers
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtGui import QFont, QColor, QPalette
from PySide6.QtWidgets import QApplication

from openrec.config import APP_VERSION, DATA_DIR, Settings
from openrec.single_instance import SingleInstance
from openrec.ui import MainWindow

STYLE = """
QWidget { background: #0b2545; color: #e6f1ff; font-size: 13px; }
QMainWindow { background: #0b2545; }
QLabel#title { font-size: 22px; font-weight: 700; color: #ffffff; }
QLabel#status { color: #9fc3f2; }
QLabel#hint { color: #7fa5d6; }
QLabel#timer { font-size: 26px; font-weight: 600; font-family: Consolas, monospace; color: #6da4dd; }
QGroupBox { border: 1px solid #143a66; border-radius: 10px; margin-top: 14px; padding: 12px 10px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #a8c8ef; }
QPushButton { background: #12365f; border: 1px solid #1f4c7c; border-radius: 7px; padding: 7px 14px; }
QPushButton:hover { background: #1a487e; }
QPushButton:pressed { background: #0e2b4e; }
QPushButton:disabled { color: #5d7fa8; background: #0d2b4c; border-color: #143a66; }
QPushButton#primary { background: #2c5fa8; border: none; color: #fff; font-weight: 600; }
QPushButton#primary:hover { background: #3a6fbc; }
QPushButton#primary:disabled { background: #1b4d8c; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
    background: #0e3059; border: 1px solid #1f4c7c; border-radius: 6px; padding: 5px 8px;
}
QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: center right;
    border: none; width: 22px;
}
QComboBox::down-arrow { image: url("__ARROW_DOWN__"); width: 10px; height: 6px; }
QComboBox QAbstractItemView { background: #0e3059; selection-background-color: #1f4c7c; }
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; width: 18px; border-left: 1px solid #1f4c7c;
    background: #12365f;
}
QSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 6px; }
QSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: 6px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #1a487e; }
QSpinBox::up-arrow { image: url("__ARROW_UP__"); width: 9px; height: 5px; }
QSpinBox::down-arrow { image: url("__ARROW_DOWN__"); width: 9px; height: 5px; }
QTableWidget { background: #0d2c50; border: 1px solid #143a66; border-radius: 8px; gridline-color: #12365f; }
QHeaderView::section { background: #0e3059; border: none; padding: 6px; color: #9fc3f2; }
QTableWidget::item:selected { background: #1a487e; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 4px; border: 1px solid #2e5c8f; background: #0e3059; }
QCheckBox::indicator:checked { background: #2c5fa8; border-color: #2c5fa8; }
QProgressDialog { background: #0b2545; }
QMenu { background: #0e3059; border: 1px solid #1f4c7c; }
QMenu::item:selected { background: #1a487e; }
QToolTip { background: #0e3059; color: #e6f1ff; border: 1px solid #1f4c7c; }
"""


def _arrow_icon(path: str, up: bool) -> str:
    """生成下拉/微调箭头小图：Qt 样式表无法用边框画出三角形。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPainter, QPixmap, QPolygon

    pm = QPixmap(10, 6)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#8fb0da"))
    tri = ([QPoint(0, 6), QPoint(5, 0), QPoint(10, 6)] if up
           else [QPoint(0, 0), QPoint(10, 0), QPoint(5, 6)])
    painter.drawPolygon(QPolygon(tri))
    painter.end()
    pm.save(path)
    return path.replace("\\", "/")


def _selftest():
    """无界面自检：录 3 秒并写出结果，便于验证安装是否可用。"""
    import time
    from openrec.config import ROOT
    from openrec.recorder import Recorder

    s = Settings.load()
    s.fps, s.mic_enabled, s.system_audio_enabled, s.cursor_enabled = 10, False, False, False
    try:
        r = Recorder(s, "selftest")
        r.start()
        time.sleep(3)
        path = r.stop()
        result = f"OK {path} {os.path.getsize(path)} backend={r.backend}"
    except Exception as exc:
        result = f"FAIL {type(exc).__name__}: {exc}"
    with open(os.path.join(ROOT, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write(result)
    sys.exit(0 if result.startswith("OK") else 1)


def _setup_logging():
    """本地滚动日志（最大 512 KB × 2），只记录运行事件，不上传。"""
    try:
        from openrec.config import DATA_DIR
        log_dir = os.path.join(DATA_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "openrec.log"), maxBytes=512 * 1024,
            backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)

        def _hook(exc_type, exc, tb):
            logging.getLogger("openrec").error(
                "未捕获异常", exc_info=(exc_type, exc, tb))
        sys.excepthook = _hook
    except Exception:
        pass


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return
    _setup_logging()
    logging.getLogger("openrec").info(
        "启动 v%s frozen=%s", APP_VERSION, getattr(sys, "frozen", False))
    app = QApplication(sys.argv)
    app.setApplicationName("OpenRec")
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#0b2545"))
    palette.setColor(QPalette.WindowText, QColor("#e6f1ff"))
    palette.setColor(QPalette.Base, QColor("#0d2c50"))
    palette.setColor(QPalette.Text, QColor("#e6f1ff"))
    palette.setColor(QPalette.Button, QColor("#12365f"))
    palette.setColor(QPalette.ButtonText, QColor("#e6f1ff"))
    palette.setColor(QPalette.Highlight, QColor("#2c5fa8"))
    app.setPalette(palette)
    # 箭头图标按需生成到数据目录，避免样式表依赖打包资源
    qss_dir = os.path.join(DATA_DIR, "qss")
    os.makedirs(qss_dir, exist_ok=True)
    down = _arrow_icon(os.path.join(qss_dir, "arrow_down.png"), up=False)
    up = _arrow_icon(os.path.join(qss_dir, "arrow_up.png"), up=True)
    app.setStyleSheet(STYLE.replace("__ARROW_DOWN__", down).replace("__ARROW_UP__", up))
    app.setFont(QFont("Microsoft YaHei", 9))

    settings = Settings.load()

    single = SingleInstance(parent=app)
    if not single.try_acquire():
        # 已有实例在运行：通知它前置窗口，本进程直接退出
        sys.exit(0)

    window = MainWindow(settings)
    single.activated.connect(window._show)
    app.aboutToQuit.connect(single.close)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
