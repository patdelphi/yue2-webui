# 多轨编辑器接入方案（multitrack editor plan）

> 状态：**M1/M2 已实施**（后端渲染接口 + 独立编辑页端到端打通），M3（Tab 内嵌与打磨）待定。目标是把「音色工坊」分离出的多轨（vocals / accompaniment / drums / bass / other）载入一个多轨编辑器，人工编辑后再合成成品并写回历史。
> 关联文档：`Docs/voice-tools-plan.md`、`Docs/optimization-plan-cover-quality.md`、`Docs/changelog.md`

---

## 1. 目标与范围

**目标**

- 把一次分离（或翻唱）产出的多轨当作素材，在一个可视化多轨界面里做：轨间平衡、静音/独奏、局部裁剪与静音、淡入淡出、片段移动。
- 编辑结果由后端离线渲染为成品，落到 `outputs/` 并写入历史，可在历史页回放/下载。

**不做**

- 不做 MIDI 编曲、不做虚拟乐器、不做实时多轨录音。
- 不替换现有分离 / 翻唱管线（它们是素材来源，保持不变）。

---

## 2. 现状与可复用接入点

| 能力 | 位置 | 说明 |
|---|---|---|
| Worker HTTP 接口 | `voice-tools/worker.py` L2-L6 | `GET /api/health`、`POST /api/separate`、`POST /api/convert`（另有 `/api/cancel`） |
| 分离实现 | `voice-tools/worker.py` L138-L196 | Demucs `apply_model()`，按 2 轨 / 4 轨落盘 |
| 混音链 | `voice-tools/worker.py` L461-L687 | `_convert()` 内：LUFS 对齐 → `aexciter` → ffmpeg `amix` → `loudnorm`（真峰值 -1.5dB） |
| 进度 / 取消 | `voice-tools/worker.py` L77-L102 | `_write_progress()` 写 `_progress.json`；`_check_cancelled()` 在阶段边界检查 |
| Worker 客户端 | `src/voice_client.py` L215-L248、L277-L376 | `_run()` 统一 POST、`_cancel_notify()`、`separate()` / `convert()` 参数校验 |
| 任务队列 | `src/queue_manager.py` L23-L35、L89-L128、L235-L262 | `TaskType.SEPARATION / COVER`，顺序执行 + `cancel_event` 协作取消 |
| 产物目录 | `src/voice_ui_handlers.py` L93-L109 | `outputs/separations_<ts>/`、`outputs/cover_<ts>/`；文件 `<project>_<ts>_<stem>.wav` |
| 历史写入 | `src/voice_ui_handlers.py` L199-L227 | `_record()` 构造 `HistoryRecord`（含 `record_type` / `stems` / `project` / `source_md5`） |
| 历史模型 | `src/history.py` L112-L140、L166-L200 | `stems` 存 JSON 文本列；SQLite（WAL） |
| 多轨播放器 | `app.py` L990-L1025 | `_voice_stem_items()` / `_fill_voice_players()` 按 `elem_id` 前缀填充 |
| 静态路由 | `app.py` L3263-L3272 | `demo.app.routes.insert(0, Route("/static/...", FileResponse(...)))` 模式现成 |
| 页面注入 | `app.py` L3241-L3257 | `custom_index` 在 `</head>` 前插入 `<script src>`（**经典脚本，阻塞式**） |

**关键结论**：素材（等长、同采样率的独立 wav）、任务队列、进度/取消、历史写入这四块都已具备；**唯一缺的是"按编辑参数离线渲染多轨"的接口**。

---

## 3. 技术选型：AudioMass vs waveform-playlist

| 维度 | AudioMass | waveform-playlist |
|---|---|---|
| 许可 | MIT | MIT（建议复核 `LICENSE.md`） |
| 形态 | 完整应用（SPA，`all.build.js` 单 bundle，内含自己的 wavesurfer） | 库/生态：`@waveform-playlist/*`（React 系）+ `@dawcore/*`（Web Components） |
| 构建要求 | 无（直接改 JS + uglify 拼包） | Web Components 路径**无需构建**；React 路径需引入 Vite/React 构建链 |
| 编程 API | 几乎没有：无"载入这组轨道 / 取回编辑结果"接口，工程存自有 AMSS(lzma) 格式 | 强：`@waveform-playlist/engine@13.6.0` headless，`setTracks/moveClip/trimClip/splitClip/undo` + `statechange`；`@dawcore/components` 另有 `editor.exportAudio()` |
| 多轨能力 | 2026 年新增：拖拽 clip、交叉淡变、armed 录制、mixdown | 成熟模型：多 clip/轨、trim/split、音量/声像/静音/独奏、效果链、离线导出 |
| 成熟度 | 多轨较新，单人项目，ES5/IIFE 无模块化无测试 | engine 稳定（13.x）；`@dawcore/components` 仍为 0.0.x（官方标 experimental） |
| 嵌入方式 | iframe 隔离最干净；要"自动预载 + 结果回传"必须 **fork** | 可直接挂载并编程控制；ESM 需 **vendor 到本地**（禁止走 CDN） |
| 与"参数化渲染"契合 | 差（需读内部状态） | **好**：clip/track 模型（起点/偏移/时长/gain）可 1:1 映射为 ffmpeg filter |
| 维护风险 | 上游单人、fork 后难合并 | 上游活跃（1495 commits），两套生态并存演进 |

