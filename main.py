"""截图翻译 (Light OCR Translate) 主入口。

数据流:
  全局快捷键 -> 关闭旧结果 -> 冻结截屏选区 -> [立即] 全屏遮罩 + 原图钉位
  -> 后台线程: PP-OCR(ONNX) 检测+识别 -> 背景色分析 + 原文擦除
  -> 后台线程: LLM 流式翻译 (逐行上屏) / 并行分块 (多路并发)
  -> 主线程: 节流渲染 (80ms 合并) + 逐行增量译文上屏 => "原文逐行变译文"

线程模型: UI 永不阻塞 —— OCR 走 ThreadPoolExecutor, 翻译走独立线程,
均以 Qt Signal (自动 queued) 回主线程更新界面。
"""
import ctypes
import gc
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
    done = Signal(object)    # {"styled": [StyledLine], "erased": ndarray, "original": ndarray}
    error = Signal(str)


class AppController(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self._app = app
        self.cfg = Config.load()
        self.pins: list[ResultWindow] = []
        self.capture: CaptureManager | None = None

        self.executor = ThreadPoolExecutor(max_workers=2)
        self.engine_future = None         # lazy: 首次快捷键按下时才加载 (休眠可卸载)
        self._asleep = True
        self._sleep_timer = QTimer(self)
        self._sleep_timer.setSingleShot(True)
        self._sleep_timer.timeout.connect(self._enter_sleep)

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
        else:
            provider = self.cfg.provider()
            if not provider.get("api_key"):
                QTimer.singleShot(600, lambda: self.tray.showMessage(
                    "截图翻译已就绪",
                    f"请先在托盘->设置中填写 API Key, 然后按 {self.cfg.hotkey} 截图",
                    QSystemTrayIcon.MessageIcon.Information, 6000))

        self._ensure_engine()          # 启动即预热引擎
        self._arm_sleep()

    # ---------------- 休眠 / 唤醒 ----------------
    def _ensure_engine(self) -> None:
        """唤醒: 引擎未加载则立即在后台加载 (与用户拖选区并行, 无感)。"""
        if self.engine_future is None:
            self.engine_future = self.executor.submit(self._load_engine)
        if self._asleep:
            self._asleep = False
            if hasattr(self, "tray"):
                self.tray.setToolTip("截图翻译")

    def _arm_sleep(self) -> None:
        """每次活动后重置休眠倒计时。"""
        self._sleep_timer.stop()
        minutes = int(getattr(self.cfg, "sleep_minutes", 10) or 0)
        if minutes > 0:
            self._sleep_timer.start(minutes * 60 * 1000)

    def _enter_sleep(self) -> None:
        """空闲休眠: 卸载 ONNX 会话, 内存回落到 UI 基线; 快捷键即唤醒。"""
        busy = bool(self.pins) or (self.capture is not None and self.capture.active)
        loading = self.engine_future is not None and not self.engine_future.done()
        if busy or loading:
            self._arm_sleep()
            return
        self.engine_future = None
        self._asleep = True
        self.tray.setToolTip("截图翻译 (休眠中, 按快捷键唤醒)")
        self._trim_os_memory()

    def _trim_os_memory(self) -> None:
        gc.collect()
        try:
            h = ctypes.windll.kernel32.GetCurrentProcess()
            try:
                ctypes.windll.psapi.EmptyWorkingSet(h)
            except Exception:
                ctypes.windll.kernel32.K32EmptyWorkingSet(h)
        except Exception:
            pass

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
                                  f"{self.cfg.hotkey} 无效或已被占用",
                                  QSystemTrayIcon.MessageIcon.Warning, 5000)
        return ok

    # ---------------- 截图 (单 pin, 新截图关旧窗) ----------------
    @Slot()
    def trigger(self) -> None:
        if self.capture is not None and self.capture.active:
            return
        self._ensure_engine()          # 休眠态按下快捷键 => 后台并行加载引擎
        self._arm_sleep()
        for p in list(self.pins):
            p.close()
        self._app.processEvents()
        QThread.msleep(30)
        self.capture = CaptureManager(self)
        self.capture.captured.connect(self.on_captured)
        self.capture.finished.connect(self.on_capture_done)
        self.capture.start()

    def on_capture_done(self, _ok: bool) -> None:
        if self.capture is not None:
            self.capture.deleteLater()
            self.capture = None
        self._arm_sleep()

    # ---------------- OCR ----------------
    def on_captured(self, res: CaptureResult) -> None:
        pin = ResultWindow(res.image, res.rect, res.dpr, res.screen)
        pin.closed.connect(self._forget_pin)
        self.pins.append(pin)
        pin.show()
        pin.raise_()
        pin.activateWindow()
        pin.set_status("识别中…")

        worker = OcrWorker()
        pin._worker = worker
        worker.done.connect(lambda payload, pin=pin:
                            self._safe(pin, lambda: self.on_ocr_done(pin, payload)))
        worker.error.connect(lambda msg, pin=pin: self._safe(pin, lambda: pin.set_status(
                            f"OCR 失败: {msg}")))
        self.executor.submit(self._ocr_job, worker, imgproc.qimage_to_bgr(res.image))

    def _ocr_job(self, worker: OcrWorker, bgr) -> None:
        try:
            engine = self.engine_future.result()
            lines = engine.run(bgr)
            if not lines:
                worker.done.emit({"styled": [], "erased": None, "original": None})
                return
            erased, styled = imgproc.analyze_and_erase(bgr, lines, self.cfg.erase_mode)
            worker.done.emit({"styled": styled, "erased": erased, "original": bgr})
        except Exception as e:
            worker.error.emit(str(e))

    # ---------------- 翻译 (流式逐行 + 节流渲染) ----------------
    def on_ocr_done(self, pin: ResultWindow, payload: dict) -> None:
        styled = payload["styled"]
        if not styled:
            pin.set_status("未识别到文字 (右键/Esc 关闭)")
            return
        pin._payload = payload
        pin._outs = [None] * len(styled)
        pin._dirty = False
        pin.set_source_texts([s.text for s in styled])   # Ctrl+Shift+C 复制原文
        pin.set_status(f"翻译中… 0/{len(styled)}")

        provider = dict(self.cfg.provider())             # 副本, 防止污染 config
        provider["target_lang"] = self.cfg.target_lang
        provider["concurrency"] = self.cfg.concurrency
        provider["chunk_lines"] = self.cfg.chunk_lines
        provider["chunk_chars"] = self.cfg.chunk_chars
        tr = Translator(provider_info=provider,
                        perf_mode=self.cfg.perf_mode,
                        timeout=self.cfg.timeout,
                        stream=self.cfg.stream_output)
        pin._translator = tr
        tr.line_done.connect(lambda start, arr, pin=pin:
                             self._safe(pin, lambda: self._on_line(pin, start, arr)))
        tr.finished.connect(lambda outs, pin=pin:
                            self._safe(pin, lambda: self._on_translated(pin, outs)))
        tr.failed.connect(lambda msg, pin=pin:
                          self._safe(pin, lambda: pin.set_status(f"翻译失败: {msg}")))
        tr.translate_lines([s.text for s in styled])

    def _on_line(self, pin: ResultWindow, start: int, arr: list[str]) -> None:
        pin._outs[start:start + len(arr)] = arr
        pin._dirty = True
        if not hasattr(pin, "_throttle_timer"):          # 80ms 合并渲染, 防高频重绘
            t = QTimer(self)
            t.setInterval(80)

            def flush(pin=pin, t=t):
                if pin not in self.pins:
                    t.stop()
                    return
                if not pin._dirty:
                    return
                pin._dirty = False
                self._render_progress(pin)
                done = sum(1 for o in pin._outs if o is not None)
                if done < len(pin._outs):
                    pin.set_status(f"翻译中… {done}/{len(pin._outs)}")
            t.timeout.connect(flush)
            pin._throttle_timer = t
            t.start()

    def _on_translated(self, pin: ResultWindow, outs: list) -> None:
        timer = getattr(pin, "_throttle_timer", None)
        if timer is not None:
            timer.stop()
        pin._outs = list(outs)
        self._render_progress(pin)
        styled = pin._payload["styled"]
        pin.set_texts([o if o is not None else s.text for s, o in zip(styled, outs)])
        miss = sum(1 for o in outs if o is None)
        pin.set_status(f"{miss} 行翻译失败" if miss else "")
        pin._payload = None          # 终图已渲染进 QPixmap, 释放 original/erased 大数组
        gc.collect()
        self._arm_sleep()

    def _render_progress(self, pin: ResultWindow) -> None:
        payload, outs = getattr(pin, "_payload", None), getattr(pin, "_outs", None)
        if not payload or outs is None:
            return
        styled = payload["styled"]
        pending = [s for s, o in zip(styled, outs) if o is None]
        canvas = imgproc.restore_regions(payload["erased"], payload["original"], pending)
        done_styled = [s for s, o in zip(styled, outs) if o is not None]
        done_outs = [o for o in outs if o is not None]
        pin.set_image(render.draw_translations(canvas, done_styled, done_outs,
                                               self.cfg.font_family))

    # ---------------- 杂项 ----------------
    def _safe(self, pin: ResultWindow, fn) -> None:
        if pin in self.pins:
            fn()

    def _forget_pin(self, pin) -> None:
        if pin in self.pins:
            self.pins.remove(pin)
        # 节流定时器 parent 是 controller, 其闭包持有 pin -> 必须显式销毁,
        # 否则 pin 及 _payload 里的整图 ndarray 永远无法回收
        t = getattr(pin, "_throttle_timer", None)
        if t is not None:
            t.stop()
            t.deleteLater()
        for attr in ("_throttle_timer", "_payload", "_outs", "_translator", "_worker"):
            if hasattr(pin, attr):
                try:
                    delattr(pin, attr)
                except AttributeError:
                    pass
        QTimer.singleShot(1500, self._maybe_trim)
        self._arm_sleep()

    def _maybe_trim(self) -> None:
        """空闲时 (无结果窗/截图) 触发 GC 并把物理内存还给系统。"""
        if self.pins or (self.capture is not None and self.capture.active):
            return
        self._trim_os_memory()

    def show_settings(self) -> None:
        dlg = SettingsDialog(self.cfg)
        if dlg.exec() == QDialog.Accepted:
            dlg.apply(self.cfg)
            self.cfg.save()
            self._register_hotkey()
            self.tray.update_hotkey(self.cfg.hotkey)
            self._arm_sleep()          # 休眠时长可能改变, 重置倒计时

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
    app.setQuitOnLastWindowClosed(False)
    controller = AppController(app)            # noqa: F841
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
