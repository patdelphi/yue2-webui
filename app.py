"""YuE2 Music Studio - Gradio WebUI for YuE2 Music Generation."""
import gradio as gr
import random
import time
import json
import shutil
import threading
from pathlib import Path
from datetime import datetime

from config import GenerationParams, CotMode, SamplingParams, OutFormat, validate_params
from backend_gguf import GGUFBackend
from style_presets import STYLE_PRESETS
from lyrics_templates import LYRICS_TEMPLATES
from history import HistoryManager, HistoryRecord

PROJECT_ROOT = Path(__file__).parent.parent
WEBUI_ROOT = PROJECT_ROOT / "yue2-webui"
backend = GGUFBackend(PROJECT_ROOT)
history_mgr = HistoryManager(
    history_file=WEBUI_ROOT / "history.json",
    outputs_root=WEBUI_ROOT / "outputs",
)

current_task_id = None
current_cancel_event = None

BUILTIN_PRESETS = {
    "默认": {
        "description": "标准质量",
        "params": {"cot": "full", "num_inference_steps": 8},
    },
    "快速demo": {
        "description": "最快出结果",
        "params": {"cot": "off", "num_inference_steps": 4},
    },
    "高质量": {
        "description": "最佳质量",
        "params": {"cot": "full", "num_inference_steps": 32, "out_format": "pcm24"},
    },
    "创意模式": {
        "description": "更多样化",
        "params": {"cot": "full", "num_inference_steps": 8, "sem_temp": 1.5, "sem_top_p": 0.98},
    },
    "保守模式": {
        "description": "最稳定",
        "params": {"cot": "full", "num_inference_steps": 8, "sem_temp": 0.3, "sem_rep_penalty": 1.5},
    },
}


def on_generate(
    style, lyrics, cot, seed, cfg_scale, num_inference_steps, out_format,
    abc_text,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress=gr.Progress(track_tqdm=False),
):
    """Generate button callback."""
    global current_task_id, current_cancel_event

    if not style or not style.strip():
        raise gr.Error("请输入风格描述")
    if not lyrics or not lyrics.strip():
        raise gr.Error("请输入歌词")

    params = GenerationParams(
        style=style.strip(),
        lyrics=lyrics.strip(),
        cot=CotMode(cot),
        seed=int(seed),
        cfg_scale=float(cfg_scale) if cfg_scale else None,
        num_inference_steps=int(num_inference_steps),
        out_format=OutFormat(out_format),
        abc=abc_text.strip() if abc_text and abc_text.strip() and cot != "off" else None,
        abc_sampling=SamplingParams(
            temperature=abc_temp, top_p=abc_top_p, top_k=int(abc_top_k),
            repetition_penalty=abc_rep_penalty, penalty_window=int(abc_pen_window),
            min_tokens=int(abc_min_tok), max_tokens=int(abc_max_tok),
        ),
        semantic_sampling=SamplingParams(
            temperature=sem_temp, top_p=sem_top_p, top_k=int(sem_top_k),
            repetition_penalty=sem_rep_penalty, penalty_window=int(sem_pen_window),
            min_tokens=int(sem_min_tok), max_tokens=int(sem_max_tok),
        ),
    )

    error = validate_params(params)
    if error:
        raise gr.Error(error)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"{timestamp}_{params.id}"
    output_dir = WEBUI_ROOT / "outputs" / task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    current_task_id = task_id
    current_cancel_event = threading.Event()

    def on_progress(p):
        phase_labels = {
            "loading": "加载模型...",
            "planning": "规划乐谱...",
            "generating": "生成音乐...",
            "synthesizing": "合成音频...",
            "decoding": "解码音频...",
            "done": "完成",
        }
        label = phase_labels.get(p.get("phase", ""), "处理中...")
        progress(0, desc=label)

    progress(0, desc="开始生成...")

    result = backend.generate(
        params=params,
        output_dir=output_dir,
        on_progress=on_progress,
        cancel_event=current_cancel_event,
    )

    current_task_id = None
    current_cancel_event = None

    if result.success:
        format_labels = {"pcm16": "PCM 16-bit", "pcm24": "PCM 24-bit", "float32": "Float 32-bit"}
        format_label = format_labels.get(params.out_format.value, "PCM 16-bit")
        duration_info = f"生成耗时 **{result.generation_time_seconds:.1f}s** | 音频时长 **{result.audio_duration_seconds:.1f}s** | {format_label}"

        abc_path = ""
        if result.abc_score:
            abc_file = output_dir / "score.abc"
            abc_file.write_text(result.abc_score, encoding="utf-8")
            abc_path = str(abc_file.relative_to(WEBUI_ROOT))

        record = HistoryRecord(
            task_id=task_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            style=params.style,
            lyrics_preview=params.lyrics[:60],
            cot=params.cot.value,
            seed=params.seed,
            audio_duration_seconds=result.audio_duration_seconds or 0,
            generation_time_seconds=result.generation_time_seconds or 0,
            audio_path=str(result.audio_path),
            output_dir=str(output_dir.relative_to(WEBUI_ROOT)),
            abc_path=abc_path,
            out_format=params.out_format.value,
        )
        history_mgr.append(record)
        history_mgr.auto_prune(max_entries=100)

        abc_display = result.abc_score or ""
        abc_download = str(output_dir / "score.abc") if result.abc_score else None
        flac_download = result.flac_path

        return result.audio_path, duration_info, abc_display, abc_download, flac_download
    else:
        raise gr.Error(f"生成失败: {result.error_message}")


