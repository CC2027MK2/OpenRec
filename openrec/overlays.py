"""覆盖层：区域选择、倒计时、白板标注、录制角标。"""
from PySide6.QtCore import Qt, QPoint, QRect, QTimer, Signal, QSize
from PySide6.QtGui import (QPainter, QColor, QPen, QFont, QGuiApplication, QPainterPath,
                           QImage, QPixmap)
from PySide6.QtWidgets import QWidget

from .recorder import make_badge_image


def _virtual_geometry() -> QRect:
    rect = QRect()
    for screen in QGuiApplication.screens():
        rect = rect.united(screen.geometry())
    return rect


def _to_physical(rect: QRect) -> tuple[int, int, int, int]:
    screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    g = screen.geometry() if screen else rect
    # Windows 的屏幕原点本身就是物理像素，只有宽高被 DPI 缩放
    px = round(g.x() + (rect.x() - g.x()) * dpr)
    py = round(g.y() + (rect.y() - g.y()) * dpr)
    return (px, py, max(2, round(rect.width() * dpr)), max(2, round(rect.height() * dpr)))


class RegionOverlay(QWidget):
    selected = Signal(int, int, int, int)
    cancelled = Signal()

    def __init__(self, constraint: QRect | None = None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self._constraint = QRect(constraint) if constraint else None
        self.setGeometry(self._constraint if self._constraint else _virtual_geometry())
        self._origin = None
        self._current = None

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._origin and self._current:
            r = QRect(self._origin, self._current).normalized()
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(r, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.setPen(QPen(QColor(0, 160, 255), 2))
            p.drawRect(r)
            label = f"{r.width()} × {r.height()}"
            p.setPen(QColor(255, 255, 255))
            p.setFont(QFont("Microsoft YaHei", 10))
            p.drawText(r.x() + 6, max(16, r.y() - 8), label)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._origin = e.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, e):
        if self._origin:
            self._current = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._origin:
            self._current = e.position().toPoint()
            self.update()
            self._confirm()

    def mouseDoubleClickEvent(self, _):
        self._confirm()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            self.cancelled.emit()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._confirm()

    def _confirm(self):
        if not self._origin or not self._current:
            return
        r = QRect(self._origin, self._current).normalized()
        if r.width() < 8 or r.height() < 8:
            return
        # 控件坐标 → 全局逻辑坐标，再按所在屏幕 DPI 换算成物理像素
        global_r = QRect(self.mapToGlobal(r.topLeft()), r.size())
        self.close()
        self.selected.emit(*_to_physical(global_r))

    def showEvent(self, _):
        self.activateWindow()
        self.setFocus()


class CountdownOverlay(QWidget):
    finished = Signal()

    def __init__(self, seconds: int):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                         Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(_virtual_geometry())
        self._left = max(1, seconds)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _tick(self):
        self._left -= 1
        if self._left <= 0:
            self._timer.stop()
            self.close()
            self.finished.emit()
        self.update()

    def cancel(self):
        """取消倒计时：停表并关闭，不发出 finished，避免关闭后仍自动开录。"""
        self._timer.stop()
        self.close()

    def closeEvent(self, e):
        self._timer.stop()
        super().closeEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() // 2, self.height() // 2
        p.setBrush(QColor(0, 0, 0, 140))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPoint(cx, cy), 90, 90)
        p.setPen(QColor(255, 255, 255))
        font = QFont("Microsoft YaHei", 64, QFont.Bold)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignCenter, str(self._left))


