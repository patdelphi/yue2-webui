# yue2-webui 代码审计修复 TODO

- **来源**：`Docs/code-audit-2026-10-06.md`（已逐条复核订正）
- **状态**：已完成（A1–A10 + B1/B2/B3 + B4–B8 + C1–C6；仅 C7 待 staging 验证暂缓），全量 pytest 236 passed
- **约定**：每项修复尽量先补测试；不顺手重构无关代码；危险操作（删除/回收）单独确认

---

## 一、A 类（Bug，必修）

| # | 位置 | 修复方案 | 风险 | 测试 |
|---|---|---|---|---|
| A1 | `src/backend_gguf.py` generate(216-226) / transcribe(478-481) | 用看门狗线程替代循环内 cancel 检查：`poll()` 中检测 cancel/超时→`kill()`；主循环读到 EOF 后 `process.wait()`；循环后按 cancel/超时返回对应结果（消除阻塞读 + kill 不回收） | 中 | 新增：取消即返回、超时即返回、进程被回收 |
| A2 | `app.py:403-433` + `src/postprocess.py:142` | postprocess 调用包 try/except：失败记日志但继续写历史（音频已落盘可查）；`_embed_metadata` 的 `except Exception: pass` 改为 `logger.warning` | 低 | 断言后处理异常仍写历史 |
| A3 | `app.py:390-394` | 全部变体失败（非取消）时回收 `output_dir`（走 `delete_files_to_recycle` 入回收站） | 低 | 断言失败后目录被回收、无残留 |
| A4 | `src/history.py:622-648` + `app.py:1614-1655` | 新增按类型/分页的 SQL 查询方法（`SELECT` 指定列，不含 lyrics 全文）；`to_dataframe_rows` 支持分页与类型过滤；`prune_missing` 从每次读改为写操作后触发 | 中高 | 断言过滤/分页/SQL 列正确 |
| A5 | `src/voice_client.py:226-228, 242-250` | `_run` 结束时主动 set 一个 done 事件，监视线程 `wait(timeout)` 收到后立即退出，避免 30min 空等堆积 | 低 | 断言任务结束后监视线程快速退出 |
| A6 | `src/queue_manager.py:250-256` | 排队任务取消时同样写入 `_history` 环形缓冲，与运行中取消一致 | 低 | 断言排队取消出现在 recent |
| A7 | `src/voice_ui_handlers.py:524-525` | `ensure_separation` 透传 `cancel_event`（`separate()` 已支持） | 低 | 断言参数被透传 |
| A8 | `app.py:363-369` | worker 层对 `seeds` 长度/类型做防御（不足则补齐/报错），避免 IndexError | 低 | 断言短列表不抛异常 |
| A9 | `src/voice_ui_handlers.py:204-213` | 去重比对前先比文件 size，size 相同才比 md5 | 低 | 断言 size 不同不读内容 |
| A10 | `src/mix_web.py:157-162` | 加 per-key 锁，避免同 key 并发重复解码 | 低 | 断言并发生成一次解码 |

> 原 A11（正则歧义）经实测为误报，已从文档删除，不修。

## 二、B 类（冗余清理）

| # | 位置 | 方案 | 风险 |
|---|---|---|---|
| B4 | `voice_ui_handlers.py` 191/553/625/683/788/242/570/651（417/505 视情况） | 删除与模块顶部重复的局部 import | 低 |
| B5 | `app.py:345, 361` | 删除重复的 `batch_count = int(batch_count)` | 低 |
| B6 | `src/history.py:499-509` | `rename_project` 逐条 `_update` 合并为单事务 | 低 |
| B7 | `src/voice_client.py:379` | 修正自相矛盾注释（函数被测试引用，保留实现） | 低 |
| B8 | 根目录 `=1.47` | 删除 pip 误装产物（已被 gitignore） | 低（需确认删除） |
| B1 | voice_client:253 / voice_ui_handlers:679 / mix_render:437 | 抽取单一 `_probe_duration` 工具函数并复用 | 中 |
| B2 | `voice_ui_handlers.py:361-542` | 三段管线合并 | 高（建议暂缓） |
| B3 | `static/js/app.js` | 三套 ABC 预览合并 | 高（建议暂缓） |

## 三、C 类（可优化）

