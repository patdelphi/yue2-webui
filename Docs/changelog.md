# YuE2 Music Studio WebUI — 变更日志（Changelog）

> 所有文本为本项目变更记录；最新变更在上。日期格式 YYYY-MM-DD。

---

## 2026-10-04 — 同类问题排查与修复：切 Tab 时「下拉已选中、试听却空着」（4 处）

> 起因：用户「请检查其他模块是否有类似问题」（对标刚修好的「翻唱任务历史默认不回填播放器」）。

- **排查方法**：机械枚举 `app.py` 全部事件绑定（`.select(` / `.change(` / `.input(` 共 22 处），再语义核对每个「程序化设值的下拉」是否有对应的依赖内容刷新。
- **结论（同类问题 4 处）**：Gradio 的 `.change` 只在用户交互时触发，切 Tab 用 `gr.update` 程序化设值**不会**触发它；而 `_dd_update` 会把下拉默认选中首项，其依赖的「试听」播放器却只在 `.change` 里更新 ⇒ 4 处假选中：
  - 分离 Tab：`lib_stem_dd` → `lib_stem_preview`、`lib_ref_dd` → `lib_ref_preview`
  - 翻唱 Tab：`cover_ref_dropdown` → `cover_ref_preview`、`cover_acc_dd` → `cover_acc_preview`
  - （此前两库均无写入路径、内容恒为空，问题被掩盖；补上「保存到素材库」后才显性化。）
- **已核对无问题**：`variant_selector`（与 `audio_output` 在同一次生成回调内一起产出，非假选中）；歌曲历史页无默认选中行（需点击行才加载，属设计）；`sep_src_history` / `cover_src_history` / `cover_ref_dry_sep` / `cover_ref_dry_upload` 均无配套试听组件。
- **修复**：新增 `_preview_first_update(choices)`（按下拉首项算试听值 + 显隐）；`tab_sep.select` 输出追加 `lib_stem_preview, lib_ref_preview`；`tab_cover.select` 输出追加 `cover_ref_preview, cover_acc_preview`。
- `tests/test_voice_handlers.py`：新增 `test_tab_switch_refreshes_library_previews`。
- **验证**：`py_compile` OK、全量 `pytest` **197 passed**；服务重启后真实浏览器切「音轨分离」「音色翻唱」Tab 无任何报错（回调返回值 13/15 项与 outputs 元数一致）。
- **非空场景实测（2026-10-04 补）**：真实浏览器验证 4 处试听均随切 Tab 自动出现且有值——
  1. 分离「生成 · 20261003_204007.wav」→「保存到素材库」入库 `验证伴奏入库__accompaniment.wav`；切走再回「音轨分离」Tab，`#lib-stem-preview` 自动可见、时长 2:55、下载链接指向 `_accompaniment_preview.mp3`；
  2. 同一条素材在「音色翻唱」Tab 的「自定义伴奏(可选)」自动选中并弹出 `#cover-acc-preview`（2:55）；
  3. 按 `save_ref` 语义写入音色库条目 `验证参考音色_ab12.wav`（15s 片段；MCP 文件上传无法访问 Y: 盘，故未经上传 UI）；切「音轨分离」Tab 后 `#lib-ref-preview` 自动可见、时长 0:15；
  4. 「音色翻唱」Tab `#cover-ref-preview` 自动可见（0:15），下拉值 `验证参考音色_ab12.wav`。
- **测试条目清理（已完成）**：经用户确认后清理——素材库 `验证伴奏入库__accompaniment.wav`、音色库 `验证参考音色_ab12.wav`（均走 UI「删除选中」入回收站，含各自 `_preview.mp3`）；临时片段 `voice-tools/tmp/_verify_ref_15s.wav` 直接删除；两次测试分离 `outputs/separations_20261004_105538`、`outputs/separations_20261004_113704` 经 `history_mgr.delete_project` 整目录入回收站并移除历史记录。复核：两库目录为空、无全仓残留文件、无残留 `separations_20261004_*` 记录。
- **未执行**：未 git commit / push。

---

## 2026-10-04 — 修复翻唱任务历史默认不回填播放器

> 起因：用户「翻唱任务历史，默认也没有 load 最后一首歌」。

- **根因**：`tab_cover.select` 切 Tab 刷新时只更新了「翻唱任务历史」下拉并把 `cover_selected_task` 置为首条，未回填回放播放器组；对比 `tab_sep.select` 有 `*_voice_task_first_players("separation")` → `*sep_hist_audios`，故分离页可默认加载、翻唱页停在「下拉显示任务名、播放器却空着」的假选中状态。
- `app.py`：`tab_cover.select` 输出追加 `*_voice_task_first_players("cover")` 与 `*cover_hist_audios`，上方注释同步说明。
- `tests/test_voice_handlers.py`：`test_voice_stem_items_prefers_preview_keeps_full_for_mix` 增补两条源码断言（`*_voice_task_first_players("cover")`、`cover_selected_task, *cover_hist_audios]`）。
- **验证**：`py_compile` OK、全量 `pytest` **196 passed**；真实浏览器切到「音色翻唱」Tab 后 4 个播放器自动填充（翻唱成品 / 换嗓干声 / 伴奏 / 分离人声 = 最新任务 `cover_20260927_105003`，时长 2:52，下载链接指向该任务产物）。
- **未执行**：未 git commit / push。

---

## 2026-10-04 — 补「保存到素材库」入口（素材库此前无写入路径）

> 起因：全模块同类问题排查（UI 存在但永远不可达/永远为空）发现「素材库(乐器轨)」下拉与本页/翻唱页「自定义伴奏」下拉在应用内**永远为空**——`voice_handlers.save_stem` 全仓库无 UI 调用方。用户指示「修复」。

- `app.py`：
  - 新增 `_stem_pick_choices(stems)`：由本次分离产物生成「待入库轨道」选项（value=音频路径，label=经 tr 的轨道类型名），与素材库下拉一致排除 `vocals`（人声归音色库）。
  - 新增回调 `on_voice_save_stem_to_lib(pick_path, name, stems_state)`：从 `sep_stems_state` 回查轨道类型 → 调 `voice_handlers.save_stem` 写入 `voice-tools/stems/<名>__<类型>.ext`；名称留空回退轨道类型名；返回（素材库下拉刷新, 名称清空）。
  - 新增 `_sep_running_outputs(text)` 包装（`_voice_running_outputs` 为分离/翻唱共享，不能直接扩展）。
  - `on_voice_separate` 5 处 yield 的 outputs 由 9 项扩为 11 项（末尾追加 `lib_stem_pick, sep_stems_state`）；成功路径回填「待入库轨道」并缓存本次 stems。
  - 分离页「库管理」卡片在 `lib_stem_dd` 前插入「待入库轨道 / 素材名称 / 保存到素材库」行 + `sep_stems_state = gr.State([])`；`sep_btn.click` outputs 同步扩展；新增 `lib_stem_save_btn.click` 绑定。
- `src/i18n.py`：新增 8 条词条（待入库轨道 / 素材名称 / 留空则用轨道名 / 保存到素材库 / 请先选择要入库的轨道 / 无法识别该轨的类型 / 保存失败 / 已存入素材库）。
- `tests/test_voice_handlers.py`：新增 `test_separation_tracks_can_be_saved_to_stem_library`（源码断言入库组件、回调、`save_stem` 调用、完成回填、排除 vocals）。
- **验证**：`py_compile` OK、全量 `pytest` **196 passed**；真实浏览器（`http://127.0.0.1:9898`）跑通端到端：分离「夜色」源曲 → 完成后「待入库轨道」自动选中「伴奏」→ 保存得 `测试伴奏入库__accompaniment.wav`（约 64MB 落盘 `voice-tools/stems/`）→ 素材库下拉与翻唱页「自定义伴奏」下拉均出现该条目。
- **清理**：验证用测试条目 `测试伴奏入库__accompaniment.wav` 已按用户确认经回收站删除（`voice-tools/stems/` 现为空）；**未执行**：未 git commit / push。

---

## 2026-10-04 — 移除歌曲历史页失联的「轨道回放」死 UI（P0 收尾）

> 起因：上一轮真实测试发现 P0「历史页多轨回放」在历史页不可达。用户指示「有问题吗？你自己修复」。

- **根因**：历史页表格经 `to_dataframe_rows(record_types=("generation",))` 只列 generation 记录，而 `entry.stems` 只存在于 separation/cover 记录 → `has_stem` 恒为 False → `history_stem_dd`（「轨道回放(分离/翻唱)」下拉）与 `history_stem_audio` 永远隐藏。该 UI 属死代码。
- **修法**：按既定设计（歌曲历史页仅显示生成记录；分离/翻唱逐轨回放已各自在专用页）**移除历史页这段失联 UI**，而非把分离/翻唱记录塞回历史表。
- `app.py`：
  - 删除组件 `history_stem_dd` / `history_stem_audio` 及其 `_reg`；改写区块 2 注释说明为何不再放轨道回放。
  - `history_df.select` 与「删除选中 / 清空历史 / 改项目名 / 删除项目」四处 `.click(outputs=...)` 去掉两个 stem 输出；删除 `history_stem_dd.change(...)` 绑定。
  - `_hist_player_keep/_clear` 由双值改为单值（返回 `gr.update()` / `gr.update(value=None)`）。
  - `on_history_rename_project` 去掉 stem 分支；`_load_history_entry` 去掉 stem 输出（返回值由 11 元降为 9 元），docstring 同步。
- `static/js/app.js`：`PLAYERS` 选择器与 `initPlayerZoom` 的 `PLAYER_IDS` 移除 `history-stem-audio`；两处相关注释同步改写。
- `tests/test_theme_light.py` / `tests/test_voice_handlers.py`：去掉对 `#history-stem-audio` 的断言，新增「历史页已无 `history_stem` / `history-stem-audio`」负向断言，`_voice_stem_items` 断言改指分离/翻唱页用法。
- 因 `app.js` 内容变更，`app.py` 内 `app.js?v=16` 升为 `v=17`（绕开 Cloudflare 边缘缓存）。
- **验证**：`py_compile app.py` OK、
ode --check static/js/app.js` OK、全量 `pytest` **195 passed**。
- **未执行**：未 git commit / push。

---

## 2026-10-04 — 远端回放修复（P0–P3 + 历史表行点击）整体真实测试

> 目的：用户要求「整体做真实测试」。对上述改动做端到端真实验证（真实浏览器 + 真实音频 + 真实网络），而非源码断言。

- **rt1 历史页**：`history-audio` 主播放器实际取 `夜色_20260927_104543.mp3`（P1 生效）；行点击高亮独占且 1 次点击 = 1 次 `queue/join`；空播放器无 loading 浮层。
- **rt4 多轨页（P3 进度提示）**：iframe 内真实调用 `fetchWithProgress` 下载 101.3 MB WAV（106,192,888 字节）→ 49.4s、3639 个进度点 0%→100% 单调；真实 UI 路径 `ensureBuffers()` 令 `#tp-msg` 逐帧显示「正在下载音频 1/1 · <轨> N%」→100%→「正在解码音频」→清空，返回 `true`（测试后已复原注入的假轨）。
- **rt2/rt3 库试听（`_preview_for_library`）**：临时造 2 个 3 秒小件（`voice-tools/refs/测试参考_ab12.wav`、`voice-tools/stems/测试伴奏__accompaniment.wav`）做 UI 端到端——切 Tab 刷新下拉后选中即触发服务端生成 `_preview.mp3`，四个试听播放器（`lib-stem-preview`/`lib-ref-preview`/`cover-ref-preview`/`cover-acc-preview`）均渲染波形与 0:03；**网络请求证据：仅拉取 `*_preview.mp3`，测试件 `.wav` 零请求**。测完经 `delete_ref`/`delete_stem` 将 4 个文件移入系统回收站（删除链路同时得到验证），两库目录复原为空。
- **预览件真实校验**：ffprobe 比对最大分离轨 WAV（301.0s / 2822kbps / 101.27MB）与其 `_preview.mp3`（301.0s / 192kbps / 6.89MB），时长完全一致；`_make_preview` 复用命中 0ms；`outputs/` 下 24 份 WAV 均已带预览件。
- **gen-audio（P1）已有产物验证**（经用户确认不跑 GPU 生成）：5/5 生成记录的 `audio_path` 均为 `.wav` 且同目录存在同名 `.mp3`，`_prefer_mp3` 全部返回 mp3（含批量变体 `var1/2/3`，覆盖批量 / 列表 / 变体切换三处接线）。

**新发现（行为澄清，非缺陷回归）**

- **P0「历史页多轨回放」在历史页不可达**：`_load_history_entry` 以 `to_dataframe_rows(record_types=("generation",))` 取记录，`entry` 恒为生成记录、`entry.stems` 恒空 → 历史页「轨道回放(分离/翻唱)」下拉永远隐藏。分离/翻唱的轨道回放真正可用入口是「音轨分离」「音色翻唱」两页各自的任务历史下拉（本轮网络证据显示这两处回放均取 `*_preview.mp3`）。

**未执行**

- 未真实跑歌曲生成（经用户选择跳过，改用已有产物验证）。
- 未 git commit / push。

---

## 2026-10-04 — 修复历史表行点击选择器失效（tbody tr → role=row）+ 移除冗余隐藏 trigger

> 目的：用户要求「修复 app.js 中 tbody tr 选择器不生效的问题」。

- **根因**：Gradio 6 的 Dataframe 用虚拟滚动渲染——表头是 `<thead><tr role="row">`，**数据行是 `<div class="virtual-row" role="row">`（不是 `tbody tr`）**，
  `tbody` 内仅有一条 0 高的量宽占位 `tr`。故 `e.target.closest('tbody tr')` 对数据行恒为 
ull`（若点到占位 tr 又会得到恒定 index 0）
  → 行高亮、`cursor:pointer`、hover 底色全部失效。
- **实测确认**：Gradio 原生 `history_df.select` 一直正常工作（可信点击正确加载对应行 MP3），而隐藏 `#history-row-trigger` 的值恒为 `-1`，说明 JS 这条触发链从未生效过。
- `static/js/app.js`（`initHistoryTableClick`）：
  - 行匹配改按 `[role="row"]`，并用「是否含列头单元格」(`[role="columnheader"]`) 排除表头行；
  - CSS 同步改为 `#history-table [role="row"]:not(:has([role="columnheader"]))`；
  - **移除 `setTriggerValue()` 与隐藏 trigger 依赖**——选行统一交给 Gradio 原生 `history_df.select`，避免两条路径同时触发导致重复加载后端；
  - 增加 `dataset.y2RowClickInit` 标记（该函数会被 `initPlayerZoom` 与 2s 定时器各调一次），避免重复挂监听 / 重复插样式。
- `app.py`：移除已失效的 `history_row_trigger = gr.Number(elem_id="history-row-trigger")`、`history_row_trigger.change(...)` 绑定与 `on_history_row_click()` 函数；
  脚本版本 `app.js?v=15` → `v=16`（app.js 内容变更必须换版本号，否则命中 Cloudflare 旧缓存）。
- `tests/test_history_filter.py`：新增 `test_history_row_click_uses_role_row_not_tbody_tr`
  （断言 JS/CSS 走 role=row、无 `setTriggerValue`、无 `history-row-trigger`、app.py 无残留，且 `history_df.select` 仍在）。

**验证**

- `py_compile app.py`、
ode --check static/js/app.js` 通过。
- `pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` → **195 passed**（基线 194 + 新增 1）。
- **远端实测**（`https://yue2.patdelphi.xyz/`，服务重启 + 硬刷新后，`app.js?v=16`）：
  - 数据行 `getComputedStyle(...).cursor === "pointer"`（修复前为默认值）；
  - 点击第 2 行 → 该行加 `rgba(59,130,246,0.2)` 高亮、其余行无、表头行不受影响；
  - 可信点击正确加载对应行 MP3（`夜色_20260927_104543.mp3`）；
  - 计得 1 次点击 = 1 次 `queue/join`，无重复触发。

**更正**

- 上一条 2026-10-04 条目「发现的无关问题（仅记录，未修）」所述的历史页行点击问题，已由本次修复。

**未执行**

- 未 git commit / push（改动待批准）。

---

## 2026-10-04 — 远端回放「大文件」同类问题整体排查与修复（P0–P3）

> 目的：用户在上一轮修复「音轨分离/翻唱」大文件回放慢之后追问「其他模块是不是有类似问题？你整体检查、修改、测试」。
> 经确认的两项决策：主播放器改用同目录同名 MP3；多轨编辑器暂不改格式，只强化进度提示。

- **同类问题审计**（根因一致：Gradio 前端必须把整个文件下完才给 `<audio>` 挂 `src`，经 Cloudflare 隧道下行约 1.2 MB/s）：
  - 歌曲创作 `gen-audio`：原播 31–48MB WAV（同目录已有约 3.5MB 同名 MP3）→ P1 改用同名 MP3；
  - 歌曲历史 `history-audio`：同上 → P1 改用同名 MP3；
  - 歌曲历史「轨道回放(分离/翻唱)」下拉：原指向 WAV 原件 → P0 改走 preview 小件；
  - 音轨分离「库管理」试听 `lib-stem-preview` / `lib-ref-preview`：原指向库内大 WAV → P2 按需生成/复用 preview；
  - 音色翻唱参考/伴奏试听 `cover-ref-preview` / `cover-acc-preview`：同上 → P2 按需生成/复用 preview；
  - 多轨编辑（4 轨约 250MB，需全部解码）→ P3 暂不改格式，仅强化下载/解码进度提示。
- **P0** `app.py`：`_load_history_entry` 的「轨道回放(分离/翻唱)」下拉改用 `_voice_stem_items(getattr(entry, "stems", None))`，
  preview 优先 + 命名约定推导 + 缺失回退原件。
- **P1** `app.py`：新增 `_prefer_mp3(path)`（同名 `.mp3` 存在即用，否则回退原路径；仅作用于回放/下载槽位，
  合成/分离/混音等后端链路仍用原件），覆盖生成回传、批量变体、重合成、历史页试听四处。
- **P2** `app.py`：新增 `_preview_for_library(path)`（调用 `_make_preview`，失败回退原件），4 个库试听 lambda 统一改用它；
  `src/voice_ui_handlers.py` 配套新增 `_preview_sibling` / `_is_preview_file` / `_rename_preview_sibling`，
  `list_refs` / `list_stems` 过滤预览小件（避免污染下拉），`delete_*` 连带回收预览件，`rename_*` 同步搬移。