def on_cancel():
    """Cancel button callback."""
    global current_cancel_event
    if current_cancel_event:
        current_cancel_event.set()
        return "正在取消..."
    return "没有正在运行的任务"


def on_resynthesize(
    abc_text, style, lyrics, seed, cfg_scale, num_inference_steps, out_format,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress=gr.Progress(track_tqdm=False),
):
    """Resynthesize with edited ABC score."""
    global current_task_id, current_cancel_event

    if not abc_text or not abc_text.strip():
        raise gr.Error("ABC 乐谱不能为空")

    params = GenerationParams(
        style=style.strip() if style else "",
        lyrics=lyrics.strip() if lyrics else "",
        cot=CotMode.MELODY,
        seed=int(seed),
        cfg_scale=float(cfg_scale) if cfg_scale else None,
        num_inference_steps=int(num_inference_steps),
        out_format=OutFormat(out_format),
        abc=abc_text.strip(),
        abc_sampling=SamplingParams(
            temperature=abc_temp, top_p=abc_top_p, top_k=int(abc_top_k),
            repetition_penalty=abc_rep_penalty, penalty_window=int(abc_pen_window),
            min_tokens=int(abc_min_tok), max_tokens=int(abc_max_tok),
        ),
        semantic_sampling=SamplingParams(
            temperature=sem_temp, top_p=sem_top_p, top_k=int(sem_top_k),
            repetition_penalty=sem_rep_penalty, penalty_window=int(sem_pen_window),
            min_tokens=int(sem_min_tok), max_tokens=int(sem_max_tok),
        ),
    )

    error = validate_params(params)
    if error:
        raise gr.Error(error)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"{timestamp}_resynth_{params.id}"
    output_dir = WEBUI_ROOT / "outputs" / task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    current_task_id = task_id
    current_cancel_event = threading.Event()

    def on_progress(p):
        phase_labels = {
            "loading": "加载模型...",
            "generating": "合成音乐...",
            "synthesizing": "合成音频...",
            "decoding": "解码音频...",
            "done": "完成",
        }
        label = phase_labels.get(p.get("phase", ""), "处理中...")
        progress(0, desc=label)

    progress(0, desc="开始重新合成...")

    result = backend.generate(
        params=params,
        output_dir=output_dir,
        on_progress=on_progress,
        cancel_event=current_cancel_event,
    )

    current_task_id = None
    current_cancel_event = None

    if result.success:
        format_labels = {"pcm16": "PCM 16-bit", "pcm24": "PCM 24-bit", "float32": "Float 32-bit"}
        format_label = format_labels.get(params.out_format.value, "PCM 16-bit")
        duration_info = f"重新合成耗时 **{result.generation_time_seconds:.1f}s** | 音频时长 **{result.audio_duration_seconds:.1f}s** | {format_label}"

        record = HistoryRecord(
            task_id=task_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            style=params.style,
            lyrics_preview="(重新合成)",
            cot="melody",
            seed=params.seed,
            audio_duration_seconds=result.audio_duration_seconds or 0,
            generation_time_seconds=result.generation_time_seconds or 0,
            audio_path=str(result.audio_path),
            output_dir=str(output_dir.relative_to(WEBUI_ROOT)),
            abc_path="",
            status="resynthesized",
            out_format=params.out_format.value,
        )
        history_mgr.append(record)
        history_mgr.auto_prune(max_entries=100)

        abc_download = str(output_dir / "score.abc") if result.abc_score else None
        flac_download = result.flac_path
        return result.audio_path, duration_info, abc_download, flac_download
    else:
        raise gr.Error(f"重新合成失败：{result.error_message}")


