# YuE2 Music Studio WebUI — 变更日志（Changelog）

> 所有文本为本项目变更记录；最新变更在上。日期格式 YYYY-MM-DD。

---

## 2026-09-25 — 取消链路修复（取消状态与文案统一）

- 修复：worker 侧取消（如直接 POST /api/cancel）此前显示"任务失败： 任务已取消"，现统一映射为"已取消"（状态 CANCELLED + 文案 + 不弹错误窗）。
  - `voice-tools/worker.py`：新增 `_TaskCancelled` 专用异常（`_check_cancelled` 与 Seed-VC 中止路径抛出）；do_POST 失败响应携带 `cancelled` 标志；`_separate` 取消检查点加密（模型加载后、每轨落盘前——Demucs 推理本身为阻塞 CUDA 调用不可中断，属物理限制）。
  - `src/voice_client.py`：`VoiceResult` 新增 `cancelled` 字段并在 `_run` 解析。
  - `src/voice_ui_handlers.py`：分离/翻唱失败分支识别两路取消来源（主 app cancel_event 或 worker cancelled）→ `TaskCancelledError`。
  - `app.py`：两生成器新增 `except TaskCancelledError` 分支——显示"任务已取消"、恢复按钮、正常收尾不弹错误窗（含 return 收尾修复：防止落入成功路径导致 `result=None` 崩溃）。
- 测试：新增 3 项单测（worker cancelled → TaskCancelledError ×2、`VoiceResult.cancelled` 解析），全量 pytest 64 passed；改动文件 py_compile 通过。
- 真实验证：C1（gradio_client + 直接 POST worker cancel）终态"任务已取消"、1.3s 中止、covers 零残留；C2（浏览器 UI 取消按钮）info 区显示"任务已取消"、按钮恢复、无错误弹窗，服务端 0.7s 标记 cancelled。

## 2026-09-25 — 分离/翻唱管线优化（11 项）+ 换嗓响度修复

**可靠性**

- HTTP 超时按源时长动态放大（max(900s, 时长×20)），长曲翻唱不再 300s 超时断链（`voice_client.py`）。
- worker 日志落盘 `voice-tools/worker.log`（追加写，>10MB 轮转 .old，UTF-8），失败详情可查。
- 运行中取消：UI 取消按钮 → cancel_event → /api/cancel → worker 阶段边界检查 + Seed-VC 子进程 terminate；`/api/cancel` 在串行锁 `_lock` 之外处理，避免被运行中任务阻塞。
- 阶段进度上报：worker 写产物目录 `_progress.json`（separating/converting/denoising/mixing），UI 轮询转发展示（分离中.../换嗓中.../降噪中.../混音中...）。

**功能**

- 翻唱源支持复用分离历史：翻唱源下拉新增 `[分离]` 条目（value=`sep_task:<task_id>`），选中后传人声/伴奏轨给 worker，跳过重复 Demucs 分离。
- 分离/翻唱任务完成后自动刷新本 Tab 历史下拉；切到翻唱 Tab 同步刷新翻唱源下拉（含新分离记录）。
- 音色库/素材库管理：下拉选中即试听（独立试听播放器）+ 删除选中（移系统回收站）+ 重命名（保留短 id/轨道类型命名约定）。
- 自定义伴奏响度自动对齐源伴奏：LUFS 差 clamp ±18dB 并入混音链，换伴奏不再忽大忽小。
- 上传源副本留存：上传源拷贝 `<时间戳>_source<ext>` 入产物文件夹（Gradio 临时文件会被清理）。
- dry_uploads 按文件内容 md5 去重，重复上传直接复用已有文件。
- 换嗓人声响度：静态增益改为 ffmpeg loudnorm 动态响度归一（对齐源人声 LUFS，TP=-1.5dB，pan 前置统一声道口径），修复音量偏小/削顶失真。

**测试与验证**

- 新增 `tests/test_voice_loudness.py`（10 项）：`_rms_db/_peak_db/_lufs` 相对差断言、正弦波峰因子、静音/缺文件返回 None。
- 全量 `pytest tests --ignore=tests/test_i18n.py` → 61 passed；`tests/test_i18n.py` 独立自检 → 17 通过。
- 全部改动文件 `py_compile` 通过；主 app（9898）已重启，worker 懒启动加载新代码。