- **P3** `static/multitrack/index.html`：新增 `fetchWithProgress()`（读 `Content-Length` + `getReader()` 流式下载并回传百分比），
  `ensureBuffers()` 显示「正在下载音频 x/y · 轨名 P%」→「正在解码音频 x/y · 轨名」；
  `src/i18n.py` 与 `src/mix_web.py` 的 `PAGE_TEXT_KEYS` 补「正在下载音频」。
- **修复本轮实测发现的 bug**：历史页未选中任何记录时 `history-audio` 会误显示「正在加载音频… Ns」。
  根因：上一轮的 `syncLoading` 判据只要求「可见 + 无 shadow src」，而 Gradio 空播放器（无值时只渲染空白占位 `DIV.empty`，不含 `#waveform`）也满足；
  新增 `hasPlayerChrome(root)`（要求存在 `#waveform`）作为前置判据修复；脚本版本 `app.js?v=14` → `v=15`。

**验证**

- `py_compile app.py src/voice_ui_handlers.py src/i18n.py src/mix_web.py`、
ode --check static/js/app.js`、内联脚本语法检查均通过。
- `pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` → **194 passed**（基线 190 + 新增 4 个用例）。
- **远端实测**（`https://yue2.patdelphi.xyz/`，服务重启 + 硬刷新后）：
  - `app.js?v=15` 已下发；
  - 切「歌曲历史」不选记录时 `#history-audio .yz-loading` 为 
ull`（空播放器不再误显示）；
  - 可信点击第 1 行后 shadow `<audio>` 的 `src` 为 `...\20261003_204007.mp3` —— 确认主播放器下发的是 MP3 而非 WAV。

**未执行**

- 多轨编辑器仍解码全量 WAV（按决策仅加进度提示，未改格式）；P3 只做了源码级断言与单测，未做多轨真机大文件回放实测。
- 未 git commit / push（改动待批准）。

**发现的无关问题（仅记录，未修）**

- `static/js/app.js` 的 `initHistoryTableClick` 用 `e.target.closest('tbody tr')` 判定行，但 Gradio 6 把数据行渲染为 `[role="row"]` 的 div，
  `tbody tr` 为 null → 该原生行点击高亮/触发逻辑不生效（实际靠 Gradio 原生 select 事件工作）。与本轮「大文件」主题无关，按「不做顺手重构」原则仅记录。

---

## 2026-10-04 — 远端音轨分离历史回放加速（预览小件）+ 加载提示 + 切 Tab 回填播放器

> 目的：用户反馈「音轨分离历史，远端访问，选择歌曲显示不了音轨内容，是不是无法 load 音轨？」并提出三项要求：
> 「1. 分离后有没有生成小音频？ 2. 回放只需要 load 小音频，合成才需要大音频 3. 增加 loading 显示」。

- **根因**：分离/翻唱产物是 32bit float WAV（48000×2×4 = 384,000 B/s），单轨 63–101MB；
  Gradio 前端必须把**整个文件下完**才给 shadow `<audio>` 挂 `src`（此前一直是 0:00 空壳）。
  经 Cloudflare 隧道下行约 1.2 MB/s → 单轨约 1 分钟、多轨数分钟，看起来就像「加载不出来」。
- `src/voice_ui_handlers.py`：新增 `_PREVIEW_SUFFIX = "_preview"`、`_PREVIEW_BITRATE = "192k"` 与
  `_make_preview()`（ffmpeg `libmp3lame -b:a 192k`；原件不存在 / ffmpeg 缺失 / 转码失败一律静默返回空串，
  已有且不旧于原件的预览件直接复用）；`_build_stems()` 为每条 stem 增加 `preview` 键（`path` 仍为原件）。
- `app.py`：
  - `_voice_stem_items()` 回放优先取 `preview`；**存量记录无 `preview` 键时**按命名约定从原件名推导
    `<原名>_preview.mp3`（存在即用），避免为历史数据做数据库迁移；预览件缺失则退回原件。
    合成链路（翻唱换伴奏、`sep_task:` 复用分离结果）始终读 `path`（原件），不受影响。
  - 新增 `_voice_task_first_players(record_type)`；`tab_sep.select` 追加该回填并令 `outputs` 增加 `*sep_hist_audios`
    ——修复「进入分离页时下拉已显示任务名、播放器组却是空的」假选中状态。
  - 脚本引用 `app.js?v=13` → `v=14`（`app.js` 内容变更必须换版本号，否则命中旧缓存）。
- `src/history.py`：`rename_project` 同步 `_remap(s["preview"])`；`_files_for` 删除范围纳入 `preview`（改名/删除后预览件不失效、不残留）。
- `static/js/app.js`：新增 `.yz-loading` 样式与 `watchPlayer` 内的 `syncLoading()`（`poll()` 每 1s 调用）——
  等待期在播放器根节点挂「正在加载音频… Ns」浮层，`getShadowAudio(root)` 拿到 `src` 后自动移除。
- `aipython/backfill_voice_previews.py`：新增存量补生成脚本（幂等、支持 `--dry-run`，复用产线 `_make_preview`）。

**验证**

- `py_compile app.py src/voice_ui_handlers.py src/history.py`、
ode --check static/js/app.js` 均通过。
- `pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` → **190 passed**
  （基线 183 + 新增 7 个用例：`_make_preview` 三分支、`_build_stems` 带 preview、`_voice_stem_items` 优先 preview 且合成读 path、
  history 改删处理 preview、app.js loading 浮层）。
- **存量补生成**：7 个目录 19 轨，991MB → 99.5MB（约 1/10），耗时 42.5s；复跑 dry-run 待处理 0（幂等）。
- **远端实测**（`https://yue2.patdelphi.xyz/`，服务重启后）：
  - 切「音轨分离」自动回填 4 轨（夜色 4 轨分离记录），播放器不再为空；
  - 网络请求为 `夜色_20260930_120709_{drums,bass,other,vocals}_preview.mp3`（HTTP 200/206 分段），
    shadow `<audio>` 时长 171.6s —— 确认回放走小件，不再拉 63MB 原 WAV；
  - 浮层实测：摘掉 shadow audio 的 `src` 后 2.5s 内出现「正在加载音频… 2s」（父节点 `sep-history-audio-0`），恢复 `src` 后自动移除。
- **缓存陷阱（重要）**：Cloudflare 边缘缓存了旧 `/static/js/app.js?v=13`（内容不含新代码，且 Gradio 的 `v` 不随内容变化），
  同 URL 直取会拿到旧文件；升 `v=14` 后恢复正确。远端访问时如遇「页面功能像旧版」，可硬刷新或换版本号。

**副作用**

- 回放播放器改用预览 MP3 后，`gr.Audio` 的下载按钮下载的是 **MP3 小件**（一个组件只有一个 value）；原始 WAV 仍在磁盘，合成链路使用原件。

**未执行**

- 未 git commit / push（改动待批准）。

---

## 2026-09-30 — 亮色主题修复（File 虚线拖拽区 / 多轨编辑被强制亮色时误判为暗色）

> 目的：用户反馈「gr.File 上传区有 3px 虚线框」「多轨编辑仍然是暗色」。

- `app.py`（`_TITLE_ROW_CSS`）新增规则：
  `div.styler > :not(.absolute)[style*="dashed"] { border-width: 1px !important; border-color: var(--border-color-primary) !important; }`。
  根因是 Gradio 6 自带的重置规则 `div.styler > :not(.absolute) { border-width: medium; border-style: none; border-color: currentcolor }`：
  宽度取 `medium`(3px)、颜色取 `currentColor`，本意是配合 `border-style: none` 把边框藏掉；
  但 File 组件会内联 `border-style: dashed`，于是露出一圈 3px 近黑虚线框。
  实测两个 File 拖拽区由 `2.857px solid rgb(39,39,42)` → `0.571px(1px) solid rgb(228,228,231)`，与 ABC 预览容器一致。
- `static/multitrack/index.html`（`syncParentTheme()`）回退分支不再读父页 `body` 的 `background-color`。
  根因：Windows 处于暗色系统时，用户用 `?__theme=light`（或页脚主题开关）强制亮色，Gradio 会给父页 `body` 留一份暗色背景
  （`@media (prefers-color-scheme: dark)` 直写，实测 `rgb(15,15,17)`），同时 `body` 上**没有** `dark` 类 →
  首选判定落空 → 亮度回退读到暗色 → 子页被错锁成暗色。
  改为读真正跟随主题的 Gradio 变量 `--body-background-fill`（亮 `white` / 暗 `#0f0f11`）；
  回退顺序：`--body-background-fill` → `--background-fill-primary` → 父页 body 背景 → html 背景。
  变量值写法不定（`white` / `#rrggbb` / `rgb()` / `color()`），统一用 canvas `fillStyle` 归一化后再算亮度，
  并用哨兵色 `#010203` 识别解析失败，避免把非法色值当作纯黑。
- `tests/test_theme_light.py`：新增 `test_file_dropzone_border_is_normalized`；`test_multitrack_theme_detection_is_robust` 改为断言主题变量路径。

**验证**

- 
ode --check`（提取 index.html 内联脚本）通过；`py_compile app.py` 通过。
- `pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` → **183 passed**（基线 182 + 新增 1 个用例）。
- 浏览器实测（本机 Windows 为暗色系统，Chrome DevTools 不施加颜色模拟）：
  - `?__theme=light`：父页 `body` 无 `dark` 类、`--body-background-fill=white`，但 `body` 背景仍是 `rgb(15,15,17)`；修复后 iframe `data-theme=light`、`--bg=#ffffff`（修复前会被判成 dark）。
  - `?__theme=dark`：父页 `body.dark`、`--body-background-fill=#0f0f11` → iframe `data-theme=dark`、`--bg=#16181d`（无回归）。
  - 全页 3 个 dashed 元素（ABC 预览容器 + 两个 File 拖拽区）宽度均 1px、颜色均 `rgb(228,228,231)`。
- 服务已重启（HTML 下发 `app.js?v=13`）；多轨编辑页响应头为 `Cache-Control: no-store`。

---

## 2026-09-30 — 亮色主题适配修复（播放器/ABC 预览/多轨编辑页）

> 目的：用户反馈「亮色主题下个别组件不适配、多轨编辑全部不适配」。本轮全量排查亮色渲染，逐项修复硬编码深色。

- `static/js/app.js`：
  - **播放器整块发黑（主因）**：`initAudioTimeDisplay()` 原写法 `SEL + ' .timestamps {...}'` 中，`SEL` 是 6 个播放器 `id` 的逗号列表，拼接后**只有最后一项**带 `.timestamps` 后代限定，前 5 项（`#gen-audio`、`#history-audio`、`[id^="sep-audio-"]`、`[id^="cover-audio-"]`、`[id^="sep-history-audio-"]`）直接命中播放器根节点，把整块播放器染成 `rgba(0,0,0,.7)` + 白字。已改为 `var SEL = ':is(' + PLAYERS + ')';` 整体包裹后再接后代选择器。
  - **播放器「仍然是暗色」（第二轮反馈的根因）**：Gradio 音频块根节点带内联 `border-style: solid` 却**没有 `border-width`**，宽度回落到 CSS 初始值 `medium`(3px)、颜色为 `currentColor` —— 亮色主题下就是一圈 3px 近黑边框（`rgb(39,39,42)`），整块播放器看着像「暗色」。已加 `SEL + ' { border: 1px solid var(--border-color-primary, transparent) !important; }'` 覆盖为 Gradio 常规 1px 主题边框。
  - **`PLAYERS` 列表补齐**：原只列 6 项固定 id，漏掉 `#history-stem-audio` / `#lib-stem-preview` / `#lib-ref-preview` / `#cover-ref-preview` / `#cover-acc-preview`，这些播放器（截图中的第二个「音频」块等）仍保留 3px 近黑内联边框。已扩为 11 项，与 `initPlayerZoom()` 的 `PLAYER_IDS` 对齐。实测全页 `borderTopWidth > 1px` 的元素数 = 0。
  - 时间码条 `.timestamps`：底色/文字/描边改用 `--background-fill-secondary` / `--body-text-color` / `--border-color-primary`；当前时间读数亮色用深绿 `#15803d`、暗色 `#4ade80`（`body.dark` 覆盖）。
  - 缩放工具条 `.yz-toolbar button` 与波形底 `.yz-wave`：`rgba(0,0,0,.5)` / `rgba(0,0,0,.25)` 改走 `--button-secondary-background-fill` / `--button-secondary-text-color` / `--border-color-primary` / `--background-fill-secondary`。
  - 歌词段落卡底色 `rgba(255,255,255,0.05)`（亮色下白底白卡、看不见）→ `var(--background-fill-secondary, …)`；逐句高亮不再写死 `#fff` → `var(--body-text-color, #fff)`。
  - PlayerZoom 波形配色随主题切换：`cursorColor` 亮色 `#1f2328`（原 `#fff` 在白底上完全不可见）、暗色 `#ffffff`；`progressColor` 亮色 `#15803d`、暗色 `#4ade80`；`waveColor` 亮色 `#a1a1aa`、暗色 `#7f7f7f`。
- `app.py`：三处 ABC 预览容器（生成/历史/转谱）虚线边框 `rgba(255,255,255,0.15)`（亮色下不可见）→ `var(--border-color-primary)`；`app.js?v=12` → `v=13`（脚本内容变更需换版本号，否则浏览器命中旧缓存）。
- `static/multitrack/index.html`（多轨编辑页加固）：
  - `syncParentTheme()` 重写——**优先**读父页面 `body.dark` / `html.dark` 类（Gradio 把主题类挂在 `body` 上，是最稳的暗色标识）；回退到背景亮度判定时，兼容 Chrome 可能返回的 `color(srgb 0~1)` 分量格式（按 ×255 折算）与 `transparent` 背景（向上回落 `html`，再取不到就保持现状，不再盲目判暗）。
  - 四处主题块补 `color-scheme: light/dark`，让原生复选框、滚动条、数字输入框跟随主题。
- `tests/test_theme_light.py`（新增，5 个用例）：断言 `:is()` 包裹写法、主题变量替换、ABC 边框、PlayerZoom 主题配色、多轨主题判定健壮性。

**验证**

- 
ode --check "static/js/app.js"` 通过；`pytest tests/test_theme_light.py -q` → 5 passed。
- `pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` → **182 passed**（基线 177 + 新增 5 个用例）。
- 浏览器实测（Chrome DevTools MCP，`emulate colorScheme` 切明暗；Gradio 6.28 只跟随 `prefers-color-scheme`，`localStorage.theme` / `?__theme=` 均无效）：亮色下 `#gen-audio` 背景由 `rgba(0,0,0,0.7)` 恢复为 `rgb(255,255,255)`；多轨编辑 iframe 由父页 `body.dark` 驱动，亮/暗切换后 `data-theme` 与 `--bg` 均正确跟随（`#ffffff` ↔ `#16181d`）。
- 播放器外框二次复核（硬刷新后切「歌曲历史」Tab）：`#history-audio` 边框由 `2.857px solid rgb(39,39,42)` → `0.571px solid rgb(228,228,231)`；全页扫描 `borderTopWidth > 1px` 元素数 = 0；`.timestamps` 底色 `rgb(250,250,250)`、文字 `rgb(39,39,42)`。
- 说明：多轨编辑页在本机亮色下未能复现「全部不适配」（iframe 主题判定与全部组件渲染均正常），已按最可能的根因（父页背景色值格式敏感导致误判暗色）做加固；请用户 `Ctrl+F5` 后复测确认。

---

## 2026-09-30 — 多轨编辑器：iframe 随内容长高 + 音质/压缩一键归零 + 「FX 工具」工具栏

> 目的：用户两点诉求——(1) 编辑器锁在固定 760px 的 iframe 里，顶部标题与菜单常驻、不能随页面滚走，浪费顶部空间；(2) 音质、压缩要「一键归零」，并把该行右侧没用上的空白放点别的功能。

- `static/multitrack/index.html`：
  - **内嵌自适应高度**：新增 `fitParentHeight()` / `watchEmbedHeight()`。同源下用 `window.frameElement` 反向改写自身 iframe 高度，父页 app.py 零改动。高度源取 `document.body.getBoundingClientRect().height`（**不用** `documentElement.scrollHeight`，后者会被视口撑大导致「只能长不能缩」）。`ResizeObserver(document.body)` + `resize` 事件触发，实测内容长高/缩短后 iframe 双向跟随（1337 ↔ 1937px）。
    - 踩坑 1：不能拿 `requestAnimationFrame` 的**句柄**当「已排队」哨兵——内嵌 Tab 未渲染时 rAF 不触发，句柄非 0 会让后续更新**永久卡死**（实测加载轨道后高度不再跟随、停在 262px）。改为布尔标记 + `setTimeout(run, 150)` 兜底。
    - 踩坑 2：`ResizeObserver` 实例必须持有引用，实测不持有引用时回调不再触发，改为模块级 `_fitObserver`。
    - 踩坑 3：父 Tab 隐藏时 body 未参与布局、矩形为 0，`fitParentHeight` 增加 `if (!h) return` 防守，避免把 iframe 误设成 0 高。
  - **一键归零**：`mkFxCtl` 返回值新增 `resetV`（复位到中性值并广播）；`mkMod(key, title, resets)` 在标题条右侧渲染 `.fxmod-r` 归零按钮。音质登记 6 项（声像/低/中/高/高通/低通），压缩登记 2 项（阈值/比率），回声登记 3 项（延迟/反馈/混合）。实测：套用「人声」预设后点音质归零 → 三频/高通/声像回中性、压缩值保持不动；点压缩归零 → `-20dB / 3.0:1` 回 `0.0dB / 1.0:1`；点回声归零 → 延迟/反馈/混合一并回 `0`（混合 0 即关闭回声）。
  - **「FX 工具」模块**（新增 `--mod-tool` 主题色）：旁通开关（`.ftbtn.on` + `.fxpanel.fx-off` 只压暗处理模块与电平表，工具模块保持全亮）/ FX 预设（内置 中性·人声·伴奏 + localStorage 自定义 `yue2.mix.fxPresets` + 「保存为预设…」）/ 复制到（目标列表在 `mousedown` / `focus` 时惰性重建，含「全部其他轨」）。实测：旁通按钮 正常 ↔ 旁通 切换并给面板加 `fx-off`；预设「人声」套用 -2/+1/+2 / 80Hz / -20 / 3.0:1；「复制到 → 全部其他轨」把伴奏轨写成 +1/-1/+1 / -12 / 2.0:1；自定义预设写入 localStorage 并立即出现在下拉里。
  - **布局取舍**：`--fxmod-w: 362px` 让音质与压缩严格同宽（压缩内容居中）；此时该行只剩 ~66px，并排已塞不下工具卡，故 FX 工具做成**独占整行、内部三组横向均布**的工具栏（`flex: 1 1 100%`）——面板高 165 → 232px；若改回竖排三行堆叠则要 283px。741px 视口下三组仍在同一行，无横向溢出。
  - 工程契约 version 1 新增每轨 `fx_on`：`buildPayload` 写出、`applyTrackState` 回灌（缺省 true）。旁通语义为「不改用户旋钮数值，只把节点链推中性」，与后端「`fx_on=false` 不生成滤镜」等价，试听与导出一致。实测拦截 `/api/mix/render` 请求体：正常 `[true,true]`，第一轨旁通后 `[false,true]`。
