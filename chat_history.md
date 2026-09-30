# 开发会话记录 — 2026-09-22

本次会话围绕 yue2-webui 陆续完成以下功能（均已浏览器端到端验证）：

## 1. 批量变体文件布局扁平化
- 批量生成的所有变体文件统一写入单个任务目录 `outputs/<时间戳>_<id>/`，不再建 `var1/`、`var2/` 子目录
- 变体文件命名为 `时间戳_varN`（如 `20260922_215507_song_var1.wav/.mp3/.abc/.txt`）
- `backend_gguf.py` 的 `build_command`/`generate` 新增 `output_name` 参数，允许同一目录下用不同文件名区分变体（默认仍用目录名，单文件生成行为不变）
- 删除了 `batch.zip` 打包逻辑，批量模式下载槽位返回 `None`

## 2. "使用上一次" 输入恢复功能
- 风格描述、歌词、ABC 乐谱三处输入框下方右对齐各加一个"使用上一次"小按钮（`elem_classes="last-btn-row"` + 注入 CSS 右对齐、负 margin 贴紧输入框）
- 新增 `yue2-webui/last_inputs.json` 存储 `{style, lyrics, abc, updated_at}`，原子写入（temp + `os.replace`），已加入 `.gitignore`
- 保存时机：`_generate_worker` 和 `_resynthesize_worker` 校验通过后、生成开始前自动写入**原始输入**（含 `//` 注释行）；重新合成也会更新
- 无历史内容时原样返回当前输入，不抛任何异常（曾有 `raise gr.Warning` 导致报错，已去掉）
- `on_cot_change` 现在同时更新 ABC 输入框和其按钮的可见性（off 模式下按钮同步隐藏）

### 已知问题与处理
- 歌词段落拖拽卡片监听 `input` 事件，Gradio 程序化回填不触发 DOM input → 在 `app.js`（v=10）给歌词 textarea 打 value setter 补丁，回填后卡片自动刷新
- 验证测试的生成会覆盖 `last_inputs.json`，曾把用户最近一次内容顶掉 → 从 history.json 恢复过一次；今后测试后需恢复

## 3. 批量变体独立随机种子
- 批量生成时每个变体独立 `random.randint(0, 2**31-1)` 抽取种子，不再 `base_seed + i` 递增
- 固定种子开关只作用于单次生成；种子回填显示变体1的种子
- 种子列表在 `on_generate` 中生成并经队列传给 worker（worker 参数 `seed` → `seeds`）
- 「批量生成数量」提示改为"每个变体使用独立随机种子"

## 4. 变体选择报错修复
- `on_variant_select` 找不到标签对应数据时（页面刷新 State 重置、选择器重置瞬间触发 change）原来抛 `gr.Error("变体不存在")`
- 改为返回 `gr.update()` 空更新保持当前显示，任何情况下不再报错

## 提交
- `995fa5d` Flatten batch variant layout, restore last inputs, and randomize variant seeds（本地，未推送）
- 待提交：`on_restore_last` 去掉异常、`on_variant_select` 去掉 gr.Error 两处修复

## 调试备忘
- 验证种子是否随机：`grep -a "seed=" server.log` 看 CLI 命令里的 `--request-option seed=...`
- 变体切换验证：radio 是真实 `<input type=radio>`，value 即标签文本，用 JS `inp.click()` 可靠切换；验证播放器时长与 MP3 链接文件名
- Bash 工具 cwd 会漂移，命令前确认 `pwd`

---
## 2026-09-24 09:38 阶段3文档（音色工坊）

完成阶段3文档工作，含：
- Docs/setup.md 新增第6节音色工坊安装章节（特性/显存、install_voice.bat、[voice]段配置、命令行冒烟、WebUI操作、参考音色库）+ 第7节补充FAQ。
- README.md 中英文同步更新：功能特性新增音色工坊、界面结构新增可选Tab说明、目录结构新增 voice-tools/voice_client/voice_ui_handlers、安装说明。
- Docs/changelog.md 新建，记录音色工坊与README双语化变更。
编码验证：三文档均 UTF-8 BOM + CRLF（无游离LF）。
未执行：git commit/push（需用户批准）；音色工坊真实推理端到端（需独立venv+Seed-VC，待安装）。

---
## 2026-09-24 09:53 前端冒烟测试（音色工坊）

- 启动服务（根 venv，端口 9898）：首次 17.6s HTTP 200，启动日志无错误。
- 浏览器实测（Chrome DevTools MCP）：5 Tab 渲染齐全；音色工坊 Tab 分离/翻唱面板、源双入口、历史下拉（含真实记录）、音色库、半音快捷、扩散步数、伴奏增益均正常。
- **发现并修复 bug**：翻唱模式下「开始翻唱」按钮永远隐藏——on_voice_mode 仅返回 2 个面板 visible，事件 outputs 未含两个执行按钮。修复：on_voice_mode 返回 4 值（app.py L865），voice_mode.change outputs 补 voice_sep_btn/voice_cover_btn（app.py L2018）。
- 修复后重启验证：6.0s HTTP 200，刷新页面后模式切换按钮正确显隐。
- 服务当前保持运行（后台 job-9e108d7a269a4fb2a8a3ca1646063986），可直接浏览器测试。

---
## 2026-09-24 09:56 音色工坊 UI 左右分栏重排

- app.py 音色工坊 Tab 重构为 gr.Row 左右分栏（scale 5:4）：左栏=输入配置（### 输入：功能模式/当前源/历史下拉/上传源 + 分离或翻唱参数区），右栏=执行与输出（执行按钮 + voice_info + voice_files 输出产物）。事件绑定不变。
- 顺带修复：①源上传控件 voice_src_upload 移出分离面板独立显隐（原翻唱模式下切换「上传音频」源时上传控件随分离面板隐藏不可用）；②模式 Radio label 误用「分离轨数」改为「功能模式」（i18n.py 词条同步：Stems→Function mode）。
- 验证：py_compile 通过；test_i18n.py 脚本 15 通过；pytest voice 三套件 18 passed；重启服务 4.6s HTTP 200；浏览器实测分离/翻唱两模式、上传源切换（翻唱模式下上传控件正确出现）均正常。
- 服务保持运行（job-c48aa0e9ce7d43ea9817af5c258ada7e）。

---
## 2026-09-24 10:01 音色工坊 UI 微调（去标题 + 上传组件疑问排查）

- 删除 Tab 内容顶部冗余大标题「### 音色工坊」（voice_md 及 _reg，Tab 名已标识，app.py L1932-1933）。
- 排查「选『从历史记录选择』仍显示上传组件」：当前版本浏览器实测不复现——默认历史源时仅显示历史下拉，上传组件正确隐藏；判断为旧页面缓存（服务重启后未刷新，已知问题）。提醒用户刷新浏览器。
- 验证：py_compile 通过；重启 5.3s HTTP 200；刷新后快照确认标题消失、源切换正常。

---
## 2026-09-24 参考音色双入口显隐改造（翻唱面板）

- 用户反馈「选择翻唱，还是有上传组件」：指翻唱面板中「上传参考干声」与「音色库选择」下拉并存冗余。
- app.py 翻唱参数区重构为双入口：新增 voice_ref_mode Radio（从音色库选择[默认] / 上传参考干声(1-30秒)），音色库下拉默认可见；上传+命名+保存按钮+提示包进 voice_ref_upload_panel 默认隐藏；新增 on_voice_ref_mode 回调 + voice_ref_mode.change 事件绑定。
- i18n.py 新增词条「从音色库选择」(Pick from library)。
- 验证：py_compile 通过；test_i18n.py 脚本 15 通过；重启服务 6.4s HTTP 200；浏览器实测翻唱模式默认仅音色库下拉（上传隐藏），切「上传参考干声」后上传+命名+保存出现、下拉隐藏；源音频区历史/上传切换正常。
- 服务保持运行（job-68f9754fff0e466bae300fd9a914efed）。


---
## 2026-09-24 音色工坊拆分为「音轨分离」+「音色翻唱」两个 Tab

- 用户反馈：翻唱参考干声应可从「人声分离的人声干声历史」选择，而非每次上传；并要求把分离与翻唱功能完全分开、放到两个 Tab。
- app.py 重构：删除原「音色工坊」Tab 及 voice_mode 模式切换（on_voice_mode/visible 面板切换逻辑全部移除），拆为两个平级 Tab：
  - 「音轨分离」Tab：源音频（历史 generation/cover 或上传）+ 分离模式 + 开始分离 + 输出产物（左右分栏 scale 5:4）。
  - 「音色翻唱」Tab：被翻唱歌曲（历史 generation/cover 或上传）+ 参考音色三入口（音色库[默认]/干声历史/上传含命名保存）+ 翻唱参数（半音/扩散步数/伴奏增益）+ 开始翻唱 + 输出产物。
- 新增 _voice_dry_history_choices()：干声历史下拉仅列 record_type=separation 的人声干声记录；on_voice_ref_mode 改三入口显隐；on_voice_cover 参考音色解析优先级=上传→干声历史→音色库；删除未使用的 voice_ref_state 残留。
- i18n.py 新增词条：音色翻唱/从干声历史选择/干声历史选择/### 音轨分离/### 参考音色翻唱/### 被翻唱歌曲/### 参考音色/### 源音频 等。
- 验证：py_compile 通过；test_i18n.py 15 通过；voice 三套件 18 passed；重启 6.7s HTTP 200；浏览器实测两 Tab 独立布局、参考音色三入口互斥切换（音色库默认→干声历史→上传）均正常；服务日志无错误。
- 服务保持运行（job-879d78cf1c964045bf2d34c32621e0e4）。

---
## 2026-09-24 补齐音色工坊模块间衔接（分离 vocals 一键入音色库 + 翻唱 Tab 刷新）

- 用户要求「每个模块既能独立工作也能前后衔接」。
- app.py：
  - on_voice_separate 改为返回第三值 vocals 路径（存入 sep_vocals_state）。
  - 新增 on_voice_sep_save_ref()：把最近一次分离的人声干声存语音色库（voice-tools/refs/）。
  - 音轨分离 Tab 右栏 output 区新增「### 另存为人声参考」区块：sep_ref_name 命名 + sep_save_btn 保存按钮 + sep_save_info 提示。
  - cover Tab 新增 tab_cover.select→刷新 cover_ref_dropdown 音色库下拉，保存后切到翻唱 Tab 自动出现新条目。