class Whiteboard(QWidget):
    """全屏透明画板，绘制内容直接呈现在屏幕上，因此会被录制进画面。"""

    closed = Signal()

    COLORS = ["#ff4d4f", "#ffd666", "#52c41a", "#40a9ff", "#ffffff", "#000000"]
    WIDTHS = [3, 6, 12]

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(_virtual_geometry())
        self.setCursor(Qt.CrossCursor)
        self._strokes: list[tuple[list[QPoint], QColor, int]] = []
        self._current: list[QPoint] = []
        self._color = QColor(self.COLORS[0])
        self._width = 3
        self._toolbar = QRect(0, 0, 0, 0)
        self._buttons: list[tuple[QRect, str, object]] = []
        self._build_toolbar()

    def _build_toolbar(self):
        geo = self.geometry()
        w = 6 * 34 + 3 * 40 + 150
        self._toolbar = QRect(geo.width() // 2 - w // 2, 12, w, 44)
        self._buttons = []
        x = self._toolbar.x() + 10
        y = self._toolbar.y() + 9
        for c in self.COLORS:
            self._buttons.append((QRect(x, y, 26, 26), "color", QColor(c)))
            x += 34
        x += 10
        for wd in self.WIDTHS:
            self._buttons.append((QRect(x, y + 10, 24, 8), "width", wd))
            x += 40
        x += 10
        self._buttons.append((QRect(x, y, 56, 26), "undo", None)); x += 62
        self._buttons.append((QRect(x, y, 56, 26), "clear", None)); x += 62
        self._buttons.append((QRect(x, y, 56, 26), "exit", None))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        for pts, color, width in self._strokes:
            self._draw_stroke(p, pts, color, width)
        if self._current:
            self._draw_stroke(p, self._current, self._color, self._width)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(30, 30, 30, 210))
        p.drawRoundedRect(self._toolbar, 10, 10)
        for rect, kind, value in self._buttons:
            if kind == "color":
                p.setBrush(value)
                p.drawEllipse(rect)
                if value == self._color:
                    p.setPen(QPen(QColor(0, 160, 255), 2))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(rect.adjusted(-3, -3, 3, 3))
                    p.setPen(Qt.NoPen)
            elif kind == "width":
                p.setBrush(QColor(255, 255, 255))
                p.drawRoundedRect(rect, 4, 4)
                if value == self._width:
                    p.setPen(QPen(QColor(0, 160, 255), 2))
                    p.setBrush(Qt.NoBrush)
                    p.drawRoundedRect(rect.adjusted(-3, -3, 3, 3))
                    p.setPen(Qt.NoPen)
            else:
                p.setBrush(QColor(70, 70, 70))
                p.drawRoundedRect(rect, 6, 6)
                p.setPen(QColor(255, 255, 255))
                p.setFont(QFont("Microsoft YaHei", 9))
                text = {"undo": "撤销", "clear": "清屏", "exit": "退出"}[kind]
                p.drawText(rect, Qt.AlignCenter, text)
                p.setPen(Qt.NoPen)

    @staticmethod
    def _draw_stroke(p: QPainter, pts: list[QPoint], color: QColor, width: int):
        if not pts:
            return
        pen = QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        if len(pts) == 1:
            p.drawPoint(pts[0])
        else:
            path = QPainterPath(pts[0])
            for pt in pts[1:]:
                path.lineTo(pt)
            p.drawPath(path)

    def mousePressEvent(self, e):
        pos = e.position().toPoint()
        if self._toolbar.contains(pos):
            for rect, kind, value in self._buttons:
                if rect.contains(pos):
                    if kind == "color":
                        self._color = value
                    elif kind == "width":
                        self._width = value
                    elif kind == "undo":
                        if self._strokes:
                            self._strokes.pop()
                    elif kind == "clear":
                        self._strokes.clear()
                    elif kind == "exit":
                        self.close()
                        self.closed.emit()
                    self.update()
                    return
            return
        self._current = [pos]
        self.update()

    def mouseMoveEvent(self, e):
        if self._current:
            self._current.append(e.position().toPoint())
            self.update()

    def mouseReleaseEvent(self, _):
        if len(self._current) >= 1:
            self._strokes.append((self._current, self._color, self._width))
        self._current = []

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            self.closed.emit()
        elif e.key() == Qt.Key_Z and e.modifiers() & Qt.ControlModifier:
            if self._strokes:
                self._strokes.pop()
                self.update()

    def showEvent(self, _):
        self.activateWindow()
        self.setFocus()


def _pil_to_pixmap(img) -> QPixmap:
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class RecIndicator(QWidget):
    """录制中的屏幕角标：贴在录制区域右上角，鼠标穿透，红点闪烁。"""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
                         Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._pix_on = _pil_to_pixmap(make_badge_image(True))
        self._pix_off = _pil_to_pixmap(make_badge_image(False))
        self._dot_on = True
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._blink)

    def badge_physical_size(self) -> tuple[int, int]:
        return self._pix_on.width(), self._pix_on.height()

    def _blink(self):
        self._dot_on = not self._dot_on
        self.update()

    def place_physical(self, x: int, y: int):
        """把角标放到物理像素坐标 (x, y) 处，自动换算为 Qt 逻辑坐标。"""
        screen = None
        for s in QGuiApplication.screens():
            g = s.geometry()
            dpr = s.devicePixelRatio()
            if g.x() <= x <= g.x() + g.width() * dpr and g.y() <= y <= g.y() + g.height() * dpr:
                screen = s
                break
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        dpr = (screen.devicePixelRatio() if screen else 1.0) or 1.0
        g = screen.geometry()
        lx = round(g.x() + (x - g.x()) / dpr)
        ly = round(g.y() + (y - g.y()) / dpr)
        w = max(1, round(self._pix_on.width() / dpr))
        h = max(1, round(self._pix_on.height() / dpr))
        self.setGeometry(lx, ly, w, h)

    def show_indicator(self):
        if not self._timer.isActive():
            self._timer.start()
        self.show()
        self.raise_()

    def hide_indicator(self):
        self._timer.stop()
        self.hide()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawPixmap(self.rect(), self._pix_on if self._dot_on else self._pix_off)