- `src/mix_render.py`：`MixTrack.fx_on`（默认 True）+ `_parse_track` 归一化 + `_fx_filters()` 首行 `if not track.fx_on: return []`。
- `src/mix_web.py` / `src/i18n.py`：新增 15 个文案键与英文译文（归零 / 全部复位到中性值 / FX 工具 / 开关 / 正常 / 旁通 / 旁通本轨全部音质与音效 / 预设 / 应用 / 保存为预设… / 预设名称 / 中性 / 复制到 / 复制 / 全部其他轨）。
- `tests/test_mix_render.py`：新增 `test_fx_bypass_skips_all_filters`。
- `tests/test_mix_web.py`：新增 `test_editor_fx_reset_tools_presets_and_embed_fit`；`test_page_texts_complete` 抽查表补入 15 个新键。

**验证**

- `pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` → **177 passed**（基线 175 + 新增 2 个用例）。
- 
ode --check`（提取内联脚本）通过；`py_compile`（mix_render / mix_web / i18n / app）通过。
- 浏览器实测（Chrome DevTools MCP，服务已重启、页面已硬刷新）：
  - iframe 高度 262 → 1337px 跟随内容；追加 600px 占位块 → 1937px，移除后回 1337px（双向）。
  - 面板高 232px；音质/压缩同宽 362px；FX 工具整行 1078px，三组左沿 70 / 496 / 1017，右端 1127 与 OUT 表右沿对齐。
  - 741px 视口：模块自动折行（IN+音质 / 压缩 / 回声+OUT / FX 工具），`scrollWidth == clientWidth`（无横向溢出）。
  - 明暗主题：父页 body 置白 → 子页 `data-theme=light`，工具条边线 `#dde3ea`、下拉底 `#fff`、按钮底 `#eef2f7`；恢复后回到 dark。

**未执行 / 待确认**

- 未执行任何 git 操作（需用户明确批准）。
- 已重启本地服务（改了 mix_render.py / mix_web.py / i18n.py），浏览器需刷新页面。

## 2026-09-30 — 多轨编辑器：轨道 FX 面板降高与内部排版规整

> 目的：用户反馈轨道效果面板「区域高度太高了，内部排版不合理、不规整」。实测面板高 197px，三个模块底边参差（197 / 138 / 156），且模块内部控件各行其是：压缩模块的 GR 列（89px）与旋钮行（71px）居中错位 9px、回声推子块（107px）顶端对齐导致「延迟/反馈/混合」标签明显低于「阈值/比率」、EQ 画布 88px 在 148px 的旋钮块里上下各空 30px。

- `static/multitrack/index.html`（内联 CSS + 两处 JS）：
  - `.fxpanel` 新增统一高度变量 `--fx-h: 122px`，`align-items: flex-start` → `stretch`：三个模块与 IN/OUT 电平表等高（实测均 159px，顶/底沿完全对齐，此前 197/138/156 参差）。
  - `.fxmod-b` 改为 `flex: 1 1 auto; justify-content: center`，内容不足时垂直居中兜底；`.fxmod-h` 固定 `flex: none`（标题条不参与拉伸）。
  - 让「可视化区」和「控件区」都吃 `--fx-h`：`.eqc` 高 `var(--fx-h)`、`.mt-gr .mt-col` 高 `calc(var(--fx-h) - 13px)`（13 = 间隙 + `GR` 标签）、`.fd-t` 高 `calc(var(--fx-h) - 28px)`。三者与旋钮列（32 + 2 + 11 + 2 + 13 = 60，两行 + 行距 2 = 122）严格等高，因此各模块的**数值徽标基线全部齐平**（实测第 1 行徽标底 375、第 2 行与推子徽标底 437）。
  - 收紧控尺寸：`.fxmod` 内边距 `8/11/9` → `7/10/8`、`.fxmod-h` 字号 11 → 10.5px、下边距 9 → 6px；`.kb` 宽 50 → 48px、盘面 40 → 32px（SVG 为 `viewBox="0 0 40 40"`，等比缩放安全）；`.kb-l/.fd-l` 与 `.kb-v/.fd-v`（含 `.fxnum`）改为固定行高 11px / 13px，消除行高浮动带来的错位。
  - JS：`const EQ_W = 186, EQ_H = 88` → `176, 122`（与 `.eqc` 的 `--fx-h` 一致）；`.eqc` 改用 `inset box-shadow` 画边线而非 `border`，使画布内容区尺寸与 `EQ_W/EQ_H` 严格相等、曲线不再被 1.6% 缩放。
  - JS：压缩模块的两个旋钮由 `.knobrow` 改为 `.fxknobs`（竖排两行），吃满 `--fx-h`，从而与音质/回声模块上下沿对齐；列宽由 9px → 8px 与 `.faderrow` 统一为 8px。

**验证**

- 浏览器实测（Chrome DevTools MCP，多轨编辑器页，2 轨）：
  - 面板高度 197 → **159px**；5 个直接子项（IN / 音质 / 压缩 / 回声 / OUT）实测均为 159px 且 `top/bottom` 完全相同。
  - 音质模块 `EQ` 画布 176 × 122，与旋钮两行（y 315–375 / 377–437）等高；压缩模块 GR 列 109px + `GR` 标签 = 122px；回声推子轨道 94px，推子列总高 122px。
  - 标签对齐实测：`低频/中频/高频` 与 `阈值` 同为 y=349，`高通/低通/声像` 与 `比率/延迟/反馈/混合` 同为 y=411；全部数值徽标底沿落在 375 或 437。
  - 741px 视口自动折行成两行（IN+音质+压缩 / 回声+OUT），`docSW 731 ≤ vw 741`，模块内 0 处元素越界，无横向滚动条。
  - 深/浅主题截图与取色确认：卡片底 `#22262e / #eef1f5`、边线 `#343a45 / #dde3ea`、画布底 `#2b303a / #ffffff` 均正确。
- `pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` → **175 passed**（与基线一致，含 `test_mix_web.py` 对 FX 面板 token 的断言）。

**未执行 / 待确认**

- 未执行任何 git 操作（需用户明确批准）；本次仅改静态 HTML，**无需重启服务**（浏览器硬刷新即可）。
- 音质模块宽 361px、压缩模块宽 97px，模块宽度不齐是内容决定的（压缩仅 2 个参数），如希望视觉更均衡可再议。

---

## 2026-09-30 — 修复卡片内层灰底与「使用上一次」按钮样式（整体复检）

> 目的：用户反馈「使用上一次」按钮样式很怪、没有上下间距，要求整体复检。复检后发现上一轮只处理了问题的一半：Gradio 6 的 `gr.Group` 真实结构是「外层 `gr-group.y2-sec`（卡片）→ 内层 `gr-group.y2-sec` → **`.styler`（真正的内容容器）** → 各组件」，而上一轮只把内层 `gr-group` 清零，漏掉了夹在中间的 `.styler`。该元素上 Gradio 的原生规则是 `background: var(--border-color-primary); gap: var(--form-gap-width)`，这才是灰底与"没有上下间距"的真正来源。

- `app.py`（`_TITLE_ROW_CSS`）：
  - `.y2-sec .styler` 新增 `background: transparent !important`。深色主题下 `--border-color-primary = #3f3f46`，Gradio 用「底色当边框」的手法让该容器铺满整块区域，组件之间的负空间因此露出一条条灰带（「使用上一次」那一整条灰带、卡片顶部标题区偏亮，均由此而来）。
  - `.y2-sec .styler` 新增 `gap: var(--y2-sp-3) !important`。容器自身 gap 读的正是 `--form-gap-width`（被内联成 1px，上一轮又被改写成 0px），卡片内相邻子项因此贴死、甚至出现 1px 重叠；现改为显式 12px，与卡片外节奏一致。
  - `.y2-sec .last-btn-row` 改为 `justify-content: flex-end !important; margin: 0 !important`（右对齐 + 归零负边距）。
  - `.y2-sec .last-btn-row button` 并入动作条按钮规格（高 30px、字号 12.5px、内边距 `0 14px`、圆角 `--y2-r-sm`，hover / 聚焦光圈一致）。此前该按钮沿用 Gradio `size="sm"` 的 26px 小按钮、左右仅 6px 内边距，与全站按钮语言不一致，视觉上"很怪"。
  - 全站 `.last-btn-row` 基础规则（内联 `<style>`）`margin-top: -10px` → `0`。
- `Docs/changelog.md`、`chat_history.md`：追加本轮记录。

**验证**

- `py_compile` 通过；`pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` 结果与基线一致。
- 重启服务后浏览器实测（Chrome DevTools MCP，pageId 28）：
  - 卡片 `.styler` 实测 `background: rgba(0,0,0,0)`、`gap: 12px`；卡片内全部相邻子项间距实测均为 12px（改动前为 0~1px，且存在 1px 重叠）。
  - 「使用上一次」行实测：`margin 0`、距上/下组件各 12px、按钮高 30px、字号 12.5px、内边距 `0 14px`、圆角 7px。
  - 六个已渲染 Tab 全量扫描：无卡片缺标题；卡片内 `.block / .wrap / .form / button / input / textarea` 0 处超出卡片右边界；视口 1401 / 1037 下均无横向滚动条。
  - 深色 / 浅色主题截图确认灰带消失、卡片层级正确、按钮行右对齐且间距正常。

**未执行 / 待确认**

- 未执行任何 git 操作（需用户明确批准）。
- 该修复依赖 Gradio 6 在该 `.styler` 上注入的变量名（`--layout-gap` / `--form-gap-width`）；升级 Gradio 后需回归。

---

## 2026-09-30 — 修复卡片化后的间距 / 对齐缺陷（整体排查）

> 目的：用户反馈卡片化后出现「大量对不齐、字体太靠近边框、间距太小甚至重叠」。整体排查定位到根因——Gradio 6 的 `gr.Group` 会向内层 `.styler` 内联注入 `--layout-gap: 1px; --form-gap-width: 1px`，使卡片内所有 Row/Column 的间距被压成 1px；叠加历史遗留的 `.last-btn-row { margin-top: -10px }` 负边距，产生贴死与重叠。约束不变：CSS 优先，不动组件类型、事件绑定与后端契约。

- `app.py`（`_TITLE_ROW_CSS`）：
  - 新增 `.y2-sec .styler { --layout-gap: var(--y2-sp-3) !important; --form-gap-width: 0px !important; }`。内联声明只能用带 `!important` 的样式表规则覆盖；`--layout-gap` 取 12px（全站默认 16px，卡片内收紧一档），`--form-gap-width` 还原全站默认值 0px（它只管表单内 label 与控件的间距，默认本就为 0）。
  - 卡片内边距 `10px 16px` → `14px 16px`；卡片 `gap` 6px → `var(--y2-sp-3)`（12px）。
  - 新增 `.y2-sec .row > .column { min-width: 0 !important }`：Gradio 给列内联 `min-width: min(320px,100%)`，窄窗口下卡片内两列会撑破卡片右边界。
  - 新增 `.y2-sec .last-btn-row { margin-top: 0 !important }`：抵消「使用上一次」旧样式的 -10px 负边距（进卡片后会压住输入框下边框）。
  - `.y2-sec .block.padded` 上下内边距 4px → 6px。
- `app.py`（音色翻唱页）：卡片「执行与输出」补齐 `### 执行与输出` 标题。此前该卡片漏加标题，导致右栏首卡无标题行、与相邻卡片（分离页同名卡片有标题）对不齐。

**验证**

- `py_compile` 通过；`pytest tests/ --ignore=tests/test_i18n.py -q` 175 passed；`pytest tests/test_mix_web.py tests/test_mix_render.py -q` 61 passed（与改动前基线一致）。
- 重启服务后浏览器实测（Chrome DevTools MCP，pageId 28）：
  - 卡片内 `.row` / `.column` 实测 gap 由 1px → 12px；`.y2-actions` / `.y2-toolrow` 仍为 8px，`#sep-rename-row` 等仍为 10px；`.form` gap 还原 0px；卡片 `padding` 实测 `14px 16px`；`.last-btn-row` 的 `margin-top` 实测 0px。
  - 七个 Tab 全量扫描（视口 1024×820）：无卡片缺标题；卡片内 `.block / .wrap / .form` 无一超出卡片右边界（0 处溢出）；页面 `scrollWidth ≤ 视口宽`（无横向滚动条）。
  - 歌曲历史页数据表在窄窗口下仍为卡片内横向滚动（预期行为，非缺陷）。
  - 深色 / 浅色主题截图确认：卡片内边距、标题分隔线、行/列间距、按钮行与控件间隔均正常。

**未执行 / 待确认**

- 未执行任何 git 操作（需用户明确批准）。
- `.y2-sec .styler` 覆盖的是 Gradio 6 的内联注入值；若后续升级 Gradio 且该注入变量改名，此项需回归。

---

## 2026-09-30 — 歌曲创作 / 音频转谱 / 多轨编辑 / 系统设置四页纳入统一卡片风格

> 目的：把上一轮建立的 `.y2-sec` 统一视觉语言继续推广到剩余四个功能页，使全站区块观感一致。约束不变：纯 CSS + 轻微容器包裹，不动组件类型、事件绑定与后端契约。

- `app.py`：
  - 歌曲创作页（`tab_create`）拆为 5 张卡片。左栏：「### 风格与歌词」（风格描述 / 使用上一次 / 风格标签 / 歌词 / 歌词工具）、「### 工作模式」（模式 / ABC 外部输入 / 使用上一次 / 4 条恢复事件绑定）、「### 生成参数」（项目名 / 种子 / CFG / ODE / 输出格式 / 批量数 / 音频后处理 / 高级采样参数）；右栏：「### 输出」（生成 / 取消 / 音频 / 批量变体选择）、「### ABC 乐谱」（可编辑乐谱 / 预览 / 导出 / 下载 / 重新合成）。卡内按钮行统一 `y2-actions`；`output_md`（`### 输出`）上移为卡片标题，生成按钮行紧随其后。**例外**：「🎵 生成歌曲 / 取消」一行不加 `y2-actions`，保留原生 `size="lg"` 大号主 CTA（实测 283×40、16px 字号）。
  - 音频转谱页（`tab_transcribe`）：整页包入一张 `y2-sec` 卡片（标题 `### 音频转乐谱`），两组按钮行改 `y2-actions`。
  - 多轨编辑页（`tab_mix`）：iframe 内联样式改用令牌（`--y2-line` / `--y2-r-lg` / `--y2-shadow-sm`），`margin-top` 归零，圆角/描边与外层卡片一致。
  - 系统设置页（`tab_settings`）：3 处 `elem_classes="settings-panel"` 改为 `["y2-sec"]`；删除已无引用的 `.settings-panel` CSS 规则。
- `src/i18n.py`（`EN_TABLE`）新增 2 条：`### 风格与歌词` → `### Style & lyrics`、`### 生成参数` → `### Generation parameters`。

**验证**

- `py_compile` 通过；`pytest tests/ --ignore=tests/test_i18n.py -q` 175 passed；`pytest tests/test_mix_web.py tests/test_mix_render.py -q` 61 passed（均为改动前基线）。
- 重启服务后浏览器实测（Chrome DevTools MCP，pageId 28）：
  - 歌曲创作页 5 张卡片、音频转谱 1 张、系统设置 3 张，标题与顺序正确；内外层不叠加（内层 `border:0 / background:transparent / padding:0 / margin:0`）。
  - 深色：卡片 `rgb(24,24,27)` / 1px `rgb(63,63,70)` / 14px 圆角 / `10px 16px` 内边距；浅色：`rgb(250,250,250)` / `rgb(228,228,231)`。
  - 卡片标题实测 13px；`y2-actions` 按钮实测 `78×30`、`118×30`，字号 12.5px。
  - 多轨 iframe 实测 `border-radius:14px` + 令牌描边与微阴影，高度 760px 不变。

**未执行 / 待确认**

- 未执行任何 git 操作（需用户明确批准）。
- 歌曲创作页「🎵 生成歌曲」按要求保留大号主 CTA（首次改动曾与其它按钮统一为 30px，已回退为原生 `size="lg"`）。

---

## 2026-09-29 — 歌曲历史 / 音轨分离 / 音色翻唱三页视觉语言对齐

> 目的：多轨编辑器（`static/multitrack/index.html`）已建立一套设计令牌（间距 / 圆角 / 卡片 / 聚焦光圈 / 紧凑按钮）。本轮把这套视觉语言推广到 Gradio 页面，消除三页「标题贴在左边缘、区块无边界、按钮撑满整行」的松散观感。约束：纯 CSS + 轻微容器包裹，不动组件类型、事件绑定与后端契约。