**选型结论**：走 **waveform-playlist**。优先 `@waveform-playlist/engine`（稳定）+ 自建轻量 UI；若要省 UI 工作量可试 `@dawcore/components`，但需接受 0.0.x API 漂移。**排除 React 路径**（为一个编辑器引入 Node 构建链不划算）。

---

## 4. 分层方案

| 分层 | 内容 | 可行性 | 代价 |
|---|---|---|---|
| **C1 最小验证** | iframe 挂 AudioMass（或 wp 示例页），用户手动选/拖分轨，编辑后导出下载 | 高 | 流程割裂（导出文件需再上传回系统）；用于验证"多轨编辑是否真的有用" |
| **C2 生产接入（推荐）** | 独立页面预载分轨 → 编辑 → **回传结构参数** → 后端 ffmpeg 渲染 → 写历史 | 中高 | 主要工作量在后端渲染接口 + 数据契约 |
| **C3 完全内嵌 Tab** | 不用 iframe，直接嵌进 Gradio Tab | 中 | 需处理 Gradio 重渲染冲突（可复用现有 `MutationObserver` 重绑先例），收益有限 |

### 为什么 C2 回传"参数"而不是"浏览器渲染的成品"

- 精度与一致性：后端 `ffmpeg` + `loudnorm` 与现有链一致，避免二次编码与响度失控。
- 可复现与可存档：工程参数 JSON 与 `history.db` 天然对齐，随时可重渲染。
- 前端只做交互与波形显示，渲染责任单一，便于测试（filter 生成可单测）。

---

## 5. C2 架构设计（待确认后实施）

### 5.1 数据契约：编辑工程 JSON

```json
{
  "version": 1,
  "sample_rate": 48000,
  "source_root_task_id": "sep-20260927-104930",
  "duration": 224.31,
  "tracks": [
    {
      "id": "vocals",
      "name": "人声",
      "src": "outputs/separations_20260927_104930/xxx_vocals.wav",
      "gain_db": -1.5,
      "mute": false,
      "clips": [
        { "start": 0.0, "in": 12.5, "out": 40.0, "fade_in": 0.2, "fade_out": 0.5 }
      ]
    }
  ],
  "master": { "loudness_target": -14.0, "true_peak": -1.5 }
}
```

- `src` 只允许 `outputs/` 下、且属于同一 `root_task_id` 的已登记产物（白名单校验，禁止任意路径）。
- `clips` 为空表示整轨全长；`in/out` 为素材内偏移，`start` 为时间线位置。

### 5.2 后端渲染接口

- 新增 `TaskType.MIX`（`src/queue_manager.py` L23-L35），走既有队列（顺序执行 + 可取消）。
- 新增执行器（建议 `src/mix_handlers.py`，或并入 `src/voice_ui_handlers.py`）：
  - **不依赖 voice-tools venv**，直接用系统 `ffmpeg`（与现有 `aipython`/ffmpeg 约定一致），因此"多轨编辑"不受是否安装音色工坊影响。
  - 参数校验 + filtergraph 生成（可单测）→ 执行 → 产物落盘 → 写历史。
- 参考 filtergraph：

```
[0:a]atrim=start=12.5:end=40,asetpts=N/SR/TB,volume=-1.5dB,afade=t=in:st=0:d=0.2[ a0];
[1:a]volume=0dB[a1];
[a0][a1]amix=inputs=2:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11
```

- 对齐要求：各轨按最短长度 `apad`/`atrim` 对齐；与源轨叠加前做一致性检查（样本数差、相位）。

### 5.3 产物与历史

