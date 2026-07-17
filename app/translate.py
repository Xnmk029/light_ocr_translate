"""LLM 翻译: OpenAI 兼容 /chat/completions。

双模式:
- 流式 (stream=True): SSE 逐 token 推送, ArrayScanner 增量扫描 JSON 数组,
  每个字符串元素一闭合立即 emit line_done(全局行偏移, 新行), 首行秒级上屏;
  流式异常自动降级为非流式请求。
- 高性能模式 (perf_mode=turbo): 小分块 (3 行) + 高并发 (8 路), 长文总延迟
  约为标准模式的 1/2 ~ 1/3; 标准模式分块大, 上下文更完整。

所有信号从后台线程发出, Qt 自动 queued 回主线程, UI 永不阻塞。
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

TURBO_CONCURRENCY = 8
TURBO_CHUNK_LINES = 3
TURBO_CHUNK_CHARS = 240


class ArrayScanner:
    """流式 JSON 数组的增量解析器: 每当一个元素闭合立即产出, 无需等整段合法。"""

    def __init__(self):
        self._buf = ""
        self._pos = 0
        self._in_array = False
        self.emitted = 0          # 已产出的元素个数

    def text(self) -> str:
        return self._buf

    def feed(self, piece: str) -> list[str]:
        """喂入新字符流, 返回本次新闭合的元素。"""
        self._buf += piece
        out: list[str] = []
        while True:
            el = self._next_element()
            if el is None:
                break
            out.append(el)
        self.emitted += len(out)
        return out

    def _next_element(self):
        buf, n = self._buf, len(self._buf)
        if not self._in_array:
            i = buf.find("[", self._pos)
            if i == -1:
                return None
            self._pos = i + 1
            self._in_array = True
        p = self._pos
        while p < n and buf[p] in " \t\r\n,":
            p += 1
        if p >= n or buf[p] == "]":
            self._pos = p
            return None
        if buf[p] == '"':                       # 字符串元素: 扫描非转义闭合引号
            j, esc = p + 1, False
            while j < n:
                c = buf[j]
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    raw = buf[p:j + 1]
                    self._pos = j + 1
                    try:
                        return json.loads(raw)
                    except Exception:
                        return raw[1:-1]
                j += 1
            return None                          # 字符串未闭合, 等更多数据
        j = p                                    # 裸元素 (数字/null 等)
        while j < n and buf[j] not in ",]":
            j += 1
        if j >= n:
            return None
        token = buf[p:j].strip()
        self._pos = j
        return token or None


class Translator(QObject):
    line_done = Signal(int, list)        # (全局行偏移, [新增行, ...])
    finished = Signal(list)              # 全部完成, 失败位置为 None
    failed = Signal(str)                 # 所有块均失败

    def __init__(self, provider_info: dict, perf_mode: str = "standard",
                 timeout: float = 30.0, stream: bool = True, parent=None):
        super().__init__(parent)
        self._p = provider_info
        self._timeout = timeout
        self._turbo = (perf_mode == "turbo")
        self._stream = stream
        self._lock = threading.Lock()

    def translate_lines(self, lines: list[str]) -> None:
        lines = list(lines)
        if not lines:
            self.finished.emit([])
            return
        base_url = (self._p.get("base_url") or "").strip()
        if not base_url:
            self.failed.emit("未配置 Base URL (托盘 → 设置)")
            return
        local = "127.0.0.1" in base_url or "localhost" in base_url
        if not self._p.get("api_key") and not local:
            self.failed.emit("未配置 API Key (托盘 → 设置)")
            return
        chunks = self._split(lines)
        self._outs: list = [None] * len(lines)
        self._errors: list[str] = []
        self._total_chunks = len(chunks)
        self._remaining = len(chunks)
        conc = TURBO_CONCURRENCY if self._turbo else \
            max(1, int(self._p.get("concurrency", 4)))
        sem = threading.Semaphore(conc)
        for start, chunk in chunks:
            threading.Thread(target=self._chunk_work,
                             args=(start, chunk, sem), daemon=True).start()

    def _split(self, lines: list[str]) -> list[tuple[int, list[str]]]:
        max_lines = TURBO_CHUNK_LINES if self._turbo else \
            max(1, int(self._p.get("chunk_lines", 8)))
        max_chars = TURBO_CHUNK_CHARS if self._turbo else \
            max(50, int(self._p.get("chunk_chars", 600)))
        chunks, start, buf, chars = [], 0, [], 0
        for i, ln in enumerate(lines):
            if buf and (len(buf) >= max_lines or chars + len(ln) > max_chars):
                chunks.append((start, buf))
                start, buf, chars = i, [], 0
            buf.append(ln)
            chars += len(ln)
        if buf:
            chunks.append((start, buf))
        return chunks

    def _chunk_work(self, start: int, chunk: list[str], sem) -> None:
        arr, err = None, None
        with sem:
            try:
                arr = self._request(chunk, start)
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
            except Exception as e:
                err = str(e)
        with self._lock:
            self._remaining -= 1
            last = self._remaining == 0
            if arr is not None:
                arr = arr[:len(chunk)]
                self._outs[start:start + len(arr)] = arr
            elif err:
                self._errors.append(err)
            if last:
                if len(self._errors) >= self._total_chunks:
                    self.failed.emit(self._errors[0] if self._errors else "未知错误")
                else:
                    self.finished.emit(list(self._outs))

    # ---------------- 请求 ----------------
    def _request(self, chunk: list[str], start: int) -> list[str]:
        if self._stream:
            try:
                return self._do_stream(chunk, start)
            except Exception:
                pass                                 # 流式失败 -> 非流式兜底
        return self._do_nonstream(chunk)

    def _build_request(self, chunk: list[str], stream: bool) -> urllib.request.Request:
        payload = {
            "model": self._p.get("model", ""),
            "messages": [
                {"role": "system",
                 "content": SYSTEM_PROMPT.format(
                     lang=self._p.get("target_lang", "简体中文"))},
                {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "stream": stream,
        }
        headers = {"Content-Type": "application/json"}
        if self._p.get("api_key"):
            headers["Authorization"] = f"Bearer {self._p['api_key']}"
        return urllib.request.Request(
            self._p["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"), headers=headers)

    def _do_stream(self, chunk: list[str], start: int) -> list[str]:
        req = self._build_request(chunk, stream=True)
        scanner = ArrayScanner()
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    choices = json.loads(data).get("choices") or []
                    delta = (choices[0].get("delta") or {}) if choices else {}
                    content = delta.get("content") or ""
                except Exception:
                    continue
                if not content:
                    continue
                prev = scanner.emitted                 # 已产出行数 = 块内偏移
                new = scanner.feed(content)
                if new:
                    self.line_done.emit(start + prev, [str(x) for x in new])
        return _parse_array(scanner.text(), chunk)

    def _do_nonstream(self, chunk: list[str]) -> list[str]:
        req = self._build_request(chunk, stream=False)
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_array(data["choices"][0]["message"]["content"], chunk)


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