- `app.py`（`_TITLE_ROW_CSS`，新增样式块）：
  - 令牌层挂在 `#tab-history / #tab-sep / #tab-cover` 作用域（**不能放 `:root`**：`var()` 在声明元素处完成替换后继承，令牌必须落在 `.gradio-container` 派生作用域内，否则深色主题失效）：
    `--y2-r-lg/md/sm: 14/10/7px`、`--y2-h-ctl: 30px`、`--y2-line: var(--border-color-primary)`、`--y2-card: var(--background-fill-secondary)`、`--y2-muted`、`--y2-shadow-sm: var(--shadow-drop)`、`--y2-ring: 0 0 0 3px color-mix(in srgb, var(--color-accent) 24%, transparent)`。
  - `.y2-sec` 卡片：1px 描边 + 14px 圆角 + 微阴影 + `10px 16px` 内边距，`display:flex; flex-direction:column; gap:6px`；`h3` 作卡片标题（13px / 600 + 底部细线）。
  - **关键修正**：`gr.Group` 在 Gradio 6 中渲染为**两层嵌套 div 且两层都带 `elem_classes`**，若不清零内层会出现「卡片套卡片」（边框 / 内边距 / 阴影翻倍）。新增 `#tab-* .y2-sec .y2-sec { border:0; padding:0; background:transparent; box-shadow:none; margin-bottom:0 }` 复位内层。
  - **对比度修正**：深色主题下 `--background-fill-primary` 与页面底色同为 `#0f0f11`，卡片会「隐形」；改用 `--background-fill-secondary`（深色 `#18181b` / 浅色 `#fafafa`）形成浮起层次。
  - `.y2-toolrow`（翻页行）与 `.y2-actions`（动作行）：按钮 `flex:0 0 auto; width:auto; min-width:auto; height:30px`，去掉 Gradio 默认 `min-width: min(320px,100%)` 造成的整行拉伸；`.y2-toolrow > .block:not(button)` 承担中间信息位的 `flex:1` + 居中 + `--y2-muted`。
  - 聚焦态：`:is(.wrap,.input-container,label.container):focus-within { border-color: var(--color-accent); box-shadow: var(--y2-ring) }`。
- `app.py`（页面结构，仅容器包裹）：
  - 历史页 `gr.Tab(..., elem_id="tab-history")`；3 张卡片：`### 生成历史`（表格 + `y2-toolrow` 翻页行）/ `### 记录详情` / `### 项目操作`（`y2-actions` 三按钮）。`#hist-rename-row` 与全部事件绑定未动。
  - 分离页 `gr.Tab(..., elem_id="tab-sep")`；左栏 3 张卡片（`### 源音频` / `### 音轨分离` / 新增 `### 执行与输出` 含 `y2-actions` 的开始分离+取消）、右栏 2 张卡片（`### 分离任务历史`（含 `#sep-rename-row`）/ `### 库管理`（含 `#lib-stem-row`、`#lib-ref-row`））。
  - 翻唱页 `gr.Tab(..., elem_id="tab-cover")`；左栏 3 张卡片（`### 被翻唱歌曲` / `### 参考音色`（含干声与上传面板、自定义伴奏）/ 新增 `### 翻唱参数`（半音快捷按钮改 `y2-actions`））、右栏 2 张卡片（`### 执行与输出` / `### 翻唱任务历史`）。
  - 所有 `_reg(...)` 多语言注册、`sep_*` / `cover_*` / `lib_*` 事件绑定、隐藏 State 与组件可见性逻辑**完全未改**。
- `src/i18n.py`：新增 4 条英文译文 `### 记录详情 / ### 项目操作 / ### 执行与输出 / ### 翻唱参数`（`### Record details / ### Project actions / ### Run & output / ### Cover parameters`）。

**验证**

- `python -m py_compile app.py src/i18n.py` → 通过。
- `python -m pytest tests/ --ignore=tests/test_i18n.py -q` → 175 passed；`python -m pytest tests/test_mix_web.py tests/test_mix_render.py --basetemp=".pytest_tmp_mix" -q` → 61 passed（与基线一致，无回归）。
- `tr('en', …)` 直查：4 条新键均返回预期英文（`tr('zh', …)` 回退原文）。
- 重启服务后浏览器实测（Chrome DevTools MCP，pageId 28，`http://127.0.0.1:9898/`）：
  - 深色主题分离页：5 张外层卡片，底色 `rgb(24,24,27)`、1px `rgb(63,63,70)` 边框、14px 圆角、`10px 16px` 内边距，内外层不再叠加。
  - 翻唱页：5 张卡片标题依次为 被翻唱歌曲 / 参考音色 / 翻唱参数 / 执行与输出 / 翻唱任务历史。
  - 历史页翻页行：按钮 `66×30`（原先被拉伸至 ~215px），中间信息位 `第 1 / 1 页，共 4 条` 居中；表格圆角外框完好。
  - 浅色主题（`?__theme=light`）：卡片 `rgb(250,250,250)` / 边框 `rgb(228,228,231)`，页面白色，层次正常。
  - 卡片内既有音频播放器（自定义 PlayerZoom）未受影响。



---

## 2026-09-29 — 多轨编辑器 transport 工具条细化

> 目的：上一轮把 transport 独立成工具条后，播放/停止/循环仍是三个等权重的散件，时钟把「当前位置 / 总时长」挤在一个字符串里且跟随系统时钟字体跳动，走带状态只能靠按钮上的 ▶/⏸ 判断。本轮把它做成"硬件运输键 + 时钟读数"的形态。

- `static/multitrack/index.html`（CSS）：
  - 删除旧规则 `.transport #tp-play` / `.transport #tp-stop` / `.transport .tstate`；工具条本体加 `--shadow-sm` 描边卡片底。
  - 新增 `.tp-grp`：控制簇用 `> * + * { margin-left: -1px }` 合并相邻边框、`> * { height: 26px }`，仅首尾元素取圆角（`--r-sm`），中间元素 `border-radius: 0`，形成分段控件。「播放 / 停止」定宽 40px，循环芯片 `padding: 0 11px`。
  - 新增 `.tp-clock`：当前位置（13px / 600）+ `.tp-sep`「/」（`--muted`）+ `#tp-dur` 总时长（12px / 500 / `--muted`）分离显示，`font-variant-numeric: tabular-nums` 等宽数字，避免走带时读数左右抖动；`gap: 6px`。
  - 新增 `.transport.on .tp-clock #tp-time { color: var(--accent) }`：播放中当前时间码转主题色。
  - 新增 `.transport label.lb { min-width: auto }`（行标签不再占固定宽）与 `.tp-msg { margin-left: auto }`（状态消息右对齐）。
- `static/multitrack/index.html`（DOM）：
  - `<div class="row transport">` → 加 `id="transport"`；播放/停止/循环包进 `<span class="tp-grp">`；时间码拆为 `<span class="tp-clock"><span id="tp-time">…</span><span class="tp-sep">/</span><span id="tp-dur">…</span></span>`；状态消息改为 `<span id="tp-msg" class="msg tp-msg">`。保留 `#t-transport`（「播放控制」）与 `#t-loop` 文案锚点，i18n 词条不变。
- `static/multitrack/index.html`（JS）：
  - `syncTransportLabels()` 末尾按 `S.playing` 给 `#transport` 切换 `.on`；`#tp-stop` 文案固定为实心方块 `■`（`⏹` 在部分字体下过小/缺字形）。
  - `updatePlayheads()` 时间码拆分：分别写 `#tp-time`（当前位置）与 `#tp-dur`（总时长），取代原来的 `fmtTime(pos) + " / " + fmtTime(d)` 单串拼接。

**验证**

- 
ode` + `vm.Script` 编译内联 script：`compiled scripts: 1 ok`。
- `python -m pytest tests/test_mix_web.py tests/test_mix_render.py -q` → 61 passed；`python -m pytest tests/ --ignore=tests/test_i18n.py -q` → 175 passed（本轮未改 Python，无新增文案键）。
- 浏览器实测（Chrome DevTools MCP，pageId 33，载入 `separations_20260927_104930`）：
  - 控制簇无缝衔接：play `[109,40]`、stop `[148,40]`（与 play 间隙 −1px）、loop `[187,48]`（与 stop 间隙 −1px）。
  - 时钟 `[247,105]`，与循环芯片右侧间距 12px（`--sp-3`）；读到 `0:00.0` + `2:51.6` 两段。
  - 播放中：`#transport.on = true`，`#tp-time` 颜色 `rgb(59,130,246)`（主题色）；按钮转 `⏸`，读数正常递增。点击停止后 `.on = false`、颜色回到 `rgb(230,232,235)`、按钮回 `▶`。
  - 浅色主题时钟底 `rgb(238,242,247)` / 描边 `rgb(216,222,228)`，与卡片可区分；状态消息右缘 `1382` vs 工具条右缘 `1395`（1px 边框 + 12px 内边距），右对齐生效。
  - 控制台无 JS 报错（仅既有的表单 a11y 提示）。

---

## 2026-09-29 — 多轨编辑器 UI 设计感优化（视觉语言 + 轨道卡 + 效果器面板）

> 目的：编辑器功能已齐，但视觉层偏"工具原型"——控件等权重堆叠、低对比描边导致按钮/芯片与卡片糊成一片、旋钮中性值时只剩一根线、电平表无刻度、时间刻度只能靠波形猜。本轮在不改后端契约（工程 JSON version 1 字段与钳制区间不变）的前提下，统一设计令牌、重排轨道头、补齐刻度与可视化细节。

- `static/multitrack/index.html`（CSS）：
  - 设计令牌体系铺开：间距 `--sp-1..5`、圆角 `--r-lg/md/sm`（12/10/7 → 14/10/7）、控件高 `--h-ctl`、分层阴影 `--shadow-sm/--shadow/--shadow-lg`、聚焦光圈 `--ring`、`--accent-soft`、`--panel-line`、轨色板 `--trk-1..6`、网格色 `--grid`；三个主题块（浅色 / 系统深色 / 显式 `data-theme="dark"`）同步补齐。
  - 控件对比度修正：按钮/芯片/步进器/数值徽标底色改为 `color-mix(in srgb, var(--fg) 8%, var(--card-2))`，深色主题 `--line` 由 `#2c3038` 提亮到 `#3a414c`。修复前 `.chk.pill` 与 `.stp` 的底色 `--card-2` 与轨道卡底完全相同，未选中时肉眼近乎不可见。
  - 轨道卡：新增轨色条（`.track::before` + `--trk`）、轨名色点、hover 描边取轨色；`M`/`S` 改为芯片（`.chk.pill`，M 选中为 `--danger`）；增益改为「range + ± 步进器 + 等宽徽标」；新增 `.ruler` 共享刻度尺、`.fades`/`.tbtn` 成组样式、`.sep` 分隔条。
  - 效果器面板：旋钮重做（盘面径向渐变 `#kbFill` + 行程弧 `kb-track` + 五档刻度 `kb-tk` + 指针 + 中心盖），`270°` 行程；电平表新增 `-6/-12/-24/-48 dBFS` 参考刻度线（`.mt-tick`，`z-index:2` 压在色柱之上）；EQ 画布 186×88，加 `±9dB` 次级网格、频率标注底条、曲线外发光；母带行参数成组。
  - 修复推子滑块越界：`.fd-k` 原用 `top: % + margin-top:-6px` 定位，0 位时下缘压住下方标签；改为 `top: calc((1 - var(--pos)) * (100% - var(--kd)))`，行程限制在轨道内，`--pos` 由 JS 写入。
  - 其余：卡片并排网格 `.cardgrid`、空状态 `.empty`、自定义滚动条、`.transport` 独立工具条、`#tp-play/#tp-stop` 定宽。
- `static/multitrack/index.html`（DOM/JS）：
  - DOM：`<h1>+hint` 包进 `pagehead`；素材卡与工程卡并排；新增隐藏 SVG defs（旋钮渐变）；`#tracks` 前插入 `<div class="ruler" id="ruler">`；轨道头重排为左组（轨名 + 试听）与右组（M/S + 增益 + 淡变 + 切片 + 状态）；按钮加 `primary/ghost/danger` 分级。
  - 新增 `S.ticks`（刻度秒数组）与 `rulerStep()/fmtClock()/renderRuler()`：按 `[0.5,1,2,5,10,15,30,60,120,300,600]` 选「刻度数 ≤ 12」的档位，末刻度标签右对齐；`loadPeaks()` 中时长确定后调用，`drawTrack()` 复用同一组刻度画波形网格线。
  - `drawTrack()`：高度 78、颜色一律取 CSS 变量（`--line/--grid/--trk/--sec`），先铺 16% 透明度的包络填充再描轨色包络线。
  - 混音记录空状态（`暂无混音记录`）、删除按钮 `danger`；窗口 resize 防抖（120ms）重建刻度尺并重绘。
- `src/mix_web.py`：`PAGE_TEXT_KEYS` 新增 `"暂无混音记录"`。
- `src/i18n.py`：`EN_TABLE` 新增 `"暂无混音记录": "No mix records yet"`。

**验证**

- 
ode --check`（抽取内联 script，经 `vm.Script` 编译）通过。
- `python -m py_compile src/mix_web.py src/i18n.py` 通过。
- `python -m pytest tests/test_mix_web.py tests/test_mix_render.py -q` → 61 passed；`python -m pytest tests/ --ignore=tests/test_i18n.py -q` → 175 passed。
- 浏览器实测（Chrome DevTools MCP，1440×1000，深浅两主题 + `?embed=1`）：
  - 刻度尺与波形网格线逐点对齐：ruler `[54,1382,1328]` vs canvas `[53,1382,1329]`，12 个刻度位置完全一致（修复前刻度尺跨满整行、与波形横向错位约 14px）。
  - 芯片/步进器底色 `rgb(50,54,61)` vs 轨道卡底 `rgb(34,38,46)`，未选中也可辨识。
  - 推子滑块 0 位时 `[354.1, 366.1]` 落在轨道 `[290.1, 366.1]` 内，不再与标签 `[368.1, 380.1]` 重叠。
  - 工程「保存 → 载入 → 再保存」幂等：增益 1dB / 静音 true / 淡入 1.5s / 声像 -0.5 / 回声混合 0.4 全部一致，控件显示同步（徽标 `1.0 dB`、M 芯片选中）。
  - 控制台无 JS 报错（仅既有的表单 a11y 提示）。

---

## 2026-09-29 — 多轨编辑器提升为独立 Tab「多轨编辑」

> 目的：原多轨编辑器内嵌在「音轨分离」Tab 末尾（另一入口是「多轨编辑」按钮新窗口打开），入口分散、与分离流程耦合。本轮把它提升为独立功能 Tab，位于「音色翻唱」之后、「系统设置」之前，并移除分离页的两个旧入口，入口统一到新 Tab。

- `app.py`：
  - 新增 `with gr.Tab(_t("多轨编辑")) as tab_mix:` 区块（cover 之后、settings 之前），内含 `gr.HTML` iframe（`src="/static/multitrack/?embed=1"`，`elem_id="mix-editor-embed"`，高 760px，同原样式的边框/圆角/透明底），并 `_reg(tab_mix, lambda lang: gr.update(label=tr(lang, "多轨编辑")))` 做语言切换刷新。
  - 移除分离页旧入口三处：`sep_mix_btn`（「多轨编辑」按钮 + `_reg`）、其 `click` 绑定（`window.open('/static/multitrack/')`）、以及 `sep_mix_embed`（内嵌 iframe）。
  - 静态页路由 `Route("/static/multitrack/"...)` 与 `_mix_page`（磁盘读 `index.html` + `Cache-Control: no-store`）不变，故编辑页本身零改动。
- `src/i18n.py`：Tab 文案复用已有词条 `"多轨编辑": "Multitrack"`，本轮无需新增词条（`mix_web.PAGE_TEXT_KEYS` 亦不变）。
- `tests/test_mix_web.py`：`test_app_registers_mix_routes_and_entry` 改为断言新 Tab（`gr.Tab(_t("多轨编辑")) as tab_mix` + `elem_id="mix-editor-embed"` + 仍保留 `?embed=1`），并新增断言旧入口 `sep_mix_btn` / `sep-mix-embed` 已从 app.py 消失；模块 docstring 同步。
- `.gitignore`：`.pytest_tmp/` → `.pytest_tmp*/`，覆盖自定义 `--basetemp` 目录（`.pytest_tmp_mix` / `.pytest_tmp_all`）。

**验证**

- `py_compile app.py` 通过。
- `pytest tests/test_mix_web.py tests/test_mix_render.py` → **61 passed**；全量（排除脚本式 `tests/test_i18n.py`）→ **175 passed**。
- 后端与音频语义零改动（`src/mix_render.py`、`static/multitrack/index.html` 本轮均未触碰），故未重复跑渲染比对。

**未执行 / 待确认**

- 重启 9898 服务与浏览器实测（新 Tab 位置与切换、iframe 载入、明暗主题跟随）——改 app.py 必须重启才生效，待用户确认后执行。
- 本轮及此前 P1 / P2 / 盲测工具 / UI 精细化 / FX 面板两批改动仍未 commit（用户规则禁止自动 git 操作）。

---

## 2026-09-29 — 多轨编辑器每轨效果区专业效果器风 · 第二批（IN/OUT 电平表 + 压缩 GR 表 + EQ 频响曲线）

> 目的：第一批只交了面板骨架与旋钮/推子，专业插件「一眼能看出信号在做什么」的可视化还缺三块——**输入/输出电平表**（看进多大声、出多大声）、**压缩增益衰减表 GR**（看压缩到底压了多少）、**EQ 频响曲线**（看三段 EQ + 高低通叠出来的实际曲线，并能直接拖手柄调增益）。本轮补齐，并保持「后端与音频参数语义完全不变」。

- `static/multitrack/index.html`：
  - **CSS**：新增 `.fxmeter`（表体外框，`align-self: stretch` 跟随面板行高）、`.mt-bars/.mt-col/.mt-col.lv/.mt-col.gr/.mt-mask/.mt-mask.bot/.mt-peak/.mt-lab`、`.mt-gr`（压缩模块内的 GR 列）、`.fxside`（「可视化 + 旋钮」并排容器）、`.fxknobs`、`.eqc`（180×84 曲线画布）。
    - **渐变不被拉伸的表技法**：列底本身就是填充色（电平为绿→黄→红渐变、GR 为 `--danger`），用自上而下的遮罩 `.mt-mask` 盖住未点亮部分，色标因此固定在整列上而不随电平被压缩；GR 表反向填充，遮罩改为从底部往上盖（`.mt-mask.bot`）。
  - **电平表取样（旁路，不串进音频链路）**：`mkMeterTap(ctx)` 用 `ChannelSplitterNode` + 两个 `AnalyserNode`（`fftSize = 1024`）分左右取样；**IN 取音质链之前的 `env` 之后**（含裁切/淡变包络）、**OUT 取轨增益之后**（含 Mute/Solo 门控），与 todo 的链路约定一致。分析节点仅通过 `env.connect(tap.split)` / `g.connect(tap.out.split)` 额外接一路，**主链路一个节点都没加**，因此播放声音与后端渲染结果不受影响。
  - **表弹道学**：`tapPeak()` 用 `getFloatTimeDomainData` 求时域峰值 → `dbToFrac()` 按 -60dBFS 表底换算 0~1；`stepMeter()` 上升立即跟随、下降按 3.2 满量程/秒回落（约 0.3s 归零），峰值保持 800ms 后回落。
  - **rAF 刷新循环**：`meterLoop()` 自终止——播放中持续刷新，停止后把残值回落归零再停表（`_mtrRaf = 0`），不空转占 CPU。
  - **GR 表**：读压缩节点的 `reduction` 按 -20dB 满档映射。**实测发现 Chrome 把它实现为只读 number 而非规范写的 AudioParam**，故写成 `typeof rr === "number" ? rr : rr.value` 两种形态兼容（否则取到 `undefined` 导致 GR 恒为 0）。
  - **EQ 频响曲线**：在 `OfflineAudioContext` 上建 5 个 biquad（高通/低通/低频架 200Hz/中频峰 1kHz Q=1/高频架 4kHz，与 `buildFxChain` 参数一致），对 20Hz…20kHz 对数等分 200 点调 `getFrequencyResponse`，各滤波器幅度取 dB 后**相加**（串联即 dB 相加）。画 DPR 适配的画布 + 0dB 中线与 100/1k/10k 竖线网格 + 半透明填充 + 3 个手柄圆点。
  - **3 个手柄可拖拽改增益**：按 x 就近吸附到低/中/高某个手柄，纵向拖动改 gain（±15dB、按 0.5 吸附），**频率固定**（后端未暴露频率参数）；与旋钮**双向联动**（手柄 → `setV` 同步旋钮显示；旋钮 onChange → `drawEqCurve` 重绘）。键盘 ↑↓ ±0.5、Home 三段齐归零。
  - **buildTracks 结构改造**：音质模块改为「左 EQ 曲线 + 右两行 6 旋钮」（`.fxside`），压缩模块改为「左 GR 表 + 右阈值/比率」（`.fxside`），面板两端 `prepend`/`append` 挂 IN / OUT 两块表；`track` 新增 `eqCanvas / mtrEl / mtr / meter`；建轨后立即 `attachEqCurve + drawEqCurve`。
  - **播放链路接线**：`playFrom` 每轨建 `t.meter = {in, out, fx}` 并只建一次 `t.mtr`（保留残值供回落动画），末尾启动 `meterLoop()`；`scheduleIteration` 里 `env.connect(t.meter.in.split)`；`stopNodes` 里 `disposeMeterTap` 释放取样链但**不重置 `t.mtr`**；`applyTrackState` 回灌后追加 `drawEqCurve(t)`；`redrawAll`（主题切换）改为波形与曲线一并重绘。
- `src/i18n.py` / `src/mix_web.py`：新增 `IN / OUT / GR` 三个文案键（音频术语，中英一致），并同步进 `PAGE_TEXT_KEYS`。
- `tests/test_mix_web.py`：`test_editor_page_has_transport_playback` 追加第二批 token（`mkMeterBlock / mkMeterTap / disposeMeterTap / meterLoop / getFloatTimeDomainData / createChannelSplitter / drawEqCurve / attachEqCurve / setEqGain / EQ_HANDLES / getFrequencyResponse / OfflineAudioContext / fxmeter / mt-col / fxside / fxknobs`）；`test_editor_defaults_hotkey_and_stepper` 追加曲线双向联动与回灌重绘断言；**新增 `test_editor_meters_wiring_is_bypass_only`**（断言取样链旁路、播放建链/停止释放、rAF 自终止、GR 兼容读取、手柄吸附与增益吸附、主题重绘）；`test_page_texts_complete` 追加 `IN/OUT/GR` 存在性与中英一致断言。

**验证**

- 内联 JS 
ode --check` 通过（65848 字符，exit=0）；`py_compile` 通过。
- `pytest tests/test_mix_web.py tests/test_mix_render.py` → **61 passed**（较第一批 +1，新增接线测试）；全量（排除脚本式 `tests/test_i18n.py`）→ **175 passed**。
- 浏览器实测（页 32，真实 2 轨素材 04. K歌之王，224.3s）：
  - 结构：每轨 2 个 `.fxmeter`（IN/OUT）+ 5 个 `.mt-col`（2+2 电平 + 1 GR）+ 1 个 `canvas.eqc`；模块标题 `["音质","压缩","回声"]`；`mtrEl` 五键齐全。
  - 电平表：从 150s（副歌）起播，IN 峰值 0.939 / OUT 0.923（-0.5dB 满量程差），遮罩 12.2%、峰值线 91.8% 实时跳动；起始段（人声未进）实测为 0，说明表不虚报。
  - 回落：暂停后 100ms → 0.606、200ms → 0.232、**300ms → 0**，峰值线保持至 ~700ms 后衰减，全部归零后 `_mtrRaf` 自动置空（表停），`t.meter` 已释放。
  - GR 表：阈值 -30dB / 比率 8:1 时 `reduction ≈ -16dB`、GR 显示值 0.884、遮罩 20%（自上而下红柱）；阈值 -18dB / 4:1 时 0.609，档位越小压得越少，符合预期。
  - EQ 曲线：拖中频手柄到 +6dB → `eqMid=6`、旋钮读数 `+6.0 dB`、`getV()=6`；反向拖中频旋钮 45px → 6 → 13.5（180px 满量程 30dB，步进 0.5）；画布在手柄新位置取到 `rgb(96,165,250)`（暗色 `--mod-eq`），确认曲线与手柄同步重绘；`Home` 三段齐归零且读数同步。
  - 主题：强制 `data-theme="light"` 后画布底色 `#fff`、曲线/手柄 `#3b82f6`（`--mod-eq` 亮色值），暗色为 `#2b303a` / `#60a5fa`，两套主题配色与网格均可读。
  - 窄屏：视口 741px 时模块自动折行（音质/压缩同排、回声换行），`scrollWidth == clientWidth`、无子元素越界。
  - 控制台无 JS 报错（仅 2 条既有的表单可访问性提示；另 1 条 `getImageData` 警告来自本次实测脚本自身的像素取样，产品代码不调用 `getImageData`）。