| # | 方案 | 风险 | 状态 |
|---|---|---|---|
| C2 | 历史查询走 SQL（同 A4，合并处理） | 中高 | ✅ 已完成（随 A4） |
| C3 | Task 增加 `max_runtime` 任务级超时，防队列卡死 | 中 | ✅ 已完成 |
| C4 | `_make_preview` 转码后台化 | 中 | ✅ 已完成 |
| C5 | `on_preset_save` 24 参数改 dict/dataclass；`_generate_worker` 12 元组改结构 | 中 | ✅ 已完成 |
| C1 / C6 | 拆分 app.py（3699→842 行）/ multitrack 单文件（拆出 CSS/JS） | 高 | ✅ 已完成 |
| C7 | `(record_type, source_md5)` 加索引（需 staging 验证） | 中 | ⏸ 暂缓（待 staging） |

---

## 建议执行顺序
A1 → A2 → A3 → A5/A6/A7/A8/A9/A10 → A4(=C2) → B4/B5/B6/B7/B8 → B1 → C3/C4/C5

## 验证
- 每步：`py_compile` + 对应 pytest 用例
- 收尾：`pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` 全绿

---

## 四、剩余优化（用户确认「全部剩余项，含高风险重构」）

- **范围**：B1、B2、B3、C1、C6。C2 已随 A4 完成；C7（加索引）按项目规则需 staging 验证，暂不做。
- **总原则**：每步先补/更新测试 → `py_compile` → 跑相关 pytest → 全量 pytest；前端改动另需 `node --check`；一次性只做一步，全绿再进入下一步。
- **顺序（低风险 → 高风险，最大的放最后）**：B1 → B2 → B3 → C6 → C1。

### 4.1 B1 — 抽取统一时长探测（中）
三处重复实现，统一为一个工具函数复用：

| 位置 | 函数 | 工具 | 语义 |
|---|---|---|---|
| `src/voice_client.py:264-277` | `_probe_duration` | `subprocess.run` ffprobe `-of csv=p=0` | float 秒；失败返回 0.0 |
| `src/voice_ui_handlers.py:749-761` | `_probe_duration` | 同上 | float 秒；失败返回 0.0 |
| `src/mix_render.py:438-452` | `_probe_duration` | `shutil.which("ffprobe")` + `-of default=nw=1:nk=1` | float 秒；失败记日志返回 0.0 |

- 方案：新增 `src/audio_probe.py::probe_duration(path, *, logger=None) -> float`（保留「ffprobe 缺失/失败 → 0.0」与可选的失败告警），三处改为调用它。
- 注意：保持各调用点的失败语义不变（mix_render 保留 warning 日志）。
- 测试：新增 `tests/test_audio_probe.py`（正常解析、ffprobe 缺失、非零退出、输出非数字）。

### 4.2 B2 — `voice_ui_handlers.py` 三段管线合并（高）
- `separate_worker()` L440-490、`cover_worker()` L494-563、`ensure_separation()` L565-609 三者共享：产物目录创建、`_build_stems`、历史写入、时长探测、取消映射。
- 方案：抽出共享的「产物落盘 + 写 history + 组装 stems」helper（参照 dataclass/小函数，避免过度抽象），三条管线改为调用。
- 风险：取消语义（`TaskCancelledError`）与产物目录回收路径不能改变。测试：`tests/test_voice_handlers.py` 全绿为准。

### 4.3 B3 — `static/js/app.js` 三套 ABC 预览合并（高）
| 函数 | 行号 | 场景 | 差异 |
|---|---|---|---|
| `initAbcPreview()` | L246-369 | 生成页 | `#gen-abc-output`，绑定导出 MIDI/PNG |
| `initHistoryAbcPreview()` | L371-437 | 历史页 | `#history-abc`，仅定时刷新 |
| `initTranscribeAbcPreview()` | L439-505 | 转谱页 | 转谱文本框，隐藏占位容器 |

- 方案：抽 `function initAbcPreview(opts)`，参数化 `findFn / paperId / audioId / exportButtons / hidePlaceholder`；三处改为薄封装调用。
- 风险：DOM 选择器与导出按钮绑定差异；改完必须 `node --check` + 真实浏览器回归三个 Tab 的 ABC 渲染与导出。
- 静态资源版本号同步升级：`app.py` 中 `app.js?v=17` → `v=18`（避免 Cloudflare 边缘缓存旧 JS）。

### 4.4 C6 — `static/multitrack/index.html` 拆分（中高）
- 现状：134KB 单文件，内联 `<style>`（约 L24-220）与内联 `<script>`；由 `app.py` 以 `/static/multitrack/` 路由下发。
- 方案：抽出 `static/multitrack/multitrack.css` 与 `static/multitrack/multitrack.js`，html 改为外链并加版本号查询串；确认 app.py 的静态挂载能覆盖新文件（若不覆盖需补路由）。
- 风险：html 内可能用 Python 侧字符串拼接下发（需确认路由是读文件还是内嵌字符串）；模板字符串/转义。
- 测试：`python -c` 读取页面确认外链存在；浏览器打开多轨编辑页确认样式与交互正常。

