# yue2-webui 代码探查报告

- **日期**：2026-10-06
- **范围**：app.py（3699 行）+ src/ 全部核心模块 + backend_gguf 子进程链路 + static/tests/.gitignore 概览
- **复核**：2026-10-06 逐条核对引用行号与结论，删除 1 条误报（原 A11 正则歧义）、订正数字偏差，并在 A1/A4 补充遗漏点
- **性质**：纯只读审计，未修改任何代码；报告中所有行号均经 Read 逐一核实
- **方法**：根目录结构扫描 + 核心模块精读 + 两个 Explore subagent 顺序执行（仅做代码定位，结论人工核实）

---

## A. 错误 / Bug（按严重度排序）

### A1（中高）生成 / 转谱子进程：取消检测会阻塞、无超时、kill 后不回收

位置：`src/backend_gguf.py:216-226`（generate）、`src/backend_gguf.py:478-481`（transcribe）

```python
for line in process.stdout:          # 阻塞读：CLI 长时间无日志时 cancel 检查不到
    if cancel_event and cancel_event.is_set():
        process.kill()
        return ...                   # 未 process.wait()，管道/句柄不回收
```

- `audiocpp_cli` / SheetSage2 长时间不输出日志时，取消按钮完全失灵（事件检查依赖新日志行到达）
- `generate` 与 `transcribe` 两条 CLI 链路均无超时；CLI 挂死会**永久阻塞串行队列**（queue_manager 也无任务级超时兜底）
- 取消分支 `process.kill()` 后直接 return，未 `process.wait()` 回收进程（`backend.cancel()` 同样 kill 不 wait）

### A2（中）后处理失败会吞掉成功生成的历史记录

位置：`app.py:403-433` + `src/postprocess.py:142-143`

- `postprocess_audio` 调用处无 try/except；若 soundfile 读/写失败，异常传播使任务标 FAILED，而历史写入（app.py:447）在其后 → **音频已落盘但无记录**（孤儿文件）
- `postprocess.py` 末尾 `except Exception: pass` 静默吞掉 mutagen 元数据写入失败，无日志可查

### A3（中）生成失败不回收孤儿目录

位置：`app.py:390-394`

- 全部变体失败（非取消）时直接 raise，`song_<ts>` 目录及半成品留在磁盘且无历史记录
- voice 侧有 `_recycle_created_derived` 兜底，生成侧没有 —— 行为不一致

### A4（中）历史页每次操作触发"全表扫描 ×2 + 全记录磁盘 stat"

位置：`src/history.py:629` + `app.py:1652-1655`

- `to_dataframe_rows` → `prune_missing`（SELECT * 含 lyrics 全文 + 每记录逐文件 exists()）+ `list_all`（再扫一遍全表）
- `on_history_next_page` 一次点击调用它**两次**（L1652 算页数 + L1655 取页）= 4 次 SELECT * + 2 轮 stat 风暴
- `_generate_worker` 末尾（app.py:487/506）也在 worker 内再刷一遍；循环结束后 `auto_prune`（app.py:477）再全表扫一次（每任务 1 次，非每变体）
- 记录满 100 条时每次 UI 操作约数百次 stat；若 outputs 在网络盘上更明显

### A5（中）取消监视线程 30 分钟空等堆积

位置：`src/voice_client.py:226-228, 242-250`

- 每次 `_run` 都起一个 daemon 线程 `cancel_event.wait(timeout=1800)`
- 任务正常结束不 set 事件 → 高频任务下空闲线程持续累积（每个最多空等 30 分钟）
- 建议改为任务结束时主动触发或缩短等待

### A6（轻）排队中取消的任务不进"最近任务"

位置：`src/queue_manager.py:250-256`

- 排队任务取消后直接移出队列，不写 `_history` 环形缓冲；运行中取消则会记录
- 设置页"最近任务"展示不一致

### A7（轻）`ensure_separation` 不响应取消、无进度上报

位置：`src/voice_ui_handlers.py:524-525`

- 同步调 `separate` 未传 `cancel_event`/`progress_file`，该阶段 UI 取消无效

