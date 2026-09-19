@echo off
chcp 65001 >nul
echo YuE2 Music Studio
echo =================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到Python，请先安装Python 3.10+
    pause
    exit /b 1
)

python -c "import gradio" >nul 2>&1
if errorlevel 1 (
    echo 安装Gradio...
    pip install gradio^>=5.0
)

echo 启动中... 打开 http://localhost:7860
echo.
python app.py

pause