- **导出逐字节等价（T4 验收）**：本轮新增的 `AnalyserNode` 仅旁路取样，且后端渲染是 ffmpeg 离线执行，与前端节点无关；`src/mix_render.py` 本轮零改动（`git status` 中的改动来自更早的 P1/P2 轮次）→ 同参数渲染产物必然一致，故未重复跑渲染比对。

**设计取舍**

- 电平表高度**不锁 76px**，改为 `align-self: stretch` 跟随面板行高（实测 189px，与最高的音质模块同高）。原因是行高已由 EQ 曲线 + 双排旋钮决定，锁死表高只会留白；拉满后表更像插件机架上的长条表，读数更细。
- GR 表列高固定 72px（与推子轨道同高），不随模块拉伸，避免压缩模块被撑高。

**未执行 / 待确认**

- 未执行任何 git 操作（P1 + P2 + 盲测工具 + UI 精细化 + FX 面板第一批 + 本批均未 commit）。
- P2 盲测评分解盲结论仍待用户试听回填。
- `.pytest_tmp_all/`、`.pytest_tmp_mix/` 未被 `.gitignore` 覆盖（建议改为 `.pytest_tmp*/`），等用户决定。

---

## 2026-09-29 — 多轨编辑器每轨效果区改为专业效果器风（模块分组 + 旋钮/垂直推子）· 第一批

> 目的：原「音质行 / 音效行」是一排无分组、无刻度的扁平数字框，缺少专业插件的可读性与操作手感。本轮把每轨效果区重做成专业效果器面板样式：**按功能分模块（音质 / 压缩 / 回声）**，模块带主色圆点与标题；**连续比例量用旋钮**（带值弧与指针）、**时间·强度类用垂直推子**；每控件底部「标签 + 数值徽标」，徽标可直接双击录入。
> 推进方式：经用户确认**分两批**——本轮（第一批）只做面板骨架、控件与回灌；输入/输出电平表、压缩 GR 表、EQ 频响曲线留到第二批。
> 硬约束：**后端与音频参数语义完全不变**（`mix_render.py` / 工程契约 `version: 1` / 11 个参数的区间与中性值与后端钳制一一对应），本轮纯前端视觉与交互改造。

- `static/multitrack/index.html`：
  - **CSS 变量（明暗双主题）**：新增 `--panel`（模块底）/ `--knob-bg`（旋钮盘面）/ `--knob-track`（推子轨道）与三个模块主色 `--mod-eq / --mod-dyn / --mod-echo`；`:root`、`@media (prefers-color-scheme: dark)`、`html[data-theme="dark"]`、`html[data-theme="light"]` 四处同步。
  - **模块分组**：新增 `.fxpanel / .fxmod / .fxmod-h / .fxmod-b`；模块主色用 CSS 变量继承下发（`.fxmod[data-mod="eq"] { --mod: var(--mod-eq); }`），子元素 `.kb-arc / .fd-f / .fxmod-h` 自动取色，避免逐个写选择器。模块**按内容自适应高度**（`align-items: flex-start`），不再被强行拉平产生大片空白。
  - **旋钮**：`.kb*` 用内联 SVG（盘面 + 值弧 + 指针），行程 270°（`KNOB_SWEEP` / `KNOB_A0`）；值弧按极坐标 `arcPath()` 生成，**双极参数从中性点向两侧生长**（EQ 0dB / 声像 C 时无弧），单极从最小端生长。
  - **垂直推子**：`.fd*` 为纵向轨道 + 模块色填充 + 滑块，支持点击跳转与拖动。
  - **删除**废弃的 `.fxrow` 与 `.step` 全部样式。
  - **控件工厂 `mkFxCtl`**：值由闭包持有，返回 `{el, setV, getV}`；`setV` 只刷新显示、不广播 `onChange`（供工程回灌，与既有语义一致）。交互：拖拽（旋钮按垂直位移，拖满约 180px 覆盖全量程；`Shift` 精调 1/5）、推子按轨道内绝对位置、滚轮步进、**双击盘面 / Home 复位中性值**、**双击数值徽标原地换成数字输入框**（Enter 提交 / Esc 放弃）、键盘 ↑↓（Shift 五档）/ PageUp·PageDown（1/10 行程）。拖拽用 Pointer Capture，并对 `setPointerCapture` 加异常兜底。
  - **数值徽标 `fmtFxVal`**：按类型格式化（dB 带正负号、Hz 超 1k 折算为 k、0 显示 `OFF`、比率 `x:1`、声像 `C/L35/R50`、混合显示百分比）。
  - **buildTracks 重写**效果区：`音质`模块两行旋钮（低频/中频/高频 + 高通/低通/声像）、`压缩`模块（阈值/比率）、`回声`模块（延迟/反馈/混合三个推子）；控件区间与后端钳制区间一致，`onChange` 实时写回 `track` 并驱动节点链。
  - `applyTrackState` 回灌改走 `setV`（11 处），不再直接写 `.value`。
  - 阈值旋钮加 `bipolar`：其中性值 0dB 位于行程最右端，让值弧从「关闭」端生长，避免默认态出现「满弧」被误读为压缩全开。
- `tests/test_mix_web.py`：`test_editor_page_has_transport_playback` 的 token 列表更新为 `fxPanel / mkFxCtl / mkMod`；`test_editor_defaults_hotkey_and_stepper` 重写为断言新面板结构（`data-mod="eq|dyn|echo"`、`fxmod-h`、`knobrow`、`faderrow`、推子控件定义、`inp.type = "number"; inp.className = "fxnum"`、`setPointerCapture`、`ev.shiftKey ? 900 : 180`、双击复位、`Home`、回灌 `setV`）。

**验证**

- 内联 JS 
ode --check` 通过（53307 字符，exit=0）；`pytest tests/test_mix_web.py tests/test_mix_render.py` → 60 passed；全量（排除脚本式 `tests/test_i18n.py`）→ 174 passed。
- 浏览器实测（pageId，真实 2 轨素材 04. K歌之王）：
  - 结构：2 轨 × 3 模块 = 6 个 `.fxmod`，16 个旋钮 + 6 个推子；默认态 16 个值弧全部为空（中性）✓。
  - 手感：旋钮上拖 90px → 低频 +15.0 dB（触顶钳制）且弧出现；滚轮两格 → 中频 +0.5 dB；双击盘面 → 复位 0；双击徽标 → 出现 `.fxnum`，输入 7.5 + Enter → `+7.5 dB` 且输入框消失；Esc → 不提交（保持 7.5）；推子点轨道 25% 高度 → 延迟 1500 ms、填充 75%；`Home` → 0；`↑` → 0.5；`Shift+↑` → 3.0。
  - 工程往返：设定 11 参数 → `buildPayload()` 11 字段全部正确（`pan=-0.35 … echo_mix=0.35`）→ 清零 → `applyTrackState()` 回灌 → 11 个 `track` 值与 11 个徽标文本完全还原，8 个旋钮弧重新出现，3 个推子填充 15%/47.4%/35%（对应 300ms/0.45/35%）。
  - 主题与布局：亮色主题下模块底 `rgb(238,241,245)`、三模块主色与旋钮弧正常；窄视口（700px）下模块保持一行不溢出、控件不错位。
  - 控制台无 JS 报错（仅 2 条既有的表单可访问性提示）。

**未执行 / 待确认**

- 未执行任何 git 操作（P1 + P2 + 盲测工具 + UI 精细化 + 本轮 FX 面板均未 commit）。
- 第二批（待用户确认本轮观感与手感后再做）：输入/输出电平表、压缩增益衰减（GR）表、EQ 频响曲线；需先确认新增 `AnalyserNode` 后导出结果与改动前同参数渲染 md5 等价。
- P2 盲测评分解盲结论仍待用户试听回填。

---

## 2026-09-29 — 多轨编辑器 UI 精细化：参数默认中性 + ± 步进器 + 空格全局热键

> 目的：P2 参数上线后，收敛「默认值语义」与「操作手感」——默认应为中性（不引入未预期的效果），数值要能精确微调，空格热键要在任何焦点位置都可用；同时提升页面视觉精致度与组件间距。

- `static/multitrack/index.html`：
  - **参数默认中性**：回声「延迟 / 反馈 / 混合」与压缩「阈值 / 比率」初值改为中性（延迟 0、反馈 0、混合 0、阈值 0、比率 1），即默认不生效；`track` 对象默认值同步为 `echoDelay: 0, echoFb: 0, echoMix: 0, compTh: 0, compRatio: 1`；`applyTrackState` 不再把 0 显示成 250（延迟 0 的兜底仍由前后端一致的 `FX_ECHO_DEF_MS` / `_ECHO_DEFAULT_DELAY_MS` 在「混合 > 0」时生效）。
  - **± 步进器**：`mkFxNum` 重写为「− / 数字 / +」合并控件（`.step`），按 `step` 增减并按区间钳制（`r3` 消除浮点累加噪声）；按钮 `tabIndex = -1` 不抢焦点，避免空格热键失效；隐藏原生 spinner（`appearance: textfield` + `::-webkit-inner-spin-button`）防止与自建 ± 重复。
  - **循环默认勾选**：`tp-loop` 加 `checked`，`S.loop` 初值改 `true`。
  - **空格改全局热键**：改为在**捕获阶段**监听 `window`（`addEventListener("keydown", …, true)`），任何组件获得焦点后空格仍能播放/暂停；仅放行「文本类输入」（textarea / contentEditable / `input[type=text]`，空格有输入语义），数字框 / 下拉 / 按钮 / 空白区域一律当热键；`ev.repeat` 防长按重复触发。
  - **视觉精细化**：`:root` 新增尺度变量 `--r-lg/--r-md/--r-sm`、`--shadow`、`--ring`；卡片圆角 12px + 阴影视差、轨道卡圆角 8px + hover 阴影、输入/按钮统一圆角与 `transition`、聚焦光圈 `box-shadow: var(--ring)`、`.transport` 改为带边框+背景+圆角的独立工具条（时间码为等宽数字的独立小面板）、复选框 `accent-color` 跟随主题；组件间距整体放大（body padding `16px 18px 44px`、行距 10px、卡片间距 12px）。
- `src/i18n.py`：新增词条 `"延迟（0 = 自动 250ms）"` → `"Delay (0 = auto 250ms)"`（步进器 tooltip）。
- `src/mix_web.py`：`PAGE_TEXT_KEYS` 的 P2 组同步加入 `"延迟（0 = 自动 250ms）"`。
- `tests/test_mix_web.py`：新增 `test_editor_defaults_hotkey_and_stepper`（断言循环默认勾选、`loop: true`、捕获阶段 `window` 监听 + `ev.repeat` + 文本输入放行、音效参数初值中性、`.step` 包裹与 `mkBtn(-1)/(1)`、按钮 `tabIndex = -1`）；文案抽查列表加入新词条。

**验证**

- `py_compile src/i18n.py src/mix_web.py` 通过；内联 JS 
ode --check` 通过。
- `pytest tests/test_mix_web.py tests/test_mix_render.py --basetemp=".pytest_tmp_mix"` → 60 passed；全量（排除脚本式 `tests/test_i18n.py`）→ 174 passed。
- 浏览器实测（pageId 载入 2 轨真实素材）：`loopChecked=true`、`Sloop=true`；每轨 10 个 `.step` 控件，按钮文本 `["−","+","−"]`；回声延迟控件 `value="0"`、tooltip「延迟（0 = 自动 250ms）」、反馈 0、混合 0、压缩阈值 0 / 比率 1；卡片 `radius 12px / shadow 生效`、transport `radius 8px + 背景`、轨道卡 `radius 8px`。
- 全局热键实测：数字框聚焦后按空格 → 播放；再按 → 暂停；文本输入（工程名）内空格不拦截、不触发播放。
- ± 步进器实测：反馈 0 → 0.1（两次 +0.05）；延迟 0 → 10（step 10）；点 + 后焦点仍在数字框。

**未执行 / 待确认**

- 未执行任何 git 操作（P1 + P2 + 盲测工具 + 本轮 UI 精细化均未 commit）。
- 一处取舍待用户确认：空格为全局热键后会「吃掉」下拉框/复选框聚焦时的原生空格行为（仅文本输入放行），若不接受可改回「仅非输入类元素」判定。

---

## 2026-09-29 — P2 听感盲测（压缩 / 回声参数梯度）