**修改文件**

- `voice-tools/worker.py`：协作取消 + 阶段进度 + 复用分离 + 伴奏 LUFS 对齐 + loudnorm 响度。
- `src/voice_client.py`：动态超时 + worker 日志落盘 + cancel_event 通知线程 + 进度/复用参数透传。
- `src/voice_ui_handlers.py`：进度轮询线程 + from_upload 源副本 + 复用参数 + md5 去重 + 库管理方法（delete/rename × refs/stems）。
- `app.py`：翻唱源含分离记录、任务完成刷新历史下拉、取消按钮、音色库/素材库管理 UI 与回调。
- `src/i18n.py`：追加阶段进度/取消/库管理等 18 条中英词条。


## 2026-09-24 — 音色工坊（音轨分离 + 参考音色翻唱）

**新增功能：Tab「音色工坊」**

- 音轨分离（Demucs / HTDemucs）：任意音频 → 人声/伴奏（2 轨）或 鼓/贝斯/其他（4 轨），约 1.5–2GB 显存。
- 参考音色翻唱（Demucs + Seed-VC + ffmpeg）：完整歌曲 → 分离人声 → 换成参考音色 → 与伴奏混音，支持半音移调 / 扩散步数 / 伴奏增益，约 2–3GB 显存。
- 源双入口：从生成历史选择源音频，或直接上传。
- 参考音色库：上传参考干声可一键存入 `voice-tools/refs/`，下拉复用。
- 产物聚合到源任务目录：`outputs/<root_task_id>/derived/sep_XXXX` / `cover_XXXX`，并自动写入历史（`record_type`=separation/cover）。

**架构与新增文件**

- `voice-tools/worker.py`：独立 venv HTTP worker，端口默认 8190（占用自动 +1），懒加载 + 空闲 5 分钟卸载。
- `voice-tools/install_voice.bat`：独立安装脚本（Python 3.11 + torch2.6 cu126，与主环境隔离）。
- `src/voice_client.py`：`VoiceClient`，`ensure_running / separate / convert`，全异常包装。
- `src/voice_ui_handlers.py`：音色工坊队列 worker / 历史 / 音色库逻辑（与 app.py 解耦）。
- `config.cfg` 新增 `[voice]` 段：`enabled` / `worker_port` / `seedvc_dir` / `max_input_minutes`。

**修改文件**

- `app.py`：新增第 5 个 Tab「音色工坊」，含布局、事件绑定、`_voice_ref_choices` 等业务函数、`_QUEUE_TYPE_LABELS` 补 `separation/cover`。
- `src/queue_manager.py`：`TaskType` 新增 `SEPARATION` / `COVER`。
- `src/i18n.py`：`EN_TABLE` 追加音色工坊全部中英词条。
- `tests/`：新增 `test_voice_handlers.py`、`test_voice_client.py`。
- `Docs/setup.md`：新增第 6 节「音色工坊（可选功能）安装」。
- `README.md`：中英文同步更新（功能特性 / 界面结构 / 目录结构 / 安装说明）。

**验证**

- `python -m pytest tests/test_voice_handlers.py tests/test_voice_client.py -q` → 11 passed。
- `python -m py_compile src/voice_ui_handlers.py src/i18n.py app.py` 通过。
- 冒烟构建 `PYTHONPATH=.../src python -c "import app; app.build_ui()"` 通过。

> 说明：Seed-VC 为 GPL-3.0，本方案不 vendor 其源码进仓库，主 app 仅通过进程边界（HTTP）调用（见 [voice-tools-plan.md](voice-tools-plan.md)）。

---

## 2026-09-24 — README 双语化

- README.md 增加完整英文版本，顶部「中文 | English」锚点双向切换，GitHub 与本地 Markdown 均适用。
- 同步更新中英文内容：补充「当前队列」窗口说明、参数预设覆盖 23 项参数说明、tests 目录预设测试套件注释。