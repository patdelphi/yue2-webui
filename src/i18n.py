# -*- coding: utf-8 -*-
"""应用级 i18n：运行时用「语言 -> {中文原文: 译文}」字典，.po 文件仅作 Babel 归档。

设计要点：
- 中文原文即 msgid（保证 build_ui 里 tr(lang, "风格描述") 可读性强）。
- 语言 zh 时直接返回原文；语言 en 时查 EN_TABLE，缺 key 回退原文。
- 不使用原生 gettext 的进程级单域 install（运行时切换会全局受影响且与 gr.update 冲突），
  因此运行时真相源是字典，Babel/pygettext 仅用于抽取与归档（维护可选）。
"""

from __future__ import annotations
from pathlib import Path
from typing import Dict

# 默认语言
DEFAULT_LANG = "zh"

# locales 位于 webui 根目录（本模块已移至 src/，需向上一级）
LOCALES_ROOT = Path(__file__).parent.parent / "locales"

SUPPORTED_LANGS = ("zh", "en")

# 英文译文表：key 为中文原文（msgid），value 为英文译文。
EN_TABLE: Dict[str, str] = {
    "语言 / Language": "Language",
    "创作": "Create",
    "音频转谱": "Transcribe",
    "历史": "History",
    "设置": "Settings",
    "AI音乐创作 — 输入歌词和风格，生成完整歌曲": "AI music studio - input lyrics and style to generate full songs",
    "风格描述": "Style description",
    "使用上一次": "Use last time",
    "风格标签": "Style tags",
    "人声标签": "Vocal tags",
    "乐器标签": "Instrument tags",
    "情绪标签": "Mood tags",
    "语言标签": "Language tags",
    "流派标签": "Genre tags",
    "#### 风格快捷标签": "Quick style tags",
    "歌词": "Lyrics",
    "在这里输入歌词...": "Enter lyrics here...",
    "副歌歌词...": "Chorus lyrics...",
    "歌词工具": "Lyrics tools",
    "段落标记说明": "Section marker notes",
    "#### 段落标记说明": "#### Section marker notes",
    "前奏/器乐引入": "Intro / instrumental intro",
    "主歌段落": "Verse section",
    "预副歌，制造期待感": "Pre-chorus, builds anticipation",
    "副歌，全曲最抓耳的部分": "Chorus, most catchy part",
    "桥段，打破重复，情感转折": "Bridge, breaks repetition, emotional turn",
    "尾声/渐弱收尾": "Outro / fade-out ending",
    "注释行：以 `//` 或 `**` 开头的行视为注释，不会送入模型生成。": "Lines starting with `//` or `**` are comments and are not sent to the model.",
    "#### 歌曲结构模板": "Song structure templates",
    "段落拖拽排序": "Drag-sort sections",
    "结构分析": "Structure analysis",
    "结构:": "Structure:",
    "行": " lines",
    "输入歌词后显示结构分析": "Structure analysis appears after you enter lyrics",
    "歌词模板 (内容)": "Lyrics template (content)",
    "选择模板将填充歌词内容（覆盖现有内容）": "Selecting a template fills the lyrics content (overwrites current)",
    "### 工作模式": "Work mode",
    "完整创作 (生成乐谱+和弦)": "Full creation (score + chords)",
    "旋律创作 (仅旋律，适合翻唱)": "Melody only (for covers)",
    "直接生成 (跳过乐谱，最快)": "Direct (skip score, fastest)",
    "ABC 乐谱 (外部输入)": "ABC score (external input)",
    "提供外部ABC乐谱文本。仅在 full/melody 模式下生效。留空则自动生成。": "Paste external ABC score text. Only effective in full/melody modes. Leave empty to auto-generate.",
    "随机种子": "Random seed",
    "🎲 随机": "🎲 Random",
    "随机种子变化": "Randomize seed",
    "勾选「随机种子变化」时每次生成自动换新，此处显示实际使用的种子": "When \"Randomize seed\" is checked, each generation uses a new seed; the actual seed used is shown here",
    "勾选: 每次点击「生成歌曲」自动换新种子; 取消勾选: 使用上方固定种子": "Check: new seed on each \"Generate song\"; uncheck: use the fixed seed above",
    "CFG 引导强度": "CFG guidance scale",
    "0=Auto (off模式=1.01, 其他=1.0)": "0=Auto (off mode=1.01, others=1.0)",
    "ODE 求解步数": "ODE steps",
    "8=快速, 16=标准, 32=高质量": "8=fast, 16=standard, 32=high quality",
    "输出格式": "Output format",
    "PCM 16-bit (标准)": "PCM 16-bit (standard)",
    "PCM 24-bit (高动态)": "PCM 24-bit (high dynamics)",
    "Float 32-bit (最大动态)": "Float 32-bit (max dynamics)",
    "PCM16=标准质量, PCM24=更高动态范围, Float32=最大动态范围(文件更大)": "PCM16=standard, PCM24=higher dynamic range, Float32=max range (bigger file)",
    "批量生成数量": "Number of variants",
    "一次生成多个变体 (每个变体使用独立随机种子)": "Generate multiple variants (each with an independent seed)",
    "音频后处理": "Audio post-processing",
    "#### 后处理选项": "#### Post-processing options",
    "音量标准化": "Normalize volume",
    "归一化到 -1dB": "Normalize to -1dB",
    "淡入淡出": "Fade in/out",
    "首尾各 0.5 秒": "0.5s at start and end",
    "裁剪静音": "Trim silence",
    "移除首尾静音 (< -40dB)": "Remove leading/trailing silence (< -40dB)",
    "嵌入元数据": "Embed metadata",
    "标题/风格/种子": "Title / style / seed",
    "高级采样参数": "Advanced sampling params",
    "#### ABC 乐谱采样 (Stage 1)": "#### ABC score sampling (Stage 1)",
    "ABC 温度": "ABC temperature",
    "ABC 重复惩罚": "ABC repetition penalty",
    "ABC 惩罚窗口": "ABC penalty window",
    "ABC Min Tokens": "ABC Min Tokens",
    "ABC Max Tokens": "ABC Max Tokens",
    "#### 语义 Token 采样 (Stage 2)": "#### Semantic token sampling (Stage 2)",
    "语义 温度": "Semantic temperature",
    "语义 Top-P": "Semantic Top-P",
    "语义 Top-K": "Semantic Top-K",
    "语义 重复惩罚": "Semantic repetition penalty",
    "语义 惩罚窗口": "Semantic penalty window",
    "语义 Min Tokens": "Semantic Min Tokens",
    "语义 Max Tokens": "Semantic Max Tokens",
    "🎵 生成歌曲": "🎵 Generate song",
    "取消": "Cancel",
    "=== 输出 ===": "=== Output ===",
    "### 输入": "### Input",
    "### 输出": "### Output",
    "生成的歌曲": "Generated song",
    "批量变体选择": "Variant selector",
    "✅ 选定为最终版": "✅ Set as final",
    "保留全部变体": "Keep all variants",
    "### ABC 乐谱": "### ABC score",
    "生成的乐谱 (可编辑)": "Generated score (editable)",
    "ABC 乐谱文本": "ABC score text",
    "生成后可编辑乐谱，点击「重新合成」使用修改后的乐谱生成新音频": "Editable after generation. Click \"Resynthesize\" to generate audio from the edited score",
    "#### 乐谱预览": "#### Score preview",
    "生成歌曲后乐谱将在此处渲染": "Score will render here after generation",
    "导出 MIDI": "Export MIDI",
    "导出 PNG": "Export PNG",
    "下载乐谱": "Download score",
    "下载 MP3": "Download MP3",
    "重新合成": "Resynthesize",
    "### 音频转乐谱": "### Transcribe to score",
    "上传音频文件，使用 SheetSage2 模型自动转写为 ABC 乐谱": "Upload audio; SheetSage2 transcribes it to an ABC score automatically",
    "上传音频 (支持 WAV/MP3/FLAC/OGG/M4A 等)": "Upload audio (WAV/MP3/FLAC/OGG/M4A etc.)",
    "开始转谱": "Start transcribing",
    "→ 发送到生成页": "→ Send to Create",
    "转谱完成后乐谱将显示在这里...": "The score will appear here after transcription...",
    "转谱后乐谱预览将在此处显示": "Score preview will appear here after transcription",
    "下载 ABC": "Download ABC",
    "下载 MIDI": "Download MIDI",
    "### 生成历史": "### Generation history",
    "时间": "Time",
    "风格": "Style",
    "模式": "Mode",
    "音频时长": "Duration",
    "生成耗时": "Elapsed",
    "Task ID": "Task ID",
    "上一页": "Prev",
    "下一页": "Next",
    "试听": "Preview",
    "歌词同步": "Lyric sync",
    "点击历史记录后乐谱将在此处渲染": "Score renders here after selecting a history entry",
    "刷新": "Refresh",
    "删除选中": "Delete selected",
    "清空历史": "Clear history",
    # —— 以下为动态文案 / 校验 / 进度 / 设置类 (运行时 tr()) ——
    "ABC 乐谱 (可编辑)": "ABC score (editable)",
    "ABC 乐谱不能为空": "ABC score cannot be empty",
    "CFG强度必须在0-20之间": "CFG scale must be between 0 and 20",
    "CUDA 不可用": "CUDA unavailable",
    "GPU 信息": "GPU info",
    "ODE步数必须在1-64之间": "ODE steps must be between 1 and 64",
    "PyTorch 未安装": "PyTorch not installed",
    "✅ 所有模型文件就绪": "✅ All model files ready",
    "❌ 模型文件缺失": "❌ Model files missing",
    "不支持的音频格式": "Unsupported audio format",
    "个变体": "variants",
    "个段落": "sections",
    "为最终版": "as the final version",
    "主模型": "Main model",
    "任务已取消": "Task cancelled",
    "未知错误": "Unknown error",
    "保存当前参数": "Save current params",
    "保存预设名称": "Preset name",
    "删除失败": "Failed to delete",
    "剩余": "left",
    "加载": "Load",
    "加载模型...": "Loading model...",
    "加载预设": "Load preset",
    "默认": "Default",
    "快速demo": "Quick Demo",
    "高质量": "High Quality",
    "创意模式": "Creative Mode",
    "保守模式": "Conservative Mode",
    "变体": "Variant",
    "变体不存在": "Variant does not exist",
    "合成音乐...": "Synthesizing music...",
    "合成音频...": "Synthesizing audio...",
    "后处理音频...": "Post-processing audio...",
    "处理中...": "Processing...",
    "字符": "characters",
    "完成": "Done",
    "就绪": "Ready",
    "已保存预设": "Preset saved",
    "已删除": "Deleted",
    "已清空所有历史": "Cleared all history",
    "平均音频时长": "Avg duration",
    "开始重新合成...": "Starting resynthesis...",
    "总耗时": "Total time",
    "我的预设": "My preset",
    "执行中...": "Running...",
    "批量生成": "Batch generate",
    "排队中...": "Queued...",
    "前面还有 {n} 个任务": "{n} task(s) ahead",
    "支持 [Verse] [Chorus] [Bridge] 段落标记，可拖拽排序": "Supports [Verse] [Chorus] [Bridge] section markers; drag to reorder",
    "支持的格式": "Supported formats",
    "显存": "VRAM",
    "检查模型": "Check models",
    "模型状态": "Model status",
    "歌词不能为空": "Lyrics cannot be empty",
    "正在取消...": "Cancelling...",
    "没有可发送的乐谱内容": "No score content to send",
    "没有可用的批量变体": "No batch variants available",
    "没有正在运行的任务": "No task is running",
    "生成变体": "Generating variant",
    "生成失败": "Generation failed",
    "生成音乐...": "Generating music...",
    "磁盘": "Disk",
    "种子必须为非负整数": "Seed must be a non-negative integer",
    "缺失": "missing",
    "行歌词": "lines",
    "规划乐谱...": "Planning score...",
    "解码音频...": "Decoding audio...",
    "记录不存在": "Record does not exist",
    "设备": "Device",
    "语言 + 流派 + 乐器 + 人声 + 速度": "Language + genre + instruments + vocals + tempo",
    "请先上传音频文件": "Please upload an audio file first",
    "请先点击选择要删除的记录": "Please select a record to delete first",
    "请输入歌词": "Please enter lyrics",
    "请输入预设名称": "Please enter a preset name",
    "请输入风格描述": "Please enter a style description",
    "请选择一条记录": "Please select a record",
    "转谱": "Transcribe",
    "转谱中...": "Transcribing...",
    "转谱失败": "Transcription failed",
    "转谱耗时": "Transcription time",
    # —— backend_gguf 推理后端文案（generate/transcribe 的错误与进度，随任务语言）——
    "已取消": "Cancelled",
    "退出码": "exit code",
    "生成完成但输出文件不存在": "Generation finished but the output file is missing",
    "SheetSage2 模型未找到，请检查 config.cfg 中 sheetsage2_path 配置": "SheetSage2 model not found; check sheetsage2_path in config.cfg",
    "转谱完成但未生成乐谱": "Transcription finished but no score was generated",
    "转谱失败，退出码": "Transcription failed with exit code",
    "无输出": "no output",
    "加载 SheetSage2 模型...": "Loading SheetSage2 model...",
    "转谱完成": "Transcription complete",
    "重新合成中...": "Resynthesizing...",
    # —— 设置页队列状态窗口 ——
    "### 当前队列": "### Current queue",
    "空闲": "Idle",
    "无运行中或排队任务": "No running or queued tasks",
    "运行中": "Running",
    "排队中": "Queued",
    "等待": "waiting",
    "已用时": "elapsed",
    "进度": "progress",
    "最近任务": "Recent tasks",
    "生成": "Generation",
    "失败": "failed",
    "已取消": "cancelled",
    "秒": "s",
    "队列 worker 线程异常，请重启服务": "Queue worker thread error; please restart the service",
    "重新合成失败": "Resynthesis failed",
    "重新合成耗时": "Resynthesis time",
    "音频文件不存在": "Audio file does not exist",
    "预估时长": "Estimated duration",
    "风格描述不能为空": "Style description cannot be empty",
    "，全部变体已保留": ", all variants kept",
    "，已清理其余": ", removed the other",
    "🏆 已选定": "🏆 Selected",
    "### 系统状态": "### System status",
    "### 参数预设": "### Parameter presets",
    "标记": "Marker",
    "用途": "Purpose",
    "乐谱预览": "Score preview",
    "生成歌曲后乐谱将在此处渲染": "Sheet music will be rendered here after song generation",
    "转谱后乐谱预览将在此处显示": "Sheet music preview will appear here after transcription",
    "点击历史记录后乐谱将在此处渲染": "Sheet music will be rendered here after you click a history record",
}


def tr(lang: str, text: str) -> str:
    """按语言返回文案；语言非 en 或缺少译文时回退原文（中文）。"""
    if lang == "en":
        return EN_TABLE.get(text, text)
    return text


def normalize_lang(lang) -> str:
    """把 'zh-CN'/'zh'/'en-US'/'en'/'中文'/'English' 等归一化为支持的短标识。"""
    if not lang:
        return DEFAULT_LANG
    if lang == "English":
        return "en"
    if lang == "中文":
        return "zh"
    key = str(lang).lower().strip()
    if key.startswith("zh"):
        return "zh"
    if key.startswith("en"):
        return "en"
    return DEFAULT_LANG


def load_po_to_en() -> Dict[str, str]:
    """（可选）从 locales/en/LC_MESSAGES/yue2.po 读取译文合并进 EN_TABLE。失败静默跳过。"""
    po = LOCALES_ROOT / "en" / "LC_MESSAGES" / "yue2.po"
    if not po.exists():
        return {}
    try:
        import polib
        merged = {}
        for entry in polib.pofile(str(po)):
            if entry.msgid and entry.msgstr:
                merged[entry.msgid] = entry.msgstr
        return merged
    except Exception:
        return {}