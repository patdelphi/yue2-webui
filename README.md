# YuE2 Music Studio WebUI

<a id="top"></a>

**中文** | [English](#english)

本项目是 **YuE2 音乐生成 WebUI** 主项目。基于香港科技大学联合 M-A-P 团队开发的 YuE2 模型，将 **歌词 + 风格描述** 转化为 48kHz 立体声音频，并引入 **ABC 乐谱** 作为可编辑的符号化中间表示，实现"白盒"音乐创作。

> 底层推理代码位于上级目录 `src` / `backend_gguf.py`，本目录自成一个完整的 Gradio WebUI 应用。

---

## 功能特性

- 🎵 **完整创作模式**：生成乐谱 + 和弦 + 音频（`cot=full`，默认推荐）
- 🎼 **旋律创作模式**：仅生成旋律，适合翻唱（`cot=melody`）
- ⚡ **直接生成模式**：跳过乐谱，最快出结果（`cot=off`）
- 🎤 **音频转谱**：上传音频自动转写为 ABC 乐谱（SheetSage2）
- 🎨 **ABC 乐谱可视化预览**：SVG 渲染乐谱并可试听
- 📝 **歌词结构编辑器**：段落标记 + 拖拽排序 + 结构实时分析
- 💬 **歌词注释**：以 `//` 或 `**` 开头的行视为注释，不送入模型
- 🔁 **"使用上一次"**：一键恢复最近一次生成使用的风格、歌词、乐谱输入
- 🎛️ **音频后处理**：音量标准化、淡入淡出、裁剪静音、嵌入元数据
- 📦 **批量变体生成**：一次最多 10 个变体，每个独立随机种子，变体选择器试听对比并一键选定最终版
- 🎚️ **完整采样参数控制**：ABC 阶段（Stage 1）与语义 Token 阶段（Stage 2）独立温度 / Top-P / Top-K / 重复惩罚等
- 📜 **生成历史管理**：分页浏览、试听、歌词同步、乐谱预览，自动清理缺失文件记录，可删除/清空
- 🎚️ **预设系统**：5 套内置参数预设，支持自定义预设的保存与加载
- 🌐 **中英双语界面**：右上角即时切换，含 Gradio 内置文案（上传提示/页脚）同步切换；模型路径外置 `config.cfg` 配置

---

## 界面结构（4 个 Tab）

### 🎼 创作
- **风格描述**：语言 + 流派 + 乐器 + 人声 + 速度。内置风格 / 人声 / 乐器 / 情绪 / 语言 / 流派快捷标签，点击自动追加到文本框。
- **歌词**：支持 `[Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Outro]` 段落标记；提供结构模板（V-C、V-C-V-C、V-C-V-C-B-C、A-A-B-A 等）、段落拖拽排序、内容模板。
- **工作模式**：完整创作 / 旋律创作 / 直接生成。
- **ABC 乐谱**：可外部输入；生成后乐谱会回填到这里，可编辑后点「重新合成」用修改后的乐谱生成新音频。
- **生成参数**：随机种子（可勾选每次自动换新）、CFG 引导强度、ODE 求解步数、输出格式（PCM16/PCM24/Float32）、批量生成数量。
- **高级采样参数**：ABC 乐谱采样（Stage 1）与语义 Token 采样（Stage 2）的两组温度 / Top-P / Top-K / 重复惩罚 / 惩罚窗口 / Min-Max Tokens。
- **输出区**：音频播放、变体选择器（批量时显示，可"选定为最终版"或"保留全部变体"）、ABC 乐谱预览、导出 MIDI / PNG / MP3、重新合成。

### 🎤 音频转谱
- 上传音频（WAV / MP3 / FLAC / OGG / M4A 等），使用 SheetSage2 转写为 ABC 乐谱。
- 乐谱可预览、下载 ABC / MIDI，并可「发送到生成页」直接复用。

### 📜 历史
- 分页列表显示：时间、风格、模式、音频时长、生成耗时、Task ID。
- 点击记录可试听音频、查看歌词同步、预览乐谱、查看 ABC 文本与风格描述。
- 自动清理缺失文件的历史记录；支持删除选中、清空全部、刷新。

### ⚙️ 设置
- **系统状态**：检查模型文件是否就绪（模型 GGUF / VAE GGUF）。
- **当前队列**：实时显示运行中 / 排队中任务与最近完成记录（每 2 秒自动刷新）。
- **参数预设**：内置预设（默认 / 快速demo / 高质量 / 创意模式 / 保守模式），支持保存当前参数为自定义预设并加载（覆盖 CFG / 批量 / 后处理 / 采样全部 23 项参数）。

---

## 系统要求

- Windows 10/11（也可在 Linux 下通过 `run.sh` 运行）
- Python 3.10 或更高版本
- NVIDIA GPU（推荐 8GB+ VRAM）+ CUDA 11.8+（可改用 CPU 推理，速度较慢）
- 磁盘空间：模型文件至少 10GB

---

## 安装与运行

> 完整安装流程（含 YuE2 主项目环境、模型下载、转谱模型、config.cfg 配置）见 **[Docs/setup.md](Docs/setup.md)**。

### Windows
```bash
install.bat   # 在 yue2-webui 下创建独立虚拟环境并安装依赖（检查模型文件，缺失会给出下载指引）
run.bat       # 启动 WebUI
```

### Linux
```bash
bash install.sh
bash run.sh
```

启动后浏览器访问 Gradio 地址（**http://127.0.0.1:9898**）。

### 手动运行（推荐：与主项目共享根目录虚拟环境）
```bash
python -m venv .venv           # 在仓库根目录执行
.venv\Scripts\activate         # Windows；Linux 用 source .venv/bin/activate
pip install -e .               # 安装 YuE2 主项目依赖（先装主项目）
pip install -r yue2-webui/requirements.txt   # 再装 WebUI 依赖
python yue2-webui/app.py
```

---

## 模型文件与 config.cfg

模型路径由仓库根目录的外置 **`config.cfg`** 统一配置（`[models]` 段：`models_dir` / `main_model` / `vae_model` / `sheetsage2_path`，相对路径基于仓库根，也可用绝对路径；文件缺失时回退内置默认值）：

- `models/yue2-3b-q8_0.gguf` — 主模型（约 4.0GB，需手动下载，见 setup.md 第 4 步）
- `models/yue2-vae-f16.gguf` — VAE 解码器（约 250MB，同上）
- `audio-cpp/models/SheetSage2-GGUF/sheetsage2-orig.gguf` — 转谱模型（约 2.5GB，随 audio-cpp 目录自带，无需下载）

模型下载**不会自动执行**；「设置 → 系统状态」页可随时检查，显示的路径即来自 `config.cfg`。

---

## 目录结构

```
yue2-webui/
├── app.py                # 唯一入口：Gradio UI 与全部交互逻辑（核心模块在 src/）
├── src/                  # 核心业务模块
│   ├── backend_gguf.py   # GGUF 后端推理封装 + config.cfg 模型路径加载
│   ├── config.py         # 参数定义 / 默认值 / 校验 (validate_params)
│   ├── i18n.py           # 中英双语词典与 tr() 翻译接口
│   ├── queue_manager.py  # 任务队列与并发 / 取消控制
│   ├── history.py        # 生成历史持久化 (history.json)
│   ├── postprocess.py    # 音频后处理（标准化 / 淡入淡出 / 裁剪 / 元数据）
│   ├── style_presets.py  # 风格快捷标签
│   ├── vocal_presets.py  # 人声 / 乐器 / 情绪 / 语言 / 流派标签
│   └── lyrics_templates.py # 歌词内容模板
├── tests/                # 测试套件（i18n / 模型配置 / 语言持久化 / 使用上一次 / 历史回收站 / 队列 / 预设）
├── presets/              # 自定义参数预设存储
├── outputs/              # 生成结果（按任务定时戳分目录）
├── logs/                 # 运行日志
├── static/               # 前端静态资源（乐谱渲染等）
├── Docs/                 # 项目文档（setup 安装指南 / design 设计方案 / requirements 需求）
├── config.cfg            # 模型路径外置配置（[models] 段；相对路径基于上级系统根解析）
├── requirements.txt      # Python 依赖
├── install.bat / install.sh
├── run.bat / run.sh
├── history.json          # 历史记录数据
├── last_inputs.json      # 「使用上一次」记录（风格 / 歌词 / 最近生成的乐谱）
└── lang_state.json       # 界面语言持久化（重启后恢复上次选择）
```

---

## 典型参数建议

| 预设 | ODE 步数 | CFG | 说明 |
| --- | --- | --- | --- |
| 默认 | 8 | 0 | 标准质量（推荐起步） |
| 快速demo | 4 | 0 | 最快出结果，用于快速试听 |
| 高质量 | 32 | 0 | 最佳质量（PCM24 输出） |
| 创意模式 | 8 | 0 | sem_temp 1.5 / sem_top_p 0.98，更多样化 |
| 保守模式 | 8 | 0 | sem_temp 0.3 / sem_rep_penalty 1.5，更稳定 |

> CFG = 0 时为 Auto。批量生成时每个变体使用独立随机种子，固定种子仅对单个生成生效。

---

## 依赖库

见 [requirements.txt](requirements.txt)，核心为 `gradio`、`torch`、`numpy`，音频处理使用 `audio-cpp` 工具。

---

<a id="english"></a>

# YuE2 Music Studio WebUI (English)

[中文](#top) | **English**

This is the **YuE2 music generation WebUI** project. Built on the YuE2 model jointly developed by HKUST and the M-A-P team, it turns **lyrics + style descriptions** into 48kHz stereo audio, and introduces **ABC notation** as an editable symbolic intermediate representation for "white-box" music creation.

> The underlying inference code lives in the parent directory (`src` / `backend_gguf.py`); this folder is a self-contained Gradio web app.

---

## Features

- 🎵 **Full creation mode**: generates score + chords + audio (`cot=full`, default & recommended)
- 🎼 **Melody mode**: melody only, ideal for covers (`cot=melody`)
- ⚡ **Direct mode**: skips the score for the fastest results (`cot=off`)
- 🎤 **Audio transcription**: upload audio and transcribe it to an ABC score (SheetSage2)
- 🎨 **ABC score preview**: SVG-rendered sheet music with playback
- 📝 **Lyrics structure editor**: section markers + drag-to-reorder + live structure analysis
- 💬 **Lyrics comments**: lines starting with `//` or `**` are treated as comments and never sent to the model
- 🔁 **"Use last time"**: one-click restore of the style / lyrics / score from the most recent generation
- 🎛️ **Audio post-processing**: volume normalization, fade in/out, silence trimming, metadata embedding
- 📦 **Batch variants**: up to 10 variants per run, each with an independent seed; variant selector for A/B listening and one-click "set as final"
- 🎚️ **Full sampling control**: independent temperature / Top-P / Top-K / repetition penalty for the ABC stage (Stage 1) and semantic-token stage (Stage 2)
- 📜 **Generation history**: paginated browsing, playback, lyric sync, score preview; auto-cleans records with missing files; delete/clear supported
- 🎚️ **Preset system**: 5 built-in parameter presets plus save/load of custom presets
- 🌐 **Bilingual UI (Chinese/English)**: instant switch at the top right, including Gradio built-in texts (upload hints / footer); model paths configured via external `config.cfg`

---

## UI Overview (4 Tabs)

### 🎼 Create
- **Style description**: language + genre + instruments + vocals + tempo. Built-in style / vocal / instrument / mood / language / genre quick tags — click to append.
- **Lyrics**: supports `[Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge] [Outro]` section markers; structure templates (V-C, V-C-V-C, V-C-V-C-B-C, A-A-B-A, etc.), drag-to-reorder sections, content templates.
- **Work mode**: full creation / melody / direct.
- **ABC score**: can be entered externally; after generation the score is filled back here — edit it and click "Resynthesize" to generate new audio from the modified score.
- **Generation params**: random seed (optional auto-renew per run), CFG guidance scale, ODE steps, output format (PCM16/PCM24/Float32), batch variant count.
- **Advanced sampling**: two groups of temperature / Top-P / Top-K / repetition penalty / penalty window / Min-Max tokens for ABC sampling (Stage 1) and semantic-token sampling (Stage 2).
- **Output**: audio player, variant selector (shown for batches — "set as final" or "keep all"), ABC score preview, export MIDI / PNG / MP3, resynthesize.

### 🎤 Transcribe
- Upload audio (WAV / MP3 / FLAC / OGG / M4A, etc.) and transcribe it to an ABC score with SheetSage2.
- Preview the score, download ABC / MIDI, or "Send to Create" to reuse it directly.

### 📜 History
- Paginated list: time, style, mode, audio duration, elapsed time, Task ID.
- Click a record to play the audio, view lyric sync, preview the score, and inspect the ABC text and style description.
- Records with missing files are cleaned automatically; delete selected / clear all / refresh supported.

### ⚙️ Settings
- **System status**: check whether model files are ready (main GGUF / VAE GGUF).
- **Current queue**: live view of running / queued tasks and recent completions (auto-refreshes every 2s).
- **Presets**: built-in presets (Default / Quick Demo / High Quality / Creative / Conservative); save the current parameters as a custom preset and load it back (covers all 23 params — CFG / batch / post-processing / sampling).

---

## Requirements

- Windows 10/11 (also runs on Linux via `run.sh`)
- Python 3.10 or newer
- NVIDIA GPU (8GB+ VRAM recommended) + CUDA 11.8+ (CPU inference works but is much slower)
- Disk space: at least 10GB for model files

---

## Installation & Running

> For the full setup guide (YuE2 main-project environment, model download, transcription model, config.cfg), see **[Docs/setup.md](Docs/setup.md)**.

### Windows
```bash
install.bat   # creates a dedicated venv under yue2-webui and installs dependencies (checks model files, prints download hints if missing)
run.bat       # start the WebUI
```

### Linux
```bash
bash install.sh
bash run.sh
```

After startup, open the Gradio address in your browser (**http://127.0.0.1:9898**).

### Manual (recommended: share the repo-root venv with the main project)
```bash
python -m venv .venv           # run in the repository root
.venv\Scripts\activate         # Windows; on Linux use source .venv/bin/activate
pip install -e .               # install YuE2 main-project dependencies (main project first)
pip install -r yue2-webui/requirements.txt   # then WebUI dependencies
python yue2-webui/app.py
```

---

## Model Files & config.cfg

Model paths are configured centrally in the external **`config.cfg`** (`[models]` section: `models_dir` / `main_model` / `vae_model` / `sheetsage2_path`; relative paths are resolved against the repository root, absolute paths also work; falls back to built-in defaults if the file is missing):

- `models/yue2-3b-q8_0.gguf` — main model (~4.0GB, manual download, see setup.md step 4)
- `models/yue2-vae-f16.gguf` — VAE decoder (~250MB, same as above)
- `audio-cpp/models/SheetSage2-GGUF/sheetsage2-orig.gguf` — transcription model (~2.5GB, bundled with the audio-cpp folder, no download needed)

Model download is **never automatic**; check anytime under Settings → System status — the paths shown there come from `config.cfg`.

---

## Directory Layout

```
yue2-webui/
├── app.py                # single entry point: Gradio UI and all interaction logic (core modules in src/)
├── src/                  # core modules
│   ├── backend_gguf.py   # GGUF backend wrapper + config.cfg model-path loading
│   ├── config.py         # parameter definitions / defaults / validation (validate_params)
│   ├── i18n.py           # bilingual dictionary and tr() interface
│   ├── queue_manager.py  # task queue, concurrency and cancellation
│   ├── history.py        # generation history persistence (history.json)
│   ├── postprocess.py    # audio post-processing (normalize / fade / trim / metadata)
│   ├── style_presets.py  # style quick tags
│   ├── vocal_presets.py  # vocal / instrument / mood / language / genre tags
│   └── lyrics_templates.py # lyric content templates
├── tests/                # test suites (i18n / model config / lang persistence / use-last-time / recycle bin / queue / presets)
├── presets/              # custom parameter presets
├── outputs/              # generation results (one folder per task timestamp)
├── logs/                 # runtime logs
├── static/               # frontend static assets (score rendering, etc.)
├── Docs/                 # documentation (setup guide / design / requirements)
├── config.cfg            # external model-path config ([models] section; relative paths resolved against the parent system root)
├── requirements.txt      # Python dependencies
├── install.bat / install.sh
├── run.bat / run.sh
├── history.json          # history data
├── last_inputs.json      # "use last time" record (style / lyrics / latest generated score)
└── lang_state.json       # UI language persistence (restored after restart)
```

---

## Typical Parameter Presets

| Preset | ODE steps | CFG | Notes |
| --- | --- | --- | --- |
| Default | 8 | 0 | standard quality (recommended starting point) |
| Quick Demo | 4 | 0 | fastest results, for quick previews |
| High Quality | 32 | 0 | best quality (PCM24 output) |
| Creative | 8 | 0 | sem_temp 1.5 / sem_top_p 0.98, more variety |
| Conservative | 8 | 0 | sem_temp 0.3 / sem_rep_penalty 1.5, more stable |

> CFG = 0 means Auto. Each variant in a batch gets an independent random seed; the fixed seed only applies to single generations.

---

## Dependencies

See [requirements.txt](requirements.txt) — the core ones are `gradio`, `torch`, and `numpy`; audio processing uses the `audio-cpp` tools.