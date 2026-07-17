"""原地覆盖结果窗: 无边框置顶, 精确钉在选区的屏幕位置上,
先显示原图 (瞬时反馈), OCR/翻译完成后无缝换成替换了译文的图。
Esc/右键/双击关闭, 左键拖动, Ctrl+C 复制全部译文。
"""
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QGuiApplication, QImage, QKeySequence,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import QLabel, QWidget


class ResultWindow(QWidget):
    closed = Signal(object)

    def __init__(self, image: QImage, rect: QRect, dpr: float):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._dpr = dpr
        self._pix = None
        self._texts: list[str] = []
        self._drag = None
        self._status = QLabel(self)
        self._status.setStyleSheet(
            'background: rgba(15,15,15,210); color: #fff; padding: 3px 10px;'
            'border-radius: 9px; font: 12px "Microsoft YaHei UI";')
        self._status.hide()
        self.setGeometry(rect)
        self.set_image(image)

    # ---------------- 状态更新 ----------------
    def set_image(self, image: QImage) -> None:
        pm = QPixmap.fromImage(image)
        pm.setDevicePixelRatio(self._dpr)    # 物理像素图按 DPR 显示 => 与屏幕 1:1 对齐
        self._pix = pm
        self.update()

    def set_status(self, text: str) -> None:
        if text:
            self._status.setText(text)
            self._status.adjustSize()
            self._status.move(max(2, self.width() - self._status.width() - 8),
                              max(2, self.height() - self._status.height() - 8))
            self._status.show()
        else:
            self._status.hide()

    def flash_status(self, text: str, ms: int = 900) -> None:
        self.set_status(text)

        def _clear():
            try:
                self.set_status("")
            except RuntimeError:
                pass                     # 窗口已被关闭销毁
        QTimer.singleShot(ms, _clear)

    def set_texts(self, texts: list[str]) -> None:
        self._texts = list(texts)

    # ---------------- 绘制/交互 ----------------
    def paintEvent(self, _e):
        p = QPainter(self)
        if self._pix:
            p.drawPixmap(0, 0, self._pix)
        p.setPen(QPen(QColor(46, 124, 246, 170), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.end()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
        elif e.matches(QKeySequence.Copy) and self._texts:
            QGuiApplication.clipboard().setText("\n".join(self._texts))
            self.flash_status("已复制")

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.close()
        elif e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e):
        self._drag = None

    def mouseDoubleClickEvent(self, _e):
        self.close()

    def closeEvent(self, e):
        self.closed.emit(self)
        super().closeEvent(e)
