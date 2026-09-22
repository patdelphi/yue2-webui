#!/bin/bash
set -e

echo "========================================"
echo "YuE2 WebUI 安装脚本"
echo "========================================"
echo

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 Python3，请先安装 Python 3.10 或更高版本"
    echo "下载地址: https://www.python.org/downloads/"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "[√] Python 版本: $PYTHON_VERSION"

# 检查是否在正确的目录
if [ ! -f "app.py" ]; then
    echo "[错误] 未找到 app.py"
    echo "请确保在 yue2-webui 目录下运行此脚本"
    exit 1
fi

echo
echo "[1/4] 创建虚拟环境..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "[√] 虚拟环境已创建"
else
    echo "[√] 虚拟环境已存在"
fi

echo
echo "[2/4] 激活虚拟环境..."
source .venv/bin/activate
echo "[√] 虚拟环境已激活"

echo
echo "[3/4] 安装 Python 依赖..."
python -m pip install --upgrade pip -q

if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt -q
    echo "[√] 依赖安装完成"
else
    echo "[!] 未找到 requirements.txt，跳过依赖安装"
fi

echo
echo "[4/4] 检查外部依赖..."
DEPS_OK=1

# 检查 audio-cpp (在上级目录)
if [ -f "../audio-cpp/audiocpp_cli.exe" ] || [ -f "../audio-cpp/audiocpp_cli" ]; then
    echo "[√] audio-cpp 已就绪"
elif [ -f "../audio.cpp/build/bin/Release/audiocpp_cli.exe" ] || [ -f "../audio.cpp/build/bin/audiocpp_cli" ]; then
    echo "[√] audio.cpp 已就绪"
else
    echo "[!] 未找到 audiocpp_cli"
    echo "    请确保 audio.cpp 已正确编译"
    DEPS_OK=0
fi

# 检查模型文件 (在上级目录)
if [ -f "../models/yue2-3b-q8_0.gguf" ]; then
    echo "[√] 主模型已就绪"
else
    echo "[!] 缺少主模型: ../models/yue2-3b-q8_0.gguf"
    DEPS_OK=0
fi

if [ -f "../models/yue2-vae-f16.gguf" ]; then
    echo "[√] VAE 模型已就绪"
else
    echo "[!] 缺少 VAE 模型: ../models/yue2-vae-f16.gguf"
    DEPS_OK=0
fi

if [ "$DEPS_OK" -eq 0 ]; then
    echo
    echo "========================================"
    echo "外部依赖下载指引:"
    echo "========================================"
    echo
    echo "模型文件下载:"
    echo "  pip install huggingface-hub"
    echo "  huggingface-cli download patdelphi/yue2-gguf --local-dir ../models"
    echo
    echo "或手动下载:"
    echo "  访问: https://huggingface.co/patdelphi/yue2-gguf"
    echo "  下载到 ../models 目录:"
    echo "    - yue2-3b-q8_0.gguf"
    echo "    - yue2-vae-f16.gguf"
    echo
    echo "========================================"
fi

echo
echo "========================================"
echo "安装完成！"
echo "========================================"
echo
echo "启动 WebUI:"
echo "  1. 运行: ./run.sh"
echo "  2. 或手动: source .venv/bin/activate && python app.py"
echo
echo "访问地址: http://127.0.0.1:9898"
echo
