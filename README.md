# Light OCR Translate

[简体中文](README_ZH.md) | English

Screen region OCR + in-place overlay translation tool for Windows. Select any area on screen with a global hotkey, and the recognized text will be replaced directly on the image by its translation -- no popup windows, no context switching.

## Architecture

| Layer | Technology | Role |
|-------|-----------|------|
| GUI | PySide6 (Qt6) | Native Windows rendering, minimal footprint |
| OCR | PP-OCR (ONNX Runtime CPU) | Text detection (DB) + recognition (CTC), no PyTorch/PaddlePaddle |
| Translation | OpenAI-compatible API | Async LLM call, configurable Base URL / Model / Key |
| Hotkey | Win32 RegisterHotKey (ctypes) | Zero-dependency global shortcut |

File layout:

```
light_ocr_translate/
  main.py                  -- entry, threading orchestration
  requirements.txt
  启动.bat                 -- Windows batch launcher (with console output)
  无控制台启动.bat         -- Windows batch launcher (silent, no console window)
  app/
    config.py              -- JSON config (auto-created)
    hotkey.py              -- Win32 global hotkey
    capture.py             -- multi-monitor freeze + rubber-band selection
    result_window.py       -- floating overlay result window
    tray.py                -- system tray + settings dialog
    translate.py           -- OpenAI /chat/completions batch translation
    imgproc.py             -- background color extraction + text erasure
    render.py              -- adaptive font sizing + rotated-box rendering
    ocr/
      det.py               -- DB text detection
      rec.py               -- CTC text recognition
      pipeline.py          -- detection -> cropping -> recognition
  models/
    .gitkeep               -- place ONNX models here
```

## Dependencies

Only 5 third-party packages (no Electron, no CEF, no full DL framework):

- PySide6-Essentials
- onnxruntime
- numpy
- opencv-python-headless
- pyclipper

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Place PP-OCR ONNX models in `models/`:

   | File | Source |
   |------|--------|
   | `det.onnx` | PP-OCR v4/v5/v6 mobile detection model, exported via paddle2onnx |
   | `rec.onnx` | PP-OCR v4/v5/v6 mobile recognition model, exported via paddle2onnx |
   | `charset.txt` | Character dictionary matching the recognition model (e.g. ppocrv5_dict.txt) |

   *Note: If you have `rapidocr-onnxruntime` installed in your Python environment, you can copy the ONNX files directly from its package directory to `models/` and extract the character list metadata as `charset.txt`.*

   Example conversion (run once on any machine, PaddlePaddle not required at runtime):

   ```bash
   paddle2onnx --model_dir ch_PP-OCRv6_mobile_det_infer --model_filename inference.pdmodel --params_filename inference.pdiparams --save_file models/det.onnx --opset_version 14
   paddle2onnx --model_dir ch_PP-OCRv6_mobile_rec_infer --model_filename inference.pdmodel --params_filename inference.pdiparams --save_file models/rec.onnx --opset_version 14
   ```

3. Launch:

   * **Console Launch**: Double-click **`启动.bat`**, or run:
     ```bash
     python main.py
     ```
   * **Silent Launch**: Double-click **`无控制台启动.bat`**.

   The app resides in the system tray. Configure API endpoint in tray -> Settings before first use.

## Usage

1. Press the global hotkey (default: `Ctrl+Alt+D`). The screen freezes.
2. Drag to select the region to translate.
3. Release -- the selected image appears pinned in-place instantly on a translucent mask.
4. After OCR + translation completes, all text is replaced in-place (streaming renders line-by-line).
5. **View modes**: click toolbar or press `1` (translated), `2` (original), `3` (side-by-side comparison).
6. **Pin**: press `P` or click "钉住" to detach as a floating always-on-top window (survives subsequent captures).
7. **Save**: press `S` or click "保存" to save original + translated images to disk.
8. Close: `Esc`, right-click anywhere, or click the dark mask outside the image.

### Performance Modes

| Mode | Description |
|------|-------------|
| standard | Moderate concurrency (4), larger chunk (8 lines), balanced speed/quality |
| turbo | High concurrency (8), small chunk (3 lines), fast first-paint for long text, lower per-chunk context quality |

Turbo mode + streaming achieves near-instant first-line display for long text.

### Memory

- ONNX Runtime memory arena is disabled (`enable_cpu_mem_arena = False`), so inference buffers are returned to the OS after each run instead of growing a permanent pool.
- Recognition batches are width-budgeted to bound peak activation memory.
- Capture frames, OCR intermediates, and result bitmaps are released as soon as their window closes; an idle-time working-set trim returns freed pages to the OS.

### Idle Sleep and Fast Wake

After `sleep_minutes` of inactivity (default 10, configurable in Settings) the ONNX sessions are unloaded and memory drops to the bare UI baseline. Pressing the hotkey wakes the app instantly: the engine reloads in a background thread while you are still dragging the selection, so the wake-up latency is fully hidden by the selection gesture.

## Download (Portable)

Grab the portable x64 build from [Releases](https://github.com/Xnmk029/light_ocr_translate/releases): unzip anywhere and run `LightOcrTranslate.exe`. No installation, no admin rights required.

Since v0.3.0 the PP-OCR ONNX models ship inside the zip (Apache License 2.0, redistributable), so it works out of the box.

## Packaging

```bash
pip install pyinstaller
pyinstaller -w -n LightOcrTranslate --collect-binaries onnxruntime main.py
```

Copy the `models/` directory (with ONNX files and charset.txt) to the same directory as the resulting `.exe`.

## Configuration

`config.json` is auto-created next to the executable. Key fields accessible via Settings:

| Field | Default | Description |
|-------|---------|-------------|
| hotkey | ctrl+alt+d | Global capture hotkey |
| target_lang | 简体中文 | Translation target language |
| perf_mode | standard | standard or turbo |
| stream_output | true | Whether to use SSE streaming for progressive rendering |
| sleep_minutes | 10 | Idle minutes before unloading the OCR engine (0 = never sleep) |
| concurrency | 4 | Concurrent translation requests |
| chunk_lines | 8 | Max lines per chunk in standard mode |
| chunk_chars | 600 | Max characters per chunk in standard mode |
| erase_mode | solid | solid = fill with dominant background; inpaint = TELEA inpainting |

Providers are managed entirely in the Settings dialog (add / remove / switch presets, configure Base URL / API Key / Model per provider). Built-in presets include DeepSeek, Qwen, OpenAI, Kimi, GLM, SiliconFlow, Ollama.

## License

MIT