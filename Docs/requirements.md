# YuE2 WebUI 需求文档

> 状态：重建版（原 `demands.md` 已丢失，无法从回收站 / Git / 磁盘恢复）
> 重建依据：`README.md`、`DESIGN.md`、代码实测（`app.py` / `config.py` / `backend_gguf.py` / `history.py` / `postprocess.py`）
> 更新日期：2026-09-23
> 说明：本文为**需求梳理与重建**，非原文逐字还原。带「据设计」标记的条目来自 DESIGN.md 规划，需与原始需求核对；「待核实」条目为无法确证原稿是否收录的项。

## 1. 需求分类总览

| 分类 | 数量 | 说明 |
|------|------|------|
| ✅ 已实现 | 24 | 全部可运行、代码已验证 |
| 🕐 待实现 | 5 | 已明确规划、未完成或未启动 |
| ⚠️ 未实现/阻塞 | 1 | 明确需要但被搁置 |
| 🗑️ 已废弃 | 3 | 旧方案/目标被取代，不再维护 |
| ❓ 待核实 | 3 | 无法确证原稿是否收录 |

---

## 2. ✅ 已实现需求

| ID | 需求 | 状态 | 验证方式 |
|----|------|------|----------|
| F01 | 完整创作模式 `cot=full`（伴奏 + 人声 + 和弦） | ✅ | README / app.py |
| F02 | 旋律创作模式 `cot=melody`（仅旋律，适合翻唱） | ✅ | README / config.py |
| F03 | 直接生成模式 `cot=off`（跳过乐谱，最快） | ✅ | README / config.py |
| F04 | 音频转谱（SheetSage2）：WAV/MP3/FLAC/OGG/M4A → ABC | ✅ | README |
| F05 | ABC 乐谱可视化预览（SVG 渲染）+ 试听 | ✅ | README |
| F06 | ABC 乐谱外部输入 + 生成后回填 + 编辑后「重新合成」 | ✅ | app.py (resynthesize) |
| F07 | 导出 ABC / MIDI / PNG / MP3 | ✅ | README |
| F08 | 歌词结构编辑器：段落标记、拖拽排序、结构实时分析 | ✅ | README |
| F09 | 歌词注释：`//` 或 `**` 开头行忽略，不送模型 | ✅ | README |
| F10 | 歌词模板：结构模板（V-C 等）+ 内容模板 + 段落拖拽 | ✅ | lyrics_templates.py |
| F11 | 歌词同步高亮（前端 data-lyrics 同步） | ✅ | app.py |
| F12 | 风格快捷标签：语言/流派/人声/乐器/情绪 | ✅ | style_presets.py / vocal_presets.py |
| F13 | 「使用上一次」一键恢复上次风格/歌词/乐谱 | ✅ | README |
| F14 | 生成参数：随机种子（+每次自动换）、CFG 引导、ODE 步数、输出格式（PCM16/24/Float32）、批量数量 | ✅ | config.py |
| F15 | Stage1(ABC) / Stage2(语义) 独立采样参数（温度/Top-P/Top-K/重复惩罚/窗口/Min/Max） | ✅ | config.py |
| F16 | 批量变体生成（≤10）+ 独立随机种子 + 变体选择器试听对比 + 选定最终版 / 保留全部 | ✅ | app.py (batch) |
| F17 | 音频后处理：音量标准化、淡入淡出、裁剪静音、嵌入元数据 | ✅ | postprocess.py |
| F18 | 历史管理：分页、试听、歌词同步、乐谱预览、自动清理缺失、删除/清空/刷新 | ✅ | history.py |
| F19 | 删除改为文件级 + 移入系统回收站（不整目录删） | ✅ | history.py（2026-09-23 修复 + 测试） |
| F20 | sidecar JSON 写入完整生成参数（cot/cfg/ODE/批量/采样/模型） | ✅ | postprocess.py（2026-09-23 修复 + 测试） |
| F21 | 参数预设系统：5 套内置（默认/快速demo/高质量/创意/保守）+ 自定义保存/加载 | ✅ | README / presets/ |
| F22 | 设置页：系统状态 / 模型文件检查（主模型+VAE GGUF） | ✅ | app.py |
| F23 | GGUF(audio.cpp) 后端子进程推理；兼容 Python 后端 | ✅ | backend_gguf.py |
| F24 | 多任务队列管理：多个生成任务后台排队、并发 worker、取消/中断控制 | ✅ | queue_manager.py |