- i18n.py 新增词条：另存为人声参考/命名参考干声/保存 vocals 到音色库。
- 衔接闭环：分离 vocals →（一键）音色库 →（切翻唱Tab自动刷新）翻唱参考音色；上传干声也可入库；cover 产物可作新源/再分离。
- 验证：py_compile 通过；test_i18n.py 15 通过；清理残留占用 9898 端口的旧进程(PID 49472)后重启 5.8s HTTP 200；浏览器实测分离 Tab 新增控件正常，日志无错误。
- 服务保持运行（job-5784f6b6d6d64b57a897757f4b87432c）。

---
## 2026-09-24 分离乐器轨入库（素材库）+ 翻唱自定义伴奏 + 修复 cover 分离管线

- 用户问「分离除了人声，乐器/鼓等如何入库留存」；梳理发现：轨道文件虽落盘任务目录但历史只索引人声、音色库只收人声；且 worker._convert 缺分离步骤（整曲直接换嗓+空伴奏混音，e2e 必失败）。
- voice_ui_handlers.py：新增素材库（voice-tools/stems/）：stems_dir/save_stem(名__类型.wav)/list_stems((名称,类型,路径))；类型与 worker 产物键一致（vocals/accompaniment/drums/bass/other）。
- worker.py _convert 修复：补「0) 分离源整曲」步骤——人声轨供 Seed-VC 换嗓（不再整曲换嗓）、伴奏轨供混音；accompaniment 非空时用自定义伴奏替代原曲伴奏；返回新增 separated_vocals。
- app.py：
  - 分离 Tab「另存为人声参考」升级为「保存分离轨到库」：轨道下拉（分离后动态填充，人声在前）+ 命名 + 保存按钮；保存回调按类型分流：vocals→音色库 refs，乐器轨→素材库 stems。
  - 翻唱 Tab 参考音色下方新增「自定义伴奏(可选)」下拉：列素材库乐器轨（排除人声），留空自动用源伴奏；on_voice_cover 增加 custom_acc 参数校验后传 cover_worker。
  - tab_cover.select 同时刷新音色库+伴奏下拉。
- i18n.py：新增 保存分离轨到库/选择轨道/命名/保存到库/已保存到素材库/自定义伴奏(可选)/留空自动使用源伴奏/人声/伴奏/鼓/贝斯/其他；删除废弃的 另存为人声参考/命名参考干声/保存 vocals 到音色库。
- 测试：test_voice_handlers 新增 3 个素材库用例（保存列出/非法类型/旧文件回退），21 passed；test_i18n 15 通过。
- 验证：三文件 py_compile 通过；重启 6.1s HTTP 200；浏览器实测分离 Tab「保存分离轨到库」区块与翻唱 Tab「自定义伴奏(可选)」均正常；日志无错误。
- 衔接闭环新增：乐器轨→素材库→翻唱自定义伴奏（换伴奏翻唱）。
- 服务保持运行（job-774d7c4a3df04935951f60fdd52b3cea）。

---
## 2026-09-24 分离/翻唱多轨落盘 + 历史页轨道查看与回放

- 用户要求：分离的乐器/鼓等产物要落盘，历史可查看和回放。此前历史记录只索引 vocals，其他轨文件只留在任务目录，历史页无法查看/回放。
- history.py：HistoryRecord 新增可选字段 stems(list，兼容旧记录)；_files_for 纳入 stems 轨文件（删除记录时一并回收）。
- voice_ui_handlers.py：新增 _build_stems(products) 把产物整理为 [{label,type,path}]；_record 增加 stems 参数；separate_worker/cover_worker 写历史时记录全部分离/翻唱轨道（人声/伴奏/鼓/贝斯/其他/翻唱成品等）。
- app.py：历史页「试听」下方新增「轨道回放(分离/翻唱)」下拉 + 独立 Audio；选中 separation/cover 记录时按 entry.stems 填充轨道下拉并默认回放第一轨；下拉切换联动回放；无轨道的普通记录自动隐藏（gr.update visible）。
- i18n.py：新增词条 轨道回放(分离/翻唱) 的英文翻译。
- 效果：分离/翻唱的所有产物轨道均落盘（任务目录 derived/sep_*）并写入历史 stems，可在历史页逐轨选择查看与回放；乐器/鼓等从此可留存复用。
- 验证：三文件 py_compile 通过；test_i18n.py 15 通过；voice 三套件 21 passed；重启 5.7s HTTP 200；浏览器实测历史页正常（无 separation 记录时轨道回放区隐藏属预期）；日志无错误。
- 服务保持运行（job-fcc11655405d45fd9276017a71c37527）。
- 待办：真实端到端分离/翻唱验证（首次点「开始分离」懒启动 worker），届时历史页轨道回放下拉可见并可回放。

---
## 2026-09-24 翻唱上传干音轻量人声检测（方案B）

- 用户确认采用方案B：上传参考干声后轻量信号特征判断是否人声（秒级、不占显存、仅提示不拦截）。
- voice_ui_handlers.py：新增模块级 detect_voice(path)（ffmpeg 解码 mono 16kHz PCM）+ _voice_score(x, sr) 纯 DSP 打分：人声频带(80-1000Hz)能量占比 0.65 + 谱平坦度反向 0.35，阈值 0.5；跳过静音帧；异常/短音频返回 None（无法检测）。
- app.py：新增 on_voice_ref_upload_check 回调；上传组件下方新增 cover_ref_check_md 检测提示；绑定 cover_ref_upload.change 触发检测。修复：detect_voice 为模块级函数，导入改为 from voice_ui_handlers import VoiceHandlers, detect_voice。
- i18n.py：新增词条 人声检测: 通过 / 人声检测: 疑似非人声，建议上传清唱干声 / 无法检测。
- tests/test_voice_handlers.py：新增 3 用例（谐波信号判人声/白噪声判非人声/文件不存在返回 None）。
- 验证：py_compile 通过；i18n 15 通过；voice 三套件 24 passed；ffmpeg 全链路（人声样 0.92/粉噪 0.29）；浏览器真实上传实测：check_voice.wav 显示「人声检测: 通过 (p=0.92)」，check_noise.wav 显示「人声检测: 疑似非人声，建议上传清唱干声 (p=0.30)」；测试音频已清理。
- 服务保持运行（job-cbd94f5cfa58417197133203af6f66a0）。
---
## 2026-09-24 干声历史两来源拆除下拉+翻唱参考入口验证

- 用户明确「不要混一起」：干声历史来源拆为两级——先 gr.Radio 选来源（分离历史 / 上传干音），再在对应 gr.Dropdown 选文件，不再聚合到一个下拉。
- app.py：干声历史入口重建为 cover_ref_dry_panel（gr.Column，含 cover_ref_dry_src radio + cover_ref_dry_sep / cover_ref_dry_upload 两个独立下拉）；新增 on_voice_dry_src_mode 回调按 dry_src 显隐对应下拉；choices 拆分 _voice_dry_sep_choices / _voice_dry_upload_choices；on_voice_cover 签名扩为 6 个参考输入，按 (ref_upload, ref_dry_upload, ref_dry_sep, ref_library) 顺序取第一个有效值。
- 一致性修复：cover_ref_dry_src 原用 .select，统一改为 .change（与 cover_ref_mode / cover_src_mode 一致）——gradio5 Radio 用 change。
- voice_ui_handlers.py：dry_dir/save_dry_upload/list_dry_uploads 留存上传干音（voice-tools/dry_uploads/dry_<id>.<ext>）；上传格式白名单 wav/mp3/flac/m4a/ogg(主) + aac/wma；on_voice_ref_upload_check 通过后留存并入 uploads 下拉。
- i18n.py：新增 干声历史来源/分离历史/上传干音/分离历史干声/上传历史干音/不支持格式提示 等词条。
- 验证：gradio_client predict 确认 dry_src(sep)/(upload) 两级显隐正确返回；抓 /config 前端 dependencies 确认三者均以 change 触发、输出含 Column 面板（ref_mode out=[320,321,326]，dry_src out=[324,323]）——Column 显隐在配置层完整可用；此前浏览器点 radio「时灵时不灵」实为 CDP 合成事件无法可靠触发 gradio Radio，非代码 bug，须人工刷新浏览器点击确认。
- 服务保持运行（job-20a236f26d4e41f5a8823e5216376194）。

---
## 2026-09-24 整体检查与测试（全绿）

- 用户要求整体检查代码与流程。逐项执行：
- 编译：全部项目 .py（app/src/tests/voice-tools/worker）py_compile 通过。
- 测试：pytest 49 passed（0 error）+ i18n 15 通过 = 64 项全绿。
  - 修复 tests\test_queue.py：helper 函数 test_task 被 pytest 误收集为 test 报「fixture _task not found」，改名 task_worker（非 test_ 前缀）后队列套件 5 passed。
  - test_i18n.py 为脚本式（模块级 sys.exit），pytest 无法收集，排除后用 python 单独执行 15 通过。
- 服务：HTTP 200，页面 533KB 正常加载；python 进程运行中。
- gradio 配置完整性：/on_voice_cover 10 参数，前 6 个参考输入（source_history/source_upload/ref_library/ref_dry_upload/ref_dry_sep/ref_upload）顺序与函数签名一致；参考解析优先级 上传→上传干音历史→分离干声→音色库；自定义伴奏缺失回退原伴奏。
- 流程复查：worker _convert 始终先分离源曲（行151），再用自定义或原伴奏混音，cover 管线完整；voice_ui_handlers 干音留存（dry_dir/save_dry_upload/list_dry_uploads）与素材库（save_stem/list_stems）逻辑正确。
- 唯一代码改动：tests\test_queue.py 的 task_worker 重命名（不影响行为）。
- 未执行：真实端到端分离/翻唱推理（需 GPU/lazy worker）；git commit/push（需用户批准）。
- 服务保持运行（job-20a236f26d4e41f5a8823e5216376194）。

---
## 2026-09-24 翻唱/分离新增降噪选项（默认关闭）

