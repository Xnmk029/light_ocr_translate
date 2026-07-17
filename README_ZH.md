# Light OCR Translate (截图翻译)

简体中文 | [English](README.md)

适用于 Windows 的屏幕区域 OCR + 原地覆盖翻译工具。使用全局快捷键选择屏幕上的任意区域，识别出的文本将直接在原图上被翻译文本替换——无弹窗，无上下文切换。

## 架构设计

| 分层 | 技术栈 | 作用 |
|------|-----------|------|
| GUI | PySide6 (Qt6) | 原生 Windows 界面与渲染，极小内存占用 |
| OCR | PP-OCR (ONNX Runtime CPU) | 文本检测 (DB) + 文本识别 (CTC)，无需安装 PyTorch/PaddlePaddle |
| 翻译 | OpenAI 兼容接口 | 异步大模型调用，支持自定义 Base URL、模型名称与 API Key |
| 快捷键 | Win32 RegisterHotKey (ctypes) | 零依赖的系统级全局快捷键 |

文件布局：

```
light_ocr_translate/
  main.py                  -- 入口函数，线程调度
  requirements.txt
  启动.bat                 -- Windows 启动脚本（显示控制台日志）
  无控制台启动.bat         -- Windows 静默启动脚本（不显示控制台窗口）
  app/
    config.py              -- JSON 配置管理（自动生成）
    hotkey.py              -- Win32 全局快捷键注册
    capture.py             -- 多显示器截图冻结与橡皮筋选区
    result_window.py       -- 悬浮覆盖结果窗口
    tray.py                -- 系统托盘菜单与设置对话框
    translate.py           -- OpenAI /chat/completions 接口批量翻译
    imgproc.py             -- 背景色提取与原文字幕擦除
    render.py              -- 自适应字号计算与旋转文本矩形渲染
    ocr/
      det.py               -- DB 文本检测器
      rec.py               -- CTC 文本识别器
      pipeline.py          -- 检测 -> 裁剪 -> 识别完整管线
  models/
    .gitkeep               -- 用于存放 ONNX 模型文件
```

## 依赖项

仅需 5 个第三方包（不含 Electron、无 CEF、无需庞大的深度学习框架）：

- PySide6-Essentials
- onnxruntime
- numpy
- opencv-python-headless
- pyclipper

## 安装与配置

1. **安装依赖**：

   ```bash
   pip install -r requirements.txt
   ```

2. **放置 PP-OCR ONNX 模型到 `models/` 目录**：

   | 文件名 | 说明 |
   |------|--------|
   | `det.onnx` | PP-OCR v4/v5/v6 轻量检测模型（通过 paddle2onnx 导出） |
   | `rec.onnx` | PP-OCR v4/v5/v6 轻量识别模型（通过 paddle2onnx 导出） |
   | `charset.txt` | 与识别模型匹配的字符字典（如 ppocrv5_dict.txt） |

   *提示：如果您的 Python 环境中已安装 `rapidocr-onnxruntime`，可以参考 `rapidocr` 目录下的 ONNX 文件直接复制到 `models/` 目录，并将对应的 `rec` 元数据导出为 `charset.txt`。*

   示例转换命令（在任意机器运行一次即可，运行时无需 PaddlePaddle）：

   ```bash
   paddle2onnx --model_dir ch_PP-OCRv6_mobile_det_infer --model_filename inference.pdmodel --params_filename inference.pdiparams --save_file models/det.onnx --opset_version 14
   paddle2onnx --model_dir ch_PP-OCRv6_mobile_rec_infer --model_filename inference.pdmodel --params_filename inference.pdiparams --save_file models/rec.onnx --opset_version 14
   ```

3. **启动程序**：

   * **控制台启动**：双击运行 **`启动.bat`** 即可，或执行命令：
     ```bash
     python main.py
     ```
   * **后台静默启动**：双击运行 **`无控制台启动.bat`**。

   启动后程序将常驻于系统右下角托盘。首次使用前，请右键托盘图标 -> **设置**，配置您的 API 接口信息。

## 使用方法

1. 按下全局快捷键（默认：`Ctrl+Alt+D`），屏幕会被冻结并变暗。
2. 拖动鼠标左键选择需要翻译的屏幕区域。
3. 松开鼠标——选中的图片区域会立即被原地钉住（钉图窗口）。
4. 后台 OCR 识别与 LLM 翻译完成后，钉图窗口中的原文会被直接无缝替换为译文（流式模式下译文逐行浮现）。
5. 按下 `Esc`、右键任意位置、或点击图外的暗色遮罩即可关闭结果。
6. 左键拖动译文图可调整位置；`Ctrl+C` 复制译文，`Ctrl+Shift+C` 复制原文。

### 性能模式

| 模式 | 说明 |
|------|------|
| standard | 并发 4 路、8 行分块，速度与语境质量均衡 |
| turbo | 并发 8 路、3 行分块，长文首屏更快，单块语境略降 |

### 内存与休眠

- 关闭 ONNX Runtime 内存池（arena），推理缓冲用完即还系统；识别批次按宽度预算限制峰值内存。
- 空闲 `sleep_minutes` 分钟（默认 10，设置中可改）后自动休眠：卸载 OCR 引擎，内存回落到纯 UI 基线。
- 快速唤醒：按下快捷键的瞬间引擎即在后台线程重新加载，与拖拽选区并行完成，唤醒延迟被选区手势完全掩盖，体感零等待。

## 下载（免安装版）

从 [Releases](https://github.com/Xnmk029/light_ocr_translate/releases) 下载 x64 免安装压缩包：解压到任意目录，将三个模型文件放入 exe 旁的 `models/` 文件夹，双击 `LightOcrTranslate.exe` 即可运行，无需安装、无需管理员权限。

## 打包

```bash
pip install pyinstaller
pyinstaller -w -n LightOcrTranslate --collect-binaries onnxruntime main.py
```

打包完成后，请将 `models/` 文件夹（包含 ONNX 文件与 charset.txt）复制到生成的 `.exe` 可执行文件所在同级目录下。

## 配置文件说明

程序首次启动时会自动在主程序目录下生成 `config.json` 配置文件。您可以通过右键托盘 -> **设置** 进行修改，常用字段如下：

| 字段名 | 默认值 | 说明 |
|-------|---------|-------------|
| hotkey | ctrl+alt+d | 全局截图翻译快捷键 |
| target_lang | 简体中文 | 翻译目标语言 |
| perf_mode | standard | standard 标准 / turbo 高速 |
| stream_output | true | SSE 流式输出，译文逐行上屏 |
| sleep_minutes | 10 | 空闲休眠分钟数（0 = 不休眠） |
| concurrency | 4 | 标准模式翻译并发数 |
| chunk_lines | 8 | 标准模式单请求最大行数 |
| chunk_chars | 600 | 标准模式单请求最大字符数 |
| erase_mode | solid | solid = 主背景色填充；inpaint = 使用 TELEA 算法进行图像修复 |

多供应商（DeepSeek / 通义千问 / OpenAI / Kimi / 智谱 / SiliconFlow / Ollama 本地）在设置对话框中管理：可切换、新增、删除预设，每个供应商独立保存 Base URL / API Key / 模型名。

## 开源协议

MIT
