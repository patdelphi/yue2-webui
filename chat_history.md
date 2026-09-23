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
