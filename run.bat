@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo YuE2 WebUI
echo ========================================
echo.

REM 检查虚拟环境
if not exist ".venv\Scripts\activate.bat" (
    echo [错误] 未找到虚拟环境
    echo 请先运行: install.bat
    pause
    exit /b 1
)

REM 激活虚拟环境
call .venv\Scripts\activate.bat

REM 检查模型文件
set MODELS_OK=1
if not exist "..\models\yue2-3b-q8_0.gguf" set MODELS_OK=0
if not exist "..\models\yue2-vae-f16.gguf" set MODELS_OK=0

if %MODELS_OK%==0 (
    echo [警告] 模型文件缺失，WebUI 可能无法正常工作
    echo 请运行 install.bat 查看下载指引
    echo.
)

REM 检查 audio-cpp
set AUDIO_CLI=
if exist "..\audio-cpp\audiocpp_cli.exe" (
    set AUDIO_CLI=..\audio-cpp\audiocpp_cli.exe
) else if exist "..\audio.cpp\build\bin\Release\audiocpp_cli.exe" (
    set AUDIO_CLI=..\audio.cpp\build\bin\Release\audiocpp_cli.exe
)

if "%AUDIO_CLI%"=="" (
    echo [警告] 未找到 audiocpp_cli.exe
    echo.
) else (
    echo [√] audio-cpp 已就绪
)

echo 启动 WebUI...
echo 访问地址: http://127.0.0.1:9898
echo 按 Ctrl+C 停止服务
echo.

python app.py

if errorlevel 1 (
    echo.
    echo [错误] WebUI 启动失败
    pause
)
