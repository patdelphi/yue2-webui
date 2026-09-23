# YuE2 音乐生成系统 — 安装部署指南（Setup）

> 适用范围：Windows 10/11 本地部署。整个系统 = **YuE2 主项目**（仓库根目录）+ **WebUI**（`yue2-webui/`）+ **推理引擎**（`audio-cpp/`，预编译）+ **模型文件**（`models/`）。
> 安装顺序：**先装 YuE2 主项目环境 → 再装 WebUI 依赖 → 下载模型 → 配置 config.cfg → 启动**。

---

## 1. 系统架构与目录关系

```
Yue2/                          ← 仓库根（YuE2 主项目，pip 包名 yue2-infer）
├── src/                       ← YuE2 官方 PyTorch 推理代码（Python 后端）
├── audio-cpp/                 ← GGUF 推理引擎（预编译，含 DLL 与 SheetSage2 转谱模型）
│   ├── audiocpp_cli.exe       ← WebUI 实际调用的推理命令行（生成/转谱都靠它）
│   └── models/SheetSage2-GGUF/sheetsage2-orig.gguf   ← 转谱模型（约 2.5GB，随目录自带）
├── models/                    ← 主模型目录（需手动下载，见第 4 步）
│   ├── yue2-3b-q8_0.gguf      ← 主模型（约 4.0GB）
│   └── yue2-vae-f16.gguf      ← VAE 解码器（约 250MB）
├── .venv/                     ← 推荐的共享虚拟环境（主项目 + WebUI 共用）
└── yue2-webui/                ← 本 WebUI（Gradio 应用）
    ├── app.py                 ← 启动入口（核心模块在同目录 src/ 下）
    ├── src/                   ← 核心模块（backend_gguf / config / i18n / 队列 / 历史等）
    ├── tests/                 ← 测试套件
    ├── config.cfg             ← 模型路径外置配置（[models] 段，见第 5 步）
    └── requirements.txt       ← WebUI 依赖
```

依赖关系说明：
- WebUI 的 GGUF 后端通过**子进程调用** `audio-cpp/audiocpp_cli.exe` 完成推理，不直接 import YuE2 的 Python 代码；但整个仓库需完整获取（audio-cpp 引擎与模型体系属于整体）。
- `audio-cpp/` 已预编译（exe + ggml DLL），**无需用户编译**；若目录缺失，可从仓库附带的 `audio-cpp.zip` 解压。

---

## 2. 前置条件

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11（Linux 可用 `install.sh` / `run.sh`） |
| Python | 3.10 或更高 |
| GPU | NVIDIA GPU（推荐 8GB+ 显存）+ CUDA 11.8+；无 GPU 可用 CPU（慢） |
| 磁盘空间 | 模型共约 **7GB**（主模型 4.0GB + VAE 250MB + SheetSage2 2.5GB），另留生成输出空间 |

---

## 3. 安装步骤

### 步骤 1：获取完整仓库

```bash
git clone --recurse-submodules <仓库地址> Yue2
cd Yue2
```

或直接下载并解压发行包（含 `audio-cpp.zip` 时解压到仓库根）。

### 步骤 2：安装 YuE2 主项目环境（先装主项目）

在**仓库根目录**创建共享虚拟环境并安装主项目依赖：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .            # 安装 yue2-infer 及其依赖（torch/transformers 等）
```

> 也可用 `uv sync`（仓库含 `uv.lock`）。

### 步骤 3：安装 WebUI 依赖（再装 WebUI）

同一虚拟环境中继续：

```bash
pip install -r yue2-webui\requirements.txt
```

> 备选：`yue2-webui\install.bat` 会在 webui 目录下**另建独立 venv**（`yue2-webui\.venv`）。两种方式二选一即可；推荐用根目录共享 venv，与主项目环境一致。

### 步骤 4：下载主模型文件（不会自动下载！）

安装与启动过程**只检查、不自动下载**模型。缺失时启动日志和「设置 → 系统状态」页会给出提示。需手动下载两个文件到 `models/` 目录：

```bash
pip install huggingface-hub
huggingface-cli download patdelphi/yue2-gguf --local-dir models
```

或浏览器手动下载：<https://huggingface.co/patdelphi/yue2-gguf>

| 文件 | 大小 | 放置位置 |
| --- | --- | --- |
| `yue2-3b-q8_0.gguf` | ~4.0GB | `models/` |
| `yue2-vae-f16.gguf` | ~250MB | `models/` |

### 步骤 5：转谱模型（SheetSage2）

**无需下载**：SheetSage2 转谱模型（`sheetsage2-orig.gguf`，约 2.5GB）随 `audio-cpp/models/SheetSage2-GGUF/` 目录自带，用于「音频转谱」页的音频→ABC 乐谱功能。

若该目录为空（如部分下载），从 Hugging Face 仓库补齐 `sheetsage2-orig.gguf`，或在 `config.cfg` 中把 `sheetsage2_path` 指向已有文件。

### 步骤 6：配置 config.cfg（模型路径）

`yue2-webui/config.cfg` 决定所有模型路径（[models] 段）：

```ini
[models]
models_dir = models                          ; 主模型 + VAE 目录（相对路径基于 Yue2 系统根，也可填绝对路径）
main_model = yue2-3b-q8_0.gguf               ; 主模型文件名
vae_model = yue2-vae-f16.gguf                ; VAE 模型文件名
sheetsage2_path = audio-cpp/models/SheetSage2-GGUF/sheetsage2-orig.gguf   ; 转谱模型路径
```

规则：文件缺失或某项未填写时回退内置默认值；修改后重启 WebUI 生效；「设置 → 系统状态」页显示的路径即来自此文件。

---

## 4. 启动与验证

```bash
# 方式 A：根 venv（推荐）
.venv\Scripts\activate
python yue2-webui\app.py

# 方式 B：WebUI 脚本（使用 yue2-webui\.venv）
cd yue2-webui
run.bat
```

- 访问地址：**http://127.0.0.1:9898**（固定端口 9898）
- 后端选择：环境变量 `YUE2_BACKEND`（默认 `cuda`，无 GPU 时可设为 `cpu`）
- 验证清单：
  1. 启动日志无「模型文件缺失」警告
  2. 「设置 → 系统状态」四项全绿（主模型 / VAE / SheetSage2 / GPU 信息）
  3. 「创作」页任意输入风格与歌词后可生成；「音频转谱」页可上传音频转谱
- 界面语言：右上角下拉框切换 中文 / English（含 Gradio 内置文案同步切换）

---

## 5. 常见问题

| 现象 | 处理 |
| --- | --- |
| 启动提示模型文件缺失 | 按第 4 步下载，或检查 `config.cfg` 路径是否正确 |
| 提示未找到 audiocpp_cli.exe | 确认 `audio-cpp/` 目录完整（或解压 `audio-cpp.zip`） |
| 转谱报「SheetSage2 模型未找到」 | 检查 `config.cfg` 的 `sheetsage2_path` 指向的文件是否存在 |
| 无 NVIDIA GPU | 设置环境变量 `YUE2_BACKEND=cpu` 后重启（速度显著变慢） |
| 生成报 CUDA 错误 | 更新显卡驱动 / 确认 CUDA 11.8+；或临时切 CPU 验证流程 |
| 切换语言后组件报错 | 刷新浏览器页面（后端重启后旧页面组件序列失效） |