def on_random_seed():
    """Generate random seed."""
    return random.randint(0, 2**31 - 1)


def on_style_preset(name):
    """Fill style from preset."""
    return STYLE_PRESETS.get(name, "")


def on_lyrics_template(name):
    """Fill lyrics from template."""
    return LYRICS_TEMPLATES.get(name, "")


def on_cot_change(cot_value):
    """Toggle ABC input visibility based on mode."""
    is_off = (cot_value == "off")
    return gr.update(visible=not is_off)


def refresh_history():
    """Refresh history dataframe."""
    rows = history_mgr.to_dataframe_rows()
    return rows


def on_history_select(evt: gr.SelectData, current_state: list):
    """Handle history row selection. Returns updated state, audio, info, abc."""
    rows = history_mgr.to_dataframe_rows()
    if evt.index[0] >= len(rows):
        return current_state, None, "请选择一条记录", ""
    task_id = rows[evt.index[0]][5]
    entry = history_mgr.get(task_id)
    if not entry:
        return current_state, None, "记录不存在", ""
    audio_path = Path(entry.audio_path)
    abc_score = history_mgr.get_abc_score(task_id) or ""
    if audio_path.exists():
        return [task_id], str(audio_path), f"**{entry.task_id}** | {entry.style[:50]}...", abc_score
    return [task_id], None, "音频文件不存在", ""


def on_history_delete(selected_state):
    """Delete the currently selected history entry."""
    if not selected_state or not selected_state:
        return refresh_history(), "请先点击选择要删除的记录", selected_state
    task_id = selected_state[0]
    if history_mgr.delete(task_id):
        return refresh_history(), f"已删除 {task_id}", []
    return refresh_history(), "删除失败", selected_state


def on_history_clear():
    """Clear all history."""
    history_mgr.clear()
    return refresh_history(), "已清空所有历史", []


def on_check_models():
    """Check model files and return status."""
    checks = backend.check_models()
    lines = ["### 模型状态\n"]
    if checks["available"]:
        lines.append("✅ 所有模型文件就绪\n")
    else:
        lines.append("❌ 模型文件缺失\n")

    lines.append(f"- 主模型: {'✅' if checks['model_gguf']['exists'] else '❌'} `{checks['model_gguf']['path']}`")
    lines.append(f"- VAE: {'✅' if checks['vae_gguf']['exists'] else '❌'} `{checks['vae_gguf']['path']}`")

    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_memory / (1024**3)
            lines.append(f"\n### GPU 信息\n- 设备: {gpu_name}\n- 显存: {vram:.1f} GB")
        else:
            lines.append("\n⚠️ CUDA 不可用")
    except ImportError:
        lines.append("\n⚠️ PyTorch 未安装")

    total, used, free = shutil.disk_usage(str(PROJECT_ROOT))
    lines.append(f"\n### 磁盘\n- 剩余: {free / (1024**3):.1f} GB / {total / (1024**3):.1f} GB")

    return "\n".join(lines)