### 4.5 C1 — 拆分 `app.py`（最高，最后做）
- 现状：约 213KB / 约 3800 行；`build_ui()` 占 L2355-3601（7 个 Tab 的布局与绑定），另有生成/转谱 worker、历史页回调、分离翻唱回调、混音回调、设置页回调。
- 关键全局状态：`_CUR_LANG`、`backend`、`history_mgr`、`voice_client`、`voice_handlers`、`_active_tasks`(+lock)、`_pending_cancel`、`BUILTIN_PRESETS`、`PRESET_PARAM_KEYS`、`LAST_INPUTS_FILE`/`LANG_STATE_FILE`。
- 方案（分阶段，每阶段可独立验证）：
  1. 先抽「无状态纯工具」到 `src/`（`strip_comment_lines`、`_load/_save_last_inputs`、`_update_last_abc`、`BUILTIN_PRESETS`/`PRESET_PARAM_KEYS`、`_localize_task_error`）——app.py 内 re-export 保持既有引用；
  2. 抽「按 Tab 的 UI 构建函数」到 `src/ui_tabs.py`（创作/历史/转谱/分离/翻唱/多轨/设置），`build_ui()` 只做拼装与事件绑定；
  3. 抽「回调组」到 `src/callbacks_*.py`（生成 / 转谱 / 历史 / 音色 / 混音 / 设置），依赖用参数注入（不 import app，避免循环依赖）。
- **红线**：不得改变任何 Gradio `inputs`/`outputs` 的数量与顺序；不得改变前端静态资源版本号以外的行为；`app.py` 内既有名字（被测试直接引用的 `on_preset_save`/`on_preset_load`/`_generate_worker`/`GenerationOutcome` 等）必须保持可访问（re-export）。
- 每阶段结束跑全量 pytest（当前基线 **227 passed**）。

#### 4.5.1 C1 执行地图与进度（实时更新）

**已完成**
- ✅ Phase 1（纯常量/工具）：新增 `src/app_utils.py`（`BUILTIN_PRESETS` / `PRESET_PARAM_KEYS` / `FORMAT_LABELS` / `COMMENT_PREFIXES` / `strip_comment_lines`），app.py 顶部 `from app_utils import ...`。
- ✅ Phase 3 · 设置组（样板）：新增 `src/callbacks_settings.py`（`preset_display_names` / `preset_load` / `preset_save`），app.py 保留同名薄封装 `_preset_display_names` / `on_preset_load` / `on_preset_save`。
- ⚠️ 未搬迁（测试做了源码级断言，必须留在 app.py）：`_localize_task_error`、`_load/_save_last_inputs`、`_update_last_abc`、`_save/_load_lang_state`（它们被 `app.X` 猴子补丁或「`def ...` 必须在 app.py」类断言约束）。

**统一模式（已用设置组验证通过）**
1. 新模块放 `src/callbacks_<域>.py`，函数依赖**全部用参数注入**，不 `import app`；
2. app.py 保留同名薄封装，在**调用时**读取当前全局（如 `WEBUI_ROOT` / `_CUR_LANG`），从而不破坏既有猴子补丁与 Gradio 绑定；
3. 源码级断言的测试改为读取「bundle」（app.py + src/ui_tabs.py + src/callbacks_*.py 拼接），做法同 C6 的 `_editor_page()`；
4. 每组完成后 `py_compile` + 该组相关 pytest + 全量 pytest。

**app.py 当前行号地图（每次改动后会漂移，以最新 grep 为准）**
| 区块 | 行号 | 目标模块 |
|---|---|---|
| 头部导入/全局状态 | L1-178 | 留在 app.py（全局须可被猴子补丁） |
| 生成组（on_generate/_generate_worker/GenerationOutcome/_recycle_output_dir/variant*/on_cancel/on_resynthesize/_resynthesize_worker） | L181-819 | `src/callbacks_generate.py` |
| 转谱组（on_transcribe/_transcribe_worker/on_send_to_generate） | L820-923 | `src/callbacks_transcribe.py` |
| 音色组（_voice_* 全系列 + on_voice_* + VOICE_PLAYER_COUNT + _STEM_TYPE_LABELS） | L924-1615 | `src/callbacks_voice.py` |
| 创作页小回调（seed/style_preset/*_preset/lyrics_template/cot_change/append_to_style） | L1616-1677 | `src/callbacks_generate.py` |
| 历史组（refresh_history*/_get_history_page/on_history_*/_load_history_entry/_hist_player_*/HISTORY_PAGE_SIZE） | L1678-1839 | `src/callbacks_history.py` |
| 设置组剩余（on_check_models/_queue_status_html/_QUEUE_TYPE_LABELS/on_lyrics_change） | L1840-2007 | `src/callbacks_settings.py` |
| `_TITLE_ROW_CSS` / `_LOCALE_SYNC_JS` | L2024-2312 | `src/ui_tabs.py`（或 app.py 留常量） |
| `build_ui()` | L2313-3561 | `src/ui_tabs.py`（7 个 Tab 构建函数 + app.py 拼装） |
| `__main__`（端口 9898、Route 注册、launch） | L3562-末 | 留在 app.py |

