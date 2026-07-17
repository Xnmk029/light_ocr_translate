"""原地覆盖结果窗: 全屏黑色半透明遮罩 + 选区位置贴图。

要点:
- WA_TranslucentBackground: 顶层窗口必须声明透明背景, 否则半透明色
  会与不透明底色合成为灰色实底。
- 所有内部坐标均为"窗口本地坐标" (选区全局坐标减屏幕原点), 保证副屏
  (geometry 原点非 0) 上的贴图/拖拽/命中判断正确。
- Ctrl+C 复制译文, Ctrl+Shift+C 复制原文。
- 右键/Esc/点遮罩空白处退出, 左键拖移译文图。
"""
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QGuiApplication, QImage,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import QLabel, QWidget


class ResultWindow(QWidget):
    closed = Signal(object)

    def __init__(self, image: QImage, rect: QRect, dpr: float, screen=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_TranslucentBackground)   # 遮罩真正半透明的关键
        self._dpr = dpr
        self._pix: QPixmap | None = None
        self._texts: list[str] = []
        self._source_texts: list[str] = []
        self._drag: QPoint | None = None
        self._status_text = ""

        if screen is None:
            screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        geo = screen.geometry()
        self.setScreen(screen)
        self.setGeometry(geo)
        # 选区全局坐标 -> 窗口本地坐标
        self._img_rect = QRect(rect.topLeft() - geo.topLeft(), rect.size())

        self._status_label = QLabel(self)
        self._status_label.setStyleSheet(
            'background: rgba(15,15,15,220); color:#fff; padding:3px 10px;'
            'border-radius:9px; font:12px "Microsoft YaHei UI";')
        self._status_label.hide()

        self.set_image(image)

    # ---------------- 状态 ----------------
    def set_image(self, image: QImage) -> None:
        pm = QPixmap.fromImage(image)
        pm.setDevicePixelRatio(self._dpr)
        self._pix = pm
        self.update()

    def set_status(self, text: str) -> None:
        self._status_text = text
        if text:
            self._status_label.setText(text)
            self._status_label.adjustSize()
            self._reposition_label()
            self._status_label.show()
        else:
            self._status_label.hide()

    def flash_status(self, text: str, ms: int = 900) -> None:
        self.set_status(text)

        def _clear():
            try:
                self.set_status("")
            except RuntimeError:
                pass
        QTimer.singleShot(ms, _clear)

    def set_texts(self, texts: list[str]) -> None:
        self._texts = list(texts)

    def set_source_texts(self, texts: list[str]) -> None:
        self._source_texts = list(texts)

    def _reposition_label(self) -> None:
        lw = self._status_label.width()
        lh = self._status_label.height()
        x = min(self._img_rect.right() - lw - 6, self.width() - lw - 4)
        y = self._img_rect.bottom() + 6
        if y + lh > self.height() - 4:
            y = self._img_rect.top() - lh - 6
        self._status_label.move(max(4, x), max(4, min(y, self.height() - lh - 4)))

    # ---------------- 绘制 ----------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))        # 黑色半透明遮罩
        if self._pix:
            p.drawPixmap(self._img_rect, self._pix)
            p.setPen(QPen(QColor(46, 124, 246, 200), 2))
            p.drawRect(self._img_rect)
        hint = ("右键 / Esc 关闭  |  左键拖移  |  "
                "Ctrl+C 复制译文  |  Ctrl+Shift+C 复制原文")
        p.fillRect(QRect(0, self.height() - 28, self.width(), 28), QColor(0, 0, 0, 170))
        p.setPen(QColor(220, 220, 220, 200))
        p.drawText(self.rect().adjusted(0, 0, 0, -6),
                   Qt.AlignBottom | Qt.AlignHCenter, hint)
        p.end()

    # ---------------- 交互 ----------------
    def mousePressEvent(self, e):
        pos = e.position().toPoint()
        if e.button() == Qt.RightButton:
            self.close()
        elif e.button() == Qt.LeftButton:
            if self._img_rect.contains(pos):
                self._drag = pos - self._img_rect.topLeft()
            else:
                self.close()                    # 点击遮罩空白处退出

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self._img_rect.moveTopLeft(e.position().toPoint() - self._drag)
            if self._status_label.isVisible():
                self._reposition_label()
            self.update()

    def mouseReleaseEvent(self, _e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        if self._img_rect.contains(e.position().toPoint()):
            self.close()

    def keyPressEvent(self, e):
        mods = e.modifiers()
        if e.key() == Qt.Key_Escape:
            self.close()
        elif e.key() == Qt.Key_C and mods & Qt.ControlModifier:
            if mods & Qt.ShiftModifier:
                if self._source_texts:
                    QGuiApplication.clipboard().setText("\n".join(self._source_texts))
                    self.flash_status("已复制原文")
            elif self._texts:
                QGuiApplication.clipboard().setText("\n".join(self._texts))
                self.flash_status("已复制译文")

    def closeEvent(self, e):
        self._pix = None           # 释放译文位图
        self.closed.emit(self)
        super().closeEvent(e)
