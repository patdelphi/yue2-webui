# YuE2 WebUI 详细设计方案

> 版本: v2.0 | 日期: 2026-09-19 | 目标硬件: RTX 3080 10GB / Windows

---

## 目录

1. [YuE2 完整能力梳理](#1-yue2-完整能力梳理)
2. [项目结构决策](#2-项目结构决策)
3. [技术架构](#3-技术架构)
4. [数据模型与接口定义](#4-数据模型与接口定义)
5. [后端模块详细设计](#5-后端模块详细设计)
6. [Gradio UI 详细设计](#6-gradio-ui-详细设计)
7. [任务管理与并发控制](#7-任务管理与并发控制)
8. [历史记录与预设系统](#8-历史记录与预设系统)
9. [错误处理与边界情况](#9-错误处理与边界情况)
10. [开发计划与里程碑](#10-开发计划与里程碑)

---

## 1. YuE2 完整能力梳理

### 1.1 系统概述

YuE2 是香港科技大学联合 M-A-P 团队开发的音乐生成模型，核心能力是将 **歌词 + 风格描述** 转化为 **48kHz 立体声音频**。其独特之处在于引入了 **ABC 乐谱** 作为可编辑的符号化中间表示，实现了"白盒"音乐创作。

- 模型参数量: 3B (AR-NAR MoT) + VAE decoder
- 上下文窗口: 24576 tokens
- 词表大小: 184704 tokens (含 32768 codec tokens)
- 输出: 48kHz 立体声 WAV/FLAC
- 支持语言: 多语言 (通过 style 参数指定，如 "English", "Mandarin")

### 1.2 四种工作流模式

#### 模式 A: 完整创作 (`cot=full`) — 默认推荐

```
输入: style + lyrics
  ↓
Stage 1 - plan(): 模型自回归生成 ABC 乐谱 (含旋律 + 和弦标记)
  ↓  输出: "X:1\nM:4/4\n..." (含 "C", "G", "Am" 等和弦符号)
  ↓  指令: "Generate a chord-annotated ABC transcription, then generate music..."
  ↓
Stage 2 - generate_semantic(): 基于乐谱条件生成语义 codec tokens
  ↓  CFG scale 默认 1.0 (无引导)
  ↓
Stage 3 - synthesize(): NAR 声学 flow matching (ODE 求解)
  ↓  输出: [frames, 64] float32 latents
  ↓
Stage 4 - decode(): VAE 解码为波形
  ↓  输出: 48kHz 立体声 PCM
```

**适用场景**: 从零创作完整歌曲，需要可编辑乐谱供后续修改
**预计耗时**: ~23s (GGUF Q8_0, ODE 8步, ~66s 音频)

#### 模式 B: 旋律创作 (`cot=melody`)

```
输入: style + lyrics
  ↓
Stage 1 - plan(): 生成仅旋律的 ABC (无和弦符号)
  ↓  指令: "Generate a melody-only ABC transcription without chord symbols..."
  ↓  伴奏有更大自由度适配新风格
  ↓
Stage 2-4: 同 full 模式
```

**适用场景**: Cover/翻唱，给定旋律重新编曲；或希望伴奏更自由适配风格
**与 full 的区别**: 不生成和弦，伴奏编排更自由

#### 模式 C: 直接生成 (`cot=off`)

```
输入: style + lyrics
  ↓
Stage 1 - plan(): 跳过，空 ABC (prefix 中 ABC_START 直接接 ABC_END)
  ↓  指令: "Generate music with codec tokens from the given conditions."
  ↓  CFG scale 默认 1.01 (轻微引导)
  ↓  负向提示: 仅 instruction 文本 (不含 lyrics/style)
  ↓
Stage 2-4: 同上，但 CFG 负向分支不同
```

**适用场景**: 快速出 demo，不需要乐谱，速度最快 (跳过 Stage 1)
**注意**: 质量可能略低于 full 模式，因为没有符号化规划

#### 模式 D: 外部乐谱 (`cot=full/melody` + `abc=...`)

```
输入: style + lyrics + 外部 ABC 乐谱
  ↓
Stage 1 - plan(): 跳过生成，直接使用提供的 ABC
  ↓  验证: cot 不能是 "off"
  ↓  将 ABC 文本 tokenize 为 abc_ids
  ↓
Stage 2-4: 同 full/melody 模式
```

**适用场景**: 已有乐谱 (手动编写或从其他工具导出)，精确控制旋律与和声
**约束**: `cot="off"` 时不允许提供外部 ABC

### 1.3 风格描述 (style) 编写指南

style 参数是自由文本，应包含以下要素 (逗号分隔):

| 要素 | 示例 | 是否必须 |
|------|------|---------|
| 语言 | English, Mandarin, Japanese | 推荐 |
| 流派 | piano pop, rock, jazz, folk, EDM | 是 |
| 乐器 | acoustic piano, electric guitar, strings | 推荐 |
| 人声 | female voice, male voice, raspy tenor | 推荐 |
| 速度 | 88 BPM, fast, slow, upbeat | 可选 |
| 情绪 | warm, melancholic, energetic, dreamy | 可选 |
| 其他 | lyrical memorable melody, unhurried phrasing | 可选 |

**完整示例**:
```
English, warm piano pop, expressive female voice, acoustic piano, 
rounded bass and light drums, lyrical memorable melody, unhurried phrasing, 88 BPM
```

```
Mandarin, indie folk, gentle male vocal, acoustic guitar, harmonica, 
soft percussion, nostalgic, 96 BPM
```

### 1.4 歌词格式规范

歌词支持段落标记，标记独占一行:

```
[Verse]
第一段的歌词内容
第二行歌词

[Chorus]
副歌歌词
更加朗朗上口的旋律

[Bridge]
桥段歌词 (可选)

[Verse]
第二段歌词

[Chorus]
重复副歌
```

**支持的段落标记**: `[Verse]`, `[Chorus]`, `[Bridge]`, `[Intro]`, `[Outro]`, `[Pre-Chorus]`, `[Interlude]`

**约束**:
- 歌词不能为空
- 段落标记必须独占一行，方括号格式
- 空行分隔段落
- 歌词长度受 `semantic_max_tokens` (默认 9000) 限制，过长会被截断

### 1.5 ABC 乐谱格式说明

ABC notation 是一种文本化的音乐记谱法，在 YuE2 中作为可编辑的中间表示:

```abc
X:1                    ← 编号
T:                     ← 标题 (可空)
M:4/4                  ← 拍号 (4/4, 3/4, 6/8 等)
L:1/16                 ← 默认音符长度
Q:1/4=88               ← 速度 (四分音符=88BPM)
V: Vocal clef=treble name="Vocal Melody" snm="Vocal"    ← 人声旋律轨
V: Ins clef=treble name="Ins Melody" snm="Inst."        ← 器乐旋律轨
K:C                    ← 调号 (C大调, G大调, Am小调 等)
% verse                ← 段落注释
V: Vocal
"C"E2G2A2G2E2D2C4|"G"D2E2G2E2D2C2D4|    ← 音符+和弦标记
V: Ins
Z4|                     ← 休止符
% chorus
V: Vocal
"C"G2A2c2B2A2G2E4|...
```

**关键元素**:
- 音符: C D E F G A B (大写=高音, 小写=更高八度), 后缀数字=时值倍数
- 和弦: 双引号包裹, 如 `"C"`, `"G"`, `"Am"`, `"Fmaj7"`
- `full` 模式生成含和弦的完整乐谱
- `melody` 模式生成仅旋律 (无和弦标记)
- `#` = 升号, `^` = 升号 (ABC标准), `_` = 降号, `=` = 还原号

### 1.6 全部输入参数详细规格

#### 1.6.1 核心请求参数 (SongRequest)

| 参数 | 类型 | 必填 | 默认值 | 约束 | 说明 |
|------|------|------|--------|------|------|
| `style` | str | 是 | - | 非空字符串 | 风格描述，见1.3编写指南 |
| `lyrics` | str | 是 | - | 非空字符串 | 歌词文本，见1.4格式规范 |
| `cot` | enum | 否 | `"full"` | `off` / `melody` / `full` | 工作流模式，见1.2 |
| `seed` | int | 否 | `831001` | `[0, 2^63)` | 随机种子，相同种子+参数=相同结果 |
| `abc` | str | 否 | `None` | 非空字符串 | 外部ABC乐谱文本，要求cot≠off |
| `abc_file` | path | 否 | `None` | 有效文件路径 | 外部ABC乐谱文件路径 |
| `cfg_scale` | float | 否 | auto | `[0, 20]`, finite | CFG引导强度。auto时: off→1.01, 其他→1.0 |
| `id` | str | 否 | `"song"` | `[A-Za-z0-9][A-Za-z0-9_.-]{0,179}`, 不能是`.`或`..` | 歌曲标识，用于文件名 |

#### 1.6.2 ABC 规划采样参数 (abc_sampling)

控制 Stage 1 乐谱生成的采样策略:

| 参数 | 类型 | 默认值 | 最小值 | 最大值 | 说明 |
|------|------|--------|--------|--------|------|
| `abc_temperature` | float | 0.7 | 0.0 | 5.0 | 温度。0=贪心，越高越随机 |
| `abc_top_p` | float | 0.9 | 0.0 | 1.0 | 核采样概率。1.0=禁用 |
| `abc_top_k` | int | 30 | 1 | - | Top-K采样。越大越多样 |
| `abc_repetition_penalty` | float | 1.005 | 0.001 | - | 重复惩罚系数。>1抑制重复 |
| `abc_penalty_window` | int | 100 | 1 | 100 | 惩罚回看窗口大小 |
| `abc_min_tokens` | int | 32 | 0 | - | 最少生成token数 (抑制过早EOS) |
| `abc_max_tokens` | int | 4096 | 1 | - | 最多生成token数 |

**调参建议**:
- 想要更确定/保守的乐谱: 降低 temperature (0.3-0.5), 降低 top_p (0.8)
- 想要更多样/创意的乐谱: 升高 temperature (1.0-1.5), 升高 top_p (0.95)
- 乐谱太长/太短: 调整 min_tokens / max_tokens

#### 1.6.3 语义 Token 采样参数 (semantic_sampling)

控制 Stage 2 语义 codec token 生成的采样策略:

| 参数 | 类型 | 默认值 | 最小值 | 最大值 | 说明 |
|------|------|--------|--------|--------|------|
| `semantic_temperature` | float | 1.0 | 0.0 | 5.0 | 温度 |
| `semantic_top_p` | float | 0.95 | 0.0 | 1.0 | 核采样概率 |
| `semantic_top_k` | int | 100 | 1 | - | Top-K采样 |
| `semantic_repetition_penalty` | float | 1.2 | 0.001 | - | 重复惩罚 (比ABC更强) |
| `semantic_penalty_window` | int | 50 | 1 | 100 | 惩罚回看窗口 |
| `semantic_min_tokens` | int | 200 | 0 | - | 最少token数 (防止歌曲太短) |
| `semantic_max_tokens` | int | 9000 | 1 | - | 最多token数 (限制歌曲长度) |

**调参建议**:
- 歌曲太短/突然结束: 增大 min_tokens (300-500)
- 歌曲太长/重复: 增大 repetition_penalty (1.3-1.5), 减小 max_tokens
- 想要更稳定: 降低 temperature (0.8)
- 想要更多样: 升高 temperature (1.2-1.5)

#### 1.6.4 NAR 声学合成参数

| 参数 | 类型 | 默认值(GGUF) | 默认值(Python) | 范围 | 说明 |
|------|------|-------------|---------------|------|------|
| `num_inference_steps` | int | 8 | 32 | `[1, 64]` | ODE求解步数。越多质量越好，但线性增加耗时 |

**调参建议**:
- 快速预览: 4步
- 日常使用: 8步 (GGUF默认，质量/速度平衡)
- 高质量: 16-32步
- 超过32步收益递减

#### 1.6.5 模型/运行参数 (session 级，GGUF后端)

| 参数 | 类型 | 默认值 | 可选值 | 说明 |
|------|------|--------|--------|------|
| `model_gguf` | str | `yue2-3b-q8_0.gguf` | 文件名 | 主模型GGUF文件名 (相对于model root) |
| `vae_gguf` | str | `yue2-vae-f16.gguf` | 文件名 | VAE解码器GGUF文件名 |
| `weight_type` | enum | `native` | native/f32/f16/bf16/q8_0/q4_0/q4_k | 共享权重精度 |
| `model_weight_type` | enum | `native` | 同上 | MoT主模型权重精度 (覆盖weight_type) |
| `vae_weight_type` | enum | `native` | 同上 | VAE权重精度 (覆盖weight_type) |
| `model_weight_context_mb` | int | 6144 | ≥1 | MoT权重上下文MiB |
| `vae_weight_context_mb` | int | 1536 | ≥1 | VAE权重上下文MiB |
| `ar_prefill_graph_arena_mb` | int | 4096 | ≥1 | AR预填充图arena |
| `ar_decode_graph_arena_mb` | int | 1536 | ≥1 | AR单token解码图arena |
| `nar_graph_arena_mb` | int | 6144 | ≥1 | NAR声流图arena |
| `vae_graph_arena_mb` | int | 1536 | ≥1 | VAE解码图arena |
| `ar_lora` | str | - | 文件路径 | AR LoRA适配器safetensors文件 |
| `ar_lora_scale` | float | 1.0 | - | AR LoRA delta缩放，0=禁用 |
| `nar_lora` | str | - | 文件路径 | NAR适配器safetensors文件 |
| `nar_lora_scale` | float | 1.0 | - | NAR LoRA delta缩放，0=禁用整个适配器 |

**显存分配估算 (Q8_0, RTX 3080 10GB)**:
```
模型权重:    ~4.0 GB (yue2-3b-q8_0.gguf)
VAE权重:    ~0.25 GB (yue2-vae-f16.gguf)
计算缓冲:   ~3.5 GB (graph arena + KV cache + activations)
系统保留:   ~1.0 GB
─────────────────────
峰值合计:   ~8.7 GB ← 10GB显卡可用
```

### 1.7 输出产物详细规格

#### 1.7.1 音频文件

| 属性 | 值 |
|------|-----|
| 采样率 | 48000 Hz |
| 声道数 | 2 (立体声) |
| WAV格式 | PCM_24 (24-bit) 或 FLOAT (32-bit float) |
| FLAC格式 | PCM_24 (24-bit, 无损压缩) |
| 典型时长 | 30-120秒 (取决于歌词长度和semantic tokens数) |
| 典型大小 | WAV ~13MB/60s, FLAC ~8MB/60s |

#### 1.7.2 ABC 乐谱文件 (full/melody模式)

| 属性 | 值 |
|------|-----|
| 格式 | ABC notation 纯文本, UTF-8编码 |
| 内容 | 包含旋律轨(Vocal) + 器乐轨(Ins) + 段落标记 |
| full模式 | 含和弦符号 ("C", "G", "Am"...) |
| melody模式 | 仅旋律音符，无和弦 |

#### 1.7.3 中间产物 (仅Python后端)

| 文件 | 格式 | 内容 |
|------|------|------|
| `semantic.npy` | int32, shape=[N] | 语义codec token IDs (已减去CODEC_OFFSET=151853) |
| `latent.npy` | float32, shape=[T, 64] | NAR声学latent (64维) |
| `abc_tokens.npy` | int32, shape=[M] | ABC文本的BPE token IDs |
| `prefix.npy` | int32, shape=[P] | 完整前缀token序列 |
| `plan.json` | JSON | 规划详情: request, timing, truncated, prefix, abc_ids, abc |
| `plan_manifest.json` | JSON | plan文件的SHA256哈希 (完整性校验) |
| `config.json` | JSON | 有效生成配置 |
| `result.json` | JSON | 完整结果摘要: status, identity, timing, weights, artifacts哈希 |

### 1.8 两种推理后端详细对比

| 维度 | audio.cpp (GGUF) | Python Pipeline |
|------|-----------------|----------------|
| **运行时** | C++ 原生, 无Python依赖 | PyTorch 2.10 + transformers |
| **模型格式** | GGUF量化 (q8_0/q4_0/bf16) | Safetensors (bf16) + 可选FP8 |
| **模型来源** | `audio-cpp/Yue2-3B-GGUF` | `m-a-p/YuE2-3B` + `m-a-p/YuE2-Vae` |
| **显存需求** | ~8.7GB (Q8_0) | ~11GB (bf16, 24GB推荐) |
| **RTX 3080** | 可用 | 不可行 (OOM) |
| **生成速度** | ~23s/66s音频 (ODE 8步) | ~90s+/66s音频 (ODE 32步) |
| **ODE默认步数** | 8 | 32 |
| **LoRA适配器** | AR + NAR LoRA支持 | 不支持 |
| **外部噪声注入** | `nar_noise_file` 支持 | 不支持 |
| **中间产物** | 仅WAV输出 | 全部中间产物 (tokens, latents, plans) |
| **可复现性** | 种子+参数级别 | SHA256全链路 (权重/代码/产物) |
| **VAE解码** | 单次通过 | 分块解码 (core_frames=512/1024, halo=16) |
| **CFG负向提示** | 内部处理 | 显式构建: instruction_only (off) 或 same_instruction_and_exact_abc (full/melody) |
| **批量生成** | `--batch-text-file`, `--batch-text-dir` | `batch` 子命令 + JSONL |
| **断点续传** | 不支持 | `--resume` + identity验证 |
| **后端选项** | cpu/cuda/hip/rocm/vulkan/metal | torch(CUDA graphs)/torch-eager/vllm |
| **进度报告** | `--log` 文本日志 | stderr进度条 |

**结论**: 对于 RTX 3080 10GB，GGUF 后端是唯一可行选择。Python 后端作为未来升级路径保留。

---

## 2. 项目结构决策

### 2.1 方案对比

| 方案 | 描述 | 优点 | 缺点 | 适合场景 |
|------|------|------|------|---------|
| **A. Fork YuE2** | 克隆官方仓库，在其中添加WebUI | 代码紧密集成，可直接import pipeline | 上游更新难合并；项目变重；GPL兼容性风险 | 24GB+显卡用户，需要完整产物 |
| **B. 独立子项目** | 在YuE2目录下新建yue2-webui/ | 轻量独立；同时支持两种后端；可单独分发 | 需通过subprocess/import引用外部代码 | 通用方案，特别是10GB显卡 |
| **C. 完全独立仓库** | 独立Git仓库，绝对路径引用 | 完全解耦 | 路径配置复杂，不便携 | 分发给不同环境用户 |

### 2.2 决策: 方案B — 独立子项目 `yue2-webui`

**理由**:
1. WebUI 是展示层，不应和推理引擎耦合
2. 默认使用 audio.cpp GGUF 后端 (subprocess)，不依赖 Python pipeline
3. 可选切换到 Python 后端 (import)，两种后端互不干扰
4. 独立版本管理，不受 YuE2 上游更新影响
5. 可以单独打包分发给只用 GGUF 的用户
6. 放在 YuE2 子目录下方便引用 `models/` 和 `audio-cpp/`

### 2.3 项目位置与目录结构

```
Y:\NewStore\AI\Yue2\
├── models/                    ← 已有: GGUF模型文件
│   ├── yue2-3b-q8_0.gguf
│   ├── yue2-vae-f16.gguf
│   └── sidecars/
├── audio-cpp/                 ← 已有: audio.cpp 预编译二进制
│   ├── audiocpp_cli.exe
│   ├── audiocpp_server.exe
│   └── model_specs/yue2.json
├── src/yue2/                  ← 已有: Python推理代码 (可选后端)
├── examples/                  ← 已有: 示例文件
│
└── yue2-webui/                ← 新建: WebUI项目
    ├── app.py                 # Gradio主应用入口
    ├── config.py              # 参数schema、默认值、预设管理
    ├── backend_gguf.py        # audio.cpp GGUF后端封装
    ├── backend_python.py      # Python Pipeline后端封装 (Phase 4)
    ├── task_manager.py        # 任务队列、进度追踪、取消
    ├── history.py             # 生成历史CRUD
    ├── style_presets.py       # 风格预设模板库
    ├── lyrics_templates.py    # 歌词模板库
    ├── abc_utils.py           # ABC乐谱验证/解析工具
    ├── requirements.txt       # Python依赖
    ├── run.bat                # Windows一键启动
    ├── run.sh                 # Linux/Mac启动
    ├── outputs/               # 生成输出目录
    │   └── 20260919_143022_city_lights/
    │       ├── audio.wav
    │       ├── audio.flac
    │       ├── score.abc
    │       └── config.json
    ├── presets/               # 用户参数预设
    │   └── default.json
    ├── history.json           # 生成历史索引
    └── DESIGN.md              # 本文档
```

### 2.4 依赖关系图

```
yue2-webui/app.py
    ├── imports config.py          (参数schema, 预设)
    ├── imports backend_gguf.py    (GGUF后端)
    │       └── subprocess → audio-cpp/audiocpp_cli.exe
    │               └── reads models/*.gguf + models/sidecars/*
    ├── imports backend_python.py  (可选, Phase 4)
    │       └── imports src/yue2/pipeline.py
    │               └── loads models/YuE2-3B/ + models/YuE2-Vae/
    ├── imports task_manager.py    (队列管理)
    ├── imports history.py         (历史管理)
    ├── imports style_presets.py   (风格模板)
    └── imports lyrics_templates.py(歌词模板)
```

---

## 3. 技术架构

### 3.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (Chrome/Edge)                     │
│                                                                  │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│   │ 创作 Tab │  │ 历史 Tab │  │ 设置 Tab │  │ 关于/帮助 Tab │  │
│   └────┬─────┘  └────┬─────┘  └────┬─────┘  └───────────────┘  │
│        └──────────────┼─────────────┘                            │
└───────────────────────┼──────────────────────────────────────────┘
                        │ HTTP (localhost:7860)
                        │ Gradio API (POST /api/predict, /upload, /download)
┌───────────────────────┼──────────────────────────────────────────┐
│                  Gradio Server (app.py)                           │
│                                                                   │
│  ┌────────────────────┴────────────────────────────────────────┐ │
│  │                  UI Layer (Gradio Components)                │ │
│  │  - 输入组件: TextBox, Radio, Slider, Number, Dropdown, File  │ │
│  │  - 输出组件: Audio, Code, File, Markdown, Gallery            │ │
│  │  - 状态组件: Progress, Status, Error Banner                  │ │
│  └────────────────────┬────────────────────────────────────────┘ │
│                       │                                           │
│  ┌────────────────────┴────────────────────────────────────────┐ │
│  │              Generation Controller (Controller Layer)        │ │
│  │                                                              │ │
│  │  validate_params()     → 参数校验 + 默认值填充                │ │
│  │  resolve_backend()     → 根据设置选择后端                     │ │
│  │  prepare_output_dir()  → 创建输出目录，命名规则               │ │
│  │  submit_task()         → 提交到TaskManager                   │ │
│  │  poll_progress()       → Gradio every()轮询进度               │ │
│  │  collect_results()     → 收集输出文件，更新历史               │ │
│  └──────┬──────────────────────────────────┬───────────────────┘ │
│         │                                  │                      │
│  ┌──────┴──────────┐              ┌───────┴──────────────────┐   │
│  │  GGUF Backend    │              │  Python Backend (Phase4) │   │
│  │  (backend_gguf)  │              │  (backend_python)        │   │
│  │                  │              │                          │   │
│  │  build_command() │              │  load_pipeline()         │   │
│  │  execute()       │              │  plan()                  │   │
│  │  parse_log()     │              │  generate_semantic()     │   │
│  │  cancel()        │              │  synthesize()            │   │
│  │                  │              │  decode()                │   │
│  │  subprocess ─────┼──┐           │  save_artifacts()        │   │
│  │  .Popen()        │  │           │                          │   │
│  └──────────────────┘  │           └──────────────────────────┘   │
│                        │                                           │
│  ┌─────────────────────┴───────────────────────────────────────┐ │
│  │                    Task Manager                               │ │
│  │  - threading.Lock 保证单任务执行                              │ │
│  │  - queue.Queue 排队                                          │ │
│  │  - subprocess.Popen.kill() 取消                              │ │
│  │  - 进度回调 → UI更新                                         │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─────────────────────────────┐  ┌───────────────────────────┐  │
│  │     History Manager          │  │     Preset Manager         │  │
│  │  history.json (append-only)  │  │  presets/*.json            │  │
│  │  - 记录每次生成               │  │  - 保存参数组合             │  │
│  │  - 查询/筛选/删除             │  │  - 加载/应用               │  │
│  │  - 重新生成                   │  │  - 导出/导入               │  │
│  └─────────────────────────────┘  └───────────────────────────┘  │
│                                                                   │
│  ┌─────────────────────────────┐                                  │
│  │     Output Manager           │                                  │
│  │  outputs/{timestamp}_{id}/   │                                  │
│  │  - audio.wav / audio.flac    │                                  │
│  │  - score.abc                 │                                  │
│  │  - config.json               │                                  │
│  │  - 自动清理旧文件             │                                  │
│  └─────────────────────────────┘                                  │
└───────────────────────────────────────────────────────────────────┘
```

### 3.2 技术栈选型

| 组件 | 选型 | 版本 | 理由 |
|------|------|------|------|
| Web框架 | Gradio | 5.x | 内置Audio播放器；File上传/下载；Progress组件；零前端代码 |
| 推理后端(默认) | audio.cpp | v0.8.1 | GGUF量化，10GB显存可用，速度快 |
| 推理后端(可选) | yue2-infer | 官方 | 完整产物，需24GB+显存 |
| 并发控制 | threading + Lock | stdlib | 单GPU串行，防止OOM |
| 进程管理 | subprocess.Popen | stdlib | 调用CLI + 日志解析 + kill取消 |
| 配置存储 | JSON文件 | - | 简单可靠，人类可读 |
| 音频转换 | soundfile | 已有 | WAV↔FLAC转换 (Python后端时可用) |
| 启动脚本 | batch/shell | - | Windows bat + Unix sh |

### 3.3 数据流图

```
用户输入 (UI组件值)
    │
    ▼
┌─────────────────────────────────────┐
│ validate_and_build_params()         │
│                                     │
│ 1. 校验必填: style, lyrics 非空      │
│ 2. 校验范围: seed≥0, cfg∈[0,20]等   │
│ 3. 填充默认值: 未设置的参数用默认     │
│ 4. 处理ABC模式:                      │
│    - cot=off → 清空abc字段           │
│    - cot=external → 验证abc非空      │
│ 5. 生成id: 用户指定或自动            │
│ 6. 生成output_dir名: timestamp_id   │
│                                     │
│ 输出: GenerationParams dataclass     │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ TaskManager.submit(params)          │
│                                     │
│ 1. 创建 TaskRecord:                  │
│    {id, status, params, created_at} │
│ 2. 加入队列                          │
│ 3. 如果当前无任务在运行:              │
│    → 启动 worker 线程执行             │
│                                     │
│ 输出: task_id (str)                  │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ worker_thread(task)                  │
│                                     │
│ 1. task.status = "running"           │
│ 2. 选择后端:                         │
│    ┌─ GGUF ────────────────────────┐│
│    │ backend_gguf.generate(params) ││
│    │                               ││
│    │ a. build_command(params)      ││
│    │    → CLI参数列表               ││
│    │ b. subprocess.Popen(cmd)      ││
│    │    → stdout=PIPE, stderr=PIPE ││
│    │ c. 逐行读取stdout             ││
│    │    → parse_log_line(line)     ││
│    │    → 更新 task.progress       ││
│    │ d. 等待进程结束                ││
│    │ e. 检查返回码                  ││
│    │ f. 验证输出文件存在             ││
│    └───────────────────────────────┘│
│ 3. 收集输出文件:                     │
│    - 扫描 output_dir                │
│    - 生成 config.json               │
│    - 计算音频时长 (soundfile.info)   │
│ 4. task.status = "completed"         │
│ 5. history.append(task_record)       │
│ 6. 如果队列非空 → 处理下一个          │
│                                     │
│ 输出: TaskRecord (updated)           │
└─────────────────────────────────────┘
```

### 3.4 模块间接口定义

```python
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import Enum
from pathlib import Path
import datetime

class CotMode(str, Enum):
    FULL = "full"
    MELODY = "melody"
    OFF = "off"

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class BackendType(str, Enum):
    GGUF = "gguf"
    PYTHON = "python"

@dataclass
class SamplingParams:
    """单个阶段 (ABC或semantic) 的采样参数"""
    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 100
    repetition_penalty: float = 1.2
    penalty_window: int = 50
    min_tokens: int = 200
    max_tokens: int = 9000

@dataclass
class GenerationParams:
    """完整的生成参数集"""
    # 核心
    style: str = ""
    lyrics: str = ""
    cot: CotMode = CotMode.FULL
    seed: int = 831001
    id: str = "song"
    
    # ABC
    abc: Optional[str] = None
    abc_file: Optional[str] = None
    
    # CFG
    cfg_scale: Optional[float] = None  # None = auto
    
    # NAR
    num_inference_steps: int = 8
    
    # 分阶段采样
    abc_sampling: SamplingParams = field(default_factory=lambda: SamplingParams(
        temperature=0.7, top_p=0.9, top_k=30,
        repetition_penalty=1.005, penalty_window=100,
        min_tokens=32, max_tokens=4096
    ))
    semantic_sampling: SamplingParams = field(default_factory=SamplingParams)
    
    # 后端
    backend: BackendType = BackendType.GGUF
    
    # GGUF session
    model_gguf: str = "yue2-3b-q8_0.gguf"
    vae_gguf: str = "yue2-vae-f16.gguf"

@dataclass
class TaskProgress:
    """任务进度信息"""
    task_id: str
    status: TaskStatus
    phase: str = ""           # "planning" | "generating" | "synthesizing" | "decoding" | ""
    phase_label: str = ""     # 人类可读: "Planning score" | "Generating song" | ...
    current_step: int = 0
    total_steps: Optional[int] = None
    elapsed_seconds: float = 0.0
    message: str = ""

@dataclass
class TaskRecord:
    """一条完整的任务记录"""
    task_id: str
    params: GenerationParams
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = ""          # ISO 8601
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    output_dir: Optional[str] = None
    audio_path: Optional[str] = None
    abc_path: Optional[str] = None
    audio_duration_seconds: Optional[float] = None
    generation_time_seconds: Optional[float] = None
    error_message: Optional[str] = None
    backend: BackendType = BackendType.GGUF
    
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> 'TaskRecord': ...

@dataclass
class GenerationResult:
    """生成结果"""
    success: bool
    audio_path: Optional[str] = None
    abc_text: Optional[str] = None
    audio_duration_seconds: Optional[float] = None
    generation_time_seconds: Optional[float] = None
    output_dir: Optional[str] = None
    error_message: Optional[str] = None
    config_snapshot: Optional[dict] = None
```

---

## 4. 数据模型与接口定义

### 4.1 config.py — 参数 Schema 与 UI 映射

```python
# 每个参数的完整定义，用于:
# 1. UI自动生成 (Gradio组件)
# 2. 参数校验
# 3. CLI命令构建
# 4. 预设保存/加载

PARAM_SCHEMA = {
    # ── 核心参数 ──
    "style": {
        "type": "string",
        "required": True,
        "default": "",
        "ui": {
            "component": "Textbox",
            "label": "风格描述 (Style)",
            "placeholder": "English, warm piano pop, expressive female voice, acoustic piano, 88 BPM",
            "lines": 2,
            "info": "语言 + 流派 + 乐器 + 人声 + 速度 + 情绪",
            "tab_order": 0,
        },
        "validation": {
            "min_length": 1,
            "error_message": "风格描述不能为空",
        },
        "cli": {
            "gguf": lambda v: ("--request-option", f"style={v}"),
        },
    },
    
    "lyrics": {
        "type": "string",
        "required": True,
        "default": "",
        "ui": {
            "component": "Textbox",
            "label": "歌词 (Lyrics)",
            "placeholder": "[Verse]\n在这里输入歌词...\n\n[Chorus]\n副歌歌词...",
            "lines": 10,
            "info": "支持 [Verse] [Chorus] [Bridge] 等段落标记",
            "tab_order": 1,
        },
        "validation": {
            "min_length": 1,
            "error_message": "歌词不能为空",
        },
        "cli": {
            "gguf": lambda v: ("--request-option", f"lyrics={v}"),
            # 注意: lyrics可能包含换行和特殊字符，需要特殊处理
        },
    },
    
    "cot": {
        "type": "enum",
        "required": False,
        "default": "full",
        "values": ["full", "melody", "off"],
        "ui": {
            "component": "Radio",
            "label": "工作模式",
            "choices": [
                ("完整创作 (生成乐谱+和弦)", "full"),
                ("旋律创作 (仅旋律，适合翻唱)", "melody"),
                ("直接生成 (跳过乐谱，最快)", "off"),
            ],
            "info": "完整创作质量最高但最慢，直接生成最快但质量略低",
            "tab_order": 2,
        },
        "cli": {
            "gguf": lambda v: ("--request-option", f"cot={v}"),
        },
    },
    
    "seed": {
        "type": "int",
        "required": False,
        "default": 831001,
        "ui": {
            "component": "Number",
            "label": "随机种子 (Seed)",
            "minimum": 0,
            "maximum": 2**63 - 1,
            "precision": 0,
            "info": "相同种子+参数 = 相同结果。点击[随机]按钮获取新种子",
            "tab_order": 3,
        },
        "validation": {
            "min": 0,
            "max": 2**63 - 1,
        },
        "cli": {
            "gguf": lambda v: ("--request-option", f"seed={v}"),
        },
    },
    
    "cfg_scale": {
        "type": "float",
        "required": False,
        "default": None,  # None = auto
        "ui": {
            "component": "Slider",
            "label": "CFG 引导强度",
            "minimum": 0.0,
            "maximum": 20.0,
            "step": 0.1,
            "info": "Classifier-Free Guidance。auto时: off模式=1.01, 其他=1.0。>1增强风格一致性但可能降低多样性",
            "tab_order": 4,
        },
        "validation": {
            "min": 0.0,
            "max": 20.0,
            "allow_none": True,
        },
        "cli": {
            "gguf": lambda v: ("--request-option", f"guidance_scale={v}") if v is not None else None,
        },
    },
    
    "num_inference_steps": {
        "type": "int",
        "required": False,
        "default": 8,
        "ui": {
            "component": "Slider",
            "label": "ODE 求解步数",
            "minimum": 1,
            "maximum": 64,
            "step": 1,
            "info": "NAR声学生成步数。8=快速, 16=标准, 32=高质量。越多越慢但质量越好",
            "tab_order": 5,
        },
        "validation": {
            "min": 1,
            "max": 64,
        },
        "cli": {
            "gguf": lambda v: ("--request-option", f"num_inference_steps={v}"),
        },
    },
    
    # ── ABC 采样参数 ──
    "abc_temperature": {
        "type": "float", "default": 0.7,
        "ui": {"component": "Slider", "label": "ABC 温度", "minimum": 0.0, "maximum": 5.0, "step": 0.1,
               "info": "乐谱生成随机性。低=保守确定，高=多样创意"},
        "validation": {"min": 0.0, "max": 5.0},
        "cli": {"gguf": lambda v: ("--request-option", f"abc_temperature={v}")},
    },
    "abc_top_p": {
        "type": "float", "default": 0.9,
        "ui": {"component": "Slider", "label": "ABC Top-P", "minimum": 0.0, "maximum": 1.0, "step": 0.05},
        "validation": {"min": 0.0, "max": 1.0},
        "cli": {"gguf": lambda v: ("--request-option", f"abc_top_p={v}")},
    },
    "abc_top_k": {
        "type": "int", "default": 30,
        "ui": {"component": "Slider", "label": "ABC Top-K", "minimum": 1, "maximum": 500, "step": 1},
        "validation": {"min": 1},
        "cli": {"gguf": lambda v: ("--request-option", f"abc_top_k={v}")},
    },
    "abc_repetition_penalty": {
        "type": "float", "default": 1.005,
        "ui": {"component": "Slider", "label": "ABC 重复惩罚", "minimum": 0.001, "maximum": 3.0, "step": 0.01},
        "validation": {"min": 0.001},
        "cli": {"gguf": lambda v: ("--request-option", f"abc_repetition_penalty={v}")},
    },
    "abc_penalty_window": {
        "type": "int", "default": 100,
        "ui": {"component": "Slider", "label": "ABC 惩罚窗口", "minimum": 1, "maximum": 100, "step": 1},
        "validation": {"min": 1, "max": 100},
        "cli": {"gguf": lambda v: ("--request-option", f"abc_penalty_window={v}")},
    },
    "abc_min_tokens": {
        "type": "int", "default": 32,
        "ui": {"component": "Slider", "label": "ABC 最少Token", "minimum": 0, "maximum": 4096, "step": 1},
        "validation": {"min": 0},
        "cli": {"gguf": lambda v: ("--request-option", f"abc_min_tokens={v}")},
    },
    "abc_max_tokens": {
        "type": "int", "default": 4096,
        "ui": {"component": "Slider", "label": "ABC 最多Token", "minimum": 1, "maximum": 8192, "step": 1},
        "validation": {"min": 1},
        "cli": {"gguf": lambda v: ("--request-option", f"abc_max_tokens={v}")},
    },
    
    # ── 语义 Token 采样参数 ──
    "semantic_temperature": {
        "type": "float", "default": 1.0,
        "ui": {"component": "Slider", "label": "语义 温度", "minimum": 0.0, "maximum": 5.0, "step": 0.1,
               "info": "音乐token生成随机性。低=稳定可预测，高=多样意外"},
        "validation": {"min": 0.0, "max": 5.0},
        "cli": {"gguf": lambda v: ("--request-option", f"semantic_temperature={v}")},
    },
    "semantic_top_p": {
        "type": "float", "default": 0.95,
        "ui": {"component": "Slider", "label": "语义 Top-P", "minimum": 0.0, "maximum": 1.0, "step": 0.05},
        "validation": {"min": 0.0, "max": 1.0},
        "cli": {"gguf": lambda v: ("--request-option", f"semantic_top_p={v}")},
    },
    "semantic_top_k": {
        "type": "int", "default": 100,
        "ui": {"component": "Slider", "label": "语义 Top-K", "minimum": 1, "maximum": 500, "step": 1},
        "validation": {"min": 1},
        "cli": {"gguf": lambda v: ("--request-option", f"semantic_top_k={v}")},
    },
    "semantic_repetition_penalty": {
        "type": "float", "default": 1.2,
        "ui": {"component": "Slider", "label": "语义 重复惩罚", "minimum": 0.001, "maximum": 3.0, "step": 0.01,
               "info": "防止音乐片段循环重复。1.2=适度，>1.5=强力去重"},
        "validation": {"min": 0.001},
        "cli": {"gguf": lambda v: ("--request-option", f"semantic_repetition_penalty={v}")},
    },
    "semantic_penalty_window": {
        "type": "int", "default": 50,
        "ui": {"component": "Slider", "label": "语义 惩罚窗口", "minimum": 1, "maximum": 100, "step": 1},
        "validation": {"min": 1, "max": 100},
        "cli": {"gguf": lambda v: ("--request-option", f"semantic_penalty_window={v}")},
    },
    "semantic_min_tokens": {
        "type": "int", "default": 200,
        "ui": {"component": "Slider", "label": "语义 最少Token", "minimum": 0, "maximum": 9000, "step": 10,
               "info": "歌曲最少token数。增大可防止歌曲突然结束"},
        "validation": {"min": 0},
        "cli": {"gguf": lambda v: ("--request-option", f"semantic_min_tokens={v}")},
    },
    "semantic_max_tokens": {
        "type": "int", "default": 9000,
        "ui": {"component": "Slider", "label": "语义 最多Token", "minimum": 1, "maximum": 9000, "step": 100,
               "info": "歌曲最多token数。限制歌曲最大长度"},
        "validation": {"min": 1},
        "cli": {"gguf": lambda v: ("--request-option", f"semantic_max_tokens={v}")},
    },
}
```

### 4.2 输出目录命名规则

```
格式: outputs/{YYYYMMDD}_{HHMMSS}_{sanitized_id}/

示例:
  outputs/20260919_143022_city_lights/
  outputs/20260919_150105_song/
  outputs/20260919_161230_my_jam/

sanitized_id 规则:
  - 取 params.id 或自动生成 "song"
  - 移除非法字符，仅保留 [A-Za-z0-9_.-]
  - 截断到 64 字符
  - 如果为空或为 "." / ".."，使用 "song"
```

### 4.3 config.json 输出格式

每次生成完成后在输出目录写入 `config.json`:

```json
{
  "task_id": "20260919_143022_city_lights",
  "created_at": "2026-09-19T14:30:22+08:00",
  "completed_at": "2026-09-19T14:30:45+08:00",
  "backend": "gguf",
  "params": {
    "style": "English, warm piano pop, expressive female voice, acoustic piano, 88 BPM",
    "lyrics": "[Verse]\nNeon fades along the lane\n...",
    "cot": "full",
    "seed": 831001,
    "cfg_scale": null,
    "num_inference_steps": 8,
    "abc_sampling": {
      "temperature": 0.7, "top_p": 0.9, "top_k": 30,
      "repetition_penalty": 1.005, "penalty_window": 100,
      "min_tokens": 32, "max_tokens": 4096
    },
    "semantic_sampling": {
      "temperature": 1.0, "top_p": 0.95, "top_k": 100,
      "repetition_penalty": 1.2, "penalty_window": 50,
      "min_tokens": 200, "max_tokens": 9000
    }
  },
  "result": {
    "audio_path": "audio.wav",
    "audio_duration_seconds": 66.3,
    "audio_sample_rate": 48000,
    "audio_channels": 2,
    "generation_time_seconds": 23.1,
    "abc_available": true
  },
  "model": {
    "model_gguf": "yue2-3b-q8_0.gguf",
    "vae_gguf": "yue2-vae-f16.gguf"
  }
}
```

---

## 5. 后端模块详细设计

### 5.1 backend_gguf.py — audio.cpp 后端

#### 5.1.1 CLI 命令构建

将 `GenerationParams` 转换为 `audiocpp_cli.exe` 命令行参数:

```python
class GGUFBackend:
    def __init__(self, project_root: Path):
        self.cli_path = project_root / "audio-cpp" / "audiocpp_cli.exe"
        self.model_dir = project_root / "models"
    
    def build_command(self, params: GenerationParams, output_path: Path) -> list[str]:
        """
        构建完整的CLI命令参数列表。
        
        示例输出 (对应 generate.bat):
        [
            "audio-cpp/audiocpp_cli.exe",
            "--task", "gen",
            "--family", "yue2",
            "--model", "models/yue2-3b-q8_0.gguf",
            "--backend", "cuda",
            "--threads", "8",
            "--text", "<lyrics_text>",
            "--request-option", "style=English, warm piano pop...",
            "--request-option", "cot=full",
            "--request-option", "seed=831001",
            "--request-option", "num_inference_steps=8",
            "--request-option", "abc_temperature=0.7",
            "--request-option", "abc_top_p=0.9",
            ... (其他采样参数)
            "--session-option", "yue2.model_gguf=yue2-3b-q8_0.gguf",
            "--session-option", "yue2.vae_gguf=yue2-vae-f16.gguf",
            "--out", "outputs/20260919_143022/audio.wav",
            "--log",
        ]
        """
        cmd = [
            str(self.cli_path),
            "--task", "gen",
            "--family", "yue2",
            "--model", str(self.model_dir / params.model_gguf),
            "--backend", "cuda",
            "--threads", "8",
        ]
        
        # 歌词: 使用 --text 传入 (注意换行处理)
        cmd.extend(["--text", params.lyrics])
        
        # Request options
        cmd.extend(["--request-option", f"style={params.style}"])
        cmd.extend(["--request-option", f"cot={params.cot.value}"])
        cmd.extend(["--request-option", f"seed={params.seed}"])
        cmd.extend(["--request-option", f"num_inference_steps={params.num_inference_steps}"])
        
        # CFG (仅非None时传入)
        if params.cfg_scale is not None:
            cmd.extend(["--request-option", f"guidance_scale={params.cfg_scale}"])
        
        # ABC 外部乐谱
        if params.abc is not None:
            cmd.extend(["--request-option", f"abc={params.abc}"])
        elif params.abc_file is not None:
            cmd.extend(["--request-option", f"abc_file={params.abc_file}"])
        
        # ABC 采样参数 (仅在非默认值时传入，减少命令长度)
        abc_defaults = {"temperature": 0.7, "top_p": 0.9, "top_k": 30,
                        "repetition_penalty": 1.005, "penalty_window": 100,
                        "min_tokens": 32, "max_tokens": 4096}
        for key, default in abc_defaults.items():
            val = getattr(params.abc_sampling, key)
            if val != default:
                cmd.extend(["--request-option", f"abc_{key}={val}"])
        
        # 语义采样参数
        sem_defaults = {"temperature": 1.0, "top_p": 0.95, "top_k": 100,
                        "repetition_penalty": 1.2, "penalty_window": 50,
                        "min_tokens": 200, "max_tokens": 9000}
        for key, default in sem_defaults.items():
            val = getattr(params.semantic_sampling, key)
            if val != default:
                cmd.extend(["--request-option", f"semantic_{key}={val}"])
        
        # Session options
        cmd.extend(["--session-option", f"yue2.model_gguf={params.model_gguf}"])
        cmd.extend(["--session-option", f"yue2.vae_gguf={params.vae_gguf}"])
        
        # 输出
        cmd.extend(["--out", str(output_path)])
        
        # 日志 (用于进度解析)
        cmd.append("--log")
        
        return cmd
```

#### 5.1.2 歌词传入方式

歌词包含换行符和特殊字符，有两种传入方式:

| 方式 | 命令 | 优点 | 缺点 | 选择 |
|------|------|------|------|------|
| `--text` | `--text "lyrics\n..."` | 简单直接 | Windows cmd引号转义复杂 | 用subprocess列表传参，避免shell转义 |
| `--request-option lyrics=` | `--request-option "lyrics=..."` | 统一走request-option | 同上 | 备选 |

**决策**: 使用 `--text` + `subprocess.Popen(cmd_list)` (不用shell=True)，这样换行符通过列表元素直接传入，无需shell转义。

#### 5.1.3 进度日志解析

`--log` 模式下 `audiocpp_cli.exe` 输出到 stdout 的日志格式 (需要从实际运行中确认):

```
[可能的日志格式 - 需要实际运行一次抓取]
Loading model: yue2-3b-q8_0.gguf
Model loaded in 2.3s
[Planning score] token 1/4096
[Planning score] token 100/4096
[Planning score] completed: 342 tokens in 5.2s
[Generating song] token 1/9000
[Generating song] token 500/9000
[Generating song] completed: 2341 tokens in 12.1s
[Synthesizing audio] step 1/8
[Synthesizing audio] step 8/8
[Synthesizing audio] completed in 4.5s
[Decoding audio] chunk 1/3
[Decoding audio] completed in 1.3s
Total: 23.1s, audio: 66.3s, RTF: 0.35
```

**解析策略**:

```python
class LogParser:
    """解析 audiocpp_cli --log 输出，提取进度"""
    
    # 阶段识别正则
    PHASE_PATTERNS = {
        "planning": re.compile(r"\[Planning score\].*token (\d+)/(\d+)"),
        "generating": re.compile(r"\[Generating song\].*token (\d+)/(\d+)"),
        "synthesizing": re.compile(r"\[Synthesizing.*\].*step (\d+)/(\d+)"),
        "decoding": re.compile(r"\[Decoding.*\].*chunk (\d+)/(\d+)"),
    }
    
    # 完成识别
    COMPLETED_PATTERNS = {
        "planning": re.compile(r"\[Planning score\] completed: (\d+) tokens in ([\d.]+)s"),
        "generating": re.compile(r"\[Generating song\] completed: (\d+) tokens in ([\d.]+)s"),
        "synthesizing": re.compile(r"\[Synthesizing.*\] completed in ([\d.]+)s"),
        "decoding": re.compile(r"\[Decoding.*\] completed in ([\d.]+)s"),
    }
    
    # 总计行
    TOTAL_PATTERN = re.compile(r"Total: ([\d.]+)s, audio: ([\d.]+)s, RTF: ([\d.]+)")
    
    def parse_line(self, line: str) -> Optional[TaskProgress]:
        """解析一行日志，返回进度更新 (如果有的话)"""
        ...
```

> **注意**: 以上日志格式是推测的，需要在 Phase 1 开发时实际运行一次 `audiocpp_cli.exe --log` 抓取真实输出，然后调整正则。如果日志格式不包含进度信息，退化为阶段级别的状态更新 (loading → planning → generating → synthesizing → decoding → done)。

#### 5.1.4 执行与取消

```python
class GGUFBackend:
    def generate(self, params: GenerationParams, output_dir: Path,
                 on_progress: Optional[Callable[[TaskProgress], None]] = None,
                 cancel_event: Optional[threading.Event] = None) -> GenerationResult:
        """
        执行生成。
        
        参数:
            params: 生成参数
            output_dir: 输出目录 (已创建)
            on_progress: 进度回调
            cancel_event: 取消信号 (threading.Event)
        
        返回:
            GenerationResult
        """
        output_path = output_dir / "audio.wav"
        cmd = self.build_command(params, output_path)
        
        start_time = time.time()
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # 合并stderr
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.cli_path.parent.parent),  # 工作目录设为项目根
            )
            
            # 将process引用保存到可取消的位置
            self._current_process = process
            
            parser = LogParser()
            
            for line in process.stdout:
                # 检查取消
                if cancel_event and cancel_event.is_set():
                    process.kill()
                    return GenerationResult(success=False, error_message="已取消")
                
                # 解析进度
                progress = parser.parse_line(line)
                if progress and on_progress:
                    on_progress(progress)
            
            process.wait()
            elapsed = time.time() - start_time
            
            if process.returncode != 0:
                return GenerationResult(
                    success=False,
                    error_message=f"audiocpp_cli 退出码 {process.returncode}",
                    generation_time_seconds=elapsed,
                )
            
            if not output_path.exists():
                return GenerationResult(
                    success=False,
                    error_message="生成完成但输出文件不存在",
                    generation_time_seconds=elapsed,
                )
            
            # 获取音频时长
            audio_duration = self._get_audio_duration(output_path)
            
            # 尝试读取ABC (GGUF后端不直接输出ABC，需要额外处理)
            abc_text = None  # GGUF后端不输出ABC文件
            
            return GenerationResult(
                success=True,
                audio_path=str(output_path),
                abc_text=abc_text,
                audio_duration_seconds=audio_duration,
                generation_time_seconds=elapsed,
                output_dir=str(output_dir),
            )
            
        except Exception as e:
            return GenerationResult(success=False, error_message=str(e))
        finally:
            self._current_process = None
    
    def cancel(self):
        """取消当前正在执行的生成"""
        if self._current_process:
            self._current_process.kill()
    
    def _get_audio_duration(self, path: Path) -> float:
        """获取WAV文件时长 (秒)"""
        import wave
        with wave.open(str(path), 'rb') as f:
            frames = f.getnframes()
            rate = f.getframerate()
            return frames / rate
```

#### 5.1.5 模型可用性检查

```python
class GGUFBackend:
    def check_models(self) -> dict:
        """
        检查模型文件是否齐全。
        
        返回:
            {
                "available": True/False,
                "model_gguf": {"path": "...", "exists": True, "size_mb": 4096},
                "vae_gguf": {"path": "...", "exists": True, "size_mb": 253},
                "sidecars": {
                    "yue2-model-config.json": True,
                    "yue2-generation-config.json": True,
                    "yue2-qwen.tiktoken": True,
                    "yue2-vae-config.json": True,
                },
                "missing": [],
            }
        """
        required_sidecars = [
            "sidecars/yue2-model-config.json",
            "sidecars/yue2-generation-config.json",
            "sidecars/yue2-qwen.tiktoken",
            "sidecars/yue2-vae-config.json",
        ]
        ...
    
    def list_available_packages(self) -> list[dict]:
        """
        扫描models目录，列出可用的模型包。
        
        返回:
            [
                {"id": "q8_0", "model": "yue2-3b-q8_0.gguf", "size_mb": 4096, "precision": "q8_0"},
                {"id": "q4_0", "model": "yue2-3b-q4_0.gguf", "size_mb": 2048, "precision": "q4_0"},
                {"id": "bf16", "model": "yue2-3b-bf16.gguf", "size_mb": 6144, "precision": "bf16"},
            ]
        """
        ...
```

### 5.2 backend_python.py — Python Pipeline 后端 (Phase 4)

```python
class PythonBackend:
    """
    封装 YuE2Pipeline，提供与 GGUFBackend 相同的 generate() 接口。
    需要 24GB+ VRAM，RTX 3080 不可用。
    """
    
    def __init__(self, project_root: Path, memory_budget_gib: float = 24):
        self.project_root = project_root
        self.memory_budget_gib = memory_budget_gib
        self._pipe = None
    
    def _ensure_loaded(self):
        """懒加载 pipeline"""
        if self._pipe is None:
            import sys
            sys.path.insert(0, str(self.project_root / "src"))
            from yue2 import YuE2Pipeline
            self._pipe = YuE2Pipeline.from_pretrained(
                str(self.project_root / "models" / "YuE2-3B"),
                vae=str(self.project_root / "models" / "YuE2-Vae"),
                device="cuda",
                memory_budget_gib=self.memory_budget_gib,
                offload_ar=True,
            )
    
    def generate(self, params: GenerationParams, output_dir: Path,
                 on_progress=None, cancel_event=None) -> GenerationResult:
        """
        调用 Python pipeline 完整 4 阶段生成。
        产物: audio.flac + score.abc + semantic.npy + latent.npy + config.json + result.json
        """
        self._ensure_loaded()
        
        # Stage 1: Plan
        if on_progress:
            on_progress(TaskProgress(task_id="", status=TaskStatus.RUNNING,
                                     phase="planning", phase_label="Planning score"))
        
        request_kwargs = {
            "style": params.style,
            "lyrics": params.lyrics,
            "cot": params.cot.value,
            "seed": params.seed,
        }
        if params.abc:
            request_kwargs["abc"] = params.abc
        if params.cfg_scale is not None:
            request_kwargs["cfg_scale"] = params.cfg_scale
        
        plan = self._pipe.plan(**request_kwargs)
        
        # Stage 2: Semantic
        if on_progress:
            on_progress(TaskProgress(task_id="", status=TaskStatus.RUNNING,
                                     phase="generating", phase_label="Generating song"))
        
        semantic = self._pipe.generate_semantic(plan)
        
        # Stage 3: Synthesize
        if on_progress:
            on_progress(TaskProgress(task_id="", status=TaskStatus.RUNNING,
                                     phase="synthesizing", phase_label="Synthesizing audio"))
        
        latents = self._pipe.synthesize(semantic)
        
        # Stage 4: Decode
        if on_progress:
            on_progress(TaskProgress(task_id="", status=TaskStatus.RUNNING,
                                     phase="decoding", phase_label="Decoding audio"))
        
        audio = self._pipe.decode(latents)
        
        # Save all artifacts
        # ... (调用 save_artifacts)
        
        return GenerationResult(success=True, ...)
    
    def close(self):
        """释放显存"""
        if self._pipe:
            self._pipe.close()
            self._pipe = None
```

---

## 6. Gradio UI 详细设计

### 6.1 组件清单与映射

#### Tab 1: 创作

| 区域 | Gradio组件 | 绑定参数 | 备注 |
|------|-----------|---------|------|
| **风格输入** | `Textbox(lines=2, label="风格描述")` | `style` | placeholder含示例 |
| **风格快捷标签** | 一排 `Button` | - | 点击填入预设风格文本 |
| | `Button("钢琴流行")` | → style | "English, warm piano pop..." |
| | `Button("民谣")` | → style | "Indie folk, acoustic guitar..." |
| | `Button("摇滚")` | → style | "Rock, electric guitar..." |
| | `Button("爵士")` | → style | "Jazz, piano, saxophone..." |
| | `Button("电子")` | → style | "EDM, synth, upbeat..." |
| | `Button("中文流行")` | → style | "Mandarin, C-pop, female vocal..." |
| **歌词输入** | `Textbox(lines=10, label="歌词")` | `lyrics` | 支持段落标记 |
| **歌词模板** | `Dropdown(label="歌词模板")` | - | 选择后填充歌词区 |
| | 选项: "流行(Verse+Chorus)", "民谣(Verse×3)", "完整(Intro+V+C+Bridge+Outro)" | | |
| **工作模式** | `Radio(label="工作模式", choices=[...])` | `cot` | 3个选项+描述 |
| **ABC输入区** | `Textbox(lines=8, label="ABC乐谱", visible=False)` | `abc` | 仅cot=external时可见 |
| **ABC上传** | `File(label="上传ABC文件", file_types=[".abc"])` | `abc_file` | 同上 |
| **种子** | `Number(value=831001, label="随机种子")` | `seed` | 旁边有[随机]按钮 |
| **CFG强度** | `Slider(0, 20, value=None, label="CFG引导强度")` | `cfg_scale` | |
| **ODE步数** | `Slider(1, 64, value=8, label="ODE步数")` | `num_inference_steps` | |
| **高级折叠** | `Accordion(label="高级采样参数", open=False)` | - | 包含下面所有参数 |
| ├ ABC温度 | `Slider(0, 5, value=0.7)` | `abc_temperature` | |
| ├ ABC Top-P | `Slider(0, 1, value=0.9)` | `abc_top_p` | |
| ├ ABC Top-K | `Slider(1, 500, value=30)` | `abc_top_k` | |
| ├ ABC重复惩罚 | `Slider(0.001, 3, value=1.005)` | `abc_repetition_penalty` | |
| ├ ABC惩罚窗口 | `Slider(1, 100, value=100)` | `abc_penalty_window` | |
| ├ ABC token范围 | `Slider(0, 4096, value=32)` + `Slider(1, 8192, value=4096)` | `abc_min_tokens`, `abc_max_tokens` | |
| ├ 语义温度 | `Slider(0, 5, value=1.0)` | `semantic_temperature` | |
| ├ 语义 Top-P | `Slider(0, 1, value=0.95)` | `semantic_top_p` | |
| ├ 语义 Top-K | `Slider(1, 500, value=100)` | `semantic_top_k` | |
| ├ 语义重复惩罚 | `Slider(0.001, 3, value=1.2)` | `semantic_repetition_penalty` | |
| ├ 语义惩罚窗口 | `Slider(1, 100, value=50)` | `semantic_penalty_window` | |
| ├ 语义token范围 | `Slider(0, 9000, value=200)` + `Slider(1, 9000, value=9000)` | `semantic_min_tokens`, `semantic_max_tokens` | |
| **生成按钮** | `Button("生成歌曲", variant="primary", size="lg")` | → generate() | |
| **进度条** | `gr.Progress()` | - | Gradio内置进度追踪 |
| **状态文本** | `Markdown()` | - | 显示当前阶段: "正在规划乐谱..." |
| **音频输出** | `Audio(label="生成的歌曲", type="filepath")` | - | 内置播放器+下载 |
| **ABC输出** | `Code(label="ABC乐谱", language=None, visible=False)` | - | 仅full/melody模式有值 |
| **下载区** | `File(label="下载")` | - | WAV文件下载 |
| **耗时信息** | `Markdown()` | - | "生成耗时 23.1s, 音频时长 66.3s" |

#### Tab 2: 历史

| 组件 | 说明 |
|------|------|
| `Dataframe(label="生成历史")` | 表格: 时间, 风格(截断), 模式, 时长, 耗时, 操作 |
| `Audio(label="试听")` | 点击行时加载音频 |
| `Button("重新生成")` | 用该行的参数重新生成 |
| `Button("删除选中")` | 删除选中的历史记录和文件 |
| `Button("清空历史")` | 清空所有历史 |
| `Button("刷新")` | 重新加载历史列表 |

#### Tab 3: 设置

| 组件 | 绑定 | 说明 |
|------|------|------|
| `Radio(label="推理后端")` | `backend` | GGUF / Python (Phase 4) |
| `Textbox(label="模型目录")` | - | 默认 `../models` |
| `Dropdown(label="模型精度")` | `model_gguf` | q8_0 / q4_0 / bf16 |
| `Dropdown(label="VAE精度")` | `vae_gguf` | f16 / f32 |
| `CheckboxGroup(label="输出格式")` | - | WAV / FLAC |
| `Textbox(label="输出目录")` | - | 默认 `./outputs` |
| `Number(label="保留历史数")` | - | 默认100，超过自动清理 |
| `Button("检查模型")` | - | 运行check_models()，显示结果 |
| `Button("保存为默认")` | - | 保存当前设置为默认预设 |
| `Markdown()` | - | 显示GPU信息、显存、模型状态 |

### 6.2 交互逻辑详细设计

#### 6.2.1 模式切换联动

```python
def on_cot_change(cot_value):
    """工作模式切换时，控制ABC输入区的可见性"""
    if cot_value == "off":
        return gr.update(visible=False), gr.update(visible=False)  # 隐藏ABC输入
    elif cot_value in ("full", "melody"):
        return gr.update(visible=False), gr.update(visible=False)  # 隐藏外部ABC
    # "external" 模式 (如果添加的话)
    return gr.update(visible=True), gr.update(visible=True)
```

#### 6.2.2 风格预设填充

```python
STYLE_PRESETS = {
    "钢琴流行": "English, warm piano pop, expressive female voice, acoustic piano, rounded bass and light drums, lyrical memorable melody, unhurried phrasing, 88 BPM",
    "民谣": "English, indie folk, gentle male vocal, acoustic guitar, harmonica, soft percussion, nostalgic, warm, 96 BPM",
    "摇滚": "English, alternative rock, powerful male vocal, electric guitar, bass, drums, energetic, driving rhythm, 128 BPM",
    "爵士": "English, jazz, smooth female vocal, piano, upright bass, brushed drums, saxophone solo, sophisticated harmony, 110 BPM",
    "电子": "English, EDM, synth lead, four-on-the-floor kick, energetic build-up, drop, female vocal chops, 128 BPM",
    "中文流行": "Mandarin, C-pop, sweet female voice, piano, strings, gentle percussion, emotional ballad, 72 BPM",
    "R&B": "English, R&B, soulful female vocal, electric piano, smooth bass, snap beats, groovy, 90 BPM",
    "古典跨界": "English, classical crossover, operatic tenor, orchestra, strings, timpani, epic, 100 BPM",
}
```

#### 6.2.3 歌词模板

```python
LYRICS_TEMPLATES = {
    "流行 (Verse+Chorus)": """[Verse]
第一段的歌词
第二行歌词
第三行歌词
第四行歌词

[Chorus]
副歌歌词，更加朗朗上口
第二行副歌
第三行副歌
第四行副歌

[Verse]
第二段歌词
新的内容和情感
继续讲故事
推进情节

[Chorus]
重复副歌
强化记忆点
让听众跟唱
最后一句收尾""",

    "完整 (Intro+Verse+Chorus+Bridge+Outro)": """[Intro]
(前奏，可以是器乐或轻声吟唱)

[Verse]
第一段歌词
建立场景和情绪
描绘画面
引入主题

[Pre-Chorus]
预副歌，制造期待感
情绪逐渐上升

[Chorus]
副歌，全曲最抓耳的部分
核心情感表达
让人记住的旋律
重复的歌名或主题

[Verse]
第二段歌词
深化主题
新的视角或故事发展
保持韵脚一致

[Chorus]
重复副歌
加强印象

[Bridge]
桥段，打破重复
情感转折或高潮
新的和声进行
为最后的高潮铺垫

[Chorus]
最终副歌
最强烈的情感表达
可以略微变化歌词
完美的收尾

[Outro]
尾声
渐渐远去
最后一句话
留白""",
}
```

#### 6.2.4 生成按钮回调

```python
def on_generate(
    style, lyrics, cot, seed, cfg_scale, num_inference_steps,
    abc_text, abc_file,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress=gr.Progress(track_tqdm=False),
):
    """生成按钮回调 — Gradio 会自动将组件值按顺序传入"""
    
    # 1. 校验
    if not style or not style.strip():
        raise gr.Error("请输入风格描述")
    if not lyrics or not lyrics.strip():
        raise gr.Error("请输入歌词")
    
    # 2. 构建参数
    params = GenerationParams(
        style=style.strip(),
        lyrics=lyrics.strip(),
        cot=CotMode(cot),
        seed=int(seed),
        cfg_scale=float(cfg_scale) if cfg_scale else None,
        num_inference_steps=int(num_inference_steps),
        abc=abc_text if abc_text and cot != "off" else None,
        abc_sampling=SamplingParams(
            temperature=abc_temp, top_p=abc_top_p, top_k=int(abc_top_k),
            repetition_penalty=abc_rep_penalty, penalty_window=int(abc_pen_window),
            min_tokens=int(abc_min_tok), max_tokens=int(abc_max_tok),
        ),
        semantic_sampling=SamplingParams(
            temperature=sem_temp, top_p=sem_top_p, top_k=int(sem_top_k),
            repetition_penalty=sem_rep_penalty, penalty_window=int(sem_pen_window),
            min_tokens=int(sem_min_tok), max_tokens=int(sem_max_tok),
        ),
    )
    
    # 3. 提交任务
    task_id = task_manager.submit(params)
    
    # 4. 轮询进度
    while True:
        task = task_manager.get_status(task_id)
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            break
        
        # 更新Gradio进度
        phase_labels = {
            "planning": "正在规划乐谱...",
            "generating": "正在生成音乐...",
            "synthesizing": "正在合成音频...",
            "decoding": "正在解码音频...",
        }
        label = phase_labels.get(task.phase, "准备中...")
        
        if task.total_steps and task.total_steps > 0:
            progress(task.current_step / task.total_steps, desc=f"{label} ({task.current_step}/{task.total_steps})")
        else:
            progress(0, desc=label)
        
        time.sleep(0.5)
    
    # 5. 返回结果
    if task.status == TaskStatus.COMPLETED:
        audio_path = task.audio_path
        abc_display = None
        abc_file_path = Path(task.output_dir) / "score.abc"
        if abc_file_path.exists():
            abc_display = abc_file_path.read_text(encoding="utf-8")
        
        info = f"生成耗时 **{task.generation_time_seconds:.1f}s**，音频时长 **{task.audio_duration_seconds:.1f}s**"
        
        return audio_path, abc_display, gr.update(visible=abc_display is not None), info, audio_path
    
    elif task.status == TaskStatus.CANCELLED:
        raise gr.Error("已取消生成")
    else:
        raise gr.Error(f"生成失败: {task.error_message}")
```

#### 6.2.5 随机种子

```python
def on_random_seed():
    """生成随机种子"""
    import random
    return random.randint(0, 2**31 - 1)
```

### 6.3 完整 UI 布局线框图 (详细版)

```
┌─ YuE2 Music Studio ──────────────────────────────────────────────────────────┐
│                                                                              │
│  [创作]  [历史]  [设置]  [帮助]                                               │
│                                                                              │
│  ═══════════════════════ Tab: 创作 ═══════════════════════                     │
│                                                                              │
│  ┌─ 风格描述 ───────────────────────────────────────────────────────────────┐ │
│  │                                                                          │ │
│  │  [English, warm piano pop, expressive female voice,        (Textbox ×2)] │ │
│  │  [acoustic piano, rounded bass and light drums, 88 BPM____]              │ │
│  │                                                                          │ │
│  │  快捷: [钢琴流行] [民谣] [摇滚] [爵士] [电子] [中文流行] [R&B] [古典跨界]  │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  ┌─ 歌词 ───────────────────────────────────────────────────────────────────┐ │
│  │                                                                          │ │
│  │  模板: [下拉选择: 流行(Verse+Chorus) ▼]    [填充模板]                     │ │
│  │                                                                          │ │
│  │  [Verse]                                                   (Textbox ×10) │ │
│  │  Neon fades along the lane                                               │ │
│  │  Footsteps keep the time of rain                                         │ │
│  │  ...                                                                     │ │
│  │                                                                          │ │
│  │  [Chorus]                                                                │ │
│  │  Let the day come into view                                              │ │
│  │  ...                                                                     │ │
│  │                                                                          │ │
│  │  提示: 支持 [Verse] [Chorus] [Bridge] [Intro] [Outro] 段落标记            │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  ┌─ 工作模式 ───────────────────────────────────────────────────────────────┐ │
│  │                                                                          │ │
│  │  ● 完整创作 (full)  — 生成乐谱+和弦，质量最高                             │ │
│  │  ○ 旋律创作 (melody) — 仅旋律，适合翻唱/改编                              │ │
│  │  ○ 直接生成 (off)   — 跳过乐谱，速度最快                                  │ │
│  │                                                                          │ │
│  │  ┌─ 外部ABC乐谱 (仅选择"外部乐谱"时显示) ──────────────────────────────┐  │ │
│  │  │  粘贴ABC: [________________________] (Textbox ×8)                   │  │ │
│  │  │  或上传: [选择文件] (.abc)                                          │  │ │
│  │  └────────────────────────────────────────────────────────────────────┘  │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  ┌─ 基本参数 ───────────────────────────────────────────────────────────────┐ │
│  │                                                                          │ │
│  │  种子: [831001] [🎲随机]     CFG强度: [Auto========] 0~20               │ │
│  │  ODE步数: [8========] 1~64   (8=快速, 16=标准, 32=高质量)               │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  ┌─ ▶ 高级采样参数 (点击展开) ─────────────────────────────────────────────┐ │
│  │                                                                          │ │
│  │  ┌─ ABC 乐谱采样 ─────────────────────────────────────────────────────┐  │ │
│  │  │  温度: [0.7====] 0~5    Top-P: [0.9====] 0~1    Top-K: [30===]    │  │ │
│  │  │  重复惩罚: [1.005==] 0~3   窗口: [100===] 1~100                   │  │ │
│  │  │  Token范围: [32] ~ [4096]                                         │  │ │
│  │  │  [恢复默认]                                                        │  │ │
│  │  └────────────────────────────────────────────────────────────────────┘  │ │
│  │  ┌─ 语义 Token 采样 ──────────────────────────────────────────────────┐  │ │
│  │  │  温度: [1.0====] 0~5    Top-P: [0.95===] 0~1    Top-K: [100===]  │  │ │
│  │  │  重复惩罚: [1.2====] 0~3   窗口: [50===] 1~100                   │  │ │
│  │  │  Token范围: [200] ~ [9000]                                        │  │ │
│  │  │  [恢复默认]                                                        │  │ │
│  │  └────────────────────────────────────────────────────────────────────┘  │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │                                                                          │ │
│  │                    [ 🎵  生 成 歌 曲 ]  (大按钮, primary)                │ │
│  │                                                                          │ │
│  │  进度: ████████████░░░░░░░░ 60%  正在生成音乐... (1405/2341 tokens)      │ │
│  │                                                                          │ │
│  │  [取消生成]                                                              │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  ┌─ 输出结果 ───────────────────────────────────────────────────────────────┐ │
│  │                                                                          │ │
│  │  ┌──────────────────────────────────────────────────────────────────┐    │ │
│  │  │  ▶ ───────────●────────────────── 01:06 / 01:06   🔊            │    │ │
│  │  └──────────────────────────────────────────────────────────────────┘    │ │
│  │                                                                          │ │
│  │  生成耗时 23.1s | 音频时长 66.3s | 48kHz 立体声                          │ │
│  │                                                                          │ │
│  │  下载: [WAV] [FLAC]  |  ABC乐谱: [查看/展开]                              │ │
│  │                                                                          │ │
│  │  ┌─ ABC 乐谱 ─────────────────────────────────────────────────────────┐  │ │
│  │  │ X:1                                                                │  │ │
│  │  │ M:4/4  L:1/16  Q:1/4=88                                           │  │ │
│  │  │ V: Vocal clef=treble                                               │  │ │
│  │  │ K:C                                                                │  │ │
│  │  │ "C"E2G2A2G2E2D2C4|"G"D2E2G2E2D2C2D4|...                           │  │ │
│  │  └────────────────────────────────────────────────────────────────────┘  │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                              │
│  ┌─ 预设 ───────────────────────────────────────────────────────────────────┐ │
│  │  预设名: [__________]  [保存当前参数]  [加载预设▼]  [重置为默认]           │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 7. 任务管理与并发控制

### 7.1 TaskManager 完整设计

```python
import threading
import queue
import uuid
import time
from datetime import datetime

class TaskManager:
    """
    单GPU串行任务管理器。
    
    设计约束:
    - 单GPU同时只能运行一个生成任务 (否则OOM)
    - 支持排队: 用户可以提交多个任务，依次执行
    - 支持取消: 排队中的可以直接移除，运行中的可以kill进程
    - 进度回调: 后端通过callback上报进度，前端通过轮询获取
    """
    
    def __init__(self, backend, max_queue_size: int = 5):
        self.backend = backend
        self.max_queue_size = max_queue_size
        
        self._queue = queue.Queue(maxsize=max_queue_size)
        self._tasks: dict[str, TaskRecord] = {}  # task_id → record
        self._progress: dict[str, TaskProgress] = {}  # task_id → latest progress
        self._lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}  # task_id → cancel event
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
    
    def submit(self, params: GenerationParams) -> str:
        """
        提交生成任务。
        
        返回 task_id。
        如果队列已满，抛出 queue.Full。
        """
        task_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + params.id
        
        record = TaskRecord(
            task_id=task_id,
            params=params,
            status=TaskStatus.PENDING,
            created_at=datetime.now().isoformat(),
        )
        
        cancel_event = threading.Event()
        
        with self._lock:
            self._tasks[task_id] = record
            self._cancel_events[task_id] = cancel_event
        
        self._queue.put((task_id, cancel_event))
        
        # 确保worker在运行
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._running = True
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()
        
        return task_id
    
    def get_status(self, task_id: str) -> TaskRecord:
        """查询任务状态"""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError(f"Unknown task: {task_id}")
            # 附加最新进度
            progress = self._progress.get(task_id)
            if progress:
                record.current_phase = progress.phase
                record.current_progress = progress
            return record
    
    def cancel(self, task_id: str) -> bool:
        """
        取消任务。
        
        - 排队中: 从队列移除，状态改为CANCELLED
        - 运行中: 发送cancel_event，worker会kill进程
        - 已完成: 忽略
        """
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return False
            if record.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                return False
            
            cancel_event = self._cancel_events.get(task_id)
            if cancel_event:
                cancel_event.set()
            
            if record.status == TaskStatus.PENDING:
                record.status = TaskStatus.CANCELLED
                # 尝试从队列移除 (不保证成功，但worker会检查状态)
            
            return True
    
    def _worker_loop(self):
        """Worker线程主循环"""
        while self._running:
            try:
                task_id, cancel_event = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            
            with self._lock:
                record = self._tasks[task_id]
                if record.status == TaskStatus.CANCELLED:
                    continue  # 跳过已取消的
            
            record.status = TaskStatus.RUNNING
            record.started_at = datetime.now().isoformat()
            
            def on_progress(progress: TaskProgress):
                progress.task_id = task_id
                with self._lock:
                    self._progress[task_id] = progress
            
            # 创建输出目录
            output_dir = self._prepare_output_dir(record)
            record.output_dir = str(output_dir)
            
            # 执行生成
            result = self.backend.generate(
                params=record.params,
                output_dir=output_dir,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
            
            # 更新记录
            if result.success:
                record.status = TaskStatus.COMPLETED
                record.audio_path = result.audio_path
                record.audio_duration_seconds = result.audio_duration_seconds
                record.generation_time_seconds = result.generation_time_seconds
                
                # 写入config.json
                self._write_config(record, result, output_dir)
                
                # 追加到历史
                history_manager.append(record)
            elif cancel_event.is_set():
                record.status = TaskStatus.CANCELLED
            else:
                record.status = TaskStatus.FAILED
                record.error_message = result.error_message
            
            record.completed_at = datetime.now().isoformat()
    
    def _prepare_output_dir(self, record: TaskRecord) -> Path:
        """创建输出目录"""
        ...
    
    def _write_config(self, record: TaskRecord, result: GenerationResult, output_dir: Path):
        """写入config.json"""
        ...
```

### 7.2 取消机制详解

```
取消流程:

1. 用户点击 [取消生成]
2. Gradio回调 → task_manager.cancel(task_id)
3. cancel_event.set()
4. 两种情况:
   a. 任务排队中:
      - 从队列中移除 (或在worker取出时检查status==CANCELLED跳过)
      - 立即返回
   b. 任务运行中:
      - worker线程在下一次读取stdout行时检查cancel_event
      - 检测到取消 → process.kill()
      - 返回 GenerationResult(success=False, error_message="已取消")
      - record.status = CANCELLED

注意: process.kill() 在Windows上会立即终止进程。
      已写入的部分输出文件需要清理。
```

### 7.3 显存监控

```python
def get_gpu_info() -> dict:
    """
    获取GPU信息，用于设置页面展示。
    
    返回:
        {
            "name": "NVIDIA GeForce RTX 3080",
            "total_mb": 10240,
            "used_mb": 0,  # 空闲时
            "free_mb": 10240,
            "temperature": 45,
        }
    """
    try:
        import torch
        if not torch.cuda.is_available():
            return {"error": "CUDA不可用"}
        
        props = torch.cuda.get_device_properties(0)
        free, total = torch.cuda.mem_get_info(0)
        
        return {
            "name": props.name,
            "total_mb": total // (1024 * 1024),
            "used_mb": (total - free) // (1024 * 1024),
            "free_mb": free // (1024 * 1024),
            "compute_capability": f"{props.major}.{props.minor}",
        }
    except Exception as e:
        return {"error": str(e)}
```

---

## 8. 历史记录与预设系统

### 8.1 历史数据 Schema (history.json)

```json
{
  "version": 1,
  "entries": [
    {
      "task_id": "20260919_143022_city_lights",
      "created_at": "2026-09-19T14:30:22",
      "style": "English, warm piano pop, expressive female voice, 88 BPM",
      "lyrics_preview": "[Verse]\nNeon fades along the lane\nFootsteps keep the time...",
      "cot": "full",
      "seed": 831001,
      "audio_duration_seconds": 66.3,
      "generation_time_seconds": 23.1,
      "audio_path": "outputs/20260919_143022_city_lights/audio.wav",
      "output_dir": "outputs/20260919_143022_city_lights",
      "backend": "gguf",
      "status": "completed"
    }
  ]
}
```

**操作**:
- `append(record)` — 追加一条记录
- `list_all()` — 返回全部记录 (按时间倒序)
- `delete(task_id)` — 删除记录 + 输出目录
- `clear()` — 清空全部
- `auto_prune(max_entries=100)` — 超过上限时删除最旧的

### 8.2 预设数据 Schema (presets/{name}.json)

```json
{
  "name": "快速demo",
  "description": "最快出结果，质量一般",
  "params": {
    "cot": "off",
    "num_inference_steps": 4,
    "abc_sampling": {
      "temperature": 0.5,
      "max_tokens": 2048
    },
    "semantic_sampling": {
      "temperature": 0.8,
      "max_tokens": 4000
    }
  }
}
```

**内置预设**:

| 预设名 | 描述 | 关键差异 |
|--------|------|---------|
| 默认 | 标准质量 | cot=full, steps=8, 默认采样 |
| 快速demo | 最快出结果 | cot=off, steps=4, 低max_tokens |
| 高质量 | 最佳质量 | cot=full, steps=32, 高max_tokens |
| 创意模式 | 更多样化 | temperature=1.5, top_p=0.98 |
| 保守模式 | 最稳定 | temperature=0.3, repetition_penalty=1.5 |

---

## 9. 错误处理与边界情况

### 9.1 错误分类与处理策略

| 错误类型 | 示例 | 处理 | 用户提示 |
|---------|------|------|---------|
| **参数校验失败** | style为空, seed<0 | 阻止提交 | "风格描述不能为空" |
| **模型文件缺失** | GGUF文件不存在 | 启动时检查 | "模型文件缺失: yue2-3b-q8_0.gguf" |
| **显存不足** | CUDA OOM | 捕获stderr | "显存不足，请尝试降低ODE步数或使用q4_0模型" |
| **CLI崩溃** | 返回码非0 | 解析stderr | "生成失败 (错误码1): [具体错误]" |
| **输出文件缺失** | 生成完但无WAV | 检查文件 | "生成完成但输出文件不存在" |
| **磁盘空间不足** | 写入失败 | 捕获IOError | "磁盘空间不足" |
| **取消** | 用户取消 | 清理部分文件 | "已取消生成" |
| **队列已满** | 超过max_queue_size | 拒绝提交 | "任务队列已满(最多5个)，请等待当前任务完成" |
| **GPU不可用** | 无CUDA设备 | 启动时检查 | "未检测到CUDA GPU" |

### 9.2 启动时检查清单

```python
def startup_checks(project_root: Path) -> list[str]:
    """
    启动时运行检查，返回警告列表。
    严重问题直接阻止启动。
    """
    warnings = []
    
    # 1. audiocpp_cli.exe 存在
    cli_path = project_root / "audio-cpp" / "audiocpp_cli.exe"
    if not cli_path.exists():
        raise RuntimeError(f"找不到 audiocpp_cli.exe: {cli_path}")
    
    # 2. 模型文件
    model_path = project_root / "models" / "yue2-3b-q8_0.gguf"
    if not model_path.exists():
        raise RuntimeError(f"找不到模型文件: {model_path}")
    
    vae_path = project_root / "models" / "yue2-vae-f16.gguf"
    if not vae_path.exists():
        raise RuntimeError(f"找不到VAE文件: {vae_path}")
    
    # 3. Sidecar文件
    for name in ["yue2-model-config.json", "yue2-generation-config.json",
                  "yue2-qwen.tiktoken", "yue2-vae-config.json"]:
        path = project_root / "models" / "sidecars" / name
        if not path.exists():
            raise RuntimeError(f"找不到配置文件: {path}")
    
    # 4. CUDA 可用性
    try:
        import torch
        if not torch.cuda.is_available():
            warnings.append("警告: CUDA不可用，将使用CPU模式(非常慢)")
    except ImportError:
        warnings.append("提示: 未安装PyTorch，GPU监控不可用")
    
    # 5. 磁盘空间
    import shutil
    total, used, free = shutil.disk_usage(str(project_root))
    if free < 1 * 1024**3:  # < 1GB
        warnings.append(f"警告: 磁盘剩余空间不足 ({free // 1024**3}GB)")
    
    # 6. outputs目录
    outputs_dir = project_root / "yue2-webui" / "outputs"
    outputs_dir.mkdir(exist_ok=True)
    
    return warnings
```

### 9.3 边界情况处理

| 情况 | 处理 |
|------|------|
| 歌词超长 (>9000 tokens) | semantic_max_tokens截断，提示"歌词过长，歌曲可能在末尾被截断" |
| ABC乐谱格式错误 | GGUF后端会自行处理；Python后端可提前验证 |
| 相同task_id (同一秒提交) | task_id加随机后缀: `20260919_143022_abc123_song` |
| 输出目录已存在 | 加序号: `20260919_143022_song_2/` |
| 生成过程中GPU掉驱动 | subprocess返回非0，捕获错误提示 |
| 浏览器关闭后重连 | Gradio自动恢复session；任务在后台继续执行 |
| 历史文件损坏 | 启动时try/except加载，损坏的跳过并警告 |
| 并发浏览器访问 | Gradio多用户共享同一后端；任务排队 |

---

## 10. 开发计划与里程碑

### Phase 1: MVP (最小可用版) — 预计 2-3 小时

**目标**: 在浏览器里输入歌词+风格，点击生成，听到歌曲。

| 步骤 | 文件 | 内容 | 预计 |
|------|------|------|------|
| 1.1 | `requirements.txt` | `gradio>=5.0` | 5min |
| 1.2 | `config.py` | 核心参数schema (style/lyrics/cot/seed/steps) | 20min |
| 1.3 | `backend_gguf.py` | `build_command()` + `generate()` + 基础日志解析 | 40min |
| 1.4 | `app.py` | Gradio UI: 风格+歌词+模式+生成+音频播放 | 40min |
| 1.5 | `run.bat` | 一键启动: `python app.py` | 5min |
| 1.6 | 测试 | 实际运行一次完整生成 | 15min |

**MVP交付物**:
- 浏览器打开 `http://localhost:7860`
- 输入风格+歌词 → 选择模式 → 生成 → 播放音频
- 基本参数: style, lyrics, cot, seed, ODE步数

### Phase 2: 完整参数 — 预计 1-2 小时

| 步骤 | 文件 | 内容 |
|------|------|------|
| 2.1 | `config.py` | 全部采样参数schema |
| 2.2 | `app.py` | 高级参数面板 (Accordion) |
| 2.3 | `style_presets.py` | 8个风格预设 + UI按钮 |
| 2.4 | `lyrics_templates.py` | 3个歌词模板 + Dropdown |
| 2.5 | `app.py` | 随机种子按钮 + 恢复默认按钮 |
| 2.6 | `app.py` | ABC外部乐谱输入区 (条件显示) |

### Phase 3: 历史与管理 — 预计 1-2 小时

| 步骤 | 文件 | 内容 |
|------|------|------|
| 3.1 | `history.py` | 历史CRUD + JSON持久化 |
| 3.2 | `app.py` | 历史Tab: Dataframe + 试听 + 重新生成 |
| 3.3 | `app.py` | 设置Tab: 后端选择 + 模型检查 + GPU信息 |
| 3.4 | `app.py` | 预设保存/加载 |
| 3.5 | `app.py` | 自动清理旧输出 |

### Phase 4: Python后端 (可选) — 预计 2-3 小时

| 步骤 | 文件 | 内容 |
|------|------|------|
| 4.1 | `backend_python.py` | 封装YuE2Pipeline |
| 4.2 | `app.py` | 后端切换UI |
| 4.3 | `app.py` | Python后端产物展示 (ABC乐谱、中间产物下载) |

### Phase 5: 增强 (未来)

- [ ] ABC乐谱语法高亮编辑器
- [ ] 音频波形可视化
- [ ] 批量生成 (JSONL上传)
- [ ] 生成参数对比 (A/B试听)
- [ ] 主题切换 (暗色/亮色)
- [ ] 国际化 (英文/中文)
- [ ] 导出为独立可执行文件 (PyInstaller)

---

## 附录 A: 启动脚本

### run.bat (Windows)

```batch
@echo off
chcp 65001 >nul
echo YuE2 Music Studio
echo =================

REM 检查Python
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到Python，请先安装Python 3.10+
    pause
    exit /b 1
)

REM 检查Gradio
python -c "import gradio" >nul 2>&1
if errorlevel 1 (
    echo 安装Gradio...
    pip install gradio>=5.0
)

REM 启动
echo 启动中... 打开 http://localhost:7860
python app.py

pause
```

### run.sh (Linux/Mac)

```bash
#!/bin/bash
echo "YuE2 Music Studio"
echo "================="

if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到Python3"
    exit 1
fi

if ! python3 -c "import gradio" 2>/dev/null; then
    echo "安装Gradio..."
    pip3 install "gradio>=5.0"
fi

echo "启动中... 打开 http://localhost:7860"
python3 app.py
```

---

## 附录 B: app.py 入口结构

```python
"""YuE2 Music Studio — Gradio WebUI for YuE2 Music Generation"""
import gradio as gr
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent  # Y:/NewStore/AI/Yue2/

# 初始化
from config import PARAM_SCHEMA, DEFAULT_PARAMS
from backend_gguf import GGUFBackend
from task_manager import TaskManager
from history import HistoryManager
from style_presets import STYLE_PRESETS
from lyrics_templates import LYRICS_TEMPLATES

backend = GGUFBackend(PROJECT_ROOT)
task_manager = TaskManager(backend)
history_manager = HistoryManager(PROJECT_ROOT / "yue2-webui" / "history.json")

# 启动检查
warnings = startup_checks(PROJECT_ROOT)

# 构建UI
with gr.Blocks(title="YuE2 Music Studio", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# YuE2 Music Studio")
    gr.Markdown("AI音乐创作 — 输入歌词和风格，生成完整歌曲")
    
    for w in warnings:
        gr.Markdown(f"> ⚠️ {w}")
    
    with gr.Tabs():
        with gr.Tab("创作"):
            build_create_tab()
        with gr.Tab("历史"):
            build_history_tab()
        with gr.Tab("设置"):
            build_settings_tab()
        with gr.Tab("帮助"):
            build_help_tab()

def build_create_tab():
    """构建创作Tab的完整UI和事件绑定"""
    ...

def build_history_tab():
    """构建历史Tab"""
    ...

def build_settings_tab():
    """构建设置Tab"""
    ...

def build_help_tab():
    """构建帮助Tab — 使用说明、参数解释、ABC格式"""
    gr.Markdown("""
    ## 使用说明
    
    ### 快速开始
    1. 输入风格描述 (如 "English, piano pop, female vocal, 88 BPM")
    2. 输入歌词 (支持 [Verse] [Chorus] 段落标记)
    3. 选择工作模式 (推荐"完整创作")
    4. 点击"生成歌曲"
    5. 等待20-30秒，播放生成的歌曲
    
    ### 工作模式说明
    - **完整创作**: 生成乐谱+和弦 → 质量最高，速度最慢
    - **旋律创作**: 仅生成旋律 → 适合翻唱/改编
    - **直接生成**: 跳过乐谱 → 速度最快，适合快速试听
    
    ### 风格描述技巧
    包含以下要素效果最好:
    - 语言 (English/Mandarin/Japanese)
    - 流派 (pop/rock/jazz/folk/EDM)
    - 乐器 (piano/guitar/strings/synth)
    - 人声 (female/male, 音色描述)
    - 速度 (BPM数字 或 slow/fast/upbeat)
    """)

if __name__ == "__main__":
    demo.launch(
        server_name="127.0.0.1",
        server_port=7860,
        share=False,
        show_error=True,
    )
```

---

## 附录 C: GGUF 后端日志格式 (待确认)

> 需要在 Phase 1 开发时实际运行 `audiocpp_cli.exe --log` 一次，
> 抓取完整输出，然后据此实现 `LogParser`。
> 
> 如果日志不包含逐token进度，退化为阶段级别更新:
> `loading → planning → generating → synthesizing → decoding → done`

---

## 附录 D: 与 generate.bat 的对应关系

WebUI生成的一次调用，等价于:

```batch
audio-cpp\audiocpp_cli.exe ^
  --task gen ^
  --family yue2 ^
  --model models/yue2-3b-q8_0.gguf ^
  --backend cuda ^
  --threads 8 ^
  --text "<用户输入的歌词>" ^
  --request-option "style=<用户输入的风格>" ^
  --request-option cot=<用户选择的模式> ^
  --request-option seed=<用户设置的种子> ^
  --request-option num_inference_steps=<ODE步数> ^
  --session-option yue2.model_gguf=yue2-3b-q8_0.gguf ^
  --session-option yue2.vae_gguf=yue2-vae-f16.gguf ^
  --out outputs/<时间戳>_<id>/audio.wav ^
  --log
```

WebUI 在此基础上增加了:
- 全部采样参数的 `--request-option` 传入 (仅非默认值)
- 外部ABC的 `abc=` 或 `abc_file=` 传入
- CFG强度的 `guidance_scale=` 传入
- 自动参数校验和默认值填充
- 进度解析和历史记录