- 需求：分离出来的人声常带背景噪音，Demucs 是音源分离非降噪，用户要求加可选降噪。已确认：分离页+翻唱页都加，默认关闭手动开启，用 ffmpeg anlmdn。
- worker.py：新增 denoise_audio(src,dst) 助手（ffmpeg -af anlmdn，异常回退原文件不阻断）；_separate/_convert 加 denoise:bool=False——分离时对人声轨降噪替换 vocals，翻唱时对换嗓 converted_vocals 降噪后参与混音；HTTP /api/separate、/api/convert 读取 body denoise 透传。
- voice_client.py：separate/convert 加 denoise 入 payload。
- voice_ui_handlers.py：separate_worker/cover_worker 加 denoise 透传到 voice_client。
- app.py：分离页 sep_denoise、翻唱页 cover_denoise 各一个 gr.Checkbox(label=降噪,value=False)+_reg 语言注册；on_voice_separate/on_voice_cover 加 denoise=False 参数入队；两按钮 .click inputs 追加对应 checkbox，顺序与回调签名一致。
- i18n.py：新增 降噪/开启后对输出人声降噪（中英成对）。
- 验证：py_compile 全过；pytest 49 passed；i18n 15 通过。默认关闭时输出与之前完全一致，降噪失败自动回退原文件。
- 未执行：真实端到端推理（需 CUDA/Seed-VC）；git commit/push（需批准）。

---
## 2026-09-24 音色工坊流程优化（用户：降噪保留源文件 / 不用硬链接）

- 用户审阅流程优化清单后拍板：降噪源文件必须保留、素材库/干音库不用硬链接（保持 copyfile），其余优化项实施。
- history.py：新增 _derived_dir_for（安全判定 sep/cover 专属 derived 目录：仅 separation/cover、形态 outputs/<root>/derived/<kind>_xxx 且在 outputs_root 下）+ _recycle_derived（先逐文件移回收站，再自底向上 rmdir 空目录）；在 delete/clear/auto_prune 统一调用，消除 denoise 孤儿轨/换嗓中间文件/目录堆积。prune_missing：主轨缺失但任一 stem 存在即保留记录。
- voice_config.py：外置魔法数字 6 键（denoise_strength/voice_detect_threshold/detect_sample_rate/detect_frame_len/voice_band/detect_seconds）及 _float/_band 解析；worker denoise_audio 支持 strength；voice_ui_handlers 检测参数抽默认引用。
- worker.py：移除进程级 os.chdir 改 subprocess cwd=seed_dir；换嗓/降噪输出确定性命名（copyfile 为 converted_vocals.wav/_denoised.wav，源文件一律保留）。
- voice_client.py：separate/convert 入口 mode/denoise/数值强校验，非法回退默认并 warning。
- queue_manager.py：cancel 仅注释说明限制——Task 不持 Popen（走 VoiceClient HTTP 调独立 worker），未改终止子进程。
- 验证：py_compile 全过；pytest 49 passed + i18n 15 + test_voice_config 7 通过；抽查确认整目录回收判定与降噪保留源文件。
- 未执行：真实端到端推理；git commit/push（需批准）。

---
## 2026-09-24 复审后修复三项（执行）

- 复审无硬性回归，本null落地3项：
- 1)检测配置生效：voice_ui_handlers _parse_cfg_defaults(cfg=None) 改为接收 load_voice_config() 规范化 dict，缺失键回退默认；detect_voice(path, project_root=None) 传入 project_root 时读 config.cfg [voice]；app.py 传 PROJECT_ROOT 接通。用户改 voice_band/threshold/sr/帧长/时长 生效（与 denoise_strength 口径一致）。
- 2)失败任务 derived 孤儿目录回收：新增 _recycle_created_derived(out_dir)（文件移回收站+空目录 rmdir，不用 rm）；separate_worker/cover_worker 在异常、cancel_event、not result.ok 三分支均回收，try/except 不掩盖原始错误。解决失败/取消任务目录永不清理。
- 3)band 顺序校验：voice_config _band 解析 lo>hi 时交换，非法回退 (80,1000)，防止 band_mask 空致 voice_ratio 恒 0 误判。
- 验证：py_compile 过；pytest 49 passed + test_voice_config 通过 + 复跑 voice 两套件 18 passed；抽查 grep 确认三处改动落地。
- 未执行：队列取消终止子进程（需重构进程句柄）、换嗓文件名白名单（更大改动）、真实e2e、git commit/push（需批准）。

---
## 2026-09-24 移除参考干声的「分离历史」来源

- 用户澄清定位：音轨分离的历史记录属于历史页（与生成历史并列），用途是轨道回放，不应出现在参考干声的选择里。
- app.py：翻唱页参考音色「干声历史」入口简化——删除二级 radio（分离历史/上传干音）与「分离历史干声」下拉（cover_ref_dry_sep）、on_voice_dry_src_mode 回调及事件绑定；面板仅保留「上传历史干音」下拉（cover_ref_dry_upload）。on_voice_cover 签名由 6 参参考输入改 5 参（去 ref_dry_sep），解析优先级：上传 → 上传干音历史 → 音色库。tab_cover.select 刷新 outputs 同步精简。删除 _voice_dry_sep_choices；_voice_dry_history_choices 改为仅聚合上传干音。
- i18n.py：删除废词条（干声历史来源/分离历史/上传干音/分离历史干声），保留「上传历史干音」等在用词条。
- 衔接说明：分离出的 vocals 要用作参考，走分离 Tab「另存为人声参考」存入音色库（既有链路不变）；上传干音检测通过后自动留存 dry_uploads 并入干声历史下拉（不变）。
- 验证：py_compile 过；pytest 49 passed + i18n 15 通过；服务重启探活 HTTP 200（先清理占用 9898 的旧 python 进程）。启动无 NameError，证明无残留引用。
- 未执行：git commit/push（需批准）；浏览器端手动确认需刷新页面。

---
## 2026-09-24 分离/翻唱 Tab 结构最终定稿（用户明确产品逻辑）

- 用户定义结构：1) 分离 Tab：源=生成历史(generation/cover)或上传；新增「分离任务历史」list 可选择回放分离结果。2) 翻唱页：歌曲源=生成历史或上传（不变）；参考干声=分离的人声结果 或 上传干声；新增「翻唱任务历史」list 可选择回放。
- app.py：新增 _voice_task_history_choices(record_type)（value=task_id）、_voice_task_stem_choices(task_id)（stems 全轨优先，无则回退主轨）、on_voice_task_history_pick(task_id)（填轨道下拉+播第一轨）。
- 分离 Tab 右栏新增「### 分离任务历史」区块：选择分离任务 Dropdown + 回放轨道 Dropdown + 回放 Audio；tab_sep.select 刷新。
- 翻唱页右栏新增「### 翻唱任务历史」区块：选择翻唱任务 Dropdown + 回放轨道 Dropdown + 回放 Audio；tab_cover.select 一并刷新。
- 参考干声恢复两来源：干声历史面板内 radio「从分离人声选择/从上传干声选择」→ 各自下拉（_voice_dry_sep_choices 列 separation 记录 vocals；_voice_dry_upload_choices 列 dry_uploads）；恢复 on_voice_dry_src_mode；on_voice_cover 恢复 6 参考输入，优先级：上传 → 上传干声 → 分离人声 → 音色库。
- on_voice_ref_upload_check 刷新目标改为 _voice_dry_upload_choices（原聚合函数 _voice_dry_history_choices 删除）。
- i18n.py：新增 13 词条（干声来源/从分离人声选择/从上传干声选择/分离人声/上传干声/### 分离任务历史/选择分离任务/### 翻唱任务历史/选择翻唱任务/回放轨道/回放等）。
- 验证：py_compile 过；pytest 49 passed + i18n 15 通过；服务重启探活 200；gradio_client 验证 on_voice_dry_src_mode sep/upload 两方向显隐正确；on_voice_task_history_pick 空历史时被 Dropdown 入参校验拦截（符合预期，当前无分离/翻唱记录）。
- 未执行：git commit/push（需批准）；真实端到端分离/翻唱推理；浏览器手动确认需刷新页面。

---
## 2026-09-24 顶部菜单重排改名

- 用户要求：创作→歌曲创作；历史→歌曲历史并移到第二位；设置→系统设置；英文同步。
- app.py：三个 Tab 改名（歌曲创作/歌曲历史/系统设置，含 _reg 语言切换注册）；「音频转谱」整块下移到歌曲历史之后，新顺序：歌曲创作 → 歌曲历史 → 音频转谱 → 音轨分离 → 音色翻唱 → 系统设置。
- i18n.py：词条改名 歌曲创作/Song Creation、歌曲历史/Song History、系统设置/System Settings（删除旧词条 创作/历史/设置）。
- tests/test_i18n.py：词条测试同步改为新键（4 断言，15→17 通过）。
- 验证：py_compile 过；pytest 49 passed；i18n 17 通过；服务重启探活 200；抓 /config 确认 tabitem 顺序与中文名完全正确。
- 未执行：git commit/push（需批准）；README/Docs 中 UI 描述的 Tab 名称同步（可后续文档更新时一并处理）。

---
## 2026-09-24 音轨分离：执行与输出组件移到左列 + 点击验证

- 用户反馈：分离页「提交与执行结果组件」应放左边；点击开始分离没反应。
- app.py：音轨分离 Tab 布局调整——把「降噪/开始分离按钮/输出产物(sep_info/sep_files)」从左列外的右栏移到左列（紧跟分离参数之后）；右栏保留「保存分离轨到库」与「分离任务历史」。
- 点击「没反应」排查：通过运行中服务的 /config 确认按钮(299).click → on_voice_separate，inputs=[历史下拉294,上传295,分离模式297,降噪298] 绑定完好；gradio_client 模拟点击返回 AppError「请先选择源音频」（无源时的正确提示）——后端与绑定均正常。
- 结论：点击无反馈大概率是浏览器未刷新(旧页面缓存)或未选源音频；已刷新/选源即可。
- 验证：py_compile 过；pytest 49 passed；i18n 17 通过；重启后 /config 确认按钮绑定与输入映射正确。
- 未执行：git commit/push（需批准）；真实端到端分离推理。

---
## 2026-09-24 移除「保存分离轨到库」，分离产物靠文件名区分直接供使用

- 用户澄清：不需要单独的库（音色库/素材库），分离成功都应记录历史，通过文件名区分人声轨直接供使用。
- app.py：移除分离页「保存分离轨到库」区块（sep_products_state/sep_ref_md/sep_stem_dd/sep_ref_name/sep_save_btn/sep_save_info）；输出产物(sep_files/sep_info)保留在左列；右栏只留「分离任务历史」；sep_btn.click outputs 精简为 [sep_files, sep_info]；on_voice_separate 返回精简为 (files, note)；删除 on_voice_sep_save_ref。
- i18n.py：删除废词条（### 保存分离轨到库/选择轨道/命名/保存到库/已保存到素材库）；保留 自定义伴奏(可选)/保存到音色库（cover 上传参考仍用）。
- 说明：分离成功仍自动写历史（含全部轨，依赖 worker 不变）；参考干声「从分离人声选择」已按 separation 记录的 vocals 轨直接选文件（靠文件名/记录），无需手动入库；自定义伴奏(可选)仍从素材库 _voice_stem_choices 选（save_stem/list_stems 函数与测试保留）。
- 验证：py_compile 过；pytest 49 passed + i18n 17 通过；服务重启探活 200，日志无 Error/Traceback。
- 未执行：git commit/push（需批准）；真实端到端分离/翻唱推理。

