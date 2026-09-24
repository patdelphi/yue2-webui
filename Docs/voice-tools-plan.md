# 音轨分离与参考音色翻唱 · 功能规划（需求 / 技术 / 设计）

- 版本：v0.1（草案，待评审）
- 日期：2026-09-24
- 状态：**待用户评审** — 评审通过后替代 `todo.md` 作为实施依据
- 关联文档：[design.md](design.md)（现有系统设计 v2.1）、[requirements.md](requirements.md)、[setup.md](setup.md)

---

## 0. 背景与定位

调研 [T8mars/Comfyui-YuE2-T8](https://github.com/T8mars/Comfyui-YuE2-T8)（v1.1.0 引入 Seed-VC + Demucs 零样本翻唱）后确定的两个新功能：

| 功能 | 模型链 | 定位 |
|---|---|---|
| **F1 音轨分离** | Demucs (HTDemucs) | 独立工具：任意音频 → 人声/伴奏（2轨）或 鼓/贝斯/其他（4轨） |
| **F2 参考音色翻唱** | Demucs + Seed-VC + ffmpeg | 自动流程：完整歌曲 → 分离人声 → 换成参考音色 → 与伴奏混音 |

**核心原则（用户确认）**：既要功能分离（每个环节可独立使用），又要能组装为自动流程（一键翻唱内部串起全链路）。

**与 YuE2 的关系**：无模型耦合。YuE2 产出歌曲（或任何外部音频）只是翻唱管线的**输入文件**，集成本质是数据管道 + 队列编排 + UI 产品化。

**两种"翻唱"路线区分**（文档中统一术语）：

- 路线 A「重新演绎」：YuE2 转谱 + 新风格重新生成（**已有功能**，转谱页 + 生成页组合）
- 路线 B「参考音色翻唱」：Seed-VC 换嗓，音乐内容原封不动（**本规划新增**）

---

## 1. 需求（Requirements）

### 1.1 功能需求

**F1 音轨分离（独立功能）**

- F1-1 输入来源三入口：创作页当前输出（含批量变体的任意一个）、历史页任意记录、直接上传音频文件
- F1-2 分离模式：2 轨（vocals / accompaniment）与 4 轨（vocals / drums / bass / other）可选
- F1-3 产物可试听、可分别下载（WAV）
- F1-4 分离结果进入历史记录（可溯源、可删除），可被翻唱流程复用

**F2 参考音色翻唱（自动流程）**

- F2-1 输入：完整歌曲（三入口同 F1-1）+ 参考干声音频（1–30 秒，上传或从音色库选择）
- F2-2 一键执行完整管线：分离 → 换嗓 → 混音，分阶段显示进度
- F2-3 音高偏移：−12 ~ +12 半音滑块 + 快捷按钮（−12 原调 +12）
- F2-4 产出：48kHz 立体声成品（FLAC + WAV），以及中间产物（换嗓人声轨、所用伴奏轨）保留可下载
- F2-5 翻唱成品进入历史记录，可再次发起翻唱（链式，如男声版→女声版）
- F2-6 参数：diffusion steps（10–50，默认 30）、伴奏音量微调（dB，−6 ~ +6，默认 0）

**F3 产物管理与跨流程复用**

- F3-1 所有衍生品（分离轨、翻唱成品）物理上聚合在**源任务目录**下（见 3.4），不散落
- F3-2 历史页支持按类型筛选（生成 / 分离 / 翻唱），衍生记录显示来源任务链接
- F3-3 参考音色库：可保存常用参考干声（命名 + 列表选择），MVP 存本地目录

### 1.2 非目标（本期明确排除）

- RVC 训练 / LoRA 训练（T8 的训练工作台）
- 实时变声（Seed-VC realtime 模式）
- MuLaCover 重新编曲、MIDI 编辑器
- T8 式完整"项目工作台"（多项目 / 素材库 / 草稿 / 回收站体系）——理由见 3.6

### 1.3 验收标准（可验证）

| 编号 | 标准 |
|---|---|
| A1 | 上传任意 WAV/MP3/FLAC，2 轨分离产出可播放的 vocals.wav 与 accompaniment.wav；4 轨产出 4 个文件 |
| A2 | 从历史页选择一条生成记录 + 上传参考干声 + 半音 −12，一键翻唱产出 48kHz FLAC，人声音色明显切换、伴奏保留 |
| A3 | 翻唱任务在生成任务运行期间提交时正确排队（不并发抢显存），完成后队列窗口显示新任务类型 |
| A4 | 语言切换后，新 Tab 全部文案（含进度、错误信息）即时切换；任务运行中切换语言不影响进行中任务的文案 |
| A5 | 衍生品目录符合 3.4 命名规范；历史页可按类型筛选；删除源记录时衍生记录与文件按现有回收站规则处理 |
| A6 | 单元测试：命名规则、HistoryRecord 扩展字段、config.cfg [voice] 段解析（含缺失回退）全部通过；集成冒烟用例在无模型环境自动 skip |

---

## 2. 技术方案（Technology）

### 2.1 模型链与数据流

```
完整歌曲 (WAV/FLAC/MP3)
   │
   ▼
[1] Demucs HTDemucs ──→ vocals.wav + accompaniment.wav        (~1.5–2GB 显存)
   │                        │
   │ vocals                 └─ accompaniment ──→ 混音前暂存
   ▼
[2] Seed-VC v1 SVC 模型                                       (~2–3GB 显存)
   ├─ 内容编码器: Whisper-base
   ├─ F0 提取: RMVPE (f0-condition=True, SVC 必须)
   ├─ 说话人嵌入: CAMPPlus (读参考音频)
   └─ 声码器: BigVGAN v2 44kHz
   │
   ▼ converted_vocals.wav (44.1kHz)
[3] ffmpeg 混音 (amix + 伴奏 dB 增益) ──→ cover.flac (48kHz 立体声)
```

- SVC 主模型：`DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema.pth`（200M 参数，44.1kHz）
- Demucs：HTDemucs 权重约 80MB
- 模型总下载量约 2–2.5GB（一次性）
- 关键参数：`--f0-condition True`（固定）、`--auto-f0-adjust False`（固定）、`--semi-tone-shift`（用户可调）、`--diffusion-steps` 30–50 推荐（用户可调）
- 半音经验值：女声原曲换男声 −12；男声原曲换女声 +12（T8 同款快捷项）

### 2.2 进程架构（借鉴 T8，差异：不 vendor 源码）

```
yue2-webui/
├── app.py                     # 主 Gradio 应用（现有 venv，Python 3.10+）
├── voice-tools/               # ★ 新增：独立语音工具域
│   ├── worker.py              # HTTP worker（FastAPI 或 stdlib http.server）
│   ├── requirements.txt       # 独立依赖（torch / demucs / seed-vc 运行时）
│   ├── install_voice.bat/.sh  # 独立 venv 安装脚本
│   ├── models/                # 模型缓存目录（HF 下载自动落位）
│   └── refs/                  # 参考音色库（用户保存的干声）
└── src/
    ├── voice_client.py        # ★ 新增：主 app 侧的 worker HTTP 客户端（含异常处理）
    └── ...
```

- **独立 venv + 子进程**：Seed-VC / Demucs 的 torch 生态与主 venv 隔离，避免依赖冲突（T8 用独立推理 worker `127.0.0.1:8189` 的同款思路）
- **懒启动**：首次提交分离/翻唱任务时由主 app 拉起 worker 子进程（端口默认 8190，被占用时 +1 重试），健康检查 30s 超时给出明确错误
- **接口**（worker 内只做模型推理，不含业务逻辑）：
  - `GET /api/health` → 模型就绪状态 + 显存余量
  - `POST /api/separate` `{input, mode, output_dir}` → 产物路径字典
  - `POST /api/convert` `{source, ref, semi_tone, diffusion_steps, output_dir}` → 换嗓人声路径
- **混音在主 app 侧用 ffmpeg 完成**（遵守项目规则：音视频处理用 ffmpeg），worker 不依赖 ffmpeg
- **显存释放**：worker 空闲 5 分钟自动卸载模型（借鉴 T8 v1.6.8"任务完成自动释放"）

### 2.3 显存与性能预算（RTX 3080 10GB 实测评估）

| 项 | 数值 |
|---|---|
| YuE2 生成（现行） | ~7GB 峰值 |
| Demucs 分离 | ~1.5–2GB |
| Seed-VC 换嗓 | ~2–3GB |
| **翻唱管线顺序峰值** | **3–4GB**（两阶段不同时驻留，阶段间释放） |

- 结论：10GB 卡**够用**，但必须**全程串行**——由 queue_manager 单 worker 模型天然保证（生成 7GB + 翻唱 3GB 并发必 OOM）
- 耗时预估：每首（3–4 分钟歌曲）额外 2–4 分钟（分离 ~30–60s + 换嗓与歌曲时长正相关）

### 2.4 长曲处理（风险项，阶段 1 实测定案）

- Seed-VC 推理显存/耗时随音频长度增长，长曲（>5 分钟）存在 OOM 风险（T8 v1.1.5 踩坑记录）
- 候选策略：(a) 输入长度上限提示；(b) 自动分块（30–60s 块 + 交叉淡出拼接）；(c) 降 diffusion steps
- **MVP 采用 (a) + 上限 8 分钟**，分块拼接列入阶段 2 优化项，实测后定

### 2.5 许可与分发（GPL-3.0 处理）

- Seed-VC 为 GPL-3.0。**本方案不 vendor 其源码进仓库**：用户自行 `git clone` 到 `voice-tools` 外部指定目录（或 pip 依赖方式安装到独立 venv），主 app 仅通过**进程边界（HTTP）**调用
- 子进程调用 + 不分发对方代码 → 无 GPL 传染问题；`Docs/setup.md` 增加独立章节说明获取方式
- Demucs 为 MIT，无约束

### 2.6 配置管理（config.cfg 扩展）

```ini
[voice]
enabled = true              ; 功能开关，false 时隐藏新 Tab 并拒收任务
worker_port = 8190          ; worker 端口，占用时自动 +1
seedvc_dir =                ; Seed-VC 仓库路径（空 = 未安装，UI 显示安装指引）
max_input_minutes = 8       ; 长曲上限（2.4 策略 a）
```

- 解析逻辑沿用 `load_model_config()` 的模式：缺失段/键回退默认值，路径支持相对（基于仓库根）与绝对路径
- 新增单元测试覆盖：默认回退 / 覆盖 / 绝对路径 / 部分缺失 / 文件损坏（对齐现有 config 测试场景）

---

## 3. 详细设计（Design）

### 3.1 UI 整合：新增第 5 个 Tab「音色工坊」

```
Tab: 🎤 创作 | 🎼 音色工坊(新) | 🎙 音频转谱 | 📜 历史 | ⚙️ 设置
```

**新 Tab 内部布局**（单一页面承载分离 + 翻唱两个折叠区）：

```
┌─ Tab: 音色工坊 ──────────────────────────────────────┐
│ ◉ 分离模式: ( ) 音轨分离   ( ) 参考音色翻唱          │
│                                                      │
│ [源音频] 三入口:                                      │
│   (•) 历史记录选择 [Dropdown ▾]                       │
│   ( ) 上传音频     [Audio/File 上传]                  │
│   （创作页"发送到翻唱"按钮自动切到此 Tab 并预填）       │
│                                                      │
│ ── 仅分离模式可见 ──────────────────────              │
│ 分离轨数: (•) 2 轨 (人声/伴奏)  ( ) 4 轨 (鼓/贝斯/其他)│
│                                                      │
│ ── 仅翻唱模式可见 ──────────────────────              │
│ 参考音色: [Audio 上传 1-30s]  或 [音色库 Dropdown ▾]  │
│ 半音偏移:  [-12 ═══●═══ +12]  [−12] [原调] [+12]     │
│ 扩散步数:  [10 ══●════ 50] (默认30)                   │
│ 伴奏增益:  [-6 ══●════ +6] dB (默认0)                 │
│                                                      │
│ [ 开始处理 ]  [取消]                                  │
│ 进度: 规划→分离→换嗓→混音 阶段化文案                   │
│                                                      │
│ [输出区] 成品播放 + 文件下载列表(FLAC/WAV/中间轨)      │
└──────────────────────────────────────────────────────┘
```

**现有页面挂点（入口按钮）**：

| 位置 | 按钮 | 行为 |
|---|---|---|
| 创作页输出区 | 「发送到音色工坊」 | 以**变体选择器当前选中项**为源，跳转新 Tab 并预填（多 variant 处理见 3.7） |
| 历史页详情区 | 「发送到音色工坊」 | 以当前选中记录的 audio_path 为源 |
| 转谱页 | 不加（转谱产物是 ABC，无音频） | — |

### 3.2 Gradio 组件选型

| 需求 | 组件 | 理由 |
|---|---|---|
| 模式切换（分离/翻唱） | `gr.Radio` + `.change` 控制两组组件 `visible` | 复用现有 cot_input 切换模式（on_cot_change 同款），Tab 高度稳定 |
| 源音频-历史选择 | `gr.Dropdown`（choices 从 history_mgr 实时刷新） | 复用历史数据，无需二次上传 |
| 源音频-上传 | `gr.Audio(sources=["upload"], type="filepath")` | 预览 + 路径直取 |
| 参考音色 | `gr.Audio` + 音色库 `gr.Dropdown` + 「保存到音色库」`gr.Button` | 上传即用 + 常用音色持久化 |
| 半音偏移 | `gr.Slider(-12, 12, step=1)` + 3 个 `gr.Button` | 精调 + 快捷（T8 同款） |
| 扩散步数/伴奏增益 | `gr.Slider` | 数值区间直观 |
| 提交/取消 | `gr.Button(variant="primary")` / 普通 | 对齐现有生成按钮交互 |
| 进度 | 提交按钮旁 `gr.Markdown` + queue_timer 已有机制 | 不引入新轮询（复用 2s Timer） |
| 产出多文件下载 | `gr.File`（翻唱）/ `gr.Files`（分离 2-4 轨） | Files 支持批量下载列表 |
| 中间产物试听 | `gr.Audio`（换嗓人声 / 伴奏各一） | 便于检查分离与换嗓质量 |

### 3.3 多语言方案（沿用现有模式，零新机制）

- 所有新文案进 `src/i18n.py` 词典，代码只调 `tr(lang, ...)`，**禁止硬编码中文**
- UI 组件注册沿用 `_reg(comp, updater)`，`apply_lang` 一次性下发全部更新（含新 Tab）
- **任务回调入队语言快照**（现有惯例）：分离/翻唱任务的阶段文案在 `submit()` 时锁定语言，规避运行中切换语言导致文案混杂
- 队列类型标签 `_QUEUE_TYPE_LABELS` 增加 separation / cover 双语映射

### 3.4 文件命名与目录规范（回应"文件名、分类、跨流程选择"）

**核心设计：任务目录即轻量项目（root task 聚合衍生品）**

```
outputs/
└── 20260924_153000_abc123/              ← 源任务目录（root task，现有结构不动）
    ├── 20260924_153000_abc123.wav/.flac/.mp3/.abc/.txt/.json   （现有产物）
    └── derived/                          ← ★ 新增：该源的所有衍生品聚合于此
        ├── sep_a1b2/                     ← 分离任务（短 id，4 位，控制路径长度）
        │   ├── vocals.wav
        │   ├── accompaniment.wav         （2 轨时）
        │   ├── drums.wav / bass.wav / other.wav（4 轨时）
        │   └── meta.json                 （输入源、模式、耗时）
        └── cover_c3d4/                   ← 翻唱任务
            ├── cover.flac                ← 成品（48kHz）
            ├── cover.wav
            ├── converted_vocals.wav      ← 换嗓人声（中间产物）
            ├── accompaniment.wav         ← 所用伴奏（中间产物）
            └── meta.json                 （参考音色名、半音、步数、增益、derived_from）
```

- **命名规则**：衍生任务目录 `sep_` / `cover_` + 4 位随机短 id（避免 Windows 260 路径限制，T8 v1.5.9 教训）；文件名固定语义名（vocals / cover 等），不再叠加时间戳
- **外部上传的源**：创建 `outputs/upload_<task_id>/` 目录，上传文件拷贝为 `source.<ext>` 作为源副本，衍生品同样进其 `derived/`——保证所有任务目录结构同构
- **跨流程选择机制**：
  - 历史页 Dropdown（音色工坊源选择）列出所有 generation/cover 记录的 audio_path
  - 历史记录中衍生记录带 `derived_from`，详情区显示「来源任务」链接文本
  - 链式翻唱（翻唱成品再翻唱）：root task 不变，产物仍聚合在最初源任务目录下，`derived_from` 指向直接前驱
- **sidecar meta.json**：对齐现有 postprocess 侧车惯例，记录完整参数（参考音色、半音、diffusion steps、伴奏增益、源任务 id、模型文件名），保证可复现

### 3.5 历史记录扩展（history.json 兼容升级）

`HistoryRecord` 新增 3 个**可选字段**（默认值保证旧记录兼容，`asdict` 自动带出）：

```python
record_type: str = "generation"   # generation | separation | cover
derived_from: str = ""            # 直接前驱 task_id（分离/翻唱记录必填）
root_task_id: str = ""            # 顶层源任务 id（链式翻唱时定位聚合目录）
```

- 历史页：类型列（带图标 🎵/🎚/🎤）+ 顶部筛选 Dropdown（全部/生成/分离/翻唱）
- 分离记录的 `audio_path` 指向 vocals.wav（主试听轨）；翻唱记录指向 cover.flac
- 删除规则不变：文件级回收站删除（现有 `delete_files_to_recycle`），衍生记录删除时清空其 `derived/` 子目录内容
- `prune_missing` / `auto_prune` 逻辑不变（自动清理同样覆盖衍生记录）

### 3.6 「项目」概念评估（结论：本期不引入）

| 维度 | 引入项目实体 | 不引入（本方案） |
|---|---|---|
| 产出物统和 | 显式：项目 → 任务 → 产物树 | 隐式：root_task 目录 + derived/ 聚合 + derived_from 链 |
| 改造量 | 大：项目管理页 / 切换 / 归档 / 草稿 / 迁移现有 history | 小：3 个可选字段 + 1 个筛选下拉 |
| 与现有模型契合 | 冲突：现系统"任务为中心"、100 条自动清理 | 无冲突：完全向后兼容 |
| 适用场景 | T8 式多素材库 + 训练 + 版本管理 | 单人本地创作，量级小 |
| 演化路径 | — | 未来需要时，`root_task_id` 可直接升级为项目 id，衍生链数据无损迁移 |

**推荐**：不引入项目实体。用「root task 聚合目录 + 衍生链字段」实现用户要的"统和动态工作流与产出物"——物理聚合（目录）+ 逻辑溯源（字段）双保险，且为将来升级留了无损路径。

### 3.7 多 variant 处理（回应"yue2 的多个 var 怎么处理"）

现状：批量生成时 N 个变体共享 `outputs/<base_task_id>/`，文件名后缀 `_varN`，**每个变体是独立历史记录**。

设计决策：

1. **创作页入口**：以「变体选择器当前选中项」为翻唱源——不强制先"选定为最终版"（更灵活，用户可对任意变体试音色），按钮文案随选择器联动显示当前变体名
2. **历史页入口**：天然支持——每个变体是独立记录，Dropdown 中显示为 `task_id + 风格摘要`，选哪个翻哪个
3. **记录归属**：变体发起的衍生品，`root_task_id` = 该变体的 base_task_id，产物进 `outputs/<base_task_id>/derived/`（同批变体的衍生品天然聚合同一目录，正是"目录即项目"的体现）
4. **推荐流程**（写进 UI 提示文案）：批量生成 → 试听对比 → 选定变体 → 发送音色工坊翻唱

### 3.8 队列与任务类型扩展

```python
class TaskType(Enum):
    GENERATION = "generation"        # 现有
    TRANSCRIPTION = "transcription"  # 现有
    SEPARATION = "separation"        # ★ 新增
    COVER = "cover"                  # ★ 新增
```

- 单 worker 不变 → 与生成天然串行（显存安全）
- 阶段化进度（`push_progress`）：分离 = 单阶段；翻唱 = `分离音轨 → 换嗓合成 → 混音导出` 三段文案，带百分比
- 取消：`cancel_event` 在**阶段边界与分块边界**生效（模型推理中不可中断，取消延迟 ≤ 当前阶段耗时，UI 文案明示）
- 设置页「当前队列」窗口自动显示新类型（`_QUEUE_TYPE_LABELS` 补条目即可，无需改 queue_manager 展示逻辑）

### 3.9 失败处理

- worker 未安装 / 未启动 / 健康检查超时：提交时预检（读 config.cfg + 端口探测），明确中文指引（链接 setup.md 章节）
- 模型缺失：首次运行自动从 HF 下载（支持 `HF_ENDPOINT` 镜像），下载失败给出镜像设置提示
- 分离成功但换嗓失败：保留分离产物并记录部分成功状态（status 字段扩展 `partial`），可从中间产物重试
- 混音失败（ffmpeg 异常）：保留两轨中间产物，任务标记失败 + 明确错误信息

---

## 4. 实施计划（阶段划分）

> 本节取代 `todo.md`（评审通过后 todo.md 删除或归档入本文件附录）。

| 阶段 | 内容 | 前置 | 验收 |
|---|---|---|---|
| **0 环境验证** | 手动 clone Seed-VC + 独立 venv + 下载模型（2–2.5GB）+ 命令行冒烟（分离/换嗓各一次） | **网络操作，需用户批准** | 两命令产出正常音频；记录实测显存/耗时基线 |
| **1 worker 与管线** | `voice-tools/worker.py` + `src/voice_client.py` + TaskType 扩展 + config [voice] 段 + 命名/记录/配置单元测试 | 阶段 0 | A6 单测全绿；curl 冒烟三接口通过 |
| **2 UI 与整合** | 音色工坊 Tab + 两个入口按钮 + i18n 词典 + 历史扩展（类型/筛选/derived 字段）+ 音色库 | 阶段 1 | A1–A5 全部通过 |
| **3 收尾** | setup.md 安装章节 + README 双语更新 + changelog | 阶段 2 | 文档评审通过 |

- 每阶段结束向用户汇报并确认后进入下一阶段
- 阶段 1 起所有新功能先写测试（项目规则）

---

## 5. 风险与开放问题

| # | 风险/问题 | 应对 | 状态 |
|---|---|---|---|
| R1 | 长曲 OOM（2.4） | MVP 限 8 分钟 + 提示；分块拼接留阶段 2 优化 | 待阶段 1 实测 |
| R2 | Seed-VC 与 demucs 的 torch 版本兼容 | 独立 venv 隔离；阶段 0 命令行验证 | 待验证 |
| R3 | Windows 长路径 | derived 层级浅 + 4 位短 id；沿用 outputs 相对路径 | 设计已规避 |
| R4 | GPL-3.0 | 不 vendor、进程边界调用、setup.md 说明获取方式 | 方案已定 |
| R5 | 换嗓后人声与伴奏响度不匹配 | 伴奏增益滑块（−6~+6dB）兜底；阶段 2 实测定默认值 | 待实测 |
| R6 | worker 僵尸进程 | 空闲自动退出 + 主 app 关闭时清理 + 端口探测复活 | 设计已含 |

### 待用户拍板的决策点（文档内已给推荐）

| # | 决策 | 推荐 |
|---|---|---|
| D1 | Tab 组织：独立「音色工坊」Tab（3.1） | ✅ 推荐（功能分离原则） |
| D2 | 不引入项目实体，用 root task + derived 链（3.6） | ✅ 推荐 |
| D3 | 多 variant：以变体选择器当前项为源，不强制选定最终版（3.7） | ✅ 推荐 |
| D4 | 参考音色库做轻量持久化（refs/ 目录 + Dropdown） | ✅ 推荐（成本低） |
| D5 | 阶段 0 网络操作（clone + 2–2.5GB 下载） | 暂缓，本文档评审通过后另行申请 |
