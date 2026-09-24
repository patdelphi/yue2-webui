# YuE2 Music Studio WebUI — 变更日志（Changelog）

> 所有文本为本项目变更记录；最新变更在上。日期格式 YYYY-MM-DD。

---

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