"""应用配置: config.json 与程序同目录, 支持打包后 (frozen) 定位。

多供应商: providers 列表存放各家 OpenAI 兼容接口配置, active_provider
指定当前使用者; 旧版单供应商字段 (base_url/api_key/model) 自动迁移。
"""
import json
import os
import sys
from dataclasses import asdict, dataclass, field

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
MODELS_DIR = os.path.join(APP_DIR, "models")

# 内置供应商预设: (名称, Base URL, 推荐模型列表)
PROVIDER_PRESETS: list[tuple[str, str, list[str]]] = [
    ("DeepSeek", "https://api.deepseek.com/v1",
     ["deepseek-chat", "deepseek-reasoner"]),
    ("通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1",
     ["qwen-turbo", "qwen-plus", "qwen-max", "qwen-mt-turbo"]),
    ("OpenAI", "https://api.openai.com/v1",
     ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"]),
    ("Kimi", "https://api.moonshot.cn/v1",
     ["moonshot-v1-8k", "kimi-latest"]),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4",
     ["glm-4-flash", "glm-4-air"]),
    ("SiliconFlow", "https://api.siliconflow.cn/v1",
     ["Qwen/Qwen2.5-7B-Instruct", "THUDM/glm-4-9b-chat"]),
    ("Ollama 本地", "http://127.0.0.1:11434/v1",
     ["qwen2.5:7b", "llama3.1:8b"]),
]


def suggested_models(base_url: str) -> list[str]:
    for _, url, models in PROVIDER_PRESETS:
        if url.rstrip("/") == (base_url or "").rstrip("/"):
            return list(models)
    return []


def _default_providers() -> list[dict]:
    return [{"name": n, "base_url": u, "api_key": "", "model": m[0]}
            for n, u, m in PROVIDER_PRESETS]


@dataclass
class Config:
    hotkey: str = "ctrl+alt+d"
    target_lang: str = "简体中文"
    timeout: float = 30.0
    det_limit_side: int = 960          # 检测输入最长边, 越大越准越慢
    erase_mode: str = "solid"          # solid=主背景色填充 | inpaint=TELEA 修复
    font_family: str = "Microsoft YaHei UI"
    perf_mode: str = "standard"        # standard | turbo (高并发小分块, 长文提速)
    stream_output: bool = True         # SSE 流式输出, 译文逐行增量上屏
    sleep_minutes: int = 10            # 空闲 N 分钟后休眠释放 OCR 引擎 (0=不休眠)
    concurrency: int = 4               # standard 模式翻译并发请求数
    chunk_lines: int = 8               # standard 模式单请求最多行数
    chunk_chars: int = 600             # standard 模式单请求最多字符数
    providers: list = field(default_factory=_default_providers)
    active_provider: str = "DeepSeek"

    # ---------------- 读写 ----------------
    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            legacy = {k: data.pop(k, "") for k in ("base_url", "api_key", "model")}
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
            if not cfg.providers:
                cfg.providers = _default_providers()
            cfg._migrate_legacy(legacy)
        else:
            cfg.save()
        return cfg

    def save(self) -> None:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    def _migrate_legacy(self, legacy: dict) -> None:
        """旧版单供应商配置 -> 写入匹配的预设或插入'自定义'条目。"""
        if not any(legacy.values()):
            return
        url = (legacy.get("base_url") or "").rstrip("/")
        for p in self.providers:
            if url and p.get("base_url", "").rstrip("/") == url:
                p["api_key"] = legacy.get("api_key") or p.get("api_key", "")
                if legacy.get("model"):
                    p["model"] = legacy["model"]
                self.active_provider = p["name"]
                self.save()
                return
        self.providers.insert(0, {
            "name": "自定义", "base_url": legacy.get("base_url") or "",
            "api_key": legacy.get("api_key") or "", "model": legacy.get("model") or ""})
        self.active_provider = "自定义"
        self.save()

    # ---------------- 访问器 ----------------
    def provider(self) -> dict:
        """当前生效的供应商配置。"""
        for p in self.providers:
            if p.get("name") == self.active_provider:
                return p
        return self.providers[0] if self.providers else \
            {"name": "", "base_url": "", "api_key": "", "model": ""}

    @staticmethod
    def model_paths() -> tuple[str, str, str]:
        return (
            os.path.join(MODELS_DIR, "det.onnx"),
            os.path.join(MODELS_DIR, "rec.onnx"),
            os.path.join(MODELS_DIR, "charset.txt"),
        )

    @staticmethod
    def missing_models() -> list[str]:
        return [os.path.basename(p) for p in Config.model_paths() if not os.path.exists(p)]
