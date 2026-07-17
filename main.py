"""截图翻译 (Light OCR Translate) 主入口。

数据流:
  全局快捷键 -> 冻结截屏选区 -> [立即] 原地钉住原图
  -> 后台线程: PP-OCR(ONNX) 检测+识别 -> 背景色分析 + 原文擦除
  -> 后台线程: LLM 批量翻译 (OpenAI 兼容接口)
  -> 主线程: 自适应字号渲染译文 -> 结果窗无缝换图 => "原文变译文"

线程模型: UI 永不阻塞 —— OCR 走 ThreadPoolExecutor, 翻译走独立线程,
均以 Qt Signal (自动 queued) 回主线程更新界面。
"""
import sys
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon

from app import imgproc, render
from app.capture import CaptureManager, CaptureResult
from app.config import Config
from app.hotkey import GlobalHotkey
from app.ocr.pipeline import OcrEngine
from app.result_window import ResultWindow
from app.translate import Translator
from app.tray import SettingsDialog, Tray


class OcrWorker(QObject):
    """执行器线程 -> 主线程的信号桥。"""
    done = Signal(object)    # {"styled": [StyledLine], "erased": np.ndarray|None}
    error = Signal(str)


class AppController(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self._app = app
        self.cfg = Config.load()
        self.pins: list[ResultWindow] = []
        self.capture: CaptureManager | None = None

        # OCR 引擎在后台加载并预热, 不拖慢启动
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.engine_future = self.executor.submit(self._load_engine)

        self.tray = Tray(self.cfg.hotkey)
        self.tray.trigger_capture.connect(self.trigger)
        self.tray.open_settings.connect(self.show_settings)
        self.tray.quit_app.connect(self._quit)
        self.tray.show()

        self.hotkey = GlobalHotkey()
        self._register_hotkey()

        app.aboutToQuit.connect(self._cleanup)

        missing = Config.missing_models()
        if missing:
            QTimer.singleShot(600, lambda: self.tray.showMessage(
                "缺少 OCR 模型",
                f"models 目录缺少: {', '.join(missing)}\n"
                "请放入 PP-OCR 导出的 det.onnx / rec.onnx / charset.txt",
                QSystemTrayIcon.MessageIcon.Warning, 8000))
        elif not self.cfg.api_key:
            QTimer.singleShot(600, lambda: self.tray.showMessage(
                "截图翻译已就绪",
                f"请先在托盘\u2192设置中填写 API Key, 然后按 {self.cfg.hotkey} 截图",
                QSystemTrayIcon.MessageIcon.Information, 6000))

    # ---------------- 初始化 ----------------
    def _load_engine(self) -> OcrEngine:
        det, rec, charset = Config.model_paths()
        engine = OcrEngine(det, rec, charset, self.cfg.det_limit_side)
        engine.warmup()
        return engine

    def _register_hotkey(self) -> bool:
        try:
            ok = self.hotkey.register(self.cfg.hotkey, self.trigger)
        except ValueError:
            ok = False
        if not ok:
            self.tray.showMessage("快捷键注册失败",
                                  f"{self.cfg.hotkey} 无效或已被其他程序占用",
                                  QSystemTrayIcon.MessageIcon.Warning, 5000)
        return ok

    # ---------------- 截图 ----------------
    @Slot()
    def trigger(self) -> None:
        if self.capture is not None and self.capture.active:
            return
        for p in self.pins:                     # 隐藏已钉住的译文, 避免截到自己
            p.hide()
        self._app.processEvents()
        QThread.msleep(30)                      # 等 DWM 完成隐藏合成
        self.capture = CaptureManager(self)
        self.capture.captured.connect(self.on_captured)
        self.capture.finished.connect(self.on_capture_done)
        self.capture.start()

    def on_capture_done(self, _ok: bool) -> None:
        if self.capture is not None:
            self.capture.deleteLater()
            self.capture = None
        for p in self.pins:
            p.show()

    # ---------------- OCR ----------------
    def on_captured(self, res: CaptureResult) -> None:
        pin = ResultWindow(res.image, res.rect, res.dpr)
        pin.closed.connect(self._forget_pin)
        self.pins.append(pin)
        pin.show()
        pin.raise_()
        pin.activateWindow()
        pin.set_status("识别中…")

        worker = OcrWorker()
        pin._worker = worker                    # 持有引用防 GC
        worker.done.connect(lambda payload, pin=pin:
                            self._safe(pin, lambda: self.on_ocr_done(pin, payload)))
        worker.error.connect(lambda msg, pin=pin:
                             self._safe(pin, lambda: pin.set_status(f"OCR 失败: {msg}")))
        bgr = imgproc.qimage_to_bgr(res.image)
        self.executor.submit(self._ocr_job, worker, bgr)

    def _ocr_job(self, worker: OcrWorker, bgr) -> None:
        try:
            engine = self.engine_future.result()
            lines = engine.run(bgr)
            if not lines:
                worker.done.emit({"styled": [], "erased": None})
                return
            erased, styled = imgproc.analyze_and_erase(bgr, lines, self.cfg.erase_mode)
            worker.done.emit({"styled": styled, "erased": erased})
        except Exception as e:
            worker.error.emit(str(e))

    # ---------------- 翻译 ----------------
    def on_ocr_done(self, pin: ResultWindow, payload: dict) -> None:
        styled = payload["styled"]
        if not styled:
            pin.set_status("未识别到文字 (Esc 关闭)")
            return
        pin.set_status(f"翻译中… {len(styled)} 行")
        tr = Translator(self.cfg)
        pin._translator = tr
        tr.finished.connect(lambda outs, pin=pin, payload=payload:
                            self._safe(pin, lambda: self.on_translated(pin, payload, outs)))
        tr.failed.connect(lambda msg, pin=pin:
                          self._safe(pin, lambda: pin.set_status(f"翻译失败: {msg}")))
        tr.translate_lines([s.text for s in styled])

    def on_translated(self, pin: ResultWindow, payload: dict, outs: list[str]) -> None:
        final = render.draw_translations(payload["erased"], payload["styled"],
                                         outs, self.cfg.font_family)
        pin.set_image(final)
        pin.set_texts(outs)
        pin.set_status("")

    # ---------------- 杂项 ----------------
    def _safe(self, pin: ResultWindow, fn) -> None:
        """结果窗可能已被用户关闭, 只有仍存活才回调, 防悬空访问。"""
        if pin in self.pins:
            fn()

    def _forget_pin(self, pin) -> None:
        if pin in self.pins:
            self.pins.remove(pin)

    def show_settings(self) -> None:
        dlg = SettingsDialog(self.cfg)
        if dlg.exec() == QDialog.Accepted:
            dlg.apply(self.cfg)
            self.cfg.save()
            self._register_hotkey()
            self.tray.update_hotkey(self.cfg.hotkey)

    def _cleanup(self) -> None:
        self.hotkey.unregister()
        self.executor.shutdown(wait=False)

    def _quit(self) -> None:
        for p in list(self.pins):
            p.close()
        self._app.quit()


def main() -> int:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("LightOcrTranslate")
    app.setQuitOnLastWindowClosed(False)        # 常驻托盘
    controller = AppController(app)             # noqa: F841  保持引用
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
