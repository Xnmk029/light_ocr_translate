"""系统托盘 + 多模型设置对话框。

修复要点: 打开对话框时先回填 cfg 全部字段; 供应商切换用 _cur=-1 哨兵
避免"首次加载即把空字段回存覆盖已有配置"; 增删供应商时阻断信号后再
程序化选中, 保证存/取时序正确。
"""
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLineEdit, QMenu, QPushButton,
                               QSystemTrayIcon, QVBoxLayout)

from .config import PROVIDER_PRESETS, suggested_models


def _make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(46, 124, 246))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    p.setPen(QColor(255, 255, 255))
    p.setFont(QFont("Microsoft YaHei", 30, QFont.Bold))
    p.drawText(pm.rect(), Qt.AlignCenter, "译")
    p.end()
    return QIcon(pm)


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("截图翻译 — 设置")
        self.setMinimumWidth(520)
        self._providers: list[dict] = [dict(p) for p in cfg.providers] or [
            {"name": "自定义", "base_url": "", "api_key": "", "model": ""}]
        self._cur = -1        # 当前编辑中的供应商下标; -1 = 尚未加载(禁止回存)

        root = QVBoxLayout(self)

        # ── 供应商选择栏 ──
        bar = QHBoxLayout()
        self._prov_combo = QComboBox()
        bar.addWidget(self._prov_combo, 1)
        btn_add = QPushButton("新增")
        btn_add.clicked.connect(self._on_add_clicked)
        bar.addWidget(btn_add)
        self._btn_add = btn_add
        btn_del = QPushButton("删除")
        btn_del.clicked.connect(self._delete_provider)
        bar.addWidget(btn_del)
        root.addLayout(bar)

        # ── 供应商字段 ──
        form = QFormLayout()
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        form.addRow("Base URL", self.base_url)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        form.addRow("API Key", self.api_key)
        self.model = QComboBox()
        self.model.setEditable(True)
        form.addRow("模型名称", self.model)

        # ── 全局字段 (打开时回填当前配置) ──
        self.lang = QComboBox()
        self.lang.setEditable(True)
        self.lang.addItems(["简体中文", "繁體中文", "English", "日本語", "한국어"])
        self.lang.setCurrentText(cfg.target_lang)
        form.addRow("目标语言", self.lang)
        self.hotkey = QLineEdit(cfg.hotkey)
        form.addRow("全局快捷键", self.hotkey)
        self.perf = QComboBox()
        self.perf.addItem("标准 (并发 4 / 8 行分块)", "standard")
        self.perf.addItem("高速 (并发 8 / 3 行分块, 长文更快)", "turbo")
        self.perf.setCurrentIndex(1 if cfg.perf_mode == "turbo" else 0)
        form.addRow("性能模式", self.perf)
        self.sleep = QComboBox()
        for label, minutes in (("不休眠", 0), ("5 分钟", 5), ("10 分钟", 10),
                               ("30 分钟", 30), ("60 分钟", 60)):
            self.sleep.addItem(label, minutes)
        idx = {0: 0, 5: 1, 10: 2, 30: 3, 60: 4}.get(int(cfg.sleep_minutes), 2)
        self.sleep.setCurrentIndex(idx)
        form.addRow("空闲休眠", self.sleep)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # ── 初始化供应商列表并选中 active ──
        self._rebuild_combo()
        self._prov_combo.currentIndexChanged.connect(self._on_switch)
        names = [p.get("name") for p in self._providers]
        start = names.index(cfg.active_provider) if cfg.active_provider in names else 0
        self._select(start)

    # ---------------- 供应商管理 ----------------
    def _rebuild_combo(self) -> None:
        self._prov_combo.blockSignals(True)
        self._prov_combo.clear()
        for p in self._providers:
            self._prov_combo.addItem(p.get("name", "未命名"))
        self._prov_combo.blockSignals(False)

    def _select(self, idx: int) -> None:
        """程序化选中: combo 已在该项时手动触发加载。"""
        idx = max(0, min(idx, len(self._providers) - 1))
        if self._prov_combo.currentIndex() != idx:
            self._prov_combo.setCurrentIndex(idx)   # 触发 _on_switch
        else:
            self._on_switch(idx)

    def _on_switch(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._providers):
            return
        self._store_current()                       # _cur=-1 时自动跳过
        self._cur = idx
        p = self._providers[idx]
        self.base_url.setText(p.get("base_url", ""))
        self.api_key.setText(p.get("api_key", ""))
        self.model.blockSignals(True)
        self.model.clear()
        models = suggested_models(p.get("base_url", ""))
        if models:
            self.model.addItems(models)
        self.model.setCurrentText(p.get("model", ""))
        self.model.blockSignals(False)

    def _store_current(self) -> None:
        """把编辑器里的字段写回当前供应商 dict。"""
        if 0 <= self._cur < len(self._providers):
            p = self._providers[self._cur]
            p["base_url"] = self.base_url.text().strip()
            p["api_key"] = self.api_key.text().strip()
            p["model"] = self.model.currentText().strip()

    def _on_add_clicked(self) -> None:
        menu = QMenu(self)
        for name, url, models in PROVIDER_PRESETS:
            act = menu.addAction(f"预设: {name}")
            act.triggered.connect(
                lambda _=False, n=name, u=url, m=models[0]: self._add_provider(n, u, m))
        menu.addSeparator()
        menu.addAction("自定义 (空白)").triggered.connect(
            lambda: self._add_provider("自定义", "", ""))
        menu.exec(self._btn_add.mapToGlobal(self._btn_add.rect().bottomLeft()))

    def _add_provider(self, name: str, url: str, model: str) -> None:
        self._store_current()
        base, i = name, 2
        existing = {p.get("name") for p in self._providers}
        while name in existing:
            name = f"{base} {i}"
            i += 1
        self._providers.append(
            {"name": name, "base_url": url, "api_key": "", "model": model})
        self._cur = -1                              # 重建后禁止把旧字段误存新项
        self._rebuild_combo()
        self._select(len(self._providers) - 1)

    def _delete_provider(self) -> None:
        if len(self._providers) <= 1:
            return
        idx = self._prov_combo.currentIndex()
        if not (0 <= idx < len(self._providers)):
            return
        del self._providers[idx]
        self._cur = -1                              # 被删项的字段不再回存
        self._rebuild_combo()
        self._select(min(idx, len(self._providers) - 1))

    # ---------------- 应用 ----------------
    def apply(self, cfg) -> None:
        self._store_current()
        cfg.providers = self._providers
        idx = self._prov_combo.currentIndex()
        if 0 <= idx < len(self._providers):
            cfg.active_provider = self._providers[idx].get("name", "")
        cfg.target_lang = self.lang.currentText().strip() or cfg.target_lang
        cfg.hotkey = self.hotkey.text().strip() or cfg.hotkey
        cfg.perf_mode = self.perf.currentData() or "standard"
        cfg.sleep_minutes = int(self.sleep.currentData())


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
        self._act_settings = QAction("设置...")
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