> 目的：P2 上线前需要用人耳确认「压缩量、回声强度」的听感偏好，以决定 UI 默认值。做法是不暴露参数、只给盲编码音频，听完再揭盲。
> 设计：复用产线 `mix_render.build_ffmpeg_cmd`（同一 filtergraph，含每轨音量/音质/压缩/回声 + 总线 loudnorm），但**不写历史记录**，避免污染歌曲历史；P2 参数只加在**人声轨**（伴奏中性），符合 DAW 常规用法。

- 新增 `tools/p2_blind_test.py`（离线命令行工具，不接入 UI）：
  - 8 个变体：`C0` 基线（无效果）、`C1/C2/C3` 轻/中/重压缩、`E1/E2/E3` 轻/中/重回声、`X1` 中压缩+中回声。
  - 用固定种子 `BLIND_SEED=20260929` 把「参数组合」映射到盲编码 `V1..V8`；映射只写进《揭盲答案_听完再看.md》，不打印到终端。
  - 副歌定位：把伴奏轨解码为单声道 4kHz PCM，用滑窗平方和取「能量最高的 N 秒」（本例 30s；源 224.3s → 起点 145.5s），首尾各 0.5s 淡变避免硬切爆音。
  - 变体参数做重复性校验（两个变体参数完全相同则报错退出）；文本产物按项目约定写 UTF-8 BOM + CRLF。
- 产物（均在 `outputs/` 下，已被 .gitignore 覆盖，不入库）：`outputs/p2_blind_test/V1..V8.flac` + `盲听指南.md`（含 5 项评分表，不含映射）+ `揭盲答案_听完再看.md` + `ab_report.csv`（客观指标）。
  - 客观指标（`tools/audio_ab_report.py`）：8 个变体 LUFS 集中在 -13.4…-13.9（总线 loudnorm 归一生效），回声变体时长 30.8–31.2s（回声抽头拖尾），压缩变体真峰值随档位单调抬升（-5.1 → -2.3 dBFS，均为 loudnorm 补偿增益所致，仍远低于 -1.5 dBTP 上限）。
  - 轨级复核（人声轨 30s 段，`volumedetect`）：压缩 -24dB/4:1 使均值 -15.9 → -25.8 dB、峰值 0.0 → -10.9 dBFS；-30dB/8:1 进一步降至 -32.6 / -13.0 dB，确认压缩在真实素材上有效（倍率越大衰减越多）。

---

## 2026-09-29 — 多轨编辑器 P2：每轨音效（压缩 + 回声），预览与导出同效

> 背景：P1 补齐了声像/三段 EQ/高通低通后，还缺最常用的两个音效——动态压缩与回声。本轮实现 P2，延续 P1 的核心要求：**编辑页实时预览**与**后端离线渲染**共用同一套参数、同一处理顺序、同一判据，做到「听到什么就导出什么」。
> 设计决策：①仍不改工程契约版本（`version: 1`）——新增 5 个每轨字段全部可选、缺省中性，旧工程 JSON 照常解析；②压缩对齐 Web Audio `DynamicsCompressorNode`，只暴露阈值与比率，attack/release/knee 固定为 20ms/250ms/6dB 前后端一致；③回声对齐 3 条并联延迟线（抽头 fb¹/fb²/fb³），干声直通不衰减；④处理顺序统一为 高通→低通→EQ→声像→**压缩→回声**；⑤关闭判据：压缩 `阈值≥0dB 或 比率≤1`、回声 `混合<0.01`。

- `src/mix_render.py`：
  - 新增 P2 常量：`_COMP_TH_MIN/MAX`（-60…0 dB）、`_COMP_RATIO_MIN/MAX`（1…20）、`_ECHO_DELAY_MIN/MAX`（0…2000 ms）、`_ECHO_FB_MAX`（0.95）、`_ECHO_MIX_MIN/MAX`（0…1）、固定时间常数 `_COMP_ATTACK_MS/_COMP_RELEASE_MS/_COMP_KNEE_DB = 20/250/6`、`_COMP_MIN_LINEAR = 0.001`、`_ECHO_DEFAULT_DELAY_MS = 250`、`_ECHO_TAPS = 3`、`_ECHO_DECAY_FLOOR = 0.001`。
  - `MixTrack` 新增 `comp_th / comp_ratio / echo_delay / echo_fb / echo_mix`（默认全中性 0/1/0/0/0），`_parse_track` 解析并钳制（非法类型回退中性，越界钳制）。
  - 新增 `_comp_filter(track)`：生成 `acompressor=...`。**ffmpeg 的 threshold 是线性幅度而非 dB**，故换算 `10^(dB/20)` 并用 `_COMP_MIN_LINEAR` 兜底（ffmpeg 不接受 0）；阈值 ≥ -0.05dB 或比率 ≤ 1.05 视为关闭（返回空串）。
  - 新增 `_echo_filter(track)`：生成 `aecho=1:1:<延迟×1|×2|×3>:<混合×fb^k>`。实测 ffmpeg `aecho` 的 `out_gain` 会连干声一起缩放，故固定 `in_gain=out_gain=1`，只用 decays 表达回声强度（干声电平不变）；`decay` 取值范围为 (0, 1]，用 `_ECHO_DECAY_FLOOR` 兜底；混合 < 0.01 视为关闭。
  - `_fx_filters` 在声像之后追加压缩、回声，形成「声像→压缩→回声」的完整链路。
- `src/mix_web.py`：`_project_to_json` 输出 5 个音效字段（保证 保存→载入→再保存 幂等）；`PAGE_TEXT_KEYS` 新增 7 个文案键。
- `src/i18n.py`：新增「压缩/阈值/比率/回声/延迟/反馈/混合」英文译文（Compress/Threshold/Ratio/Echo/Delay/Feedback/Mix）。
- `static/multitrack/index.html`：
  - 每轨新增「音效行」（`.fxrow`，第 2 行）：压缩（阈值/比率）+ 回声（延迟/反馈/混合），共 5 个数值输入；首格用 `.fxlab`（与 P1 行同宽 76px）保证两行左对齐。
  - `buildFxChain` 扩展为 `highpass→lowpass→lowshelf→peaking→highshelf→StereoPanner→DynamicsCompressor→回声子图`；回声子图为 `echoIn` 分出干声直通 + 3 条「DelayNode→GainNode」支路汇入 `echoOut`，默认增益 0（等同旁通）。
  - `applyFxParams` 追加压缩（阈值/比率，关闭时回到 0dB/1）与回声（3 抽头延迟 `延迟×k`、增益 `混合×fb^k`）的 `setTargetAtTime` 平滑写入；`stopNodes` 支持 `taps` 数组的逐节点断开。
  - 单轨试听（▶）与统一播放共用同一条音效链；`buildPayload` 输出 5 个音效字段；`applyTrackState` 载入工程后钳制回灌、同步控件显示并刷新节点链。
- `tests/test_mix_render.py`：新增 `test_fx_effect_params_default_and_clamped`、`test_comp_filter_linear_threshold_and_off`、`test_echo_filter_taps_default_delay_and_decay_floor`、`test_fx_filters_p2_order_after_pan`；`test_build_cmd_includes_fx_after_volume` 扩展为覆盖「音量→音质→压缩→回声→adelay」的完整顺序。
- `tests/test_mix_web.py`：`test_save_project_persists_fx_params` 扩展为 P1+P2 往返幂等；文案抽查加 7 键；编辑页源码断言加 `createDynamicsCompressor`/`createDelay`/`fxRow2`/`comp_th`/`echo_mix` 等 10 个 token。
- 验证：`py_compile` 通过；编辑器内联 JS 
ode --check` 通过；`pytest tests/test_mix_web.py tests/test_mix_render.py` 59 项、全量（排除脚本式 `tests/test_i18n.py`）173 项通过。
  - **ffmpeg 语义实测**（源：200ms 幅度 0.8 正弦 + 0.8s 静音）：压缩 `-24dB/8:1` 使整段峰值 -1.9 → -8.0 dBFS、持续段均值 -11.9 → -29.9 dB（约 18dB 压缩量，符合阈值/比率预期，-8.0 峰值来自 20ms attack 的起音瞬态）；回声 `混合0.5/反馈0.5/延迟200ms` 在源静音窗（0.3–0.8s）由 -91.0 dB 提升至 -14.0 dB，抽头确实出现；串联链路中静音窗为 -26.0 dB（= 压缩后的信号再入回声），印证「压缩→回声」顺序。
  - **浏览器实测**（真实 171.6s 素材，2 轨）：音效行 5 控件渲染正常并显示「压缩/阈值/比率/回声/延迟/反馈/混合」；设 -24dB/8:1/300ms/0.5/0.4 后节点参数为 threshold=-24、ratio=8、knee=6、attack=0.02、release=0.25，回声抽头 delay=0.3/0.6/0.9s、gain=0.2/0.1/0.05（= 混合×fb^k，与后端公式一致），P1 节点未受影响（hpf=20 关闭、lpf=22050 关闭、pan=0）；`buildPayload` 携带 5 字段；保存工程→载入工程后控件与参数原样恢复。
  - **端到端渲染实测**：带 P2 参数走页面「渲染」成功产出 `mix_..._mix.flac`，成品 **-14.21 LUFS / 真峰值 -1.50 dBTP**，符合母带目标（压缩+回声未破坏响度归一化）。测试产物（混音记录与目录、工程文件、临时素材、pytest 临时目录）已全部移入系统回收站。

---

## 2026-09-29 — 多轨编辑器 P1：每轨音质控制（声像 + 三段 EQ + 高通/低通），预览与导出同效

> 背景：此前每轨只有音量/静音/独奏，缺少常用 DAW 的单轨音质控制。本轮实现 P1（声像、三段 EQ、高通/低通），要求**编辑页实时预览**与**后端离线渲染**使用同一套参数，做到「听到什么就导出什么」。P2（Echo/压缩）后续再做。
> 设计决策：①不改工程契约版本（仍 `version: 1`）——新增 6 个每轨字段均为**可选、缺省中性**，旧工程 JSON 仍可解析；②声像前后端统一采用 Web Audio `StereoPannerNode` 的等功率立体声算法；③三段 EQ 频率对齐 Web Audio 节点（低架 200Hz / 峰值 1kHz Q=1 / 高架 4kHz），高通/低通为 12dB/oct Butterworth；④参数全部钳制，脏数据不中断渲染。

- `src/mix_render.py`：
  - `MixTrack` 新增 `pan / eq_low / eq_mid / eq_high / hpf / lpf`（默认全中性 0），`_parse_track` 解析并钳制（pan ±1、EQ ±15dB、高通 0–2000Hz、低通 0–20000Hz，非法回退 0）。
  - 新增 `_pan_filters(pan)`：按 StereoPannerNode 规格算法生成 `aformat=channel_layouts=stereo` + `pan=stereo|...`（pan≤0：`outL=L+gL*R, outR=gR*R`；pan>0：`outL=gL*L, outR=R+gR*L`；gL=cos x、gR=sin x），居中（|pan|<0.005）不产生滤镜。
  - 新增 `_fx_filters(track)`：顺序为 高通 → 低通 → 低频架(`bass`) → 中频峰(`equalizer`) → 高频架(`treble`) → 声像；各参数中性时不产生滤镜。
  - `build_ffmpeg_cmd` 把音质滤镜排在 `volume` 之后、`adelay` 之前（无切片轨走 `volume`+fx；有切片轨逐 clip 追加 fx）。
- `src/mix_web.py`：`_project_to_json` 输出 6 个音质字段（保证 保存→载入→再保存 幂等）；`PAGE_TEXT_KEYS` 新增 9 个文案键。
- `src/i18n.py`：新增「音质/声像/低频/中频/高频/高通/低通/分贝/Hz（0 = 关闭）」英文译文（Tone/Pan/Low/Mid/High/HPF/LPF/dB/Hz (0 = off)）。
- `static/multitrack/index.html`：
  - 每轨新增「音质行」（`.fxrow`）：声像滑块（读数 C / L60 / R60）+ 低频/中频/高频/高通/低通数值输入；改动即时写入节点链，**播放中即可听出变化**。
  - 新增 `buildFxChain`（highpass→lowpass→lowshelf→peaking→highshelf→StereoPanner 固定链）、`applyFxParams`（`setTargetAtTime` 平滑，避免爆音）、`applyTrackFx`；`playFrom` 时每轨建链并接到轨增益之前，`stopNodes` 一并断开节点避免堆积。
  - 单轨试听（▶）同样串入该轨音质链（此前只套增益）；`buildPayload` 输出 6 个音质字段；`applyTrackState` 载入工程后回灌参数与控件显示。
- `tests/test_mix_render.py`：新增 `test_fx_params_default_and_clamped`、`test_pan_filters_follow_stereo_law`、`test_fx_filters_neutral_skips_and_active_orders`、`test_build_cmd_includes_fx_after_volume`。
- `tests/test_mix_web.py`：新增 `test_save_project_persists_fx_params`（含往返幂等）；文案抽查加 9 键；编辑页源码断言加 `buildFxChain`/`applyFxParams`/`fxRow`/`lowshelf`/`peaking`/`highshelf` 等。
- 验证：`py_compile` 通过；`pytest tests/test_mix_web.py tests/test_mix_render.py` 55 项、全量（排除脚本式 `tests/test_i18n.py`）169 项通过；ffmpeg 对新增滤镜链（highpass/lowpass/bass/equalizer/treble/aformat/pan）单独校验全部支持。浏览器 + 真实渲染实测（171.6s 素材）：音质行 7 个控件渲染正常；设声像 -0.6 / 低频 +4.5dB / 高通 120Hz 后，播放节点参数为 pan=-0.6、low=4.5、hpf=120、lpf=22050（关闭），工程 JSON 同步携带该 6 字段；走页面「渲染」得到成品，实测**左声道 -13.8dB、右声道 -18.2dB**（声像左偏生效），真峰值 -1.5dBTP 符合母带目标。测试期间产生的混音记录已通过「删除记录」接口移入系统回收站。

---

## 2026-09-29 — 多轨编辑器：主控 transport 加 label + 选区读数与取消选择区

- `static/multitrack/index.html`：
  - 主控行播放按钮前新增 label「播放控制」（`#t-transport`，`Transport`），与其它行 `label.lb` 样式统一。
  - 全局选区行新增「取消选择区」按钮（`#sel-clear`）：清空 `S.sel`、输入框回到 `0 ~ 时长`、重绘波形；播放中取消则从整曲起点重播。
  - 新增选区读数 `#sel-info`（等宽数字）：显示 `起始 <时间码>  时长 <时长>`；无有效选区时显示整曲范围，与 `playRange()` 的播放区间保持一致；由 `redrawAll()` 统一刷新，故拖拽选择、手填起止、载入素材、窗口缩放后都会同步更新。
- `src/mix_web.py`、`src/i18n.py`：新增文案键「播放控制 / 取消选择区 / 起始 / 时长」（编辑页文案仍全部由后端下发，前端零硬编码）。
- `tests/test_mix_web.py`：`test_page_texts_complete` 抽查加入 4 个新键；`test_editor_page_has_transport_playback` 增加 `t-transport`/`sel-clear`/`sel-info`/`updateSelInfo` 断言。
- 验证：`py_compile` 通过；编辑器内联 JS 
ode --check` 通过；`pytest tests/test_mix_web.py tests/test_mix_render.py` 50 项通过。浏览器实测（真实 171.6s 素材）：label 显示「播放控制」、按钮「取消选择区」；无选区读数「起始 0:00.0  时长 2:51.6」、选区 [5,8] 读数「起始 0:05.0  时长 0:03.0」；点「取消选择区」后 `S.sel=[0,0]`、播放范围回整曲、输入框回到 `0~171.599`、读数复位。

---

## 2026-09-28 — 多轨编辑器播放重构：统一 transport（标准 DAW 逻辑）

> 背景：此前编辑器没有「统一播放」——每轨只能各自 ▶ 试听，无法整体听混音，也没有播放头。本轮按标准 DAW 逻辑重构播放，方案见 `Docs/multitrack-playback-plan.md`。
> 设计决策：①所有轨共用一个时钟、一起播/停，只保留一套播放/暂停/停止；②发声按 Solo > Mute > 默认发声（与后端渲染的 `mute` 语义一致），默认都不勾 = 全部发声；③播放即复现编辑结果（裁切/淡变/增益/Mute-Solo 实时生效）；④有全局选区只播选区，无选区播整曲；⑤附加：播放头跟随、空格键播放/暂停、单击波形定位、循环播放；每轨 ▶ 单轨试听保留。

- `static/multitrack/index.html`：
  - UI：母带行上方新增 transport 行——`▶/⏸`（单一播放/暂停）、`■`（停止，回到播放区间起点）、`循环` 开关、时间码 `mm:ss.d / mm:ss.d`；每轨保留 `静音`/`独奏`（默认不勾 = 全部发声）。
  - 播放引擎（`audioCtx` / `scheduleIteration`）：所有轨共用一个 `AudioContext`，每轨按 `clips` 建 `AudioBufferSourceNode`（`offset=clip.in`、`duration=out-in`），**全部使用同一个 `start(t0)`** → 样本级同步；无裁切的轨按整轨调度。首次播放 `fetch('/api/mix/audio')` → `decodeAudioData` 并按 `path` 缓存，解码期间显示「正在解码音频 x/y」。
  - 增益链：`片段淡变包络 → 轨 GainNode → masterGain → destination`；淡变按片段边界生成折线（起点已落在淡入区内时从当前值续接）。Mute/Solo/增益用 `setTargetAtTime` 门控，**播放中切换即时生效，无需重启播放**。
  - 播放头：每轨 canvas 外层包 `wave-wrap`，叠加绝对定位竖线（避免每帧整幅重绘 canvas），由 `requestAnimationFrame` 按音频时钟 `pos = playStart + (ctx.currentTime - t0)` 更新；用「播放代次 `S.gen`」作废旧帧回调，避免重排后出现双 tick 循环（实测推进速率 0.994x）。
  - 循环：`setInterval` lookahead 在每轮结束前 ~0.5s 预排下一轮（
extLoopAt`），播放头按 `pos mod span` 取模；同时清理已播完的节点引用，避免长时间循环时数组膨胀。
  - 交互：波形 `pointerdown → pointerup` 位移 < 4px 视为**单击**（定位播放头，保留原选区），≥ 4px 才生成选区（此前 `pointerdown` 即清空选区）；`Space` 播放/暂停（焦点在 input/select/textarea 时不拦截）；单轨 ▶ 试听与统一播放互斥。