- 产物目录：`outputs/mix_<ts>/`，文件 `<project>_<ts>_mix.flac`（或 wav）。
- 历史：`record_type="mix"`，`derived_from` 指向源分离任务，`stems` 记录本次参与混音的各轨 + 成品。
- 历史页/分离页回放逻辑沿用现有 `_fill_voice_players()` 前缀机制，不新增播放器体系。

### 5.4 前端

- 独立静态页（例如 `/static/multitrack/index.html`），通过 `app.py` 既有 `Route(...)` 模式注册（`app.py` L3263-L3272）。
- 由 Gradio 侧传入 `root_task_id` 或 `stems` 列表（URL 参数或 `postMessage`），页面据此预载轨道。
- 依赖 vendor 到 `static/js/vendor/`（**不走 CDN**——此前 abcjs CDN 阻塞曾导致 `app.js` 不执行的教训）。

---

## 6. 任务拆分（待确认后执行）

- **M0｜C1 最小验证**：vendor 编辑器 + 静态路由 + 手动导入一组真实分离轨，确认交互与听感收益。
- **M1｜数据契约 + 渲染接口**：工程 JSON schema + `TaskType.MIX` + filtergraph 生成器 + 单元测试（含非法参数回退、路径白名单）。✅ 已完成（2026-09-28）：`src/mix_render.py` + `tests/test_mix_render.py`（20 项全通过）。
- **M2｜端到端**：前端预载分轨 → 保存工程 → 触发渲染 → 产物写入历史 → 历史页回放/下载。✅ 已完成（2026-09-28）：`src/mix_web.py` + `static/multitrack/index.html` + `app.py` 路由/入口 + `tests/test_mix_web.py`（22 项全通过）。
  - 编辑粒度（已确认）：轨间平衡（增益/静音/独奏）+ 全局选区裁切 + 每轨淡入淡出；不做多切片自由摆放。
  - 交付形态：独立静态页 `/static/multitrack/`（分离页「多轨编辑」按钮新窗口打开），混音产物的回放/下载在编辑页内完成。
- **M3｜打磨**：Tab 内嵌、进度与取消接入现有机制、中英文案（`src/i18n.py`）、`changelog.md` 更新。部分已完成（进度/取消、i18n、changelog 随 M2 落地），仅剩 Tab 内嵌（C3）与工程持久化待定。

---

## 7. 验收标准

1. 用一组真实分离轨（如 `outputs/separations_*/{vocals,accompaniment}.wav`）渲染成品：真峰值 ≤ -1.5 dBTP、无削波、与源曲响度差 ≤ 1 LU。
2. 同一工程 JSON 重复渲染结果一致（可复现）。
3. 单元测试通过：filtergraph 生成（多轨/静音/淡变/裁剪）、参数校验、路径白名单、非法值回退。
4. 渲染任务可取消，进度可通过既有机制上报。
5. 产物可在历史页回放与下载，删除记录时文件一并回收。

---

## 8. 风险与未决问题

| 风险 | 说明 | 应对 |
|---|---|---|
| `@dawcore/components` 为 0.0.x | API 可能变更 | 优先用稳定的 `@waveform-playlist/engine`，自建轻 UI |
| 依赖 vendor 化 | 需一次性 Node 打包或手工 vendor ESM | 固化到 `static/js/vendor/`，纳入版本控制（体积可控） |
| 后端渲染接口是新链路 | 与 voice-tools worker 解耦需要新代码 | 单列 `TaskType.MIX`，复用队列与取消机制 |
| 相位/对齐 | Demucs 各轨相加近似还原；MDX 不保证 | 渲染前做一致性检查，必要时提示用户 |
| AudioMass 若被选中 | 必须 fork 维护（单人上游、无测试） | 仅在 C1 验证阶段临时使用 |

**未决**

- ~~是否需要"局部静音/淡变"这类精细编辑，还是"轨间平衡 + 静音/独奏"就满足？~~ 已定：轨间平衡 + 全局选区裁切 + 每轨淡入淡出（M2 落地），不做多切片自由摆放。
- 成品格式：flac（无损、体积小）还是 wav？——M2 取 flac（`render_mix` 按扩展名选编码器，wav 路径仍保留）。
- 是否需要把混音工程持久化为可再次打开的项目？（M3 待定）

---

## 9. 参考来源

- AudioMass：<https://github.com/pkalogiros/AudioMass>（MIT；多轨为 2026 年新增）
- waveform-playlist 文档：<https://naomiaro.github.io/waveform-playlist/docs/>
- `@waveform-playlist/engine`：<https://www.npmjs.com/package/@waveform-playlist/engine>
- `@dawcore/components`：<https://www.npmjs.com/package/@dawcore/components>
