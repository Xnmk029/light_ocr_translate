"""结果窗体系:

- ResultWindow: 全屏黑色半透明遮罩 + 选区位置贴图 + 工具条
  视图模式: 译文 / 原文 / 对照(上原文下译文); 支持保存两图、钉住为悬浮窗。
- PinnedWindow: 可拖动、置顶的悬浮钉图窗, 右键菜单切换视图/保存/关闭。

坐标约定: 遮罩窗内部均为"窗口本地坐标"(全局坐标减屏幕原点); 图像均为
物理像素 QImage, 显示时按 devicePixelRatio 缩放, 保证 1:1 清晰。
"""
import os
from datetime import datetime

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QGuiApplication, QImage,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QButtonGroup, QFileDialog, QHBoxLayout, QLabel,
                               QMenu, QPushButton, QWidget)

VIEW_TRANS = "trans"
VIEW_ORIG = "orig"
VIEW_COMPARE = "compare"

_BAR_QSS = """
QWidget#bar { background: transparent; }
QPushButton {
    background: rgba(35,35,35,235); color: #ddd;
    border: 1px solid #555; border-radius: 6px;
    padding: 3px 12px; font: 12px "Microsoft YaHei UI";
}
QPushButton:hover { border-color: #2E7CF6; color: #fff; }
QPushButton:checked { background: #2E7CF6; border-color: #2E7CF6; color: #fff; }
QPushButton:disabled { color: #666; border-color: #3a3a3a; }
"""


def compose_view(orig: QImage, trans, mode: str, dpr: float) -> QImage:
    """按视图模式合成显示图 (物理像素)。对照 = 上原文 + 分隔线 + 下译文。"""
    if mode == VIEW_ORIG or trans is None:
        return orig
    if mode == VIEW_TRANS:
        return trans
    gap = max(2, round(2 * dpr))
    w = max(orig.width(), trans.width())
    h = orig.height() + gap + trans.height()
    out = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    out.fill(QColor(46, 124, 246))                    # 分隔线颜色透出
    p = QPainter(out)
    p.drawImage(0, 0, orig)
    p.drawImage(0, orig.height() + gap, trans)
    p.end()
    return out