---
## 2026-09-24 修复前端挂载中断：历史表格消失 + 点击分离无反应

- 用户报告：1) 歌曲历史 10 条表格消失；2) 选生成歌曲点开始分离无反应。
- 排查（Chrome DevTools 真实浏览器复现）：
  - 后端 /config 正常（dataframe 242 含 10 行数据、按钮绑定完好、后端日志无任何用户请求——点击根本没到后端）。
  - 前端 DOM：history-table(Dataframe)、sep-files/cover-files(gr.Files) 及隐藏面板组件均未挂载；console 有 Uncaught(in promise) at handle_mount。
  - 最小复现 + 二分 + navigate_page initScript 捕获真实错误：SyntaxError: Unexpected token ';' at new AsyncFunction。
- 根因：Gradio 5.x Blocks 级 js= 会被包装为 await (js)(); 要求「函数表达式」。_LOCALE_SYNC_JS 原为 IIFE (function(){...})();，尾部分号使包装后代码语法错误，在组件挂载流程中抛出并中断后续组件挂载（Dataframe/Files 等丢失）。该 js 为本会话新增（launch css/js 参数报错后移入 Blocks），从未成功执行过。
- 修复：_LOCALE_SYNC_JS 改为箭头函数表达式 () => {...}，并加注释说明 Gradio js= 禁止 IIFE。
- 验证：重启后浏览器实测——挂载错误消失；歌曲历史表格恢复（10 行+表头）；分离页输出产物组件恢复；真实点击开始分离 -> 任务 separation_20260924_201805_00b215 入队、worker 懒启动(端口8190)、36.4s 完成；UI 展示 vocals/no_vocals 两轨(各18MB)；history.json 新增 separation 记录含 stems(人声/伴奏)。历史共 27 条。
- 清理：删除临时诊断文件 _tmp_df_test.py/_tmp_gen_check.py/_tmp_check.js，停掉 9917/9918 复现服务。
- 未执行：git commit/push（需批准）。

## 2026-09-24 分离/翻唱产物独立目录 + 时间戳命名 + 播放器组
- 用户需求三点：1) 分离产出单独建文件夹，文件名=时间戳+分离类别；2) 产出后按文件数量显示多个播放器，与其他 Tab 统一组件且有个性化定制；3) 历史 by 文件夹，选中后一组播放器播放并可下载。
- 产物目录重构（voice_ui_handlers.py `_derived_dir`）：独立目录 `outputs/separations/<ts>_<短id>/`、`outputs/covers/<ts>_<短id>/`；worker.py `_separate/_convert` 产物命名 `<ts>_vocals.wav`、`<ts>_accompaniment.wav`（2轨）/ `<ts>_<类别>.wav`（4轨）、`<ts>_converted_vocals.wav`、`<ts>_cover.flac`；cover 改平铺结构（换嗓临时子目录用后即删）；voice_client.py 两 API 透传 prefix；history.py `_derived_dir_for` 适配新结构并兼容旧 `derived/<kind>_<id>` 形态。
- 播放器组（app.py）：预建 6 槽 gr.Audio（show_download_button=True），elem_id 为 sep-audio-N / cover-audio-N / sep-history-audio-N / cover-history-audio-N；`_voice_stem_items` 将 stems 转 [(label,path)]（_denoised 追加"已降噪"），`_fill_voice_players` 按数量前 n 可见、其余 visible=False；分离/翻唱回调与历史选中回调均返回整组 update；历史下拉显示名改为文件夹名；移除旧 gr.Files 与"回放轨道"下拉。
- 个性化定制（static/js/app.js）：PLAYER_IDS 扩充 24 个新播放器 id；initAudioTimeDisplay 选择器覆盖新 elem_id；watchPlayer 改 poll(1s)+boundRoot 重绑，支持 Gradio 显隐切换导致的 DOM 销毁重建；app.py 脚本引用 v=10→v=11（修复浏览器缓存旧 JS 导致 PlayerZoom 不生效）。
- i18n：删"回放轨道"，增"已降噪"。
- 验证：pytest 49 passed（--ignore=test_i18n.py）+ test_i18n.py 17 通过；py_compile 全部改动文件通过；浏览器实测——分离任务（源=历史 generation，模式=人声/伴奏）9.7s 完成，磁盘产物 `outputs/separations/20260924_205055_icme/20260924_205055_{vocals,accompaniment}.wav`；产出双播放器 label 人声/伴奏、含下载按钮；历史下拉显示文件夹名，选中新任务后 sep-history-audio-0/1 填充（下载 URL 指向 <ts>_vocals.wav/<ts>_accompaniment.wav）、PlayerZoom 接管（适应宽度/±缩放按钮、时间码 0:00/0:50）、zoomWraps=2；旧记录 sep_x7y2 兼容回放（vocals/no_vocals）；翻唱页布局快照正常。
- 未执行：git commit/push（需批准）；翻唱端到端真实推理未跑。

## 2026-09-25 翻唱端到端验证 + 修复 tab_cover.select 裸列表报错
- 用户指令"翻唱一样逻辑"：对翻唱侧按分离侧的三点需求（独立文件夹+时间戳命名/按产物数量多播放器/历史 by 文件夹回放+下载）做端到端验证。
- 真实翻唱推理：源=历史 generation 20260923_180303_song.wav，参考音色=分离 vocals（翻唱页"从干声历史选择→从分离人声选择"下拉选 20260924_205055_vocals.wav，半音0/扩散30步/无降噪）；任务 cover_20260925_041157_28d794 128.3s 完成。
- 磁盘产物（需求1 ✓）：`outputs/covers/20260925_041157_urtm/` 下 `20260925_041157_vocals.wav`（分离人声）、`..._accompaniment.wav`（分离伴奏）、`..._converted_vocals.wav`（换嗓干声）、`..._cover.flac`（翻唱成品），平铺+时间戳+类别命名，`_seedvc_tmp` 换嗓临时目录用后已删。
- UI 产出播放器组（需求2 ✓）：cover-audio-0/1/2/3 按产物数量 4 个挂载（label 翻唱成品/换嗓干声/伴奏/分离人声，含音频+下载按钮），PlayerZoom 接管（zoom 生效）。
- 历史（需求3 ✓）：下拉显示文件夹名 `20260925_041157_urtm`；被翻唱歌曲下拉自动出现 `cover · 20260925_041157_cover.flac`（cover 记录可作下次翻唱源）；后端回调验证 on_voice_task_history_pick 对 cover 任务返回 6 槽 update（前 4 槽 visible+label+音频路径，含下载）。
- 修复 bug（app.py L2337-2346）：tab_cover.select 刷新回调返回裸 choices 列表，Gradio 5 将其当作 Dropdown value 赋值导致 `Value not in the list of choices: []` 报错（新增 cover 记录后才暴露）；改为 gr.update(choices=...) 包裹（与分离页 tab_sep.select 一致）。验证修复后切 tab 无报错、下拉正常显示文件夹名。
- 验证方式补充：浏览器 CDP 无法触发"单选项下拉"的 value 变化 change，改用 Gradio call API（api_name=on_voice_task_history_pick_1，依赖 id 121）直接调用回调验证后端填充逻辑。
- 服务已重启（端口 9898，v=11 脚本引用不变）。
- 遗留：临时裁剪文件 `yue2-webui/tmp_ref_vocals.wav`（用于尝试上传参考干声，因浏览器工作区限制未用，待用户确认后删除）。

---
## 2026-09-25 整体 review 测试（音色工坊）
- 用户指令"整体review测试一下"：对音色工坊（分离/翻唱）做整体代码审查 + 测试回归。
- 静态检查：py_compile 全部改动文件通过（app.py / voice-tools/worker.py / src/voice_ui_handlers.py / src/history.py / src/i18n.py 等 7 个）。
- 测试回归：pytest 49 passed + test_i18n.py 17 通过（-p no:cacheprovider 规避 atexit PermissionError 无害告警）。
- 子代理代码审查（检查 7 项：播放器组数量恒定、回收安全、prefix 无冲突、watchPlayer 无泄漏等）确认：产物目录/命名/回收/history 兼容旧记录均安全；发现 2 个一般问题并修复。
- 本轮修复 3 处：
  1) worker.py `_convert` 失败分支清理——Seed-VC 换嗓失败或未产出时 shutil.rmtree(conv_tmp) 清理临时子目录，避免失败大 wav 堆积污染产物文件夹（L223/232/240/244）；
  2) app.py `_voice_stem_items` 轨道 label 走 tr 国际化（历史记录 stems 存中文原文，显示时按当前语言翻译；_denoised 追加"已降噪"）；
  3) i18n.py 补词条：换嗓人声/换嗓干声=Converted vocals、翻唱成品=Cover（L340-341）。
- 英文界面验证：切 English 后 Gradio call API 调 on_voice_task_history_pick_1 返回 label=Cover/Converted vocals/Accompaniment/Vocals from separation——i18n label 修复生效。
- 服务已重启（端口 9898），浏览器需强刷加载新脚本。
- 未执行：git commit/push（需用户批准）；app.py L1079 死代码 products = result.get("products") 未使用（review 发现，仅汇报未清理）；临时文件 yue2-webui/tmp_ref_vocals.wav 待用户确认后删除。

---
## 2026-09-25 分离/翻唱提交后进度反馈 + 按钮禁用
- 用户反馈：音轨分离提交后看不出进度（无 gradio 组件显示），按钮应变灰不可点击。
- 根因：on_voice_separate/on_voice_cover 为同步阻塞回调，仅靠 gr.Progress 弹窗（无细粒度进度值，观感为无反馈），且运行期间按钮仍可点击。
- 改造（回调式 → 生成器流式）：
  - voice_ui_handlers.py：`run_in_queue` 重构为生成器 `run_in_queue_stream`——yield 排队（前面还有 N 个任务/正在等待）与执行中（附秒表 `· Ns`，worker 无细粒度进度，用耗时反馈）文案，完成 return 结果 dict；相邻重复文案自动去重。
  - app.py：两个回调改生成器——提交即清空播放器组 + 按钮禁用（gr.update(interactive=False)）；运行中每条状态流映射为（播放器空更新 + 进度文案到 sep_info/cover_info + 按钮保持禁用）；完成/失败均恢复按钮，失败先 yield 失败文案再向上抛（Gradio 弹错误）；新增 `_voice_running_outputs` 辅助；按钮绑定 outputs 追加按钮自身（sep_btn/cover_btn）。顺带移除翻唱回调中未使用的死代码 `products = result.get("products")`。
  - i18n 无新增（复用 排队中.../正在等待.../执行中.../任务失败/分离完成/翻唱完成/写入历史 等现有词条）。
