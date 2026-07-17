"""系统托盘 + 设置对话框 (API/快捷键/目标语言)。图标为程序绘制, 免资源文件。"""
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QLineEdit, QMenu, QSystemTrayIcon)


def _make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(46, 124, 246))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    p.setPen(QColor(255, 255, 255))
    font = QFont("Microsoft YaHei", 30, QFont.Bold)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignCenter, "译")
    p.end()
    return QIcon(pm)


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("截图翻译 - 设置")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.hotkey = QLineEdit(cfg.hotkey)
        self.base_url = QLineEdit(cfg.base_url)
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        self.api_key = QLineEdit(cfg.api_key)
        self.api_key.setEchoMode(QLineEdit.Password)
        self.model = QLineEdit(cfg.model)
        self.lang = QComboBox()
        self.lang.setEditable(True)
        self.lang.addItems(["简体中文", "繁體中文", "English", "日本語", "한국어"])
        self.lang.setCurrentText(cfg.target_lang)
        form.addRow("全局快捷键", self.hotkey)
        form.addRow("API Base URL", self.base_url)
        form.addRow("API Key", self.api_key)
        form.addRow("模型名称", self.model)
        form.addRow("目标语言", self.lang)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def apply(self, cfg) -> None:
        cfg.hotkey = self.hotkey.text().strip() or cfg.hotkey
        cfg.base_url = self.base_url.text().strip() or cfg.base_url
        cfg.api_key = self.api_key.text().strip()
        cfg.model = self.model.text().strip() or cfg.model
        cfg.target_lang = self.lang.currentText().strip() or cfg.target_lang


class Tray(QSystemTrayIcon):
    trigger_capture = Signal()
    open_settings = Signal()
    quit_app = Signal()

    def __init__(self, hotkey: str, parent=None):
        super().__init__(_make_icon(), parent)
        self.setToolTip("截图翻译")
        self._menu = QMenu()
        self._act_capture = QAction(f"截图翻译\t{hotkey}")
        self._act_capture.triggered.connect(self.trigger_capture)
        self._act_settings = QAction("设置…")
        self._act_settings.triggered.connect(self.open_settings)
        self._act_quit = QAction("退出")
        self._act_quit.triggered.connect(self.quit_app)
        self._menu.addAction(self._act_capture)
        self._menu.addAction(self._act_settings)
        self._menu.addSeparator()
        self._menu.addAction(self._act_quit)
        self.setContextMenu(self._menu)
        self.activated.connect(self._on_activated)

    def update_hotkey(self, hotkey: str) -> None:
        self._act_capture.setText(f"截图翻译\t{hotkey}")

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.trigger_capture.emit()
