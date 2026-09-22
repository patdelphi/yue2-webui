@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo YuE2 WebUI 安装脚本
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10 或更高版本
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo [√] Python 版本: %PYTHON_VERSION%

REM 检查是否在正确的目录
if not exist "app.py" (
    echo [错误] 未找到 app.py
    echo 请确保在 yue2-webui 目录下运行此脚本
    pause
    exit /b 1
)

echo.
echo [1/4] 创建虚拟环境...
if not exist ".venv" (
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
    echo [√] 虚拟环境已创建
) else (
    echo [√] 虚拟环境已存在
)

echo.
echo [2/4] 激活虚拟环境...
call .venv\Scripts\activate.bat
echo [√] 虚拟环境已激活

echo.
echo [3/4] 安装 Python 依赖...
python -m pip install --upgrade pip -q

if exist "requirements.txt" (
    pip install -r requirements.txt -q
    if errorlevel 1 (
        echo [错误] 安装依赖失败
        pause
        exit /b 1
    )
    echo [√] 依赖安装完成
) else (
    echo [!] 未找到 requirements.txt，跳过依赖安装
)

echo.
echo [4/4] 检查外部依赖...
set DEPS_OK=1

REM 检查 audio-cpp (在上级目录)
if exist "..\audio-cpp\audiocpp_cli.exe" (
    echo [√] audio-cpp 已就绪
) else if exist "..\audio.cpp\build\bin\Release\audiocpp_cli.exe" (
    echo [√] audio.cpp 已就绪
) else (
    echo [!] 未找到 audiocpp_cli.exe
    echo     请确保 audio.cpp 已正确编译
    set DEPS_OK=0
)

REM 检查模型文件 (在上级目录)
if exist "..\models\yue2-3b-q8_0.gguf" (
    echo [√] 主模型已就绪
) else (
    echo [!] 缺少主模型: ..\models\yue2-3b-q8_0.gguf
    set DEPS_OK=0
)

if exist "..\models\yue2-vae-f16.gguf" (
    echo [√] VAE 模型已就绪
) else (
    echo [!] 缺少 VAE 模型: ..\models\yue2-vae-f16.gguf
    set DEPS_OK=0
)

if %DEPS_OK%==0 (
    echo.
    echo ========================================
    echo 外部依赖下载指引:
    echo ========================================
    echo.
    echo 模型文件下载:
    echo   pip install huggingface-hub
    echo   huggingface-cli download patdelphi/yue2-gguf --local-dir ..\models
    echo.
    echo 或手动下载:
    echo   访问: https://huggingface.co/patdelphi/yue2-gguf
    echo   下载到 ..\models 目录:
    echo     - yue2-3b-q8_0.gguf
    echo     - yue2-vae-f16.gguf
    echo.
    echo ========================================
)

echo.
echo ========================================
echo 安装完成！
echo ========================================
echo.
echo 启动 WebUI:
echo   1. 运行: run.bat
echo   2. 或手动: .venv\Scripts\activate ^&^& python app.py
echo.
echo 访问地址: http://127.0.0.1:9898
echo.
pause