- 测试（先写测试后实现）：tests/test_voice_handlers.py 新增 _FakeStreamTask/_FakeQueueManager/_drain_stream 与 2 个用例——stream 进度文案（排队/执行/秒表）+ 结果 return + 入参透传；FAILED 状态抛 RuntimeError 含错误信息。pytest 51 passed（含新增 2）+ i18n 17 通过。
- 浏览器端到端验证（English 界面）：点击 Start separator 后按钮 disabled=true，info 实时显示 "Separate stems · Running... · 9s" 秒表推进；10.3s 完成后按钮恢复、显示 "Separation done · Saved to history"，sep-audio-0/1 挂载并填充 20260925_083325_vocals.wav / _accompaniment.wav（label Vocals/Accompaniment）。服务日志无错误。
- 服务已重启（端口 9898，job-2a6c4a935bda4abd9209da5d605332d8），浏览器需强刷。
- 未执行：git commit/push（需用户批准）；翻唱侧真实推理验证（生成器逻辑与分离侧完全对称，单测已覆盖）。

---
## 2026-09-25 修复：翻唱换嗓干声音量与原声不匹配（人声被伴奏盖住）+ commit b5e182b
- commit b5e182b：音色工坊产物独立目录+播放器组+历史整组回放+实时进度与按钮禁用（13 文件，未 push）。
- 用户 bug：翻唱后换嗓干声音量与原声干音不匹配（例子偏小），混音成品人声听不清。
- 复现测量（用户例子 cover_20260925_085616_mv0p）：原声干声 RMS=-20.6dB、换嗓干声 RMS=-35.1dB（差 14.5dB，比伴奏小 17.4dB → 被盖住）。
- 修复（voice-tools/worker.py）：混音前人声响度匹配——新增 `_rms_db()`（ffmpeg astats 测整段 RMS，静音 -inf/缺失返回 None）；测原声干声与换嗓干声 RMS，将换嗓人声增益到与原声干声一致（双向，钳制 ±18dB），volume 后接 `alimiter=limit=0.98:level=false` 防增益削波；测量不可用回退 0dB 不阻断。
- 验证：① `_rms_db` 临时脚本单测——两已知电平文件差精确 10dB、静音/不存在返回 None、match_db 计算与 ±钳制正确；② 真实翻唱端到端（任务 20260925_090801_ni6d，68s）——本次换嗓输出偏大 8.9dB（Seed-VC 电平方向不定），匹配逻辑正确反向衰减；cover.flac RMS=-30.4dB 符合匹配后混合期望（未匹配应约 -24dB），匹配确认生效。
- 说明：换嗓干声单轨产物（<ts>_converted_vocals.wav）保留原始电平，匹配增益仅作用于混音（cover.flac）；如需单轨也匹配可后续再议。
- worker 部署注意：改 worker.py 后需杀 8190 端口旧进程（已杀，pid 38028），下次请求自动拉起新代码；主 app 无需重启。
- 未执行：本次修复的 git commit（需用户批准）；push（需批准）。

---
## 2026-09-25 排查修复：翻唱音质劣化（响度匹配放大导致 alimiter 削顶失真）
- 用户反馈：翻唱音质下降厉害。
- 排查（任务 20260925_091456_au9c，用户开启降噪）：换嗓干声 mean=-34.9dB/peak=-11.8dB vs 原声干声 mean=-20.6dB/peak=-1.3dB——响度匹配放大 +14.3dB 后换嗓人声峰值冲到 **+2.5dBFS**，alimiter(limit=0.98) 大量硬压 → 波形拍扁（削顶失真），混入成品即音质劣化。客观证据：旧增益链人声 peak 精确卡在 -0.175dB（=limit 阈值，贴限硬压特征）。
- 修复（voice-tools/worker.py）：
  - 新增 `_peak_db()`（ffmpeg astats Peak level，缺失返回 None）；
  - 响度匹配加峰值防削波：增益上限 = -1dB - 换嗓峰值（保证放大后峰值 ≤ -1dBFS），超限则收窄增益（RMS 匹配优先、峰值安全兜底）；alimiter 仅作极端兜底，正常不再触发。091456 场景：+14.3dB → +10.8dB（放大后峰值 -1.0dB）。
- 验证：① 091456 数据复算——旧增益放大后峰值 +2.5dB 超限，新增益 -1.0dB 安全；② 新旧增益人声链 astats 对比——旧 peak=-0.175dB（贴限硬压）vs 新 peak=-0.998dB（limiter 未触发）；③ 按新逻辑重混 091456 产物为对比版 `outputs/covers/20260925_091456_au9c/_verify_new_cover.flac` 供试听。
- 次要因素（待用户听感确认）：本次开启降噪（ffmpeg anlmdn 轻量降噪），若新版仍有"水声/金属感"则来自降噪环节，可关闭降噪对比。
- 部署：已杀 8190 旧 worker，下次请求自动用新代码；主 app 无需重启。
- 未执行：git commit/push（需用户批准）。

---
## 2026-09-25 修复：翻唱人声偏弱（改用 loudnorm 响度归一化）
- 用户反馈：峰值防削波版翻唱人声又变小了。
- 定位：静态增益受峰值余量限制——091456 换嗓人声峰值 -11.8dB，防削波把 +14.3dB 收窄到 +10.8dB，人声 RMS 比原声干声低 3.5dB、比伴奏低 6.4dB → 偏弱。静态增益无法同时满足“平均电平到位 + 峰值不超限”。
- 修复（voice-tools/worker.py）：
  - 新增 `_lufs()`：ffmpeg loudnorm print_format=json 解析 input_i（BS.1770 积分响度），失败/静音返回 None；
  - 混音链人声段优先 loudnorm 响度归一：`aresample=48000,pan=stereo|c0=c0|c1=c1,loudnorm=I=<原声LUFS>:TP=-1.5:LRA=11,aresample=48000`——动态归一可在真峰值不超限（TP=-1.5dB）前提下把响度拉到位；pan 在 loudnorm 之前，保证与原声干声（立体声）的 LUFS 声道求和口径一致；loudnorm 内部升采样 192k，链尾 aresample 回 48k；
  - LUFS 测量失败时回退原 RMS+峰值钳制静态增益逻辑（保底不劣化）。
- 验证：① 091456 实测——loudnorm 后换嗓人声 RMS=-21.3dB（原声干声 -20.6dB，差 0.7dB 匹配）peak=-1.5dB 无削波，比旧静态方案响 2.8dB；② 按新链重混 091456 产物为对比版 `outputs/covers/20260925_091456_au9c/_verify_loudnorm_cover.flac`（混音 peak=-3.6dB 安全）供试听；③ py_compile 通过；④ pytest 51 passed + test_i18n 自检 17 通过（test_i18n 为独立脚本，pytest 收集会因模块级 sys.exit 中断，需 --ignore 排除后单独跑）。
- 部署：已杀 8190 旧 worker（PID 26240），下次请求自动用新代码；主 app 无需重启。
- 未执行：git commit/push（需用户批准）；上一轮对比文件 `_verify_new_cover.flac` 暂保留供 A/B 试听，确认后删除。


---

## 2026-09-25 11:05 — 分离/翻唱管线 11 项优化（按顺序全部完成）

**用户指令**：「你看看还有什么要优化的，包括分离与翻唱」→ 审查产出 11 项清单 →「按顺序全部完成」。

**完成内容**（11/11）：

1. HTTP 超时动态放大：`_timeout_for` = max(900s, 源时长×20)。
2. worker 日志落盘：`voice-tools/worker.log`（10MB 轮转 + PYTHONIOENCODING=utf-8）。
3. 翻唱源复用分离历史：下拉 `[分离]` 条目 + `sep_task:` 前缀解析 + worker 跳过 Demucs。
4. 任务完成后自动刷新历史下拉（outputs +1 元素，所有 yield 同步 +1）。
5. 阶段进度上报：worker 写 `_progress.json` → handler 轮询线程 → `task.push_progress` → 前端展示。
6. 运行中取消：`/api/cancel` 在 `_lock` 外处理（防死锁）+ Seed-VC 子进程可中断 + 两 Tab 取消按钮 + `_register_task/_unregister_task` 挂接。
7. `_lufs/_rms_db/_peak_db` 单元测试：`tests/test_voice_loudness.py` 10 项（正弦波峰因子 3.01dB 物理断言规避 sine 源非满刻度问题）。
8. 音色库/素材库管理：试听 + 删除（`delete_files_to_recycle` 回收站）+ 重命名（保留短 id/轨道类型）。
9. dry_uploads md5 内容去重（分块读取比对）。
10. 上传源副本留存产物文件夹（`_keep_source_copy`，仅 from_upload 时拷贝）。
11. 自定义伴奏 LUFS 对齐源伴奏（clamp ±18dB 并入混音链）。

**验证**：py_compile 全通过；pytest 61 passed（--ignore=test_i18n）；i18n 自检 17 通过；主 app 9898 已重启（需浏览器刷新）；worker 8190 未运行，下次请求懒启动新代码。

**未执行**：git commit/push（待用户批准）；翻唱端到端推理验证（需真实 GPU 任务）。

## 2026-09-25 端到端真实任务测试 — 分离/翻唱管线 11 项优化

用 gradio_client 对主 app（9898）提交真实 GPU 任务，验证 11 项优化的关键链路：

**TEST1 分离（耗时 12s）**：源 `outputs/covers/20260925_093343_5i6i/20260925_093343_cover.flac`，vocals 双轨模式 → 人声/伴奏播放器组填充、提示"分离完成 · 写入历史"、历史下拉 choices 7→9（任务完成自动刷新生效）。

**TEST2 翻唱复用分离结果（耗时 46s）**：翻唱源选 `[分离]` 记录（value=`sep_task:separation_20260925_083325_4831f3`）、参考干声取 083325 人声轨 → "翻唱完成 · 写入历史"、翻唱历史下拉 choices 5→6；4 轨产物中伴奏/分离人声直接复用 083325 分离文件（文件时间戳证明跳过 Demucs，省分钟级 GPU 时间）。

