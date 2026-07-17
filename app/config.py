"""应用配置：config.json 与程序同目录，支持打包后 (frozen) 定位。"""
import json
import os
import sys
from dataclasses import asdict, dataclass

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
MODELS_DIR = os.path.join(APP_DIR, "models")


@dataclass
class Config:
    hotkey: str = "ctrl+alt+d"
    base_url: str = "https://api.deepseek.com/v1"   # 任意 OpenAI 格式接口
    api_key: str = ""
    model: str = "deepseek-chat"
    target_lang: str = "简体中文"
    timeout: float = 30.0
    det_limit_side: int = 960          # 检测输入最长边, 越大越准越慢
    erase_mode: str = "solid"          # solid=主背景色填充 | inpaint=TELEA 修复
    font_family: str = "Microsoft YaHei UI"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    for k, v in json.load(f).items():
                        if hasattr(cfg, k):
                            setattr(cfg, k, v)
            except Exception:
                pass
        else:
            cfg.save()
        return cfg

    def save(self) -> None:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

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