- `src/mix_web.py`、`src/i18n.py`：新增文案键「播放/暂停/停止/循环/正在解码音频/解码失败/播放失败」（编辑页文案仍全部由后端下发，前端零硬编码）。
- `tests/test_mix_web.py`：新增 `test_editor_page_has_transport_playback`（transport 控件 + 引擎/播放头/空格键源码断言）；文案抽查加入 5 个新键。
- 验证：`py_compile` 通过；`pytest tests/test_mix_web.py tests/test_mix_render.py` 50 项、全量（排除脚本式 `tests/test_i18n.py`）164 项通过。浏览器实测（真实 171.6s 双轨素材）：统一播放推进速率 0.994x、两轨播放头位移完全一致、播放中轨增益 -20dB → 0.100、Solo 门控 0/1、Mute 生效；选区 [5,8] 只播该区间并停到 8.0；循环 3.5s 后位置回绕（预排节点 4 个）；停止回起点 0.0；单击波形定位到 42.0s、拖拽生成 [10,40]；空格键播放/暂停且输入框内不触发；未改动的单轨试听仍可用；控制台无报错。

---

## 2026-09-28 — 多轨编辑器：单轨试听（每轨 ▶ 按钮 + 共享播放器）

> 背景：此前编辑器内没有单轨试听，「独奏」只在**渲染时**把其它轨静音（离线），要单独听某一轨只能渲染成品或在分离 Tab 回放。本轮给每轨加 ▶ 按钮，直接试听该轨原始分轨。

- `static/multitrack/index.html`：
  - 每轨标题行新增 ▶ 按钮（`title` 走 i18n「试听本轨」），并新增隐藏的共享播放器 `#solo-audio`。
  - `previewTrack(t)`：播放该轨原文件（`/api/mix/audio?path=`），并用 Web Audio `GainNode` 套用该轨增益（dB→线性，支持 +12dB；`audio.volume` 只能衰减故不采用）；`AudioContext` 延迟到首次点击才创建，以满足浏览器「必须用户手势」的限制。再点同一按钮 = 停止。
  - 按钮符号由播放器 `play/pause/ended` 事件统一刷新（按 `currentSrc` 匹配归属轨）：切换轨道时改 `src` 会**异步**触发 `pause` 事件，若靠手工维护按钮标记会造成多个按钮同时显示 ⏸。重建轨道（载入素材/工程）时自动停止试听。
- `src/mix_web.py`、`src/i18n.py`：文案键新增「试听本轨」（`Preview this track`）。
- `tests/test_mix_web.py`：文案抽查加入「试听本轨」；源码接线断言加入 `id="solo-audio"`、`previewTrack`、`createMediaElementSource`。
- 验证：`py_compile` 通过；混音两文件 49 项、全量（排除脚本式 `tests/test_i18n.py`）163 项通过；浏览器实测——播放推进（`currentTime` 5.8s / 时长 171.6s）、切换轨道仅当前轨显示 ⏸、再点停止、重建轨道自动停、增益换算 +6dB → 1.995（与 10^(6/20) 一致）、控制台无报错。

---

## 2026-09-28 — 多轨混音 M3：工程持久化 + 记录管理 + 分离页内嵌

> 方案与任务拆分见 `Docs/multitrack-editor-plan.md`（第 6 节 M3）。M3 的进度/取消、i18n、changelog 已随 M2 落地，本轮补齐剩余三项：工程持久化、混音记录改名/删除、（C3）分离页内嵌编辑页。
> 关于「历史页表格展示 mix 记录」：按既有约定**不进入歌曲历史页**（该页仅展示生成记录，分离/翻唱记录同样在各自 Tab 管理），改在编辑页「混音记录」列表内提供回放/下载/改名/删除，与分离 Tab 的记录管理方式一致。

- `src/mix_web.py`（新增能力，仍为与 Web 框架解耦的纯函数层）：
  - `save_project(payload, webui_root, name)`：工程先经 `parse_mix_project` 校验归一化，再落盘到 `outputs/mix_projects/<工程名>.json`（UTF-8、缩进 2、`ensure_ascii=False`，同名覆盖）；工程名清洗非法字符 `\/:*?"<>|`、去首尾点/空格、限长 60，为空回退时间戳。
  - `list_projects(webui_root)`：已保存工程清单（名称/路径/保存时间/轨数/时长），按保存时间倒序，损坏 JSON 跳过。
  - `load_project(webui_root, rel)`：读取工程——路径必须解析到 `outputs/mix_projects/` 之下（越界/缺失/非 JSON/校验失败分别返回中文错误），返回 `src` 已归一为相对 webui_root 的规范 JSON，可直接回灌编辑器。
  - `_project_to_json(project, webui_root)`：把校验后的 `MixProject` 序列化为前端可回灌的规范 JSON（增益/响度等已钳制、素材路径相对化），保证「保存 → 载入 → 再保存」幂等。
  - `rename_mix(history_mgr, task_id, new_name)` / `delete_mix(history_mgr, task_id)`：混音记录改名（走 `history.rename_project`，文件级重命名并保留时间戳）与删除（走 `history.delete_project`，整目录移入系统回收站 + 移除该目录全部记录）；记录不存在或删除无匹配时返回 `ok=false`。
- `static/multitrack/index.html`：
  - 新增「工程」区：已保存工程下拉 + 「保存工程」（工程名取「项目名」输入框，留空则后端用时间戳）+ 「打开」；打开工程可在未选素材时直接使用（轨道由工程自带 `src` 还原）。
  - `buildTracks(items)` 改为接受通用轨道列表（素材记录与工程轨道共用一套渲染），新增 `applyTrackState()` 在波形就绪后回填增益/静音/切片/淡变到界面控件（工程载入后可继续编辑并再次保存/渲染）。
  - 「混音记录」列表每项新增「改名」（`prompt` 输入新名）与「删除选中」（`confirm` 二次确认，文件入回收站）。
  - 内嵌适配：`?embed=1` 时标记 `html[data-embed="1"]` 收紧内边距；`syncParentTheme()` 定时读取**父窗口** body 背景亮度决定明暗主题（同源可访问父窗口，跨源异常时回退系统主题），主题变化后重绘波形（波形颜色取自 CSS 变量）。
- `app.py`：
  - 新增 3 个接口：`GET /api/mix/projects`（工程清单）、`GET|POST /api/mix/project`（载入/保存工程）、`POST /api/mix/record`（混音记录改名/删除，`action=rename|delete`）；均带异常兜底，失败返回 `ok=false` + 中文错误。
  - 分离 Tab 末尾新增内嵌多轨编辑器 `gr.HTML`（`iframe src=/static/multitrack/?embed=1`，`elem_id="sep-mix-embed"`）；原「多轨编辑」按钮（新窗口打开）保持不变，两种入口并存。
- `src/i18n.py`：补充「工程/已保存工程/保存工程/打开/工程已保存/工程已载入/改名/新名称/记录不存在/删除失败/删除该混音记录？文件将移入回收站。」等中英词条（编辑页文案仍全部由后端下发，前端零硬编码）。
- `tests/test_mix_web.py`：由 22 项扩到 27 项——新增工程保存/载入往返、非法工程拦截与工程名清洗、清单跳过损坏文件、载入路径越界、记录改名/删除的参数透传与无匹配分支（删除用桩函数，避免污染系统回收站）；文案抽查与源码接线断言同步扩展（3 个新接口、内嵌 iframe、主题跟随）。
- 验证：`py_compile` 通过；`pytest tests/test_mix_web.py tests/test_mix_render.py` 49 项通过；全量 `pytest tests`（排除脚本式 `tests/test_i18n.py`）163 项通过；服务重启后真实 HTTP 冒烟：`GET /api/mix/projects` → `{"ok":true,"projects":[]}`、`GET /static/multitrack/?embed=1` → 200（含 `data-embed` 样式与 `syncParentTheme`）、`GET /api/mix/sources` → 4 组分离素材、非法工程保存 → 中文越界错误、未知记录删除 → `记录不存在`。

---

## 2026-09-28 — 多轨混音 M2：独立编辑页 + Web 接口（端到端打通）

> 方案与任务拆分见 `Docs/multitrack-editor-plan.md`（第 6 节 M2）。承接 M1 的后端渲染接口，补齐编辑与产物闭环。
> 编辑粒度按确认结果实现：**轨间平衡（增益/静音/独奏）+ 全局选区裁切 + 每轨淡入淡出**（不做多切片自由摆放）。

- 新增 `src/mix_web.py`：与 Web 框架解耦的纯函数层（由 app.py 注册为路由，可独立单测）。
  - `resolve_audio(webui_root, rel, must_exist)`：素材/产物路径白名单——必须解析到 `<webui_root>/outputs` 之下且扩展名在音频白名单内，越界/非法一律返回 `None`（防止任意路径被读取或下发）。
  - `compute_peaks(path, buckets)`：波形峰值——用 ffmpeg 解码为 8kHz 单声道 s16le 后分桶求 min/max（与源格式无关，wav/flac 均可），桶数钳制在 `[200, 4000]`，结果按 `(路径, mtime, 桶数)` LRU 缓存（上限 16）避免重复解码。
  - `list_sources(history_mgr, webui_root)`：编辑页素材清单——分离记录 → `sources`（含全部有效轨道），混音记录 → `mixes`；失效文件自动跳过。
  - `page_texts(lang)`：编辑页全部文案经 `i18n.tr` 下发（前端不硬编码界面文案）。
  - `submit_render / task_status / cancel_render`：提交前先做工程校验（非法工程不占用队列）；因 `queue_manager` 只支持按 Task 对象查询，本模块维护 `task_id → Task` 的 FIFO 注册表（上限 20）；worker 内部业务失败（队列仍为 completed）对前端统一呈现为 `failed + error`。
- 新增 `static/multitrack/index.html`：单文件自包含（CSS/JS 内联）多轨编辑页。
  - 每轨独立 canvas 波形（包络 + 裁剪区压暗 + 全局选区高亮）、增益推子（-30~12 dB）、静音/独奏、淡入/淡出秒数、「裁为选区 / 恢复整轨」。
  - 选区可在任意轨波形上拖拽（所有轨共用同一时间轴），也支持手填起止秒；「应用选区到全部轨 / 恢复全部整轨」批量操作。
  - 独奏在前端折算为 `mute`（有独奏轨时其余轨置静音），后端契约保持不变；无切片但设了淡变时自动物化为整轨切片，让淡变对整轨同样生效。
  - 渲染完成后内嵌播放器试听 + 下载；下方「混音记录」列表可回放/下载历次成品；渲染支持取消；波形逐轨串行解码，避免并发拉起多个 ffmpeg 争抢磁盘。
- `app.py`：
  - `_register_custom_routes` 内新增静态页路由 `/static/multitrack/`（含无尾斜杠，`Cache-Control: no-store`）与 6 个 JSON 接口：`GET /api/mix/sources|peaks|audio|status`、`POST /api/mix/render|cancel`；全部 `insert(0, ...)` 抢在 Gradio 路由之前。
  - 分离页新增「多轨编辑」入口按钮（纯前端事件 `js=window.open(...)`，不做后端往返），中英文案随语言切换。
  - 脚本版本号不变（未改 `app.js`，编辑页为独立静态页，无需处理 `?v=N`）。