**TEST3 协作取消**：翻唱任务（diffusion_steps=30，换嗓阶段长）运行 20s 后 POST `http://127.0.0.1:8190/api/cancel` → 21s 快速中止、covers 目录零残留文件。

**辅助验证**：
- 响度对齐：换嗓干声 -22.54 vs 源人声 -23.30 LUFS（差 0.76 LUFS，loudnorm 生效；cover 成品 -27.45）。
- worker.log 落盘（UTF-8）：含"复用已有分离结果（跳过 Demucs）： 人声=20260925_083325_vocals.wav 伴奏=20260925_083325_accompaniment.wav"、"收到取消信号"、"处理失败: 任务已取消"等关键日志。

**发现并修复 bug（worker.py L522-527）**：业务失败（含协作取消）原返回 HTTP 500，客户端 urllib 抛 HTTPError 丢 body，文案退化为"无法连接推理 worker（Internal Server Error）"，误导排障方向 → 改为 HTTP 200 + `{"ok": false, "error": ...}`；重启 worker 后重跑取消测试，文案正确显示"任务失败： 任务已取消"。

**说明**：直接 POST worker `/api/cancel` 不置主 app 侧 cancel_event，故显示"任务失败"而非"已取消"；真实 UI 取消按钮先置 cancel_event 再通知 worker，文案映射正确。分离任务取消仅在开头检查一次（Demucs 推理中不可中断），对翻唱阶段边界和排队任务有效，属设计内行为。

测试后已清理临时脚本（_tmp_api.py / _tmp_e2e.py / _tmp_cancel.py）。

## 2026-09-25 取消链路修复 — 全部修复 + 双途径真实验证

用户指令"全部修复"：修复上轮汇报的两个取消链路局限。

**修复内容（4 文件 + 2 测试文件）**：
1. worker.py：`_TaskCancelled` 专用异常 + do_POST 响应 `cancelled` 标志 + `_separate` 检查点加密（模型加载后/每轨落盘前）。
2. voice_client.py：`VoiceResult.cancelled` 字段 + `_run` 解析。
3. voice_ui_handlers.py：分离/翻唱失败分支两路取消来源（cancel_event 或 result.cancelled）→ TaskCancelledError。
4. app.py：两生成器 `except TaskCancelledError` 分支（显示"任务已取消"+恢复按钮+正常收尾不弹错误窗）。
5. tests：新增 3 项单测（separate/cover 的 worker cancelled 映射、VoiceResult cancelled 解析）。

**验证**：py_compile 6 文件通过；pytest 64 passed（61+3）。

**端到端真实验证（双途径）**：
- C1（gradio_client 提交翻唱 + 直接 POST worker /api/cancel）：终态输出"任务已取消"、`job.result()` 正常返回（不抛 AppError）、1.3s 中止、covers 目录零残留。服务端日志 `cancelled by worker`。
- C2（Chrome DevTools 真实浏览器：选复用分离源 + 分离人声参考 → 开始翻唱 → 点"取消任务"）：info 区显示"任务已取消"、"开始翻唱"按钮恢复可用、无错误弹窗；服务端链路 `cancel signal sent` → `已通知 worker 取消` → `cancelled by worker`（0.7s）。

**过程中发现并修复的次生 bug**：app.py 取消分支 yield 后缺 `return`，落入成功路径导致 `result=None` 的 `.get` 崩溃（AttributeError）——补 return 后 C1 通过。

**排查记录**：① 系统 HTTP_PROXY(7890) 劫持 127.0.0.1 请求返回 502 → urllib 用 `ProxyHandler({})` 禁代理；② Gradio 5 Dropdown 严格校验 choices，脚本传小写盘符路径不匹配（choices 为大写 Y: 绝对路径）；③ gradio_client `outputs()` 中间流不含终态 yield，终态以 `job.result()` 为准（UI 实际显示正确）；④ `/on_voice_cancel` 回调为 lambda 绑定无 api_name，gradio_client 调不到 → C2 改用真实浏览器验证。

**未执行**：git commit/push（待用户批准）。

## 2026-09-25 README 更新与提交

- **README.md**：中英文 4 处音色工坊描述同步更新（功能特性、音色工坊小节、Voice Studio 概述与详情），覆盖复用分离、协作取消、阶段进度、响度对齐、库管理、动态超时、产物独立目录与整组回放等新特性；格式校验 CRLF+BOM 通过。
- **git commit**：提交 3593f69（11 个文件，+1090/-83），含取消链路修复（worker.py / voice_client.py / voice_ui_handlers.py / app.py）、管线优化、新增 test_voice_loudness.py、changelog 与 chat_history。排除 .pytest_tmp2/、tests/.tmp-ht/ 临时目录。
- **未执行**：git push（用户未指示）。

## 2026-09-25 系统设置新增音色工坊模型状态

- **需求**：系统设置「模型状态」新增显示音色工坊三个模型（用户确认方案：音色工坊全套）。
- **改动**：src/voice_client.py 新增 check_voice_models()（文件级检查，不加载模型）：Demucs=htdemucs torch hub 缓存（955717e8-8726e21a.th，TORCH_HOME 优先）、Seed-VC=models--Plachta--Seed-VC/snapshots 下 .pth、campplus=models--funasr--campplus/snapshots 下 campplus_cn_common.bin；app.py on_check_models() 新增音色工坊段（未启用/未配置/缺失三态文案，Demucs 缺失提示自动下载）；src/i18n.py 新增 7 条中英词条。
- **测试**：tests/test_voice_client.py 新增 4 项（disabled/no_seedvc_dir/all_ready/missing），修复过程中发现 snapshots 目录不存在时应报 False 而非 None。
- **验证**：py_compile 通过；pytest 68 passed；i18n 自检 17 通过；真实环境三模型均检出为 True；服务已重启（9898 就绪）。
- **未执行**：git commit（待批准）。

## 2026-09-25 系统设置页 UI 重设

- **需求**：系统设置页 UI 重新设置（用户选定方案：左右分栏+紧凑化）。
- **改动**：app.py 设置 Tab 布局重排——左列（scale=3）「系统状态」+「检查模型」按钮同行（scale=0, min_width=110, size=sm），右列（scale=2）「当前队列」；下方「参数预设」整行（预设下拉+加载按钮同行 scale 4:1，名称输入+保存按钮同行）。逻辑与事件绑定未变。
- **踩坑**：gr.Markdown 不支持 scale 参数（TypeError），改为按钮端固定宽度控制。
- **验证**：py_compile 通过；服务重启后 Chrome DevTools 真实浏览器验证——左右分栏生效（系统状态 x=85 / 当前队列 x=821 同水平线），检查模型按钮与标题同行，参数预设两行紧凑排列；音色工坊三模型 ✅ 显示正常。
- **未执行**：git commit（待批准）。

## 2026-09-25 系统设置页区块外框

- **需求**：系统设置页加外框分隔线（区块卡片化）。
- **改动**：app.py——_TITLE_ROW_CSS 追加 .settings-panel 样式（border: 1px solid var(--border-color-primary); border-radius: 10px; padding: 2px 14px 12px; background: var(--background-fill-primary)，颜色随明暗主题自动切换）；左列/右列 Column 与参数预设区 Group 挂 elem_classes="settings-panel"。
- **验证**：py_compile 通过；服务重启后浏览器 evaluate_script 确认 3 个 panel 均带边框（714px/486px/1216px，border solid、radius 10px、暗色主题背景 rgb(15,15,17)），截图确认视觉卡片分隔效果。
- **未执行**：git commit（待批准）。

## 2026-09-25 歌曲历史仅显示生成记录

- **需求**：歌曲历史页只保留生成的歌曲，不显示分离与翻唱的历史。
- **改动**：src/history.py to_dataframe_rows() 新增可选参数 record_types（None=全部，向后兼容；传入则按类型过滤）；app.py 两处取数（refresh_history 与 _get_history_page）均传 record_types=("generation",)。
- **测试**：新增 tests/test_history_filter.py——不传参返回全部 3 条、仅生成 1 条、组合过滤 2 条、app 源码断言两处调用均带过滤。
- **验证**：py_compile 通过；pytest 70 passed（含 2 项新增）；服务重启后浏览器确认历史页显示"第 1 / 3 页，共 26 条"（history.json 分布 generation 26 / separation 10 / cover 7，分离与翻唱已排除）。
- **未执行**：git commit（待批准）。

## 2026-09-25 库管理功能移至分离页

- **需求**：翻唱页去掉删除选中/重命名（只保留选择，选中即试听）；管理功能放入分离页。
- **改动**：app.py——翻唱页移除音色库/素材库两处管理行（删除选中/重命名为/重命名按钮）及绑定，保留下拉+试听播放器；分离页右栏新增"库管理"区块：素材库(乐器轨)与音色库(参考干声)两组下拉+试听+删除/重命名（复用 on_voice_stem/ref_delete/rename 回调，回收站删除不变）；tab_sep.select 扩展刷新两库下拉。src/i18n.py 新增 3 条中英词条（### 库管理/素材库(乐器轨)/音色库(参考干声)）。
- **测试**：tests/test_voice_handlers.py 新增 test_cover_tab_management_moved_to_separation（源码断言：翻唱页管理组件已移除、选择/试听保留、分离页库管理组件与绑定就位）。
- **验证**：py_compile 通过；pytest 71 passed；i18n 自检 17 通过；浏览器验证——翻唱页 0 个删除/重命名按钮、音色库选中后试听播放器显示（容器 196px）且 shadow DOM audio blob src 加载成功；分离页库管理区块两组按钮齐全。
- **过程**：JS element.click() 不触发 Gradio 下拉选择，需派发完整 pointerdown/mousedown/pointerup/mouseup/click 事件序列；试听 audio 元素在 shadow DOM 内（document.querySelector 查不到，需入 shadowRoot 取 blob src）。
- **清理**：测试用音色库条目 _ui_test_preview.wav 已删除。
- **未执行**：git commit（待批准）。

## 2026-09-25 17:22 — 文件管理整体重构 + 历史库迁移 SQLite

