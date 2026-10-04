# TODO — 多轨编辑器：页面高度自适应 + FX 面板增强

> 范围：`yue2-webui/static/multitrack/index.html`（页面本体）+ `yue2-webui/src/mix_render.py`（仅新增旁通字段）+ `app.py`（可选）
> 不做：不改轨道头/波形/transport 语义，不改既有参数含义，不做 git 操作

---

## 一、任务清单

| # | 任务 | 关键改动 | 验收标准 |
|---|---|---|---|
| T1 | **编辑器脱离固定 760px，随内容自适应高度** | 编辑器侧用 `window.frameElement` 直接改写自身 iframe 高度（同源），`ResizeObserver` 监听内容高度变化 | 编辑器内容变长时父页整体变高、顶部标题/菜单可随页面滚动移出视口；无内部滚动条（长内容除外） |
| T2 | **音质 / 压缩 / 回声 各加「一键归零」按钮** | 模块标题条右侧加小按钮；`mkFxCtl` 暴露 `resetV()`（复位到中性值并广播） | 点击后该模块所有旋钮回中性值，数值徽标、圆弧、EQ 曲线、实时声音同步复位 |
| T3 | **音质 = 压缩 固定同宽** | `--fxmod-w: 362px`，两者 `width: var(--fxmod-w)`，压缩模块内容水平居中 | 两模块左右边框对齐，压缩模块内容居中不贴边 |
| T4 | **右侧新增「FX 工具」模块** | 面板最右（`margin-left:auto`）新增模块：旁通开关 / FX 预设 / 复制 FX 到其他轨 | 三项功能均可用，窄屏自动折行不溢出 |
| T5 | **旁通（Bypass）前后端贯通** | 前端 `t.fxOn` + `applyFxParams` 旁通时全部置中性；工程 JSON 加 `fx_on`；`mix_render` 加 `fx_on` 字段，为假时 `_fx_filters` 返回 `[]` | A/B 试听可切换；导出成品与旁通状态一致（渲染后频谱无 FX 痕迹） |
| T6 | **FX 预设**（内置档 + 保存自定义） | 内置：中性 / 人声 / 伴奏；「应用」一键套用本轨；「保存」把当前轨 FX 存为自定义预设（localStorage） | 应用后旋钮与声音同步变化；刷新页面后自定义预设仍在 |
| T7 | **复制 FX 到其他轨** | 下拉选目标（其他轨 / 全部其他轨）+「复制」按钮，复制 pan/EQ/滤波/压缩/回声 + 旁通状态 | 目标轨旋钮、曲线、实时声音同步更新 |
| T8 | **测试与文档** | 更新 `tests/test_mix_web.py`（面板结构断言）+ `tests/test_mix_render.py`（`fx_on` 滤镜链断言）；`changelog.md` + `chat_history.md`（UTF-8 BOM + CRLF） | `pytest` 全绿（主测试 + mix 测试）；浏览器实测明/暗主题、窄屏折行 |

---

## 二、布局示意（改造后单轨 FX 面板）

```
[IN表] [─ 音质 ──归零─] [─ 压缩 ──归零─] [─ 回声 ─] [OUT表]        [─ FX 工具 ─]
       │曲线│旋钮×6  │  │GR表│旋钮×2 │ │推子×3 │                  │ [旁通] 开关 │
       └────────362──┘  └────362────┘ └───────┘                  │ 预设[▾][应用]│
                                                                  │ 复制[▾][复制]│
```

---

## 三、参数中性值（复用现有 init 值，归零即回到此）

| 模块 | 参数 |
|---|---|
| 音质 | pan 0 / eqLow 0 / eqMid 0 / eqHigh 0 / hpf 0 / lpf 0 |
| 压缩 | compTh 0（=关闭）/ compRatio 1（=关闭） |
| 回声 | echoDelay 0 / echoFb 0 / echoMix 0（混合 0 = 关闭回声） |

---

## 四、风险与应对

| 风险 | 应对 |
|---|---|
| iframe 高度自适应可能反复抖动（父页变高→子页重排） | 高度差 < 1px 不写；`ResizeObserver` + rAF 合并；只在本页内容高度变化时推送 |
| 面板新增模块后高度变高 | 「FX 工具」模块内容压缩为 3 行小控件；`align-items: stretch` 保证与其他模块上下沿对齐 |
| 固定 362px 宽在窄视口溢出 | 沿用 `flex-wrap: wrap`；媒体查询下改为 `width:auto` |
| 旁通与「中性值」语义混淆 | 旁通只影响音频链与导出，不改旋钮数值（保留用户设置，便于 A/B） |