## 3. 🕐 待实现需求

| ID | 需求 | 依据 | 说明 |
|----|------|------|------|
| N01 | Python 原生推理后端（PyTorch 全链路） | 据设计(§1.8) | 已声明为「未来升级路径」保留；产出中间产物 semantic.npy / latent.npy / plan.json / config.json / result.json |
| N02 | 断点续传（`--resume` + identity 验证） | 据设计(§1.8) | GGUF 后端目前不支持，Python 后端规划 |
| N03 | 外部噪声注入（`nar_noise_file`） | 据设计(§1.8) | 仅 Python 后端支持，未接 UI |
| N04 | LoRA 适配器前端配置入口（AR/NAR LoRA + scale） | 据设计(§1.6.5) | 参数已在 DESIGN 定义，UI 未暴露 |
| N05 | 历史记录持久化完整采样参数 | 代码分析 | 当前 history.json 仅存 cfg_scale/steps/batch，未存 abc/semantic 采样参数（最新改进建议） |

## 4. ⚠️ 未实现 / 阻塞需求

| ID | 需求 | 阻塞原因 | 建议 |
|----|------|----------|------|
| B01 | 云端/多用户并发部署与配置分发 | 仅本地单用户 Gradio 场景；设计定位为本地工具 | 视为明确未排期的阻塞项，需产品确认是否纳入 |

（注：未在代码与设计中找到更多已声明但搁置的功能，其余候选见「待核实」。）

## 5. 🗑️ 已废弃需求

| ID | 需求 | 废弃原因 |
|----|------|----------|
| D01 | Python pipeline 作为默认推理后端 | GGUF 后端成为默认（RTX 3080 10GB 下 Python 方案 OOM，Boot mm 10GB 唯一可行）；Python 降级为可选升级路径 |
| D02 | 纯 Python 后端的中间产物全套（plan.json/config.json/SHA256 谱系） | 当前 GGUF 后端仅输出 WAV，不再产出该套中间物 |
| D03 | >10GB 显存运行目标 | 已适配并定标 10GB 显卡（~8.7GB 峰值），显存规划以此为据 |

## 6. ❓ 待核实需求（原稿是否收录，无法确证）

| ID | 候选需求 | 备注 |
|----|----------|------|
| U01 | 人声/伴奏分离、去干声等 DSP 处理 | 代码与设计未提及，存疑 |
| U02 | 歌词多语言自动翻译 | 代码与设计未提及，存疑 |
| U03 | 批量生成模式的「批量目录/文本文件」输入（CLI 范畴） | DESIGN 提到 CLI batch，WebUI 已用「批量变体」实现，语义偏离待确认 |

---

## 7. 附：设计要点速览（供需求追踪参考）

- **中间表示**：ABC 乐谱，可编辑实现「白盒」创作。
- **流程**：歌词/风格 → Stage1 ABC 规划(采样) → Stage2 语义 Token(采样) → NAR 声学合成(ODE 步数) → VAE 解码 → 48kHz 立体声。
- **关键参数默认值**：CFG=auto(0)、ODE 8 步(GGUF)、Stage1 温度 0.7，Stage2 温度 1.0；语义 max_tokens 9000。
- **输出**：WAV(PCM16/24/Float32)，full/melody 附带 ABC 乐谱。

---

## 8. 维护说明

- 本文由原 `demands.md` 重建；若找到原稿或你有备份，请覆盖第 3/4/6 节并校正标记。
- 已实现项随代码演进保持同步，建议每次版本迭代更新第 2 节。
- 所有 `.md` 文档后续变更一律走 Code Review，不静默合并。