- **需求**：1) 生成可输项目名（文件=项目名_时间戳，目录=song_时间戳）；2) 历史页可改项目名/删除项目（回收站）；3) 分离自动沿用源项目名（目录 separations_时间戳）；4) 翻唱项目名=源项目名_音色名（目录 cover_时间戳）；5) 上传统一放一个目录（保留源名+时间戳+类别标识）；6) 历史 db 与 outputs 全部清空；后追加：历史存储由 JSON 改为 SQLite。
- **改动**：src/history.py——HistoryRecord 新增 project 字段；sanitize_project/rename_project/delete_project（文件级改名仅替换项目名段；整目录回收站删除，安全限定 song_/separations_/cover_ 前缀单层目录）；存储层整体重写为 SQLite（WAL+NORMAL，RLock+事务，自增 id 保序，stems JSON 列，接口签名不变）。src/voice_ui_handlers.py——产物目录改 outputs/separations_<ts>|cover_<ts> 单层；prefix=<项目名>_<ts> 透传 worker；persist_upload/list_uploads 统一 uploads/ 留存（sep_src/cover_src/dry_ref/transcribe 四类，md5 去重）；save_dry_upload 走统一留存。app.py——生成页项目名输入框；on_generate/_generate_worker 命名链路（song_<ts> 目录+task_id、<项目名>_<ts>[_varN] 文件）；_project_from_source/_voice_ref_name 项目名解析；分离/翻唱改名删除回调（on_voice_task_rename/delete）；历史页 7 列+改项目名+删除项目 UI；_transcribe_worker 上传留存；构造改 db_file=history.db。src/i18n.py 新增 14 条中英词条。
- **数据**：history.json 备份为 history.json.bak_20260925（44 条）后清空；outputs/ 与 dry_uploads/ 均为空（无残留需清理）。
- **测试**：新增 tests/test_history_project.py 8 项；更新 test_history_filter.py（列索引[6]）、test_voice_handlers.py（单层目录命名+project 断言）、test_history_recycle.py（db 路径+close）。
- **验证**：py_compile 通过；pytest 80 passed；i18n 自检 17 通过；服务重启 9898 HTTP 200，history.db（WAL）自动创建；浏览器实测四个页面——生成页项目名输入框、历史页 7 列表头+共 0 条+改项目名/删除项目、分离页库管理+任务历史改名/删除、翻唱页任务历史改名/删除，均正常。
- **未执行**：git commit（待批准）；真实生成/分离/翻唱任务端到端验证（建议跑一次生成验证 song_<ts> 目录与 <项目名>_<ts> 文件落盘）。
## 2026-09-25 18:30 — T1-T7 端到端真实任务测试 + 5 个 bug 修复

- **需求**：文件管理重构后整体测试一遍再交付（T1 生成 / T2 改名 / T3 分离 / T4 翻唱 / T5 删除 / T6 上传留存 / T7 收尾）。
- **Bug 修复（5 个）**：
  1. 【严重】src/history.py L57：Win32 API 名误写 SHFILEOperationW（正确 SHFileOperationW），AttributeError 被吞、静默回退 unlink 直删——回收站功能从未生效。修复后整目录真实入回收站（验证：回收站条目含原路径 Y:\...\outputs）。
  2. src/backend_gguf.py：audiocpp_cli.exe 中文 --out 路径落盘乱码 → CLI 阶段用 ASCII 安全名（song_<ts>），成功后 Python 重命名中文项目名（wav+abc）。
  3. app.py L398：批量结果 5 元组误按 [2] 取 result（abc_score 崩溃）。
  4. app.py L435-441：4 元组解包 5 元组（too many values to unpack）。
  5. app.py tab_sep.select：分离 Tab 切换遗漏刷新 sep_src_history 源下拉。
  - tests/test_history_recycle.py 加强：断言 _delete_to_recycle 主路径返回 True（文件级+目录级），防止拼写类 bug 再溜过。
- **E2E 验证（全部通过）**：T1 生成「测试曲」→ song_20260925_181142/ 四件套（wav/mp3/json/txt 中文名正确）+ db 记录；T2 改名四件套重命名+db 同步；T3 分离 → separations_20260925_181402 + 晨风曲项目名沿用 + 双播放器；T4 翻唱 → cover_<ts> 四轨 + 项目名=源_音色名；T5 历史页/分离页删除项目 → db 移除 + 整目录入回收站（两处均验）；T6 上传源 → uploads/sep_upload_test_20260925_181402_sep_src.wav（264644 字节精确副本）。
- **回归**：pytest 80 passed（--basetemp 绕开 Temp 死链接权限问题）；i18n 17 通过；py_compile 通过。
- **清理**：测试产物 5 项全部回收站移除（song_173646 乱码/174131/174336 失败残留、sep_src 留存、Temp 测试源）；.pytest_tmp3 已删。
- **遗留**：outputs/song_20260925_174551 空目录被系统进程锁定无法删除（WinError 32，非本应用占用，关闭资源管理器/索引器后可手动删）；.pytest_tmp 旧临时目录残留待用户决定。
- **未执行**：git commit（待批准）。
## 2026-09-25 18:50 — 任务历史下拉增加项目名显示

- **需求**：分离/翻唱任务历史下拉仅显示产物文件夹名（separations_<ts>/cover_<ts>），多个任务只有时间戳可辨，看不出对应哪个源/上传文件（项目名含上传文件名但前端不可见）。
- **改动**：app.py——_voice_task_history_choices label 改为「项目名 · 文件夹名」（无项目名时退回文件夹名）；_voice_cover_source_choices 的 [分离] 记录同样前缀项目名。历史页表格本就有项目名列，无需改。
- **验证**：py_compile 通过；pytest 80 passed；服务重启 9898 HTTP 200。当前 db 为空（测试记录已清），下拉效果待有真实任务后浏览器确认。
- **未执行**：git commit（待批准）。


## 2026-09-25 19:00 — 改名同步性确认测试

- **需求**：确认歌曲历史页选中一首歌改项目名后，列表里同项目全部记录（批量变体）是否同步更新。
- **改动**：tests/test_history_project.py 新增 test_rename_project_updates_all_records_in_dir——同目录两条记录（主变体+var1）改名后断言 project/audio_path 全部指向新文件名且文件存在。
- **验证**：test_history_project.py 10 passed；rename_project 遍历同目录全部记录同步字段（文件重命名 + db 更新），前端 refresh_history() 刷新表格即全量可见；分离/翻唱独立目录不级联（设计如此）。
- **未执行**：git commit（待批准）。
## 2026-09-25 19:20 — 一致性优化（行号错位 bug + 播放器同步 + 类型词中文化 + 删除确认）

- **Bug 修复（严重）**：历史页 _load_history_entry / on_history_next_page 取数未按 record_types=("generation",) 过滤，表格行号与全量记录错位——存在分离记录时点击生成记录实际选中 separation 记录（实测误改了分离目录项目名，已恢复）。两处补过滤参数；test_history_filter.py 源码断言 2→4 处。
- **一致性**：历史页 4 回调（删除选中/清空/改名/删除项目）outputs 补播放器三元组——删除清空播放器、改名按新路径重填（对齐分离/翻唱页）；分离/翻唱源下拉类型词经 _QUEUE_TYPE_LABELS+tr 中文化，i18n 补"翻唱": "Cover"；三处删除项目按钮加前端确认弹窗（实测 Gradio 5.x js 返回 false 不阻止 fn，须 throw 中断，弹窗双语文案）。
- **验证**：pytest 81 passed；i18n 17 通过；py_compile 通过；浏览器 E2E——弹窗取消（记录不动）/确定（3 条记录全删+目录入回收站）、改名后 3 记录+15 文件同步、播放器按新路径重载音频、选中行与显示行一致。
- **未执行**：git commit（待批准）。

## 2026-09-30 18:40 — 多轨编辑器：iframe 随内容长高 + 音质/压缩归零 + FX 工具工具栏

- **需求**：(1) 编辑器别锁在固定 iframe 里（顶部标题/菜单无法随滚动消失，浪费顶部空间）；(2) 音质、压缩各加一键归零，并把该行右侧空白放点功能。
- **T1 内嵌自适应高度**：`index.html` 新增 `fitParentHeight` / `watchEmbedHeight`——同源用 `window.frameElement` 反写自身 iframe 高度，父页 `app.py` 不改。修掉三个真坑：① 用 rAF 句柄当「已排队」哨兵 → 内嵌 Tab 未渲染时 rAF 不触发、句柄非 0 永久卡死（实测高度停在 262px），改布尔标记 + `setTimeout` 兜底；② `ResizeObserver` 实例不持有引用会被回收、回调不再触发；③ 父 Tab 隐藏时 body 矩形为 0，加 `if (!h) return` 防守。
- **T2 一键归零**：`mkFxCtl` 增加 `resetV`，`mkMod` 标题条右侧渲染 `.fxmod-r`「归零」；音质 6 项、压缩 2 项分别登记。
- **T4/T6/T7 FX 工具**：旁通（`.fxpanel.fx-off` 只压暗处理模块 + 电平表）、FX 预设（中性/人声/伴奏 + localStorage 自定义 + 保存为预设…）、复制到（惰性重建目标列表 + 全部其他轨 + 一并复制旁通态）。
- **T3/T5**：`--fxmod-w: 362px` 音质=压缩同宽（压缩居中）；工程 JSON 每轨新增 `fx_on`，前端 `buildPayload` 写出、`applyTrackState` 回灌，后端 `mix_render._fx_filters` 遇 false 直接返回空滤镜链。
- **布局取舍**：压缩加宽后该行仅剩 ~66px，放不下工具卡；竖排三行会把面板撑到 283px，故改为独占整行、内部三组横向均布的工具栏（面板高 232px）。
- **验证**：`pytest` 177 passed（基线 175 + 2 个新用例）；`node --check` / `py_compile` 通过；浏览器实测 iframe 双向跟随（1337↔1937）、归零/旁通/预设/复制/自定义预设落 localStorage/渲染请求体 `fx_on=[false,true]` 全部命中、741px 无横向溢出、明暗主题正确。
- **未执行**：git commit / push（待批准）。服务已重启，浏览器需刷新。

## 2026-09-30 12:04 — 多轨编辑器：回声模块一键归零

- **需求**：在已有音质/压缩归零基础上，回声模块也加「归零」。
- **改动**：`static/multitrack/index.html` 新增 `echoResets` 并传给 `mkMod("echo", "回声", echoResets)`，登记 `echoDelayEl / echoFbEl / echoMixEl` 三项 `resetV`（中性值均为 0，混合 0 即关闭回声）；`tests/test_mix_web.py` 的 `test_editor_fx_reset_tools_presets_and_embed_fit` 补 3 条断言（`echoResets` 存在、`mkMod` 传参、三项登记）。
- **验证**：`pytest tests/test_mix_web.py -q` → 34 passed；`pytest tests/ --ignore=tests/test_i18n.py -q` → 177 passed（无回归）。
- **未执行**：push（需另行批准）。

