"""全屏冻结 + 拖拽选区截图。

多显示器方案：为每个 QScreen 先 grabWindow(0) 冻结画面，再各覆盖一个
全屏遮罩窗；选区在鼠标所在屏内完成。所有坐标区分「逻辑坐标」(Qt, 受
DPI 缩放影响) 与「物理像素」(截图位图)，换算系数为 devicePixelRatio。
"""
from dataclasses import dataclass

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QGuiApplication, QImage,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import QWidget


@dataclass
class CaptureResult:
    image: QImage   # 选区图像 (物理像素, BGR888)
    rect: QRect     # 选区全局逻辑坐标 (用于原地放置结果窗)
    dpr: float      # devicePixelRatio


class _ScreenOverlay(QWidget):
    """单个屏幕的冻结遮罩：显示冻结帧 + 半透明压暗 + 橡皮筋选区。"""

    def __init__(self, mgr: "CaptureManager", screen, frozen: QPixmap):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._mgr = mgr
        self._frozen = frozen
        self._dpr = frozen.devicePixelRatio()
        self._origin = None                    # 按下起点 (逻辑坐标, None=未在拖拽)
        self._pos = QPoint(-1, -1)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    # ---------------- 交互 ----------------
    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._mgr.cancel()
        elif e.button() == Qt.LeftButton:
            self._origin = e.position().toPoint()
            self._pos = self._origin
            self.update()

    def mouseMoveEvent(self, e):
        self._pos = e.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._origin is not None:
            sel = self._sel_rect()
            self._origin = None
            if sel.width() >= 5 and sel.height() >= 5:
                self._mgr.finish(self, sel)
            else:
                self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._mgr.cancel()

    def enterEvent(self, _e):
        # 鼠标进入哪个屏, 哪个遮罩拿键盘焦点, 保证 Esc 生效
        self.activateWindow()
        self.setFocus()

    def _sel_rect(self) -> QRect:
        if self._origin is None:
            return QRect()
        return QRect(self._origin, self._pos).normalized().intersected(self.rect())

    # ---------------- 绘制 ----------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._frozen)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        sel = self._sel_rect()
        if not sel.isEmpty():
            # 选区内重绘冻结帧原图 => "掀开遮罩" 效果
            src = QRectF(sel.x() * self._dpr, sel.y() * self._dpr,
                         sel.width() * self._dpr, sel.height() * self._dpr)
            p.drawPixmap(QRectF(sel), self._frozen, src)
            p.setPen(QPen(QColor(46, 124, 246), 2))
            p.drawRect(sel)
            self._chip(p, f"{int(sel.width() * self._dpr)} × {int(sel.height() * self._dpr)}",
                       sel.x(), sel.y() - 26 if sel.y() > 28 else sel.y() + 6)
        else:
            p.setPen(QPen(QColor(255, 255, 255, 80), 1))
            p.drawLine(0, self._pos.y(), self.width(), self._pos.y())
            p.drawLine(self._pos.x(), 0, self._pos.x(), self.height())
            self._chip(p, "拖拽选择翻译区域 · Esc / 右键取消", self.width() // 2 - 110, 40)
        p.end()

    def _chip(self, p: QPainter, text: str, x: int, y: int):
        p.setFont(QFont("Microsoft YaHei UI", 9))
        w = p.fontMetrics().horizontalAdvance(text)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 15, 15, 200))
        p.drawRoundedRect(QRectF(x, y, w + 16, 22), 6, 6)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(x, y, w + 16, 22), Qt.AlignCenter, text)


class CaptureManager(QObject):
    captured = Signal(object)    # CaptureResult
    finished = Signal(bool)      # True=完成选区 False=取消

    def __init__(self, parent=None):
        super().__init__(parent)
        self._overlays: list[_ScreenOverlay] = []
        self._done = False

    @property
    def active(self) -> bool:
        return bool(self._overlays)

    def start(self) -> None:
        for screen in QGuiApplication.screens():
            pix = screen.grabWindow(0)                      # 冻结整屏 (物理像素)
            pix.setDevicePixelRatio(screen.devicePixelRatio())
            self._overlays.append(_ScreenOverlay(self, screen, pix))
        for ov in self._overlays:
            ov.show()
        target = next((ov for ov in self._overlays
                       if ov.geometry().contains(QCursor.pos())), self._overlays[0])
        target.raise_()
        target.activateWindow()
        target.setFocus()

    def cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self._close_all()
        self.finished.emit(False)

    def finish(self, overlay: _ScreenOverlay, sel: QRect) -> None:
        if self._done:
            return
        self._done = True
        dpr = overlay._dpr
        # 逻辑选区 -> 冻结位图的物理像素区域
        px = QRect(round(sel.x() * dpr), round(sel.y() * dpr),
                   round(sel.width() * dpr), round(sel.height() * dpr))
        px = px.intersected(QRect(0, 0, overlay._frozen.width(), overlay._frozen.height()))
        img = overlay._frozen.copy(px).toImage().convertToFormat(QImage.Format_BGR888)
        global_rect = QRect(overlay.geometry().topLeft() + sel.topLeft(), sel.size())
        self._close_all()
        self.captured.emit(CaptureResult(img, global_rect, dpr))
        self.finished.emit(True)

    def _close_all(self) -> None:
        for ov in self._overlays:
            ov.close()
            ov.deleteLater()
        self._overlays = []