def on_preset_load(name):
    """Load a preset and return param values."""
    preset = BUILTIN_PRESETS.get(name)
    if not preset:
        preset_path = WEBUI_ROOT / "presets" / f"{name}.json"
        if preset_path.exists():
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
        else:
            return [gr.update() for _ in range(17)]

    p = preset.get("params", {})
    return [
        p.get("cot", "full"),
        p.get("num_inference_steps", 8),
        p.get("out_format", "pcm16"),
        p.get("abc_temp", 0.7),
        p.get("abc_top_p", 0.9),
        p.get("abc_top_k", 30),
        p.get("abc_rep_penalty", 1.005),
        p.get("abc_pen_window", 100),
        p.get("abc_min_tok", 32),
        p.get("abc_max_tok", 4096),
        p.get("sem_temp", 1.0),
        p.get("sem_top_p", 0.95),
        p.get("sem_top_k", 100),
        p.get("sem_rep_penalty", 1.2),
        p.get("sem_pen_window", 50),
        p.get("sem_min_tok", 200),
        p.get("sem_max_tok", 9000),
    ]


def on_preset_save(name, cot, steps, out_format, abc_temp, abc_top_p, abc_top_k, abc_rep, abc_pen, abc_min, abc_max,
                    sem_temp, sem_top_p, sem_top_k, sem_rep, sem_pen, sem_min, sem_max):
    """Save current params as a preset."""
    if not name or not name.strip():
        return "请输入预设名称"
    presets_dir = WEBUI_ROOT / "presets"
    presets_dir.mkdir(exist_ok=True)
    preset = {
        "name": name.strip(),
        "params": {
            "cot": cot, "num_inference_steps": steps, "out_format": out_format,
            "abc_temp": abc_temp, "abc_top_p": abc_top_p, "abc_top_k": abc_top_k,
            "abc_rep_penalty": abc_rep, "abc_pen_window": abc_pen,
            "abc_min_tok": abc_min, "abc_max_tok": abc_max,
            "sem_temp": sem_temp, "sem_top_p": sem_top_p, "sem_top_k": sem_top_k,
            "sem_rep_penalty": sem_rep, "sem_pen_window": sem_pen,
            "sem_min_tok": sem_min, "sem_max_tok": sem_max,
        },
    }
    path = presets_dir / f"{name.strip()}.json"
    path.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"已保存预设: {name}"


