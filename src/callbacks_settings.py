"""设置页预设相关回调（C1 拆分 app.py：第三阶段 · 设置组）。

依赖以参数注入，本模块不 import app.py（避免循环依赖）；app.py 保留同名薄封装，
在调用时读取当前全局（WEBUI_ROOT / _CUR_LANG），因此既有的猴子补丁与调用方式不变。
"""
import json
import logging
import shutil
from pathlib import Path

import gradio as gr

from app_utils import (BUILTIN_PRESETS, PRESET_PARAM_KEYS, COMMENT_PREFIXES,
                       strip_comment_lines)
from i18n import tr
from queue_manager import queue_manager
from voice_client import check_voice_models

logger = logging.getLogger(__name__)

# 队列任务类型 → 展示名（中文原文走 tr 翻译）
_QUEUE_TYPE_LABELS = {"generation": "生成", "transcription": "转谱",
                      "separation": "分离", "cover": "翻唱"}


def preset_display_names(lang: str, root: Path, builtin: dict = BUILTIN_PRESETS) -> list:
    """预设下拉框显示名：内置预设名按语言翻译，用户自定义预设保持文件名原样。"""
    names = [tr(lang, k) for k in builtin]
    user_dir = root / "presets"
    if user_dir.exists():
        names += [p.stem for p in sorted(user_dir.glob("*.json"))]
    return names


def preset_load(name, lang: str, root: Path, builtin: dict = BUILTIN_PRESETS,
                keys: tuple = PRESET_PARAM_KEYS):
    """Load a preset and return param values.

    返回 23 个字段（17 旧字段 + CFG/批量 + 4 个后处理开关）。
    预设中缺失的字段返回 gr.update()（前端保持当前值），保证旧格式
    预设文件与仅覆盖部分字段的内置预设（如「快速demo」）不被默认值覆盖。
    """
    preset = builtin.get(name)
    if not preset:
        # 英文界面下选中翻译名（如 Default）时，反查回中文内置键
        for k in builtin:
            if tr(lang, k) == name:
                preset = builtin[k]
                break
    if not preset:
        preset_path = root / "presets" / f"{name}.json"
        if preset_path.exists():
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
        else:
            return [gr.update() for _ in range(23)]

    p = preset.get("params", {})

    def _pv(key):
        """声明字段返回存储值，缺失字段返回 gr.update()（保持当前值）。"""
        return p[key] if key in p else gr.update()

    # 前 3 项缺失走具体默认值，其余字段缺失返回 gr.update()（保持当前值）
    head = [
        p["cot"] if "cot" in p else "full",
        p.get("num_inference_steps", 8),
        p.get("out_format", "pcm16"),
    ]
    return head + [_pv(k) for k in keys[3:]]


def preset_save(name, values, lang: str, root: Path, keys: tuple = PRESET_PARAM_KEYS):
    """Save current params as a preset（含 CFG/批量数量/后处理开关等全部生成参数）。

    输入值按 PRESET_PARAM_KEYS 顺序与 JSON 键一一对应，避免长位置参数错位。
    """
    if not name or not name.strip():
        return tr(lang, "请输入预设名称")
    # 防御：组件绑定正常情况下传入 23 个值，数量不符说明调用方错位，直接报错不静默落盘
    if len(values) != len(keys):
        raise ValueError(f"preset params count mismatch: got {len(values)}, "
                         f"expect {len(keys)}")
    presets_dir = root / "presets"
    presets_dir.mkdir(exist_ok=True)
    preset = {
        "name": name.strip(),
        "params": dict(zip(keys, values)),
    }
    path = presets_dir / f"{name.strip()}.json"
    path.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"{tr(lang, '已保存预设')}: {name}"


# ---------------------------------------------------------------------------
# 设置页状态回调（模型检查 / 队列状态 / 歌词结构分析）
# ---------------------------------------------------------------------------