### A8（轻）seeds 与 batch_count 无防御

位置：`app.py:363-369`

- `seeds[i]` 列表短于批量数时 IndexError（依赖 on_generate 保证，worker 层无防御）

### A9（轻）上传去重每次全量重算 md5

位置：`src/voice_ui_handlers.py:204-213`

- 每次上传对库里每个同类别文件重新计算 md5（O(N×文件大小)）；可先比文件 size

### A10（轻）波形峰值缓存并发同 key 重复解码

位置：`src/mix_web.py:157-162`

- check-then-compute 无 per-key 锁，两个并发请求同 key 都会解码；无害但浪费

---

## B. 冗余

| # | 位置 | 说明 |
|---|---|---|
| B1 | `src/voice_client.py:253` / `src/voice_ui_handlers.py:679` / `src/mix_render.py:437` | `_probe_duration` **三处近似重复实现**（ffprobe 输出参数格式与超时 15s/30s 略异），应收敛到一个工具模块 |
| B2 | `src/voice_ui_handlers.py:361-542` | `separate_worker`/`cover_worker`/`ensure_separation` 三段重复管线（进度轮询线程、异常回收、取消映射、写记录各三份） |
| B3 | `static/js/app.js` | `initAbcPreview`/`initHistoryAbcPreview`/`initTranscribeAbcPreview` 三套近似实现（find/render 各三份） |
| B4 | `src/voice_ui_handlers.py` L191/553/625/683/788/242/417/505/570/651 | 模块内冗余局部导入（shutil/subprocess/history 模块顶部均已 import）；其中 417/505 带「避免循环导入」注释，因 `history` 已在顶部导入，实际可上提 |
| B5 | `app.py:345, 361` | `batch_count = int(batch_count)` 写了两遍 |
| B6 | `src/history.py:499-509` | `rename_project` 逐条 `_update` 各开一个事务，可合并为单事务 |
| B7 | `src/voice_client.py:379-382` | `heal_ffmpeg_check` 顶部注释自相矛盾（"此处避免误导"）；该函数仍被 `tests/test_voice_client.py` 引用，删除需同步改测试 |
| B8 | 根目录 `=1.47` | pip 误装产物（`pip install mutagen >=1.47` 未加引号），已被 gitignore 但应直接删除 |

---

## C. 可优化

1. **app.py 3699 行单文件** — 历史页/设置页/转谱回调可仿照 voice_ui_handlers 拆出，主文件只留布局与绑定。
2. **历史查询走 SQL**（对应 A4）— `WHERE record_type='generation'` + SELECT 指定列（不取 lyrics 全文）+ 分页 LIMIT；`prune_missing` 降频（写操作后或定时触发，而非每次读）。
3. **任务级超时** — Task 增加 max_runtime，超时标 FAILED 并告警，防 A1 的队列卡死。
4. **`_make_preview` 串行转码** — `src/voice_ui_handlers.py:79-103`，多轨逐个 ffmpeg（单轨超时 900s），可后台化。
5. **`on_preset_save` 24 个位置参数** — `app.py:1983`，建议改 dict/dataclass；`_generate_worker` 的 12 元素位置元组返回同理。
6. **`static/multitrack/index.html` 134KB 单文件**（内联 JS/CSS）— 维护成本高，可拆分（非紧急）。
7. **`(record_type, source_md5)` 无索引** — ≤100 条影响小，且按项目规则加索引前需 staging 验证，暂缓。

---

## 亮点

- 测试覆盖全面：tests/ 共 18 个测试文件，含回收站回归、队列、混音契约、voice handlers 等
- 路径白名单/越界校验（mix_web、history）和回收站删除（SHFileOperationW）实现正确
- 子进程普遍有 timeout（例外为 generate 与转谱两条 CLI 链路，见 A1）
- 全库无 shell=True / os.system / eval 注入风险；SQLite 全程 WAL + 事务 + 锁保护

---

## 建议修复顺序

A1（队列卡死风险）→ A2（数据丢失）→ A4（性能热点）→ A3 / A5 → 其余轻量项可与 B 类冗余清理合并处理。