## 2026-09-30 12:10 — 运维：yue2.patdelphi.xyz 无法访问（Cloudflare Tunnel 排查）

- **现象**：`https://yue2.patdelphi.xyz/` 无法访问。
- **根因**：本机仅有一个 cloudflared 进程（PID 44320，`--config provenbraid-config.yml`），其 ingress 只映射 `provenbraid.patdelphi.xyz → 127.0.0.1:8710`；承载 `yue2.patdelphi.xyz → 127.0.0.1:9898` 的隧道（`e453c5f2-7910-4a5d-aa84-9d38d2189671`，配置文件 `config.yml`）**进程未运行**。无计划任务、无启动项，重启后需手动拉起。DNS 解析正常（Cloudflare 代理 IP），本地 9898 服务正常（HTTP 200）。
- **处理**：以 `cloudflared --config C:\Users\patde\.cloudflared\config.yml tunnel run` 独立后台启动（新 PID 51768），日志写入 `.cloudflared/yue2-tunnel-20260930.{stdout,stderr}.log`；日志显示 4 条 `Registered tunnel connection`、连通性 precheck 全 PASS、无 ERR/WRN。
- **验证**：`https://yue2.patdelphi.xyz/` 走系统代理与 `--noproxy` 均返回 **HTTP 200**（首次测试的 SSL 报错系隧道刚注册时边缘未就绪的瞬时现象，复测消失）。
- **未执行**：未配置开机自启；未升级 cloudflared（日志提示 2026.8.2 过时 → 2026.9.3）；未改动任何配置文件。

## 2026-09-30 12:20 — 运维：cloudflared 升级至 2026.9.3 + 新增隧道启动脚本

- **升级**：`winget upgrade --id Cloudflare.cloudflared`，2026.8.2 → **2026.9.3**（MSI，安装路径不变 `C:\Program Files (x86)\cloudflared\cloudflared.exe`）。
- **副作用**：MSI 升级过程终止了正在运行的 cloudflared 进程，ProvenBraid 隧道（原 PID 44320）随之停止（其后端 8710 仍在监听）。
- **重启 yue2 隧道**：停旧进程后用新 exe 以 `--config C:\Users\patde\.cloudflared\config.yml tunnel run` 后台启动（PID 10508），日志 `yue2-tunnel.log` / `yue2-tunnel.err.log`；4 条 `Registered tunnel connection` 全部成功（connIndex=2 首次 QUIC 拨号超时，15 秒后自动重连成功）。
- **启动脚本**：新增 `yue2-webui/start-tunnel.bat`（前台运行、Ctrl+C 停止；含 cloudflared/配置文件存在性检查、9898 监听检查，日志追加到 `%USERPROFILE%\.cloudflared\yue2-tunnel.log`）；按用户要求**不做开机自启**。
- **验证**：`https://yue2.patdelphi.xyz/` → **HTTP 200**（`-NoProxy`）。
- **ProvenBraid 隧道**：经用户确认后一并拉起（PID 43612，`--config provenbraid-config.yml`）；因 yue2 实例已占用 cloudflared 默认 metrics 端口 20241，显式指定 `--metrics 127.0.0.1:20242` 避免冲突；日志 `.cloudflared/provenbraid-tunnel-20260930.{stdout,stderr}.log`。
- **最终状态**：两条隧道同时运行 —— yue2（PID 10508，2026.9.3）+ ProvenBraid（PID 43612）；`https://yue2.patdelphi.xyz/` 与 `https://provenbraid.patdelphi.xyz/` 均返回 **HTTP 200**。未做任何 git 操作。

## 2026-09-30 20:30 — 修复：亮色主题适配（播放器/ABC 预览/多轨编辑页）

- **需求**：用户要求「亮色主题下所有组件都要适配亮色，全部检测一下；目前有个别不适配，多轨编辑全部不适配」。
- **定位（亮色下逐元素审计 + 源码核查）**：
  1. **播放器整块发黑（主因）**——`static/js/app.js` 的 `initAudioTimeDisplay()` 里 `SEL + ' .timestamps {...}'`，`SEL` 为 6 个播放器 id 的逗号列表，拼接后只有**最后一项**带 `.timestamps` 后代限定；前 5 项（`#gen-audio`/`#history-audio`/`[id^="sep-audio-"]`/`[id^="cover-audio-"]`/`[id^="sep-history-audio-"]`）直接命中播放器根节点 → 整块被染成 `rgba(0,0,0,.7)` + 白字。浏览器实测 `#gen-audio` 背景确为 `rgba(0, 0, 0, 0.7)`，遍历 `document.styleSheets` 用 `el.matches(selectorText)` 证实命中此规则。
  2. 缩放工具条按钮 `rgba(0,0,0,.5)`、波形底 `rgba(0,0,0,.25)`、时间码条 `rgba(0,0,0,.7)`、歌词段落卡 `rgba(255,255,255,.05)`、逐句高亮写死 `#fff`。
  3. PlayerZoom 波形 `cursorColor:'#ffffff'`（亮色白底上不可见）、`progressColor:'#4ade80'`（白底对比度不足）。
  4. `app.py` 三处 ABC 预览容器虚线边框 `rgba(255,255,255,0.15)`（亮色下不可见）。
- **改动**：见 `Docs/changelog.md` 当日新增小节。`app.js?v=12 → v=13` 以破缓存。
- **多轨编辑页**：本机亮色下**未能复现**「全部不适配」（iframe 内全部组件渲染正常、`data-theme` 正确跟随父页亮色）。按最可能根因加固 `syncParentTheme()`：优先读父页 `body.dark`/`html.dark`，回退亮度判定时兼容 Chrome 的 `color(srgb 0~1)` 分量格式与 `transparent` 背景；四处主题块补 `color-scheme`。修复后实测：父页亮色 → iframe `data-theme=light`、`--bg=#ffffff`；父页暗色 → `data-theme=dark`、`--bg=#16181d`。
- **验证**：`node --check static/js/app.js` 通过；新增 `tests/test_theme_light.py`（5 用例）全过；全量 `pytest tests/ --ignore=tests/test_i18n.py -q` → **182 passed**（基线 177）。
- **用户二次反馈「播放器仍然是暗色」**：定位到真正的遗留根因——Gradio 音频块根节点带内联 `border-style: solid` 却**无 `border-width`**，宽度回落 CSS 初始值 `medium`(3px)、颜色 `currentColor`；亮色主题下即 3px 近黑边框（`rgb(39,39,42)`），整块看着像「暗色播放器」。修复：在注入 CSS 里加 `SEL + ' { border: 1px solid var(--border-color-primary, transparent) !important; }'`；同时发现 `PLAYERS` 只列了 6 个固定 id，漏掉 `#history-stem-audio`/`#lib-stem-preview`/`#lib-ref-preview`/`#cover-ref-preview`/`#cover-acc-preview`（截图里第二个「音频」块仍是黑框），已扩为 11 项与 `initPlayerZoom()` 的 `PLAYER_IDS` 对齐。`tests/test_theme_light.py` 补 2 处断言。
- **二次复核**（硬刷新后切「歌曲历史」Tab，亮色模拟）：`#history-audio` 边框 `2.857px solid rgb(39,39,42)` → `0.571px solid rgb(228,228,231)`；全页扫描 `borderTopWidth > 1px` 元素数 = **0**；`.timestamps` 底色 `rgb(250,250,250)`、文字 `rgb(39,39,42)`。
- **未执行**：未 git commit / push（改动待批准）；多轨编辑页请用户 `Ctrl+F5` 后复测确认。

## 2026-09-30 22:05 — 修复：File 虚线拖拽区 3px 边框 + 多轨编辑被强制亮色时误判为暗色

- **需求**：用户要求「先修复 gr.File 上传区的 3px 虚线框问题，然后修复多轨编辑仍然是暗色问题」。
- **定位 1（File 虚线框）**：遍历 `document.styleSheets` 用 `el.matches(selectorText)` 命中 Gradio 内置规则 `div.styler > :not(.absolute) { border-width: medium; border-style: none; border-color: currentcolor; }`——宽度 `medium`(3px)、颜色 `currentColor`，本意配合 `border-style: none` 隐藏边框；但 File 组件内联了 `border-style: dashed`，于是露出一圈 3px 近黑虚线（亮色下 `rgb(39,39,42)`）。修复：`_TITLE_ROW_CSS` 追加 `div.styler > :not(.absolute)[style*="dashed"] { border-width: 1px !important; border-color: var(--border-color-primary) !important; }`，只作用于内联带 dashed 的块（两个 File 拖拽区），不动其它组件。
- **定位 2（多轨编辑仍然是暗色）**：本机 Windows `AppsUseLightTheme = 0`（暗色系统）。用户用 `?__theme=light` 强制亮色时实测：父页 `body.className === ""`（**无 dark 类**）、`--body-background-fill === "white"`，但 `document.body` 的 `backgroundColor` 仍是 `rgb(15,15,17)`（Gradio 用 `@media (prefers-color-scheme: dark)` 直写 body）。原 `syncParentTheme()` 首选 `body.dark` 落空后，回退读 body 背景亮度 → 判成暗色 → **子页被错锁成暗色**，正是用户看到的现象。
- **修复 2**：回退分支改为读 Gradio 主题变量——`--body-background-fill`（亮 `white` / 暗 `#0f0f11`），再退 `--background-fill-primary`，最后才退回 body/html 背景。变量值写法不固定（`white`/`#rrggbb`/`rgb()`/`color()`），统一用 canvas `fillStyle` 归一化后算亮度，并用哨兵色 `#010203` 识别解析失败（避免非法值被当成纯黑判暗）。
- **验证**：`node --check`（提取内联脚本）通过；`py_compile app.py` 通过；全量 `pytest tests/ --ignore=tests/test_i18n.py -q` → **183 passed**（基线 182 + 新增 `test_file_dropzone_border_is_normalized`）。浏览器实测（不施加颜色模拟，用真实系统暗色 + `?__theme=` 切换）：`?__theme=light` → iframe `data-theme=light`、`--bg=#ffffff`（修复前为 dark，已复现并修掉）；`?__theme=dark` → iframe `data-theme=dark`、`--bg=#16181d`（无回归）；全页 3 个 dashed 元素宽度均 1px、颜色均 `rgb(228,228,231)`。
- **服务**：已重启（停旧进程 → 确认端口释放 → 同命令重新拉起），HTML 下发 `app.js?v=13`。
- **未执行**：未 git commit / push（改动待批准）。

