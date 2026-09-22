#!/bin/bash
set -e

echo "========================================"
echo "YuE2 WebUI"
echo "========================================"
echo

# 检查虚拟环境
if [ ! -f ".venv/bin/activate" ]; then
    echo "[错误] 未找到虚拟环境"
    echo "请先运行: ./install.sh"
    exit 1
fi

# 激活虚拟环境
source .venv/bin/activate

# 检查模型文件
MODELS_OK=1
if [ ! -f "../models/yue2-3b-q8_0.gguf" ]; then MODELS_OK=0; fi
if [ ! -f "../models/yue2-vae-f16.gguf" ]; then MODELS_OK=0; fi

if [ "$MODELS_OK" -eq 0 ]; then
    echo "[警告] 模型文件缺失，WebUI 可能无法正常工作"
    echo "请运行 ./install.sh 查看下载指引"
    echo
fi

# 检查 audio-cpp
AUDIO_CLI=""
if [ -f "../audio-cpp/audiocpp_cli.exe" ] || [ -f "../audio-cpp/audiocpp_cli" ]; then
    AUDIO_CLI="found"
    echo "[√] audio-cpp 已就绪"
elif [ -f "../audio.cpp/build/bin/Release/audiocpp_cli.exe" ] || [ -f "../audio.cpp/build/bin/audiocpp_cli" ]; then
    AUDIO_CLI="found"
    echo "[√] audio.cpp 已就绪"
fi

if [ -z "$AUDIO_CLI" ]; then
    echo "[警告] 未找到 audiocpp_cli"
    echo
fi

echo "启动 WebUI..."
echo "访问地址: http://127.0.0.1:9898"
echo "按 Ctrl+C 停止服务"
echo

python app.py
