# YuE2 Music Studio WebUI — 变更日志（Changelog）

> 所有文本为本项目变更记录；最新变更在上。日期格式 YYYY-MM-DD。

---

## 2026-09-28 — 多轨编辑器播放重构：统一 transport（标准 DAW 逻辑）

> 背景：此前编辑器没有「统一播放」——每轨只能各自 ▶ 试听，无法整体听混音，也没有播放头。本轮按标准 DAW 逻辑重构播放，方案见 `Docs/multitrack-playback-plan.md`。
> 设计决策：①所有轨共用一个时钟、一起播/停，只保留一套播放/暂停/停止；②发声按 Solo > Mute > 默认发声（与后端渲染的 `mute` 语义一致），默认都不勾 = 全部发声；③播放即复现编辑结果（裁切/淡变/增益/Mute-Solo 实时生效）；④有全局选区只播选区，无选区播整曲；⑤附加：播放头跟随、空格键播放/暂停、单击波形定位、循环播放；每轨 ▶ 单轨试听保留。

- `static/multitrack/index.html`：
  - UI：母带行上方新增 transport 行——`▶/⏸`（单一播放/暂停）、`■`（停止，回到播放区间起点）、`循环` 开关、时间码 `mm:ss.d / mm:ss.d`；每轨保留 `静音`/`独奏`（默认不勾 = 全部发声）。
  - 播放引擎（`audioCtx` / `scheduleIteration`）：所有轨共用一个 `AudioContext`，每轨按 `clips` 建 `AudioBufferSourceNode`（`offset=clip.in`、`duration=out-in`），**全部使用同一个 `start(t0)`** → 样本级同步；无裁切的轨按整轨调度。首次播放 `fetch('/api/mix/audio')` → `decodeAudioData` 并按 `path` 缓存，解码期间显示「正在解码音频 x/y」。
  - 增益链：`片段淡变包络 → 轨 GainNode → masterGain → destination`；淡变按片段边界生成折线（起点已落在淡入区内时从当前值续接）。Mute/Solo/增益用 `setTargetAtTime` 门控，**播放中切换即时生效，无需重启播放**。
  - 播放头：每轨 canvas 外层包 `wave-wrap`，叠加绝对定位竖线（避免每帧整幅重绘 canvas），由 `requestAnimationFrame` 按音频时钟 `pos = playStart + (ctx.currentTime - t0)` 更新；用「播放代次 `S.gen`」作废旧帧回调，避免重排后出现双 tick 循环（实测推进速率 0.994x）。
  - 循环：`setInterval` lookahead 在每轮结束前 ~0.5s 预排下一轮（`nextLoopAt`），播放头按 `pos mod span` 取模；同时清理已播完的节点引用，避免长时间循环时数组膨胀。
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
- 验证：`py_compile` 通过；`pytest tests/test_mix_web.py tests/test_mix_render.py` 41 项通过；全量 `pytest tests`（排除脚本式 `tests/test_i18n.py`）155 项通过；服务重启后对 6 个接口做真实 HTTP 冒烟：静态页 200 `text/html` + `no-store`、`sources` 列出 4 组分离素材、`peaks` 400 桶/时长 171.6s、`audio` 200 `audio/wav`、越界路径 404、非法工程/未知任务/未知取消均返回 `ok=false` + 中文错误。
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