def build_ui():
    """Build the Gradio UI."""
    with gr.Blocks(title="YuE2 Music Studio") as demo:
        gr.Markdown("# YuE2 Music Studio")
        gr.Markdown("AI音乐创作 — 输入歌词和风格，生成完整歌曲")

        with gr.Tabs():
            with gr.Tab("创作"):
                with gr.Row():
                    with gr.Column(scale=2):
                        gr.Markdown("### 风格描述")
                        style_input = gr.Textbox(
                            label="Style",
                            placeholder="English, warm piano pop, expressive female voice, acoustic piano, 88 BPM",
                            lines=2,
                            info="语言 + 流派 + 乐器 + 人声 + 速度",
                        )

                        gr.Markdown("#### 风格快捷标签")
                        preset_names = list(STYLE_PRESETS.keys())
                        half = len(preset_names) // 2
                        with gr.Row():
                            for name in preset_names[:half]:
                                btn = gr.Button(name, size="sm")
                                btn.click(fn=lambda n=name: on_style_preset(n), outputs=style_input)
                        with gr.Row():
                            for name in preset_names[half:]:
                                btn = gr.Button(name, size="sm")
                                btn.click(fn=lambda n=name: on_style_preset(n), outputs=style_input)

                        gr.Markdown("### 歌词")
                        lyrics_input = gr.Textbox(
                            label="Lyrics",
                            placeholder="[Verse]\n在这里输入歌词...\n\n[Chorus]\n副歌歌词...",
                            lines=10,
                            info="支持 [Verse] [Chorus] [Bridge] 段落标记",
                        )

                        template_dropdown = gr.Dropdown(
                            label="歌词模板",
                            choices=list(LYRICS_TEMPLATES.keys()),
                            value=None,
                            info="选择模板将填充歌词区（覆盖现有内容）",
                        )
                        template_dropdown.change(fn=on_lyrics_template, inputs=template_dropdown, outputs=lyrics_input)

                        gr.Markdown("### 工作模式")
                        cot_input = gr.Radio(
                            label="Mode",
                            choices=[
                                ("完整创作 (生成乐谱+和弦)", "full"),
                                ("旋律创作 (仅旋律，适合翻唱)", "melody"),
                                ("直接生成 (跳过乐谱，最快)", "off"),
                            ],
                            value="full",
                        )

                        abc_input = gr.Textbox(
                            label="ABC 乐谱 (外部输入)",
                            placeholder="X:1\nM:4/4\nL:1/16\nK:C\n...",
                            lines=8,
                            visible=True,
                            info="提供外部ABC乐谱文本。仅在 full/melody 模式下生效。留空则自动生成。",
                        )
                        cot_input.change(fn=on_cot_change, inputs=cot_input, outputs=abc_input)

                        with gr.Row():
                            seed_input = gr.Number(label="随机种子", value=831001, precision=0)
                            random_seed_btn = gr.Button("🎲 随机", size="sm")

                        cfg_input = gr.Slider(
                            label="CFG 引导强度",
                            minimum=0, maximum=20, step=0.1, value=None,
                            info="Auto时: off模式=1.01, 其他=1.0",
                        )

                        steps_input = gr.Slider(
                            label="ODE 求解步数",
                            minimum=1, maximum=64, step=1, value=8,
                            info="8=快速, 16=标准, 32=高质量",
                        )

                        out_format_input = gr.Dropdown(
                            label="输出格式",
                            choices=[
                                ("PCM 16-bit (标准)", "pcm16"),
                                ("PCM 24-bit (高动态)", "pcm24"),
                                ("Float 32-bit (最大动态)", "float32"),
                            ],
                            value="pcm16",
                            info="PCM16=标准质量, PCM24=更高动态范围, Float32=最大动态范围(文件更大)",
                        )

                        with gr.Accordion("高级采样参数", open=False):
                            gr.Markdown("#### ABC 乐谱采样 (Stage 1)")
                            with gr.Row():
                                abc_temp_input = gr.Slider(label="ABC 温度", minimum=0, maximum=5, step=0.1, value=0.7)
                                abc_top_p_input = gr.Slider(label="ABC Top-P", minimum=0, maximum=1, step=0.01, value=0.9)
                                abc_top_k_input = gr.Slider(label="ABC Top-K", minimum=1, maximum=500, step=1, value=30)
                            with gr.Row():
                                abc_rep_input = gr.Slider(label="ABC 重复惩罚", minimum=0.001, maximum=3, step=0.001, value=1.005)
                                abc_pen_window_input = gr.Slider(label="ABC 惩罚窗口", minimum=1, maximum=100, step=1, value=100)
                            with gr.Row():
                                abc_min_tok_input = gr.Slider(label="ABC Min Tokens", minimum=0, maximum=8192, step=1, value=32)
                                abc_max_tok_input = gr.Slider(label="ABC Max Tokens", minimum=1, maximum=8192, step=1, value=4096)

                            gr.Markdown("#### 语义 Token 采样 (Stage 2)")
                            with gr.Row():
                                sem_temp_input = gr.Slider(label="语义 温度", minimum=0, maximum=5, step=0.1, value=1.0)
                                sem_top_p_input = gr.Slider(label="语义 Top-P", minimum=0, maximum=1, step=0.01, value=0.95)
                                sem_top_k_input = gr.Slider(label="语义 Top-K", minimum=1, maximum=500, step=1, value=100)
                            with gr.Row():
                                sem_rep_input = gr.Slider(label="语义 重复惩罚", minimum=0.001, maximum=3, step=0.01, value=1.2)
                                sem_pen_window_input = gr.Slider(label="语义 惩罚窗口", minimum=1, maximum=100, step=1, value=50)
                            with gr.Row():
                                sem_min_tok_input = gr.Slider(label="语义 Min Tokens", minimum=0, maximum=9000, step=1, value=200)
                                sem_max_tok_input = gr.Slider(label="语义 Max Tokens", minimum=1, maximum=9000, step=1, value=9000)

                        with gr.Row():
                            generate_btn = gr.Button("🎵 生成歌曲", variant="primary", size="lg")
                            cancel_btn = gr.Button("取消", size="lg")

                    with gr.Column(scale=1):
                        gr.Markdown("### 输出")
                        audio_output = gr.Audio(label="生成的歌曲", type="filepath")
                        info_output = gr.Markdown()
                        gr.Markdown("### ABC 乐谱")
                        abc_output = gr.Textbox(
                            label="生成的乐谱 (可编辑)",
                            lines=10,
                            interactive=True,
                            info="生成后可编辑乐谱，点击「重新合成」使用修改后的乐谱生成新音频",
                        )
                        abc_file_output = gr.File(label="下载乐谱")
                        flac_file_output = gr.File(label="下载 FLAC")
                        with gr.Row():
                            resynthesize_btn = gr.Button("重新合成", variant="secondary")
                        resynthesize_info = gr.Markdown()

            with gr.Tab("历史"):
                gr.Markdown("### 生成历史")
                history_state = gr.State(value=[])
                history_df = gr.Dataframe(
                    headers=["时间", "风格", "模式", "音频时长", "生成耗时", "Task ID"],
                    datatype=["str", "str", "str", "str", "str", "str"],
                    row_count=10,
                    interactive=False,
                    value=refresh_history(),
                )
                history_audio = gr.Audio(label="试听", type="filepath")
                history_info = gr.Markdown()
                history_abc = gr.Textbox(label="ABC 乐谱", lines=6, interactive=False)
                with gr.Row():
                    history_refresh_btn = gr.Button("刷新")
                    history_delete_btn = gr.Button("删除选中")
                    history_clear_btn = gr.Button("清空历史")

                history_df.select(fn=on_history_select, inputs=history_state, outputs=[history_state, history_audio, history_info, history_abc])
                history_refresh_btn.click(fn=refresh_history, outputs=history_df)
                history_delete_btn.click(fn=on_history_delete, inputs=history_state, outputs=[history_df, history_info, history_state])
                history_clear_btn.click(fn=on_history_clear, outputs=[history_df, history_info, history_state])

            with gr.Tab("设置"):
                gr.Markdown("### 系统状态")
                model_status = gr.Markdown(value=on_check_models())
                check_models_btn = gr.Button("检查模型")
                check_models_btn.click(fn=on_check_models, outputs=model_status)

                gr.Markdown("### 参数预设")
                preset_dropdown = gr.Dropdown(
                    label="加载预设",
                    choices=list(BUILTIN_PRESETS.keys()),
                    value=None,
                )
                preset_name_input = gr.Textbox(label="保存预设名称", placeholder="我的预设")
                with gr.Row():
                    preset_load_btn = gr.Button("加载")
                    preset_save_btn = gr.Button("保存当前参数")
                preset_info = gr.Markdown()

        random_seed_btn.click(fn=on_random_seed, outputs=seed_input)

        generate_btn.click(
            fn=on_generate,
            inputs=[
                style_input, lyrics_input, cot_input, seed_input, cfg_input, steps_input, out_format_input,
                abc_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
            outputs=[audio_output, info_output, abc_output, abc_file_output, flac_file_output],
        )

        cancel_btn.click(fn=on_cancel, outputs=info_output)

        resynthesize_btn.click(
            fn=on_resynthesize,
            inputs=[
                abc_output, style_input, lyrics_input, seed_input, cfg_input, steps_input, out_format_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
            outputs=[audio_output, info_output, abc_file_output, flac_file_output],
        )

        preset_load_btn.click(
            fn=on_preset_load,
            inputs=preset_dropdown,
            outputs=[
                cot_input, steps_input, out_format_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
        )
        preset_save_btn.click(
            fn=on_preset_save,
            inputs=[
                preset_name_input, cot_input, steps_input, out_format_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
            outputs=preset_info,
        )

    return demo


if __name__ == "__main__":
    checks = backend.check_models()
    if not checks["available"]:
        print("警告: 模型文件缺失!")
        if not checks["model_gguf"]["exists"]:
            print(f"  缺少: {checks['model_gguf']['path']}")
        if not checks["vae_gguf"]["exists"]:
            print(f"  缺少: {checks['vae_gguf']['path']}")

    demo = build_ui()
    demo.launch(server_name="127.0.0.1", server_port=9898, share=False)
