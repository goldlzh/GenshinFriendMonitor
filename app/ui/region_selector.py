"""手动框选扫描区域：全屏半透明覆盖层，拖动鼠标框选，返回物理像素矩形。

说明：
  - 进程已启用 DPI 感知（见 window.enable_dpi_awareness），Qt 坐标是逻辑坐标，
    因此需要乘以所在屏幕的 devicePixelRatio 才能得到 mss 截图用的物理像素坐标。
  - 该组件不依赖窗口标题，适用于「云原神」等标题不含关键字、或窗口大小不一的场景。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget


class RegionSelector(QWidget):
    """全屏覆盖层。

    释放左键发出 ``selected(left, top, right, bottom)``（物理像素）；
    按 Esc 或右键发出 ``canceled``。
    """

    selected = Signal(int, int, int, int)
    canceled = Signal()

    def __init__(self):
        super().__init__()
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._dragging = False

        self.setGeometry(self._virtual_geometry())
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    def _virtual_geometry(self) -> QRect:
        """覆盖所有屏幕的虚拟矩形（逻辑坐标）。"""
        screens = QGuiApplication.screens()
        left = min(s.geometry().left() for s in screens)
        top = min(s.geometry().top() for s in screens)
        right = max(s.geometry().left() + s.geometry().width() for s in screens)
        bottom = max(s.geometry().top() + s.geometry().height() for s in screens)
        return QRect(left, top, right - left, bottom - top)

    def _to_physical(self, pos: QPoint):
        """逻辑坐标 → 物理像素坐标。"""
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio()
        return round(pos.x() * dpr), round(pos.y() * dpr)

    # ------------------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._dragging = True
            self._origin = e.globalPosition().toPoint()
            self._current = self._origin
            self.update()
        elif e.button() == Qt.RightButton:
            self._cancel()

    def mouseMoveEvent(self, e) -> None:
        if self._dragging:
            self._current = e.globalPosition().toPoint()
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.LeftButton or not self._dragging:
            return
        self._dragging = False
        self._current = e.globalPosition().toPoint()

        left, top = self._to_physical(self._origin)
        right, bottom = self._to_physical(self._current)
        l, r = min(left, right), max(left, right)
        t, b = min(top, bottom), max(top, bottom)

        if r - l <= 5 or b - t <= 5:
            # 只是单击（尚未拖动出有效区域）→ 不取消，继续等待用户重新框选
            self._origin = None
            self._current = None
            self.update()
            return

        self.close()
        self.selected.emit(l, t, r, b)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Escape:
            self._cancel()

    def _cancel(self) -> None:
        self.close()
        self.canceled.emit()

    # ------------------------------------------------------------------
    def showEvent(self, e) -> None:
        super().showEvent(e)
        # 确保覆盖层获得焦点、位于最前，避免首次点击被用于激活窗口而丢失
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._origin is not None and self._current is not None:
            o = self.mapFromGlobal(self._origin)
            c = self.mapFromGlobal(self._current)
            rect = QRect(o, c).normalized()
            p.fillRect(rect, QColor(255, 255, 255, 40))
            pen = QPen(QColor(0, 170, 255), 2)
            p.setPen(pen)
            p.drawRect(rect)
        # 操作提示
        p.setPen(QColor(255, 255, 255))
        f = p.font()
        f.setPointSize(13)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, "按住鼠标左键拖动，框选完整的 16:9 游戏画面（含四个角）\n不要只框好友横条；按 Esc 或右键取消")
        p.end()
