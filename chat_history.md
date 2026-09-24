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