- `src/history.py`：`_PROJECT_DIR_PREFIXES` 新增 `mix_`；新增 `_DERIVED_DIR_PREFIXES`（记录类型 → 产物目录前缀），`_derived_dir_for` 改为查表，使混音记录删除时其 `mix_<ts>/` 目录可整目录回收。
- `src/i18n.py`：补充「多轨编辑」及编辑页 39 条中英词条。
- 新增 `tests/test_mix_web.py`：22 项测试（路径白名单/MIME/峰值分桶与缓存/素材清单过滤/提交与状态各分支/注册表淘汰/文案完整性/app.py 路由与入口源码断言/编辑页接口引用/**真实端到端**：提交→队列渲染→产物落盘→写 mix 历史→目录可整目录回收）。
- 验证：`py_compile` 通过；`pytest tests/test_mix_web.py tests/test_mix_render.py` 41 项通过；全量 `pytest tests`（排除脚本式 `tests/test_i18n.py`）155 项通过；服务重启后对 6 个接口做真实 HTTP 冒烟：静态页 200 `text/html` + 
o-store`、`sources` 列出 4 组分离素材、`peaks` 400 桶/时长 171.6s、`audio` 200 `audio/wav`、越界路径 404、非法工程/未知任务/未知取消均返回 `ok=false` + 中文错误。
- 未完项（M3）：Tab 内嵌（C3）、编辑工程持久化为可再次打开的项目、历史页表格展示 mix 记录（当前混音成品的回放/下载在编辑页内完成）。

---

## 2026-09-28 — 多轨混音 M1：后端渲染接口（工程 JSON 契约 + ffmpeg filtergraph）

> 方案与任务拆分见 `Docs/multitrack-editor-plan.md`（第 6 节 M1）。本轮仅做纯后端能力，不碰前端。

- 新增 `src/mix_render.py`：多轨混音渲染模块（与 Gradio 解耦，可独立单测）。
  - `parse_mix_project(payload, webui_root)`：工程 JSON 校验与归一化——版本号必须为 1；轨数 1~8；素材路径必须解析到 `outputs/` 之下且文件存在（防任意路径喂给 ffmpeg）；增益钳制 `[-30, 12] dB`；母带响度目标钳制 `[-30, -5] LUFS`、真峰值 `[-6, -0.1] dBTP`；非法类型回退默认值；非法切片（`out <= in`）丢弃、`start < 0` 回退 0、淡变超过切片时长时整体回退 0；全部静音时报错。
  - `build_ffmpeg_cmd(project, out_path)`：**纯函数**（不做任何 IO）。每轨链 `atrim → asetpts=N/SR/TB → afade → volume → adelay(all=1)`；空 `clips` = 整轨；同一素材多切片自动 `asplit` 展开（一个输入 pad 只能被消费一次）；多段落 `amix=inputs=N:normalize=0:dropout_transition=0`；末尾 `loudnorm=I=..:TP=..:LRA=11.0`；输出按扩展名选无损编码（`.flac`→flac、`.wav`→pcm_s24le）。
  - `render_mix(project, out_path, cancel_event, progress_cb)`：ffmpeg stderr 重定向到临时文件（避免长音频写满管道死锁），轮询期间按 `cancel_event` 协作取消（杀子进程并抛 `TaskCancelledError`）；失败统一抛 `MixProjectError`（含 stderr 末 5 行）。
  - `mix_worker(_task, payload_json, webui_root, out_dir, project, history_mgr)`：队列 worker，产物 `<项目名>_<时间戳>_mix.flac`，写 `record_type="mix"` 历史记录（`stems` 仅一条「混音成品」，供历史页回放/下载）；业务失败返回 `{"ok": False, "error": ...}`，取消则抛出交由队列标记 CANCELLED。
- `src/queue_manager.py`：`TaskType` 新增 `MIX = "mix"`。
- 新增 `tests/test_mix_render.py`：20 项测试（解析校验/路径白名单/钳制回退/filtergraph 各分支/端到端真实渲染 + `volumedetect` 复核 `max_volume ≤ -1.2 dB`/worker 写历史/坏 JSON 返回业务错误），全部通过；渲染产物经 `loudnorm` 归一后真峰值达标（无削波）。
- 未完项（M2）：前端预载分轨与工程保存、`mix_` 产物目录的改名/删除/整目录回收支持（`history._PROJECT_DIR_PREFIXES`）、历史页展示 mix 记录。

---

## 2026-09-27 — 播放器缩放补齐（PlayerZoom 遗漏 5 个播放器）

- `static/js/app.js`：`PLAYER_IDS` 补入 `history-stem-audio`（歌曲历史「轨道回放(分离/翻唱)」）、`lib-stem-preview`、`lib-ref-preview`、`cover-ref-preview`、`cover-acc-preview`（分离/翻唱页各处「试听」）。
  - 此前仅登记 `gen-audio` / `history-audio` / `sep-audio-*` / `cover-audio-*` / `sep-history-audio-*` / `cover-history-audio-*`，上述 5 个一直无缩放条。
- `app.py`：脚本引用 `app.js?v=11` → `v=12`（避免浏览器命中旧缓存）。
- 说明：空播放器（未加载音频）不渲染波形容器，本就没有缩放条，属预期行为；「全都没有缩放」的疑因是页面缓存旧 JS，需强刷或重启前端后刷新。

---

## 2026-09-27 — 参考段优化产线化（P5C「智能挑参考段」，默认开启可关）

> 背景与结论见 `Docs/optimization-plan-cover-quality.md` 3.5。P5 方案 A（换分离算法，B2）
> 判定不接入；P5C（参考段从"能量最高 10s"改为"人声主导度最高 10s"）盲听有效，产线化。

- **翻唱 Tab 新增「参考段」下拉（默认「智能 (推荐)」）**：`smart` 智能挑段 / `energy` 能量最高段（旧行为）/ `full` 整曲不裁剪（等价关闭本优化）。中英文文案齐全，随语言切换刷新。
- `voice-tools/worker.py`：`_pick_active_ref_segment` 拆出 `_block_rms`/`_max_window`/`_trim_ref`/`_smart_dominance_win` 并新增 `mode` 与 `ref_acc`（配对伴奏）参数；`_convert` 末尾新增 `ref_acc`/`ref_mode`（非法值回退 smart）；`/api/convert` 透传两参。
  - `smart` 仅在拿得到配对伴奏时按"人声 RMS − 伴奏 RMS"挑段；伴奏缺失 / 不可测 / 块数差 > 2（非同一次分离）时**回退能量最高段**，保证默认不劣化。
- `src/voice_client.py`：`convert()` 新增 `ref_mode`/`ref_acc` 透传 + 白名单钳制（非法回退 smart）。
- `src/voice_ui_handlers.py`：`cover_worker` 透传 `ref_mode`/`ref_acc`。
- `app.py`：新增 `_voice_ref_pair_acc()`（参考干声来自分离记录时回传同一次分离的伴奏轨）；`on_voice_cover` 新增 `ref_seg_mode` 入参，仅「从分离人声选择」来源携带配对伴奏。
- `src/i18n.py`：新增「参考段 / 智能 (推荐) / 能量最高段 / 整曲不裁剪」及说明文案共 5 条英译。
- **修复（Bug）**：`_convert` 内"源曲伴奏"局部变量曾复用入参名 `ref_acc` 并覆盖，导致 smart 实际拿到**源曲**伴奏（时长不匹配即静默回退，或偶尔选错段）；改名 `src_acc`，并加源码级回归断言防复发。
- 测试：`tests/test_voice_ref_segment.py` 新增 smart/energy/full 四态测试与 `_convert` 源码级回归断言（含"入参不得被覆盖"）；`tests/test_voice_client.py` 新增 `ref_mode` 透传/回退测试。断言改用"已知区域手工裁剪基准"比对，不再依赖合成源绝对电平。两文件 28 项全通过。

---

## 2026-09-26 — 翻唱音质 P0~P4（量化脚手架 / 参数扫描 / DSP 修形 / few-shot 微调 / 第三方 BWE，均无产线改动）

> 目标：解决「音色翻唱」听感"电子音/金属感"。方案见 `Docs/optimization-plan-cover-quality.md`。
> 结论：P0/P1/P2 改动保留，**P3 微调与 P4 AP-BWE 经盲听判定无效**——P3 代码已全部回退，P4 未接入。

**P0 · 量化脚手架（保留）**

- 新增 `tools/audio_ab_report.py`：单文件（stdlib + 系统 ffmpeg），输出积分响度/真峰值/LRA/谱质心/谱滚降/谱平坦度/分带占比/削波代理，CSV 为 UTF-8+BOM；`--ref` 提供轨级对比（换嗓人声 vs 源人声）。
- 新增单测 `tests/test_audio_ab_report.py`（16 项）。
- 首轮实测推翻 H1：混音级 >8k/>10k/>12k 分带差仅 +0.2~+0.5 dB（判据 1.5 dB），"高频过度生成"不成立。
- 新增首要怀疑 H8：轨级 LRA 被压 3.7 LU（源人声 10.1 → 换嗓 6.4）。

**P1 · 参数扫描（保留）**

- 新增 `tools/cover_ab.py`：绕过 UI 直传参数、复用已运行 worker、复用同一次分离，产物落 `outputs/ab_test/<label>/` 并存参数 sidecar。
- `worker.py` 新增 `cfg_rate` / `ref_sec` 参数并补 `--inference-cfg-rate`；`src/voice_client.py` 透传 + 钳制；补单测。
- S1/S2/S3 三轮扫描（steps / cfg / ref 长度）**三条原假设全部反向**：加大步数、加大 cfg、缩短参考只会更闷。
- 最优组合 **steps 40 + cfg 0.9 + ref 10s**；`cfg 0.9` 已采纳为新默认（`worker.py` / `voice_client.py` / `cover_ab.py`）。
- 真实退化定位为 **over-smoothing**（谱滚降 6800~7900Hz vs 源 8950；谱平坦度 0.10 vs 源 0.197）+ **LRA 压缩** + 输出真峰值越界。

**P2 · 前端 DSP 修形（保留）**

- `worker.py` 混音链新增 `hf_enhance`（0~4，0=关）：ffmpeg `aexciter` 做谐波激励，**挂在 loudnorm 之前**，避免破坏其 TP=-1.5dB 承诺。
- `voice_client.py` 透传 + 钳制；`cover_ab.py` 新增 `--hf-enhance`；`tests/test_voice_client.py` 补参数透传/钳制/默认值断言。
- 方向修正：原 D2「高频搁架压制」方向错误（H1 已推翻），改为反向的「高频细节补偿」。
- 3 档扫描（0.5/1.0/2.0）指标单调有效，但**用户试听后判定"都不行"** → 根因在换嗓模型自身，转入 P3。

**P3 · few-shot 微调（已回退，❌ 判定无效）**

- 新增 `tools/ft_prep_segments.py`（保留）：用 ffmpeg silencedetect 求语音区间补集，把长人声切成 5~12s 短语片段，切点落在静音边界。实测 K歌之王人声 224.3s → 17 段（5.88~12s，合计 183s）。
- 预取训练缺失权重到 `seed-vc/checkpoints`：`myshell-ai/OpenVoiceV2 → converter/checkpoint.pth`(125MB) + `converter/config.json`、`Plachta/Seed-VC → se_db.pt`(98MB)；训练起点用 `--pretrained-ckpt` 指向本地已有 DiT v2 权重（782MB），省一次下载。
- 训练：100 步档 82s 完成；50 步档取中间存档；300 步档 **loss 自 120 步起 NaN（发散）**，权重与目录已删除。
- 训练不稳定根因：`train.py` 内 lr 硬编码 1e-5，对 17 条小数据偏高（100 步档 loss 已从 0.523 单调升到 0.586）。
- A/B（源曲 漠河小猫 var1、参考 K歌之王人声，仅换权重）：100 步档谱平坦度 0.110→0.157、LRA 6.5→7.2（有改善），但谱质心 3862→4645、谱滚降 7969→9790（超源）+ 削波计数 21→156（变差）；50 步档几乎无改善。
- **用户盲听判定微调无效** → 回退 `worker.py` / `src/voice_client.py` / `tools/cover_ab.py` / `tests/test_voice_client.py` 中的 `ft_checkpoint` / `ft_config` 透传与绑定；全仓已无 `ft_*` 残留引用。

**P4 · 第三方 BWE（AP-BWE 24kto48k，❌ 判定无效，未接入）**

- 选型：AP-BWE（MIT 代码+权重，24kto48k 权重 119 MB，零新增 Python 依赖，3080 上整首 256s 仅 1.9s）。FlashSR 因许可证缺失（jakeoneijk 版）或 8 kHz 带限过损（onnx tiny 版）不采纳。
- 落地：`git clone --depth 1` → `voice-tools/third_party/AP-BWE` + HF `rsxdalv/AP-BWE` 的 `config.json`/`g_24kto48k.ckpt` → `checkpoints/24kto48k/`；`.gitignore` 新增 `voice-tools/third_party/`（第三方 clone 与权重不进主仓）。
- 未改任何业务代码：用仓库自带 `inference/inference_48k.py` 离线对已落盘换嗓输出做 BWE，再用 `worker.py` 同款混音链生成成品做单文件 A/B。
- 轨级：谱滚降 7969→9653 Hz（回到并超过源 8950）、谱质心 3862→4780 Hz（补高频有效）；但**谱平坦度 0.110→0.305（高于源人声 0.197 达 55%）**，重建的超高频为类噪声/非谐波成分（与训练域 VCTK 英文朗读语音、非歌声一致）；LRA 6.5→6.6 对 H8 零帮助；位深被官方脚本写死 PCM_16。
- 混音级：谱质心 2681→3019、滚降 5911→6518、谱平坦度 0.096→0.185，**分带占比完全一致**（伴奏主导）。
- **用户盲听判定"没听出太大区别"** → P4 终止，不接入 `worker.py`；P0~P4 结项。

**验证**

- 每阶段：`py_compile` 编译检查 + 全量 pytest（`--ignore="tests/test_i18n.py"`）。P3 回退后 **107 passed**。
- 训练/推理均实测跑通（GPU 独占，训练完即释放；worker 重启以加载新代码）。
- 未执行：`app.py` 音色库绑定（随 P3 取消）、`app.py` 音质档控件（P0~P4 均未达"用户盲听可接受"，不落 UI）、commit / push。

---

## 2026-09-25 — 一致性优化（历史页行号错位 / 播放器同步 / 类型词中文化 / 删除确认弹窗）

> 整体一致性审查中发现并修复 1 个严重 bug + 3 处不一致。

**Bug 修复**

- 【严重】历史页行号错位：`app.py` `_load_history_entry` 与 `on_history_next_page` 内部取数用全量 `to_dataframe_rows()`，而表格显示用 `record_types=("generation",)` 过滤后行集——存在分离/翻唱记录时两者顺序错位，点击表格中的生成记录实际选中 separation 记录（实测：点击 var3 行后改项目名，误改了分离目录）。两处均补过滤参数修复；`tests/test_history_filter.py` 源码断言同步从 2 处更新为 4 处。

**一致性优化**

- 历史页 4 个回调（删除选中/清空历史/改项目名/删除项目）outputs 补播放器三元组（试听/轨道回放）：删除/清空后播放器同步清空（不再指向已删除文件），改名后按记录新路径重填（对齐分离/翻唱页 on_voice_task_rename/delete 行为）。
- 分离源/翻唱源下拉类型词中文化：`rec.record_type` 原样拼接（中文界面显示英文 "generation/cover"），改经 `_QUEUE_TYPE_LABELS` + tr 翻译；i18n 补 "翻唱": "Cover" 词条。
- 三处"删除项目"按钮（历史页/分离页/翻唱页）加前端确认弹窗（双语文案），取消则中止回调不触发删除。实测确认 Gradio 5.x 中 js 返回 false 不能阻止 fn 执行，必须 throw 中断。

**验证**

- 全量 pytest 81 passed；i18n 自检 17 通过；py_compile 通过。
- 浏览器 E2E：删除项目弹窗取消路径（记录不动）与确定路径（3 条同目录记录全删 + 整目录真实入回收站）均通过；改项目名后 3 条记录 project/15 个文件同步更新，播放器按新路径重载音频；行号映射修复后选中行与显示行一致。

---

## 2026-09-25 — 端到端测试修复（回收站失效 / 中文产物名乱码 / 回调索引）

> 文件管理重构后的 T1-T7 真实任务全链路测试（生成→改名→分离→翻唱→删除→上传留存）中发现并修复 5 个 bug。

**Bug 修复**

- 【严重】回收站从未真正生效：`src/history.py` 中 Win32 API 名误写 `SHFILEOperationW`（正确为 `SHFileOperationW`，仅 SH 大写），ctypes 抛 AttributeError 被 except 吞掉、静默回退为 unlink 直接删除——所有"移入回收站"操作实际均在直删文件，违背可还原约束。修复后整目录经 SHFileOperationW 真实入回收站（已验证回收站内条目含原路径信息）。
- 中文项目名产物乱码：audiocpp_cli.exe 对非 ASCII `--out` 路径落盘乱码（"测试曲"→"娴嬭瘯鏇"）且校验失败。`src/backend_gguf.py` 改为 CLI 阶段使用 ASCII 安全名（目录名 song_<ts>），成功后由 Python（Windows Unicode API）重命名 wav/abc 为最终中文项目名。
- 生成完成回调索引笔误：`app.py` 批量结果 5 元组 (tid, fname, dir, result, stems) 误按 [2] 取 result / 按 4 元组解包，导致 "WindowsPath has no attribute abc_score" 与 "too many values to unpack"。
- 分离 Tab 源下拉不刷新：`tab_sep.select` 遗漏 sep_src_history 刷新（翻唱页有、分离页漏），新生成歌曲需刷新页面才能作为分离源。
- 【测试加强】`tests/test_history_recycle.py`：此前仅断言"文件消失"（fallback 直删也能满足，故拼写 bug 溜过），现断言 `_delete_to_recycle` 主路径必须返回 True（文件级 + 目录级）。

**验证**

- 全量 pytest 80 passed；i18n 自检 17 通过；改动文件 py_compile 通过。
- E2E 真实任务：生成（中文项目名四件套正确落盘）→ 历史改名（文件+db 同步）→ 分离（separations_<ts> + 项目名沿用）→ 翻唱（cover_<ts> 四轨）→ 历史页/分离页删除项目（db 移除 + 整目录真实入回收站，回收站条目含原路径）→ 上传留存（uploads/<源名>_<ts>_sep_src.wav 精确副本）全部通过。

---

## 2026-09-25 — 文件管理整体重构（项目名/目录命名/上传留存 + SQLite 历史库）

**命名规范（定稿）**

- 生成：`outputs/song_<ts>/`，文件 `<项目名>_<ts>[_varN].wav/.abc/.txt`（空项目名则以 `<ts>` 开头）；生成页新增"项目名 (可选)"输入框。
- 分离：`outputs/separations_<ts>/`，文件 `<项目名>_<ts>_<类别>.wav`（类别 = vocals/accompaniment/drums/bass/other）；项目名自动取自源（历史记录 project 字段优先，其次文件名解析，无结构取文件主干）。
- 翻唱：`outputs/cover_<ts>/`，文件 `<项目名>_<ts>_<类别>.wav`；项目名自动 = 源项目名_音色名（app 层拼接传入）。
- 上传统一留存 `uploads/`：`<源文件名>_<ts>_<类别>.<ext>`（类别 sep_src/cover_src/dry_ref/transcribe），同类别 md5 内容去重；上传源副本留存不再拷入产物目录。
- task_id 与文件名解耦（目录名固定前缀+ts，不含项目名）：改名只动文件不动目录，规避记录路径级联更新。

**项目级管理**

- 历史页：表格新增"项目名"列（7 列）；选中记录可改项目名——仅替换文件名项目名段（保留时间戳/_varN 后缀），同步记录 project/audio_path/abc_path/stems[].path（绝对/相对两种形态兼容）；新增"删除项目"——整目录移系统回收站，同目录批量变体记录一并移除，安全限定 outputs 下 song_/separations_/cover_ 单层前缀目录。
- 分离页/翻唱页：任务历史区各新增"新项目名 + 改项目名 + 删除项目"操作行（与翻唱页仅保留选择功能的管理移位设计衔接）。

**存储引擎迁移（history.json → history.db）**

- `HistoryManager` 重写为 SQLite 持久化：WAL 模式 + synchronous=NORMAL（PRAGMA），一行一条记录，自增 id 保持插入顺序（list_all 按 id 倒序 = 最新在前，行为与 JSON 版一致）；stems 以 JSON 文本列存储，读写自动序列化。
- 所有写操作（insert/update/delete）在 RLock + `with conn` 事务中执行，异常自动回滚；新增 close()/__del__ 释放句柄（Windows 下避免锁文件）。
- 公共接口签名不变（append/list_all/get/set_status/delete/rename_project/delete_project/clear/auto_prune/to_dataframe_rows/prune_missing），app.py 与 voice_ui_handlers.py 零逻辑改动，仅构造参数 history_file → db_file。
- 数据清空重置：旧 history.json 备份为 history.json.bak_20260925 后置空；outputs/ 与 voice-tools/dry_uploads/ 清空（均无残留数据）。

**测试与验证**

- 新增 `tests/test_history_project.py`（8 项）：sanitize_project 清洗/长度/空值、rename_project 项目名段替换与 _varN 保留、空项目名剥离、stems 相对/绝对路径同步、非规范文件不动、delete_project 记录移除与非项目目录拒绝、app/handlers 源码断言（目录前缀/项目名输入框/persist_upload）。
- 更新 test_history_filter.py（task_id 列索引 [5]→[6]）、test_voice_handlers.py（目录命名断言改 separations_<ts>/cover_<ts> 单层 + project 透传）、test_history_recycle.py / test_history_filter.py（db 路径 + close 释放句柄）。
- 验证：pytest 80 passed；i18n 自检 17 通过；服务重启（9898）后浏览器实测——生成页"项目名 (可选)"输入框、历史页 7 列表头（共 0 条）+ 改项目名/删除项目按钮、分离页库管理 + 任务历史改名/删除、翻唱页任务历史改名/删除按钮均正常渲染。

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

## 2026-10-06 — 代码审计修复：A 类 Bug 全修 + B 类冗余清理

> 来源 `Docs/code-audit-2026-10-06.md`（逐条复核：删 1 条误报 A11、订正数字）。范围：A1–A10 全部 + B4/B5/B6/B7/B8；未做 B1/B2/B3/C1/C6/C7。

- **A1**（backend_gguf.py）：generate/transcribe 子进程改用看门狗线程——`poll()` 中检测取消/超时即 `kill()`，主循环读到 EOF 后 `process.wait()` 回收；新增宽松总超时 1800s，超时即 kill 并返回超时错误；`cancel()` 同补 `wait()`，消除「循环内阻塞读导致取消失灵」与「kill 后不回收句柄」。
- **A2**（app.py + postprocess.py）：`postprocess_audio` 调用包 try/except，失败记 `logger.exception` 但仍写历史（音频已落盘可查）；`_embed_metadata` 的 `except: pass` 改为 `logger.warning`。
- **A3**（app.py + history.py）：生成全部变体失败（非取消）时调用 `_recycle_output_dir` 回收 `song_<ts>` 目录（委托 `recycle_dir` 走回收站），与 voice 侧行为一致，不再留孤儿目录。
- **A4 / C2**（history.py + app.py）：历史读路径不再触发 `prune_missing`（改由 `auto_prune` 写后统一收尾）；新增 `count_rows` / `task_id_at` / `to_dataframe_rows(limit, offset)` 走 SQL 指定列（不含 lyrics 全文）；历史页四处读改为 COUNT + LIMIT-OFFSET + 单行取 task_id，消除「全表 ×2 + 全记录 stat」。
- **A5**（voice_client.py）：`_run` 结束主动 set done 事件，监视线程按 0.5s 短轮询退出，不再 1800s 空等堆积。
- **A6**（queue_manager.py）：排队任务取消同样写入 `_history` 环形缓冲，与运行中取消一致。
- **A7**（voice_ui_handlers.py + app.py）：`ensure_separation` 透传 `cancel_event`；cover 原唱路径在进入队列前的同步分离阶段登记 `_pending_cancel`，取消按钮此阶段亦立即生效。
- **A8**（app.py）：`_generate_worker` 对 seeds/批量数做防御（空种子补随机、批量数不超过种子数），消除 `seeds[i]` 越界；顺带删除重复的 `batch_count = int(batch_count)`（B5）。
- **A9**（voice_ui_handlers.py）：上传去重先比 size、相同才重算 md5，消除 O(N×文件大小) 全量重算。
- **A10**（mix_web.py）：波形峰值缓存加 per-key 锁 + 双重检查，同 key 并发只解码一次。
- **B4**（voice_ui_handlers.py）：局部 import（shutil/subprocess/history）上提模块顶部，删除 11 处冗余。
- **B6**（history.py）：`rename_project` 逐条 `_update` 合并为单事务 `executemany`。
- **B7**（voice_client.py）：修正 `heal_ffmpeg_check` 自相矛盾注释。
- **B8**：删除根目录 pip 误装产物 `=1.47`。
- **测试**：新增 `test_backend_watchdog.py`/`test_postprocess.py`/`test_generate_defense.py`；扩展 `test_history_filter.py`（SQL 分页/计数/legacy 空类型/prune 时机）、`test_voice_handlers.py`（cancel 透传、去重先比 size）、`test_mix_web.py`（per-key 单次解码）、`test_queue.py`/`test_history_recycle.py`。
- **验证**：全量 `pytest tests/ --ignore=tests/test_i18n.py -q` **219 passed**。