def on_check_models(backend, lang: str, project_root: Path):
    """Check model files and return status."""
    checks = backend.check_models()
    lines = [f"### {tr(lang, '模型状态')}\n"]
    if checks["available"]:
        lines.append(f"{tr(lang, '✅ 所有模型文件就绪')}\n")
    else:
        lines.append(f"{tr(lang, '❌ 模型文件缺失')}\n")

    lines.append(f"- {tr(lang, '主模型')}: {'✅' if checks['model_gguf']['exists'] else '❌'} `{checks['model_gguf']['path']}`")
    lines.append(f"- VAE: {'✅' if checks['vae_gguf']['exists'] else '❌'} `{checks['vae_gguf']['path']}`")

    sheetsage2 = backend.check_sheetsage2()
    ss_icon = "✅" if sheetsage2["exists"] else "❌"
    ss_state = tr(lang, "就绪") if sheetsage2["exists"] else tr(lang, "缺失")
    lines.append(f"- SheetSage2 ({tr(lang, '转谱')}): {ss_icon} {ss_state} `{sheetsage2['model_path']}`")

    # 音色工坊三模型状态（Demucs / Seed-VC / campplus，文件级检查不加载模型）
    try:
        vc = check_voice_models(project_root)
        lines.append(f"\n### {tr(lang, '音色工坊')}\n")
        if not vc["enabled"]:
            lines.append(f"- {tr(lang, '未启用')}（config.cfg [voice] enabled=false）")
        else:
            for key, label in (
                ("demucs", f"Demucs ({tr(lang, '音轨分离')})"),
                ("seedvc", f"Seed-VC ({tr(lang, '音色转换')})"),
                ("campplus", tr(lang, "campplus 说话人编码器")),
            ):
                item = vc[key]
                if item["exists"] is None:
                    # seedvc_dir 未配置：该项无法定位，给出配置指引
                    lines.append(f"- {label}: ⚠️ {tr(lang, '未配置')} "
                                 f"`config.cfg [voice] seedvc_dir`")
                elif item["exists"]:
                    lines.append(f"- {label}: ✅ `{item['path']}`")
                else:
                    # 缺失提示：Demucs 首次运行分离会自动下载；Seed-VC/campplus 需按文档安装
                    hint = (tr(lang, "首次运行时自动下载") if key == "demucs"
                            else tr(lang, "见 setup.md 第 6 节"))
                    lines.append(f"- {label}: ❌ {hint} `{item['path']}`")
    except Exception as e:
        # 状态检查不应阻断设置页渲染，异常时仅提示检查失败
        lines.append(f"\n### {tr(lang, '音色工坊')}\n- ⚠️ {tr(lang, '检查失败')}: {e}")

    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            lines.append(f"\n### {tr(lang, 'GPU 信息')}\n- {tr(lang, '设备')}: {gpu_name}\n- {tr(lang, '显存')}: {vram:.1f} GB")
        else:
            lines.append(f"\n⚠️ {tr(lang, 'CUDA 不可用')}")
    except ImportError:
        lines.append(f"\n⚠️ {tr(lang, 'PyTorch 未安装')}")

    total, used, free = shutil.disk_usage(str(project_root))
    lines.append(f"\n### {tr(lang, '磁盘')}\n- {tr(lang, '剩余')}: {free / (1024**3):.1f} GB / {total / (1024**3):.1f} GB")

    return "\n".join(lines)


