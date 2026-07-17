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
3. Release -- the selected image appears pinned in-place instantly.
4. After OCR + translation completes, the text is replaced in-place on the same image.
5. Press `Esc` or right-click to exit selection mode.
6. On a result pin:
   - Press `Esc`, right-click, or double-click to close.
   - Drag to move.
   - Press `Ctrl+C` to copy all translated text.

## Packaging

```bash
pip install pyinstaller
pyinstaller -w -n LightOcrTranslate --collect-binaries onnxruntime main.py
```

Copy the `models/` directory (with ONNX files and charset.txt) to the same directory as the resulting `.exe`.

## Configuration

`config.json` is auto-created next to the executable. Key fields accessible via tray -> Settings:

| Field | Default | Description |
|-------|---------|-------------|
| hotkey | ctrl+alt+d | Global capture hotkey |
| base_url | https://api.deepseek.com/v1 | OpenAI-compatible API endpoint |
| api_key | (empty) | API key |
| model | deepseek-chat | Model name |
| target_lang | 简体中文 | Translation target language |
| erase_mode | solid | solid = fill with dominant background color; inpaint = TELEA inpainting |

## License

MIT