def save_pair(parent: QWidget, orig: QImage, trans) -> int:
    """弹出保存对话框, 落盘 原图/译图 两份 (译图未就绪则只存原图)。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    default = os.path.join(os.path.expanduser("~"), "Desktop", f"LightOCR_{ts}.png")
    path, _ = QFileDialog.getSaveFileName(
        parent, "保存图片 (自动保存 原文/译文 两份)", default, "PNG 图片 (*.png)")
    if not path:
        return -1
    stem, ext = os.path.splitext(path)
    ext = ext or ".png"
    n = 0
    if orig is not None and orig.save(f"{stem}_原文{ext}"):
        n += 1
    if trans is not None and trans.save(f"{stem}_译文{ext}"):
        n += 1
    return n


class ResultWindow(QWidget):
    closed = Signal(object)
    pin_requested = Signal(object)     # dict: 交给 controller 创建 PinnedWindow

    def __init__(self, image: QImage, rect: QRect, dpr: float, screen=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_TranslucentBackground)   # 遮罩真正半透明的关键
        self._dpr = dpr
        self._orig_img = image
        self._trans_img = None
        self._mode = VIEW_ORIG
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
        self._img_rect = QRect(rect.topLeft() - geo.topLeft(), rect.size())

        self._status_label = QLabel(self)
        self._status_label.setStyleSheet(
            'background: rgba(15,15,15,220); color:#fff; padding:3px 10px;'
            'border-radius:9px; font:12px "Microsoft YaHei UI";')
        self._status_label.hide()
        self._build_bar()
        self._apply_mode(VIEW_ORIG)

    # ---------------- 工具条 ----------------
    def _build_bar(self) -> None:
        self._bar = QWidget(self)
        self._bar.setObjectName("bar")
        self._bar.setStyleSheet(_BAR_QSS)
        lay = QHBoxLayout(self._bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._btn_trans = QPushButton("译文")
        self._btn_orig = QPushButton("原文")
        self._btn_cmp = QPushButton("对照")
        group = QButtonGroup(self)
        for b, mode in ((self._btn_trans, VIEW_TRANS), (self._btn_orig, VIEW_ORIG),
                        (self._btn_cmp, VIEW_COMPARE)):
            b.setCheckable(True)
            group.addButton(b)
            b.clicked.connect(lambda _=False, m=mode: self._apply_mode(m))
            lay.addWidget(b)
        self._btn_trans.setEnabled(False)
        self._btn_cmp.setEnabled(False)
        self._btn_orig.setChecked(True)
        self._btn_pin = QPushButton("钉住")
        self._btn_pin.clicked.connect(self._do_pin)
        lay.addWidget(self._btn_pin)
        self._btn_save = QPushButton("保存")
        self._btn_save.clicked.connect(self._do_save)
        lay.addWidget(self._btn_save)
        self._bar.adjustSize()

    # ---------------- 视图 ----------------
    def set_image(self, image: QImage) -> None:
        """译文图更新入口 (进行中/最终帧都走这里)。首帧自动切到译文视图。"""
        first = self._trans_img is None
        self._trans_img = image
        if first:
            self._btn_trans.setEnabled(True)
            self._btn_cmp.setEnabled(True)
            self._btn_trans.setChecked(True)
            self._apply_mode(VIEW_TRANS)
        else:
            self._apply_mode(self._mode)

    def _apply_mode(self, mode: str) -> None:
        if mode in (VIEW_TRANS, VIEW_COMPARE) and self._trans_img is None:
            mode = VIEW_ORIG
        self._mode = mode
        img = compose_view(self._orig_img, self._trans_img, mode, self._dpr)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(self._dpr)
        self._pix = pm
        logical = QSize(round(img.width() / self._dpr), round(img.height() / self._dpr))
        r = QRect(self._img_rect.topLeft(), logical)
        if r.bottom() > self.height() - 40:            # 对照模式变高: 底部越界上移
            r.moveBottom(self.height() - 40)
        if r.top() < 4:
            r.moveTop(4)
        self._img_rect = r
        self._layout_overlays()
        self.update()

    def _layout_overlays(self) -> None:
        r = self._img_rect
        bar = self._bar
        bx = min(max(4, r.center().x() - bar.width() // 2), self.width() - bar.width() - 4)
        by = r.bottom() + 8
        if by + bar.height() > self.height() - 32:
            by = max(4, r.top() - bar.height() - 8)
        bar.move(bx, by)
        bar.show()
        if self._status_label.isVisible():
            self._place_status()

    def _place_status(self) -> None:
        lw, lh = self._status_label.width(), self._status_label.height()
        x = min(max(4, self._img_rect.left()), self.width() - lw - 4)
        y = self._img_rect.top() - lh - 6
        if y < 4:
            y = self._img_rect.top() + 6
        self._status_label.move(x, y)

    # ---------------- 状态 ----------------
    def set_status(self, text: str) -> None:
        self._status_text = text
        if text:
            self._status_label.setText(text)
            self._status_label.adjustSize()
            self._place_status()
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

    # ---------------- 动作 ----------------
    def _do_pin(self) -> None:
        self.pin_requested.emit({
            "orig": self._orig_img, "trans": self._trans_img, "mode": self._mode,
            "pos": self.geometry().topLeft() + self._img_rect.topLeft(),
            "dpr": self._dpr, "texts": self._texts,
            "source_texts": self._source_texts,
        })
        self.close()

    def _do_save(self) -> None:
        n = save_pair(self, self._orig_img, self._trans_img)
        if n >= 0:
            self.flash_status(f"已保存 {n} 张" if n else "保存失败", 1200)

    # ---------------- 绘制 ----------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        if self._pix:
            p.drawPixmap(self._img_rect, self._pix)
            p.setPen(QPen(QColor(46, 124, 246, 200), 2))
            p.drawRect(self._img_rect)
        hint = ("右键 / Esc 关闭 | 拖移图像 | 1 译文 2 原文 3 对照 | P 钉住 | "
                "S 保存 | Ctrl+C 复制译文 | Ctrl+Shift+C 复制原文")
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
                self.close()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self._img_rect.moveTopLeft(e.position().toPoint() - self._drag)
            self._layout_overlays()
            self.update()

    def mouseReleaseEvent(self, _e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        if self._img_rect.contains(e.position().toPoint()):
            self.close()

    def keyPressEvent(self, e):
        mods = e.modifiers()
        key = e.key()
        if key == Qt.Key_Escape:
            self.close()
        elif key == Qt.Key_1:
            self._sync_check(self._btn_trans, VIEW_TRANS)
        elif key == Qt.Key_2:
            self._sync_check(self._btn_orig, VIEW_ORIG)
        elif key == Qt.Key_3:
            self._sync_check(self._btn_cmp, VIEW_COMPARE)
        elif key == Qt.Key_P:
            self._do_pin()
        elif key == Qt.Key_S:
            self._do_save()
        elif key == Qt.Key_C and mods & Qt.ControlModifier:
            if mods & Qt.ShiftModifier:
                if self._source_texts:
                    QGuiApplication.clipboard().setText("\n".join(self._source_texts))
                    self.flash_status("已复制原文")
            elif self._texts:
                QGuiApplication.clipboard().setText("\n".join(self._texts))
                self.flash_status("已复制译文")

    def _sync_check(self, btn: QPushButton, mode: str) -> None:
        if btn.isEnabled():
            btn.setChecked(True)
            self._apply_mode(mode)

    def closeEvent(self, e):
        self._pix = None
        self.closed.emit(self)
        super().closeEvent(e)


class PinnedWindow(QWidget):
    """可拖动、常驻最上层的悬浮钉图窗。"""
    closed = Signal(object)

    def __init__(self, orig: QImage, trans, mode: str, pos: QPoint, dpr: float,
                 texts=None, source_texts=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._orig = orig
        self._trans = trans
        self._mode = mode
        self._dpr = dpr
        self._texts = list(texts or [])
        self._source_texts = list(source_texts or [])
        self._pix: QPixmap | None = None
        self._drag: QPoint | None = None
        self._apply()
        self.move(pos)

    def _apply(self) -> None:
        img = compose_view(self._orig, self._trans, self._mode, self._dpr)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(self._dpr)
        self._pix = pm
        self.setFixedSize(round(img.width() / self._dpr), round(img.height() / self._dpr))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        if self._pix:
            p.drawPixmap(self.rect(), self._pix)
        p.setPen(QPen(QColor(46, 124, 246, 220), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.end()

    # ---------------- 交互 ----------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif e.button() == Qt.RightButton:
            self._menu(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e):
        self._drag = None

    def mouseDoubleClickEvent(self, _e):
        self.close()

    def keyPressEvent(self, e):
        mods = e.modifiers()
        if e.key() == Qt.Key_Escape:
            self.close()
        elif e.key() == Qt.Key_C and mods & Qt.ControlModifier:
            texts = self._source_texts if mods & Qt.ShiftModifier else self._texts
            if texts:
                QGuiApplication.clipboard().setText("\n".join(texts))

    def _menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        for label, mode in (("译文", VIEW_TRANS), ("原文", VIEW_ORIG), ("对照", VIEW_COMPARE)):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._mode == mode)
            act.setEnabled(self._trans is not None or mode == VIEW_ORIG)
            act.triggered.connect(lambda _=False, m=mode: self._switch(m))
        menu.addSeparator()
        menu.addAction("保存原图/译图").triggered.connect(
            lambda: save_pair(self, self._orig, self._trans))
        menu.addAction("复制译文").triggered.connect(
            lambda: QGuiApplication.clipboard().setText("\n".join(self._texts)))
        menu.addAction("复制原文").triggered.connect(
            lambda: QGuiApplication.clipboard().setText("\n".join(self._source_texts)))
        menu.addSeparator()
        menu.addAction("关闭").triggered.connect(self.close)
        menu.exec(global_pos)

    def _switch(self, mode: str) -> None:
        self._mode = mode
        self._apply()

    def closeEvent(self, e):
        self._pix = None
        self._orig = None
        self._trans = None
        self.closed.emit(self)
        super().closeEvent(e)