def _queue_status_html(lang: str):
    """生成设置页「当前队列」状态 Markdown（运行中/排队中/最近任务，按当前语言渲染）。

    数据来自 queue_manager.get_queue_snapshot()（只读快照，不消费 drain 进度流），
    由 gr.Timer 周期刷新 + 语言切换时即时重渲染。
    """
    try:
        snap = queue_manager.get_queue_snapshot()
    except Exception as e:
        logger.warning(f"队列快照获取失败: {e}")
        return f"⚠️ {tr(lang, '队列 worker 线程异常，请重启服务')}"

    lines = []
    if not snap["worker_alive"]:
        lines.append(f"⚠️ {tr(lang, '队列 worker 线程异常，请重启服务')}")

    if snap["running"] is None and not snap["queued"]:
        lines.append(f"🟢 {tr(lang, '空闲')} — {tr(lang, '无运行中或排队任务')}")
    else:
        r = snap["running"]
        if r:
            type_label = tr(lang, _QUEUE_TYPE_LABELS.get(r["task_type"], r["task_type"]))
            pct, msg = r["progress"] or (None, "")
            prog_str = f"{pct * 100:.0f}%" if pct is not None else "-"
            msg = (msg or "")[:60]  # 进度描述截断，避免撑爆窗口
            lines.append(
                f"🔴 **{tr(lang, '运行中')}** · {type_label} · `{r['task_id']}` · "
                f"{tr(lang, '进度')} {prog_str} · {msg} · {tr(lang, '已用时')} {r['elapsed']:.0f}{tr(lang, '秒')}"
            )
        for q in snap["queued"]:
            type_label = tr(lang, _QUEUE_TYPE_LABELS.get(q["task_type"], q["task_type"]))
            lines.append(f"🟡 {tr(lang, '排队中')} · {type_label} · `{q['task_id']}` · {tr(lang, '等待')} {q['waited']:.0f}{tr(lang, '秒')}")

    if snap["recent"]:
        icon_map = {"completed": "✅", "failed": "❌", "cancelled": "🚫"}
        status_map = {"completed": tr(lang, "完成"), "failed": tr(lang, "失败"), "cancelled": tr(lang, "已取消")}
        parts = []
        for h in snap["recent"]:
            type_label = tr(lang, _QUEUE_TYPE_LABELS.get(h["task_type"], h["task_type"]))
            parts.append(f"{icon_map.get(h['status'], '•')} {status_map.get(h['status'], h['status'])} · {type_label} · {h['elapsed']:.1f}{tr(lang, '秒')}")
        lines.append(f"\n**{tr(lang, '最近任务')}**: " + " | ".join(parts))

    return "\n".join(lines)


def on_lyrics_change(lyrics, lang: str):
    """Return structure analysis HTML when lyrics change."""
    if not lyrics or not lyrics.strip():
        return '<div id="lyrics-structure" style="padding: 8px; color: #888;">' + tr(lang, "输入歌词后显示结构分析") + '</div>'

    segments = []
    current = {"name": "Intro", "lines": []}
    for line in lyrics.split("\n"):
        stripped = line.strip()
        if stripped.startswith(COMMENT_PREFIXES):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            if current["name"] != "Intro" or current["lines"] or segments:
                segments.append(current)
            current = {"name": stripped[1:-1], "lines": []}
        else:
            if stripped:
                current["lines"].append(stripped)
    segments.append(current)

    colors = {
        "verse": "#4a90d9", "chorus": "#e67e22", "bridge": "#27ae60",
        "intro": "#95a5a6", "outro": "#7f8c8d", "pre-chorus": "#8e44ad",
    }

    html = '<div id="lyrics-structure" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:8px;">'
    html += f'<span style="color:#888;font-size:12px;margin-right:4px;">{tr(lang, "结构:")}</span>'
    for seg in segments:
        name_lower = seg["name"].lower().replace(" ", "-")
        color = colors.get(name_lower, "#666")
        line_count = len(seg["lines"])
        html += (
            f'<span style="background:{color};color:white;padding:3px 10px;'
            f'border-radius:12px;font-size:12px;white-space:nowrap;">'
            f'{seg["name"]}'
            f'<span style="opacity:0.7;margin-left:4px;">({line_count}{tr(lang, "行")})</span>'
            f'</span>'
        )
    html += "</div>"

    total_lines = sum(len(s["lines"]) for s in segments)
    char_count = len(strip_comment_lines(lyrics).strip())
    est_seconds = total_lines * 4
    est_min = est_seconds // 60
    est_sec = est_seconds % 60

    html += '<div style="padding:4px 8px;font-size:12px;color:#888;">'
    html += f"{len(segments)} {tr(lang, '个段落')} | {total_lines} {tr(lang, '行歌词')} | {char_count} {tr(lang, '字符')}"
    html += f' | {tr(lang, "预估时长")} ~{est_min}:{est_sec:02d}'
    html += "</div>"

    return html