---

# TODO — 远端回放「大文件」同类问题：整体排查与修复

> 起因：分离/翻唱产物为 32bit float WAV（单轨 63–101MB），Gradio 前端须整文件下完才给 `<audio>` 挂 src，
> 远端经 Cloudflare 隧道（约 1.2MB/s）要几十秒到几分钟。已用「随产物生成 MP3 192k 预览小件 + 回放取小件 + loading 提示」修复分离/翻唱。
> 本轮要求：排查**其它模块**是否有同类问题，整体修改并测试。

## 一、审计结果（已完成的只读排查）

`outputs/` 体积分布实测：`.wav` 24 个共 1630MB（均 68MB）；`.flac` 11 个 164MB；`.mp3` 24 个 123MB（均 5MB）。

| 回放位置 | 取值来源 | 实际文件 | 典型体积 | 现状 |
|---|---|---|---|---|
| `sep-audio-*` / `cover-audio-*`（分离/翻唱**当场**回放） | `_voice_stem_items(result["stems"])` | preview mp3 | 5MB | ✅ 已修 |
| `sep-history-audio-*` / `cover-history-audio-*`（任务历史回放） | `_voice_stem_items(entry.stems)` | preview mp3 | 5MB | ✅ 已修 |
| **`history-stem-audio` + 「轨道回放(分离/翻唱)」下拉** | `stems[*].path` 原样 | 32bit float WAV | 63–101MB | ❌ 待修 |
| **`gen-audio` / `history-audio`（主播放器）** | `entry.audio_path` = `.wav` | WAV | 31–48MB | ❌ 待修（同目录已有 `.mp3` 约 3.5MB） |
| **`lib-stem-preview` / `lib-ref-preview` / `cover-ref-preview` / `cover-acc-preview`（库试听）** | 库文件（WAV 副本） | WAV | 上传干声 10–60MB | ❌ 待修 |
| **多轨编辑器**（`/static/multitrack/`） | `GET /api/mix/audio?path=` 每轨全文件 → `decodeAudioData` | 32bit float WAV | 单轨 63–101MB，4 轨约 250MB | ❌ 待修（方案待定） |
| 音频转谱 | 无音频产物（仅 `.abc`/`.mid`/`events.json`） | — | — | ✅ 无此问题 |

## 二、任务清单

- **P0（低风险，照搬已验证模式）**：歌曲历史页「轨道回放」下拉 + `history-stem-audio`
  改用 `_voice_stem_items()`（preview 优先 + `<原名>_preview.mp3` 命名推导 + 缺失回退原件）。
- **P1（需确认副作用）**：`gen-audio` / `history-audio` 主播放器改为优先取同目录同名 `.mp3`（缺失回退 WAV）。
  副作用：播放器自带下载按钮给出的从 WAV 变为 MP3（生成页本就另有「下载 MP3」槽位，仅历史页会失去即点即下 WAV）。
- **P2（低风险）**：库试听 4 个播放器统一走 preview 小件（复用 `_make_preview` 命名推导；库文件首次试听时按需生成）。
- **P3（方案待定）**：多轨编辑器播放加速。候选：
  - (a) 播放走 `<原名>_preview.mp3`，渲染仍用原件 —— 最快，但 MP3 编解码延迟/padding 会造成轨间毫秒级错位（编辑器要求样本级同步）。
  - (b) 生成 **16bit PCM WAV** 预览件（`_preview16.wav`）—— 体积约减半（63MB→32MB）、对齐无损，但仍偏大。
  - (c) 不改格式，仅强化"正在解码音频 x/y"进度提示（现状已有）。

## 三、验证计划

- 复用/新增单测：`tests/test_voice_handlers.py`（preview 命名推导）、`tests/test_theme_light.py`（源码级接线断言）、`tests/test_history_filter.py`（历史页回放取小件）。
- 全量 `pytest tests/ --ignore=tests/test_i18n.py -q --basetemp=".pytest_tmp_all"`（当前基线 190）。
- 远端实测：`https://yue2.patdelphi.xyz/` 逐项确认网络请求落在小件、波形快速出现。
- 注意：`app.js` 内容若变更需同步升级 `app.js?v=N`（Cloudflare 边缘会缓存旧文件）。
