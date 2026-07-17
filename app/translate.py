"""LLM 翻译: OpenAI 兼容 /chat/completions, 后台线程 + Qt 信号回主线程。

协议: 把 OCR 行打包成 JSON 数组一次性请求 (单次往返, 低延迟低成本),
要求模型返回等长 JSON 数组, 逐行对应; 解析失败时逐级降级兜底。
仅用标准库 urllib, 不引入额外 HTTP 依赖; 自动遵循系统 HTTPS_PROXY 环境变量。
"""
import json
import re
import threading
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, Signal

SYSTEM_PROMPT = (
    "你是嵌入在截图翻译工具中的翻译引擎。输入是一个 JSON 字符串数组, 每个元素是屏幕 OCR 出的一行文本。\n"
    "把每个元素翻译成{lang}, 输出一个与输入等长、顺序一一对应的 JSON 数组。\n"
    "规则:\n"
    "1. 只输出 JSON 数组本身, 禁止 markdown 代码块、解释或任何多余文字;\n"
    "2. 数字、URL、邮箱、代码标识符、品牌名保持原样;\n"
    "3. 某行已是{lang}或无需翻译时原样返回;\n"
    "4. 译文务必简洁, 长度尽量接近原文, 因为要贴回原文所在的版面位置。"
)


class Translator(QObject):
    finished = Signal(list)   # list[str] 与输入行等长
    failed = Signal(str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg

    def translate_lines(self, lines: list[str]) -> None:
        threading.Thread(target=self._work, args=(list(lines),), daemon=True).start()

    def _work(self, lines: list[str]) -> None:
        cfg = self._cfg
        if not cfg.api_key:
            self.failed.emit("未配置 API Key (托盘图标 → 设置)")
            return
        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(lang=cfg.target_lang)},
                {"role": "user", "content": json.dumps(lines, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "stream": False,
        }
        req = urllib.request.Request(
            cfg.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {cfg.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            self.finished.emit(_parse_array(content, lines))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:200]
            self.failed.emit(f"HTTP {e.code}: {body}")
        except Exception as e:
            self.failed.emit(str(e))


def _parse_array(content: str, fallback: list[str]) -> list[str]:
    """稳健解析: 剥代码围栏 -> 取首尾中括号 JSON -> 失败按行切 -> 补齐截断。"""
    n = len(fallback)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            arr = json.loads(text[start:end + 1])
            if isinstance(arr, list):
                arr = [str(x) for x in arr]
                arr += fallback[len(arr):]
                return arr[:n]
        except Exception:
            pass
    rows = [r.strip() for r in text.splitlines() if r.strip()]
    rows += fallback[len(rows):]
    return rows[:n]
