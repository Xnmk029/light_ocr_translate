@echo off
title 截图翻译 (Light OCR Translate) 启动器
echo 正在检测 Python 环境...

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 并添加到 PATH 环境变量中！
    pause
    exit /b 1
)

echo 正在启动 截图翻译...
echo 启动成功！软件将常驻系统右下角托盘，默认快捷键为 Ctrl + Alt + D。
echo (如果控制台没有退出，请不要关闭此窗口，关闭此窗口会退出截图翻译)
python main.py
pause
