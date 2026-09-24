# 音轨分离 + 参考音色翻唱 — 待办（todo）

> 创建：2026-09-24 · 状态：待确认
> 目标拆成两个能力、共用一套底座：**分离可单独用**（导出人声/伴奏/分轨）+ **自动翻唱流程**（分离 → 换嗓 → 混音一键完成）。

## 一、功能范围

| 能力 | 说明 | 入口 |
| --- | --- | --- |
| F1 音轨分离 | Demucs HTDemucs：4 轨（人声/鼓/贝斯/其他）或 2 轨（人声/伴奏），各轨可下载 | 新 Tab + 历史页 + 创作页输出区 |
| F2 参考音色翻唱 | 分离 → Seed-VC 换嗓（1-30s 参考干声，±12 半音）→ ffmpeg 混音 → FLAC | 同上 |
| 来源 | 任意完整歌曲：刚生成的 / 历史记录 / 外部上传 | — |

## 二、技术方案要点

- **零污染隔离**：根目录新建 `voice-tools/`，内含 seed-vc 源码（git clone）+ **独立 venv**（装 seed-vc 依赖 + demucs）；主项目环境不动
- **子进程编排**（audio-cpp 同款模式）：`src/voice_convert.py` 用 subprocess 调用，进度解析 stdout 回调进 queue_manager
- 模型：Seed-VC v1 SVC 44k（DiT 200M + Whisper-base + RMVPE + CAMPPlus + BigVGAN）+ HTDemucs 4-source（80MB）
- 队列新增任务类型（复用单 GPU 串行，避免与生成叠加显存）；`config.cfg` 新增 `[voice]` 段
- 混音用 ffmpeg；UI 新增第 5 个 Tab（独立功能区）；全量 i18n
- **GPL-3.0 处理**：seed-vc 源码独立目录、不修改、保留原 LICENSE + 顶层 NOTICE 声明（子进程调用方式不分发其代码）

## 三、阶段任务

### 阶段 0 · 环境验证 ⚠️ 需外部网络：下载 ~2.5GB 模型 + pip 安装依赖
- [ ] `git clone` seed-vc 到 `voice-tools/`，建独立 venv，装依赖（seed-vc requirements + demucs）
- [ ] 下载模型（DiT_44k ~800MB / whisper-base / RMVPE / CAMPPlus / HTDemucs）
- [ ] CLI 实测：3 分钟歌 4 轨分离 + 一次 SVC 翻唱，记录**显存峰值与耗时**（3080 10GB 上限验证）

### 阶段 1 · 管线模块（先写测试）
- [ ] `tests/test_voice_convert.py`：分离/转换/混音编排逻辑（mock 子进程）
- [ ] `src/voice_convert.py`：`separate_tracks()` / `convert_voice()` / `mixdown()` / `run_cover_pipeline()`（分离独立可调 + 自动流程组装）
- [ ] `config.cfg` `[voice]` 段（模型路径/默认 diffusion steps）+ 解析测试
- [ ] queue_manager 新增 VOICE_SEPARATE / VOICE_CONVERT 任务类型

### 阶段 2 · UI + i18n
- [ ] 新 Tab「音轨工坊」：来源选择（上传/历史）、分离模式（4 轨/2 轨）、参考干声上传、半音快捷（-12/0/+12 + 自定义）
- [ ] 历史页记录操作菜单加「分离」「翻唱」；创作页输出区加「翻唱此曲」
- [ ] i18n 中英词条全量覆盖

### 阶段 3 · 文档与验证
- [ ] setup.md 安装节、README 功能特性、requirements.md 需求条目
- [ ] 浏览器全链路真实验证（分离导出 / 翻唱成品 / 语言切换）
- [ ] chat_history 追加；**commit 前经你批准**

## 四、验收标准

1. 分离：4 轨文件全部可下载，3 分钟歌 < 1 分钟完成
2. 翻唱：完整歌 + 5-25s 参考干声 → 换嗓 FLAC；全程显存峰值 < 6GB（10GB 卡留余量）
3. 分离可独立使用，翻唱链内部自动组装，两种用法互不干扰
4. 现有功能回归：6 套测试全过、语言切换正常

## 五、风险与对策

| 风险 | 对策 |
| --- | --- |
| venv 依赖冲突（funasr/librosa 版本） | 独立 venv 隔离；失败则固定版本重装 |
| 长曲（>3 分钟）显存峰值 | 按段分块处理（T8 已验证的做法） |
| 10GB 显存与生成任务叠加 | 队列天然串行，翻唱任务排在生成后 |
| Seed-VC GPL-3.0 | 源码不修改不分发，NOTICE 声明；不改其代码，缺功能用外层参数补 |
