# yue2-webui 代码审计修复 TODO

- **来源**：`Docs/code-audit-2026-10-06.md`（已逐条复核订正）
- **状态**：已完成（A1–A10 + B4/B5/B6/B7/B8；未做 B1/B2/B3/C1/C6/C7），全量 pytest 219 passed
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

| # | 方案 | 风险 |
|---|---|---|
| C2 | 历史查询走 SQL（同 A4，合并处理） | 中高 |
| C3 | Task 增加 `max_runtime` 任务级超时，防队列卡死 | 中 |
| C4 | `_make_preview` 转码后台化 | 中 |
| C5 | `on_preset_save` 24 参数改 dict/dataclass；`_generate_worker` 12 元组改结构 | 中 |
| C1 / C6 | 拆分 app.py（3699 行）/ multitrack 单文件（134KB） | 高（建议暂缓） |
| C7 | `(record_type, source_md5)` 加索引（需 staging 验证） | 中（暂缓） |

---

## 建议执行顺序
A1 → A2 → A3 → A5/A6/A7/A8/A9/A10 → A4(=C2) → B4/B5/B6/B7/B8 → B1 → C3/C4/C5

## 验证
- 每步：`py_compile` + 对应 pytest 用例
- 收尾：`pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"` 全绿
