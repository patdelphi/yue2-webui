"""YuE2 Music Studio - Gradio WebUI for YuE2 Music Generation."""
import gradio as gr
import random
import time
import threading
from pathlib import Path
from datetime import datetime

from config import GenerationParams, CotMode, SamplingParams, validate_params
from backend_gguf import GGUFBackend
from style_presets import STYLE_PRESETS
from lyrics_templates import LYRICS_TEMPLATES

PROJECT_ROOT = Path(__file__).parent.parent
backend = GGUFBackend(PROJECT_ROOT)

current_task_id = None
current_cancel_event = None


def on_generate(
    style, lyrics, cot, seed, cfg_scale, num_inference_steps,
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
    output_dir = PROJECT_ROOT / "yue2-webui" / "outputs" / task_id
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
        duration_info = f"生成耗时 **{result.generation_time_seconds:.1f}s** | 音频时长 **{result.audio_duration_seconds:.1f}s**"
        return result.audio_path, duration_info
    else:
        raise gr.Error(f"生成失败: {result.error_message}")


def on_cancel():
    """Cancel button callback."""
    global current_cancel_event
    if current_cancel_event:
        current_cancel_event.set()
        return "正在取消..."
    return "没有正在运行的任务"


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


def build_ui():
    """Build the Gradio UI."""
    with gr.Blocks(title="YuE2 Music Studio") as demo:
        gr.Markdown("# YuE2 Music Studio")
        gr.Markdown("AI音乐创作 — 输入歌词和风格，生成完整歌曲")

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

        random_seed_btn.click(fn=on_random_seed, outputs=seed_input)

        generate_btn.click(
            fn=on_generate,
            inputs=[
                style_input, lyrics_input, cot_input, seed_input, cfg_input, steps_input,
                abc_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
            outputs=[audio_output, info_output],
        )

        cancel_btn.click(fn=on_cancel, outputs=info_output)

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
    demo.launch(server_name="127.0.0.1", server_port=7860, share=False, theme=gr.themes.Soft())