**测试对 app.py 的耦合清单（搬迁时必须处理）**
| 测试 | 耦合点 |
|---|---|
| `test_preset.py` | 猴子补丁 `app.WEBUI_ROOT`；`app.on_preset_save/load`；`app.PRESET_PARAM_KEYS`（薄封装已兼容 ✅） |
| `test_last_inputs.py` | 猴子补丁 `app.LAST_INPUTS_FILE`；`app._load/_save_last_inputs`、`app._update_last_abc`、`app.on_restore_last` |
| `test_lang_persist.py` | 猴子补丁 `app.LANG_STATE_FILE`；`app._load/_save_lang_state` |
| `test_generate_defense.py` | 猴子补丁 `app.WEBUI_ROOT/LAST_INPUTS_FILE/backend/history_mgr/refresh_history`；`app._generate_worker`、`app.GenerationOutcome`；源码断言（L136 起） |
| `test_queue.py` | 源码断言：`"def _localize_task_error(" in app.py`、`_localize_task_error(lang, status_info.get("error"))` 出现 3 次、`TIMEOUT_ERROR in app.py` |
| `test_history_recycle.py` | 源码断言：app.py 含 `_recycle_output_dir` 调用 |
| `test_postprocess.py` | 源码断言：app.py 的 `postprocess_audio` 调用被 try/except 包裹 |
| `test_history_filter.py` / `test_history_project.py` | app.py 源码断言（`record_types=("generation",)`、目录前缀命名、项目名输入框等） |
| `test_mix_web.py` | `app_src` 断言路由字符串、`'gr.Tab(_t("多轨编辑")) as tab_mix'`、`elem_id="mix-editor-embed"`、`"sep_mix_btn" not in` |
| `test_theme_light.py` | `APP` 源码断言（三处 ABC 容器边框、File 虚线框等） |

**进展**（app.py 行数：3699 → 3149 → 2836 → 2320 → **842**）
- [x] Phase 1（纯常量/工具）：`src/app_utils.py`
- [x] Phase 3 · 生成组（`src/callbacks_generate.py` 780 行）
- [x] Phase 3 · 转谱组（`src/callbacks_transcribe.py`）
- [x] Phase 3 · 音色组（`src/callbacks_voice.py` 759 行）
- [x] Phase 3 · 历史组（`src/callbacks_history.py`）
- [x] Phase 3 · 设置组剩余（`src/callbacks_settings.py`：`on_check_models` / `_QUEUE_TYPE_LABELS` / `_queue_status_html` / `on_lyrics_change`）
- [x] Phase 2 · `src/ui_tabs.py`（1511 行，7 个 Tab；`build_ui(app_module)` 调用时注入 app 命名空间）
- [x] 收尾：全量 pytest **236 passed** → 前端回归 → changelog/chat_history

**填坑记录（供后续分组复用）**
- 源码级断言统一改读 `tests/_app_bundle.py::app_bundle()`（app.py + src/ui_tabs.py + src/callbacks_*.py 拼接）；
- 依赖注入用 `@dataclass XxxDeps`（app.py 的 `_xxx_deps()` 在调用时读取当前全局，猴子补丁仍生效）；
- 被移动函数内的全局名加 `d.` 前缀后，源码断言需去掉前缀再匹配（如 `_prefer_mp3(` → `prefer_mp3(`）。

### 收尾
- 全量 `pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` 全绿。
- 前端测试前置：重启 9898 服务（当前已停），加载新代码后做浏览器回归（7 个 Tab、ABC 预览、多轨编辑页、预设保存/加载）。
- 更新 `Docs/changelog.md` 与 `chat_history.md`（UTF-8 BOM + CRLF，禁止覆盖）。
- git commit 需用户明确批准。
