"""YuE2 Music Studio - Gradio WebUI for YuE2 Music Generation."""
import gradio as gr
import random
import json
import shutil
import threading
import logging
from pathlib import Path
from datetime import datetime
from starlette.staticfiles import StaticFiles
from starlette.responses import FileResponse
from starlette.routing import Route

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

from config import GenerationParams, CotMode, SamplingParams, OutFormat, validate_params, TranscriptionResult
from backend_gguf import GGUFBackend
from style_presets import STYLE_PRESETS
from vocal_presets import VOCAL_PRESETS, INSTRUMENT_PRESETS, MOOD_PRESETS, LANGUAGE_PRESETS, GENRE_PRESETS
from lyrics_templates import LYRICS_TEMPLATES
from history import HistoryManager, HistoryRecord
from postprocess import postprocess_audio

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

FORMAT_LABELS = {"pcm16": "PCM 16-bit", "pcm24": "PCM 24-bit", "float32": "Float 32-bit"}


def on_generate(
    style, lyrics, cot, seed, cfg_scale, num_inference_steps, out_format, batch_count,
    normalize, fade, trim, metadata,
    abc_text,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress=gr.Progress(track_tqdm=False),
):
    """Generate button callback."""
    global current_task_id, current_cancel_event
    
    logger.info(f"on_generate: style={style!r}, cot={cot!r}, seed={seed!r}, cfg_scale={cfg_scale!r}, steps={num_inference_steps!r}, batch={batch_count!r}")
    
    # Handle None values from frontend (Gradio may send None for uninitialized sliders)
    cot = cot if cot is not None else "full"
    # Handle boolean cot value (frontend may send False instead of "off")
    if isinstance(cot, bool):
        cot = "off" if not cot else "full"
    seed = seed if seed is not None else 831001
    cfg_scale = cfg_scale if cfg_scale is not None else 0
    num_inference_steps = num_inference_steps if num_inference_steps is not None else 8
    batch_count = batch_count if batch_count is not None else 1
    
    # ABC sampling params defaults
    abc_temp = abc_temp if abc_temp is not None else 0.7
    abc_top_p = abc_top_p if abc_top_p is not None else 0.9
    abc_top_k = abc_top_k if abc_top_k is not None else 30
    abc_rep_penalty = abc_rep_penalty if abc_rep_penalty is not None else 1.005
    abc_pen_window = abc_pen_window if abc_pen_window is not None else 100
    abc_min_tok = abc_min_tok if abc_min_tok is not None else 32
    abc_max_tok = abc_max_tok if abc_max_tok is not None else 4096
    
    # Semantic sampling params defaults
    sem_temp = sem_temp if sem_temp is not None else 1.0
    sem_top_p = sem_top_p if sem_top_p is not None else 0.95
    sem_top_k = sem_top_k if sem_top_k is not None else 100
    sem_rep_penalty = sem_rep_penalty if sem_rep_penalty is not None else 1.2
    sem_pen_window = sem_pen_window if sem_pen_window is not None else 50
    sem_min_tok = sem_min_tok if sem_min_tok is not None else 200
    sem_max_tok = sem_max_tok if sem_max_tok is not None else 9000
    
    try:
        return _on_generate_impl(
            style, lyrics, cot, seed, cfg_scale, num_inference_steps, out_format, batch_count,
            normalize, fade, trim, metadata, abc_text,
            abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
            sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
            progress,
        )
    except Exception as e:
        logger.exception(f"生成失败: {e}")
        raise


def _on_generate_impl(
    style, lyrics, cot, seed, cfg_scale, num_inference_steps, out_format, batch_count,
    normalize, fade, trim, metadata, abc_text,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress,
):

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
    base_task_id = f"{timestamp}_{params.id}"
    output_dir = WEBUI_ROOT / "outputs" / base_task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    current_task_id = base_task_id
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

    batch_count = int(batch_count)
    results = []
    base_seed = int(seed)

    for i in range(batch_count):
        if current_cancel_event and current_cancel_event.is_set():
            break

        current_seed = base_seed + i
        task_id = f"{base_task_id}_var{i+1}" if batch_count > 1 else base_task_id
        variant_dir = output_dir / f"var{i+1}" if batch_count > 1 else output_dir
        variant_dir.mkdir(parents=True, exist_ok=True)

        params.seed = current_seed
        progress(0, desc=f"生成变体 {i+1}/{batch_count} (seed={current_seed})...")

        result = backend.generate(
            params=params,
            output_dir=variant_dir,
            on_progress=on_progress,
            cancel_event=current_cancel_event,
        )
        results.append((task_id, variant_dir, result, current_seed))

    current_task_id = None
    current_cancel_event = None

    successful = [(tid, d, r, s) for tid, d, r, s in results if r.success]
    if not successful:
        failed_msg = results[0][2].error_message if results else "未知错误"
        raise gr.Error(f"生成失败: {failed_msg}")

    format_label = FORMAT_LABELS.get(params.out_format.value, "PCM 16-bit")

    if any([normalize, fade, trim, metadata]):
        for task_id, variant_dir, result, variant_seed in successful:
            wav_path = Path(result.audio_path)
            if wav_path.exists():
                progress(0, desc="后处理音频...")
                postprocess_audio(
                    wav_path,
                    normalize=normalize, fade=fade, trim=trim, metadata=metadata,
                    style=params.style, seed=variant_seed,
                    out_format=params.out_format.value,
                )
                if result.mp3_path:
                    new_mp3 = backend.re_export_mp3(wav_path)
                    if new_mp3:
                        result.mp3_path = str(new_mp3)
                    else:
                        logger.warning("MP3 re-export failed after post-processing")
                        result.mp3_path = None

    total_time = sum(r.generation_time_seconds or 0 for _, _, r, _ in successful)
    avg_duration = sum(r.audio_duration_seconds or 0 for _, _, r, _ in successful) / len(successful)

    if batch_count > 1:
        duration_info = f"批量生成 **{len(successful)}/{batch_count}** 个变体 | 总耗时 **{total_time:.1f}s** | 平均音频时长 **{avg_duration:.1f}s** | {format_label}"
    else:
        r = successful[0][2]
        duration_info = f"生成耗时 **{r.generation_time_seconds:.1f}s** | 音频时长 **{r.audio_duration_seconds:.1f}s** | {format_label}"

    lyrics_data_html = f'<div class="gen-lyrics-data" style="display:none" data-lyrics=\'{json.dumps(params.lyrics, ensure_ascii=False)}\' data-duration="{avg_duration}"></div>'

    for task_id, variant_dir, result, variant_seed in successful:
        abc_path = ""
        if result.abc_score:
            abc_file = variant_dir / f"{variant_dir.name}.abc"
            abc_file.write_text(result.abc_score, encoding="utf-8")
            abc_path = str(abc_file.relative_to(WEBUI_ROOT))

        lyrics_file = variant_dir / f"{variant_dir.name}.txt"
        lyrics_file.write_text(params.lyrics, encoding="utf-8")

        record = HistoryRecord(
            task_id=task_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            style=params.style,
            lyrics=params.lyrics,
            lyrics_preview=params.lyrics[:60],
            cot=params.cot.value,
            seed=variant_seed,
            audio_duration_seconds=result.audio_duration_seconds or 0,
            generation_time_seconds=result.generation_time_seconds or 0,
            audio_path=str(result.audio_path),
            output_dir=str(variant_dir.relative_to(WEBUI_ROOT)),
            abc_path=abc_path,
            out_format=params.out_format.value,
        )
        history_mgr.append(record)
    history_mgr.auto_prune(max_entries=100)

    first_result = successful[0][2]
    first_dir = successful[0][1]
    abc_display = first_result.abc_score or ""
    abc_download = str(first_dir / f"{first_dir.name}.abc") if first_result.abc_score else None

    if batch_count == 1:
        mp3_download = first_result.mp3_path
        h_rows, h_info = refresh_history()
        return first_result.audio_path, duration_info, abc_display, abc_download, mp3_download, lyrics_data_html, h_rows, h_info, 0
    else:
        zip_path = output_dir / "batch.zip"
        import zipfile
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for task_id, variant_dir, result, _ in successful:
                var_name = task_id.split('_')[-1]
                base_name = variant_dir.name
                wav_file = variant_dir / f"{base_name}.wav"
                if wav_file.exists():
                    zf.write(wav_file, f"{var_name}/{base_name}.wav")
                mp3_file = variant_dir / f"{base_name}.mp3"
                if mp3_file.exists():
                    zf.write(mp3_file, f"{var_name}/{base_name}.mp3")
                abc_file = variant_dir / f"{base_name}.abc"
                if abc_file.exists():
                    zf.write(abc_file, f"{var_name}/{base_name}.abc")
                lyrics_file = variant_dir / f"{base_name}.txt"
                if lyrics_file.exists():
                    zf.write(lyrics_file, f"{var_name}/{base_name}.txt")

        audio_paths = [str(r.audio_path) for _, _, r, _ in successful]
        h_rows, h_info = refresh_history()
        return audio_paths, duration_info, abc_display, abc_download, str(zip_path), lyrics_data_html, h_rows, h_info, 0


def on_cancel():
    """Cancel button callback."""
    global current_task_id, current_cancel_event
    if current_cancel_event:
        current_cancel_event.set()
        current_task_id = None
        current_cancel_event = None
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

    # Handle None values from frontend
    seed = seed if seed is not None else 831001
    cfg_scale = cfg_scale if cfg_scale is not None else 0
    num_inference_steps = num_inference_steps if num_inference_steps is not None else 8

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
        format_label = FORMAT_LABELS.get(params.out_format.value, "PCM 16-bit")
        duration_info = f"重新合成耗时 **{result.generation_time_seconds:.1f}s** | 音频时长 **{result.audio_duration_seconds:.1f}s** | {format_label}"

        record = HistoryRecord(
            task_id=task_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            style=params.style,
            lyrics=params.lyrics,
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

        lyrics_file = output_dir / f"{output_dir.name}.txt"
        lyrics_file.write_text(params.lyrics, encoding="utf-8")

        abc_download = str(output_dir / f"{output_dir.name}.abc") if result.abc_score else None
        mp3_download = result.mp3_path
        resynth_lyrics_data = f'<div class="gen-lyrics-data" style="display:none" data-lyrics=\'{json.dumps(params.lyrics, ensure_ascii=False)}\' data-duration="{result.audio_duration_seconds}"></div>'
        return result.audio_path, duration_info, abc_download, mp3_download, resynth_lyrics_data
    else:
        raise gr.Error(f"重新合成失败：{result.error_message}")


def on_transcribe(audio_file, progress=gr.Progress(track_tqdm=False)):
    """Transcribe audio to ABC score using SheetSage2."""
    if not audio_file:
        raise gr.Error("请先上传音频文件")
    
    audio_path = Path(audio_file)
    if not audio_path.exists():
        raise gr.Error("音频文件不存在")
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"transcribe_{timestamp}"
    output_dir = WEBUI_ROOT / "outputs" / task_id
    
    def on_progress(p):
        if p and p.get("message"):
            progress(p.get("progress", 0), p["message"])
    
    result = backend.transcribe(audio_path, output_dir, on_progress=on_progress)
    
    if result.success:
        abc_score = result.abc_score or ""
        midi_path = result.midi_path
        info = f"转谱耗时 **{result.transcription_time_seconds:.1f}s**"
        
        abc_file = output_dir / f"{output_dir.name}.abc"
        abc_file_path = str(abc_file.relative_to(WEBUI_ROOT)) if abc_file.exists() else ""
        
        return abc_score, info, abc_file_path, midi_path, task_id
    else:
        raise gr.Error(f"转谱失败：{result.error_message}")


def on_send_to_generate(abc_text):
    """Send ABC score to generation tab via bridge."""
    if not abc_text or not abc_text.strip():
        raise gr.Error("没有可发送的乐谱内容")
    return abc_text


def on_random_seed():
    """Generate random seed."""
    return random.randint(0, 2**31 - 1)


def on_style_preset(name):
    """Fill style from preset."""
    return STYLE_PRESETS.get(name, "")


def append_to_style(current_style, preset_value):
    """Append preset value to current style."""
    if not current_style or not current_style.strip():
        return preset_value
    if preset_value in current_style:
        return current_style
    # Strip trailing commas and whitespace to avoid double commas
    current_style = current_style.rstrip(", ").strip()
    return f"{current_style}, {preset_value}"


def on_vocal_preset(current_style, name):
    """Append vocal preset to style."""
    preset = VOCAL_PRESETS.get(name, "")
    return append_to_style(current_style, preset)


def on_instrument_preset(current_style, name):
    """Append instrument preset to style."""
    preset = INSTRUMENT_PRESETS.get(name, "")
    return append_to_style(current_style, preset)


def on_mood_preset(current_style, name):
    """Append mood preset to style."""
    preset = MOOD_PRESETS.get(name, "")
    return append_to_style(current_style, preset)


def on_language_preset(current_style, name):
    """Append language preset to style."""
    preset = LANGUAGE_PRESETS.get(name, "")
    return append_to_style(current_style, preset)


def on_genre_preset(current_style, name):
    """Append genre preset to style."""
    preset = GENRE_PRESETS.get(name, "")
    return append_to_style(current_style, preset)


def on_lyrics_template(name):
    """Fill lyrics from template."""
    return LYRICS_TEMPLATES.get(name, "")


def on_cot_change(cot_value):
    """Toggle ABC input visibility based on mode."""
    is_off = (cot_value == "off")
    return gr.update(visible=not is_off)


HISTORY_PAGE_SIZE = 10


def refresh_history():
    """Refresh history dataframe (first page)."""
    rows = history_mgr.to_dataframe_rows()
    page_rows = rows[:HISTORY_PAGE_SIZE]
    total = len(rows)
    pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    return page_rows, f"第 1 / {pages} 页，共 {total} 条"


def refresh_history_full():
    """Refresh history with page state reset (for buttons)."""
    rows, info = refresh_history()
    return rows, info, 0


def _get_history_page(page):
    """Get a specific page of history. Returns (rows, page_info)."""
    rows = history_mgr.to_dataframe_rows()
    total = len(rows)
    pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * HISTORY_PAGE_SIZE
    page_rows = rows[start:start + HISTORY_PAGE_SIZE]
    return page_rows, f"第 {page + 1} / {pages} 页，共 {total} 条"


def on_history_prev_page(current_page):
    """Go to previous page."""
    new_page = max(0, int(current_page) - 1)
    return _get_history_page(new_page) + (new_page,)


def on_history_next_page(current_page):
    """Go to next page."""
    rows = history_mgr.to_dataframe_rows()
    pages = max(1, (len(rows) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    new_page = min(pages - 1, int(current_page) + 1)
    return _get_history_page(new_page) + (new_page,)


def _load_history_entry(row_index, current_state):
    """Load a history entry by row index. Returns state, audio, info, style, lyrics, abc, preview, lyrics_data, duration_data."""
    rows = history_mgr.to_dataframe_rows()
    if row_index < 0 or row_index >= len(rows):
        return current_state, None, "请选择一条记录", "", "", "", "", "", ""
    task_id = rows[row_index][5]
    entry = history_mgr.get(task_id)
    if not entry:
        return current_state, None, "记录不存在", "", "", "", "", "", ""
    audio_path = Path(entry.audio_path)
    abc_score = history_mgr.get_abc_score(task_id) or ""
    if audio_path.exists():
        lyrics = entry.lyrics or entry.lyrics_preview or ""
        lyrics_data = f'<div class="history-lyrics-data" style="display:none" data-lyrics=\'{json.dumps(lyrics, ensure_ascii=False)}\' data-duration="{entry.audio_duration_seconds}"></div>'
        abc_preview = '<div id="history-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px;"><div id="history-abc-paper"></div><div id="history-abc-audio"></div></div>'
        return [task_id], str(audio_path), f"**{entry.task_id}**", entry.style, lyrics, abc_score, abc_preview, lyrics_data, f'<div class="history-duration-data" style="display:none" data-duration="{entry.audio_duration_seconds}"></div>'
    return [task_id], None, "音频文件不存在", "", "", "", "", "", ""


def on_history_select(evt: gr.SelectData, current_state: list, current_page):
    """Handle history row selection via Dataframe.select (fallback)."""
    actual_index = evt.index[0] + int(current_page) * HISTORY_PAGE_SIZE
    return _load_history_entry(actual_index, current_state)


def on_history_row_click(row_index, current_state, current_page):
    """Handle history row selection via JS click handler."""
    actual_index = int(row_index) + int(current_page) * HISTORY_PAGE_SIZE
    return _load_history_entry(actual_index, current_state)


def on_history_delete(selected_state):
    """Delete the currently selected history entry."""
    if not selected_state:
        rows, info = refresh_history()
        return rows, info, "请先点击选择要删除的记录", selected_state, 0
    task_id = selected_state[0]
    if history_mgr.delete(task_id):
        rows, info = refresh_history()
        return rows, info, f"已删除 {task_id}", [], 0
    rows, info = refresh_history()
    return rows, info, "删除失败", selected_state, 0


def on_history_clear():
    """Clear all history."""
    history_mgr.clear()
    rows, info = refresh_history()
    return rows, info, "已清空所有历史", [], 0


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


def on_lyrics_change(lyrics):
    """Return structure analysis HTML when lyrics change."""
    if not lyrics or not lyrics.strip():
        return '<div id="lyrics-structure" style="padding: 8px; color: #888;">输入歌词后显示结构分析</div>'

    segments = []
    current = {"name": "Intro", "lines": []}
    for line in lyrics.split("\n"):
        stripped = line.strip()
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
    html += '<span style="color:#888;font-size:12px;margin-right:4px;">结构:</span>'
    for seg in segments:
        name_lower = seg["name"].lower().replace(" ", "-")
        color = colors.get(name_lower, "#666")
        line_count = len(seg["lines"])
        html += (
            f'<span style="background:{color};color:white;padding:3px 10px;'
            f'border-radius:12px;font-size:12px;white-space:nowrap;">'
            f'{seg["name"]}'
            f'<span style="opacity:0.7;margin-left:4px;">({line_count}行)</span>'
            f'</span>'
        )
    html += "</div>"

    total_lines = sum(len(s["lines"]) for s in segments)
    char_count = len(lyrics.strip())
    est_seconds = total_lines * 4
    est_min = est_seconds // 60
    est_sec = est_seconds % 60

    html += '<div style="padding:4px 8px;font-size:12px;color:#888;">'
    html += f"{len(segments)} 个段落 | {total_lines} 行歌词 | {char_count} 字符"
    html += f' | 预估时长 ~{est_min}:{est_sec:02d}'
    html += "</div>"

    return html


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
                    with gr.Column(scale=1):
                        style_input = gr.Textbox(
                            label="风格描述",
                            placeholder="English, warm piano pop, expressive female voice, acoustic piano, 88 BPM",
                            lines=2,
                            info="语言 + 流派 + 乐器 + 人声 + 速度",
                        )

                        with gr.Accordion("风格标签", open=False):
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

                            with gr.Accordion("人声标签", open=False):
                                vocal_names = list(VOCAL_PRESETS.keys())
                                half_vocal = len(vocal_names) // 2
                                with gr.Row():
                                    for name in vocal_names[:half_vocal]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_vocal_preset(current, n), inputs=style_input, outputs=style_input)
                                with gr.Row():
                                    for name in vocal_names[half_vocal:]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_vocal_preset(current, n), inputs=style_input, outputs=style_input)

                            with gr.Accordion("乐器标签", open=False):
                                inst_names = list(INSTRUMENT_PRESETS.keys())
                                half_inst = len(inst_names) // 2
                                with gr.Row():
                                    for name in inst_names[:half_inst]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_instrument_preset(current, n), inputs=style_input, outputs=style_input)
                                with gr.Row():
                                    for name in inst_names[half_inst:]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_instrument_preset(current, n), inputs=style_input, outputs=style_input)

                            with gr.Accordion("情绪标签", open=False):
                                mood_names = list(MOOD_PRESETS.keys())
                                half_mood = len(mood_names) // 2
                                with gr.Row():
                                    for name in mood_names[:half_mood]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_mood_preset(current, n), inputs=style_input, outputs=style_input)
                                with gr.Row():
                                    for name in mood_names[half_mood:]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_mood_preset(current, n), inputs=style_input, outputs=style_input)

                            with gr.Accordion("语言标签", open=False):
                                lang_names = list(LANGUAGE_PRESETS.keys())
                                half_lang = len(lang_names) // 2
                                with gr.Row():
                                    for name in lang_names[:half_lang]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_language_preset(current, n), inputs=style_input, outputs=style_input)
                                with gr.Row():
                                    for name in lang_names[half_lang:]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_language_preset(current, n), inputs=style_input, outputs=style_input)

                            with gr.Accordion("流派标签", open=False):
                                genre_names = list(GENRE_PRESETS.keys())
                                half_genre = len(genre_names) // 2
                                with gr.Row():
                                    for name in genre_names[:half_genre]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_genre_preset(current, n), inputs=style_input, outputs=style_input)
                                with gr.Row():
                                    for name in genre_names[half_genre:]:
                                        btn = gr.Button(name, size="sm")
                                        btn.click(fn=lambda current, n=name: on_genre_preset(current, n), inputs=style_input, outputs=style_input)

                        lyrics_input = gr.Textbox(
                            label="歌词",
                            placeholder="[Verse]\n在这里输入歌词...\n\n[Chorus]\n副歌歌词...",
                            lines=10,
                            info="支持 [Verse] [Chorus] [Bridge] 段落标记，可拖拽排序",
                        )

                        with gr.Accordion("歌词工具", open=False):
                            with gr.Row():
                                gr.Button("+ Verse", size="sm")
                                gr.Button("+ Chorus", size="sm")
                                gr.Button("+ Bridge", size="sm")
                                gr.Button("+ Intro", size="sm")
                                gr.Button("+ Outro", size="sm")
                                gr.Button("+ Pre-Chorus", size="sm")

                            gr.Markdown("#### 歌曲结构模板")
                            with gr.Row():
                                gr.Button("Verse-Chorus", size="sm")
                                gr.Button("V-C-V-C", size="sm")
                                gr.Button("V-C-V-C-B-C", size="sm")
                                gr.Button("V-V-C", size="sm")
                                gr.Button("A-A-B-A", size="sm")

                            segment_cards = gr.HTML(
                                label="段落拖拽排序",
                                value='<div id="segment-cards" style="padding:4px 0;"></div>',
                            )
                            structure_analysis = gr.HTML(
                                label="结构分析",
                                value='<div id="lyrics-structure" style="padding:4px 8px;color:#888;">输入歌词后显示结构分析</div>',
                            )

                            template_dropdown = gr.Dropdown(
                                label="歌词模板 (内容)",
                                choices=list(LYRICS_TEMPLATES.keys()),
                                value=None,
                                info="选择模板将填充歌词内容（覆盖现有内容）",
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
                            minimum=0, maximum=20, step=0.1, value=0,
                            info="0=Auto (off模式=1.01, 其他=1.0)",
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

                        batch_count_input = gr.Slider(
                            label="批量生成数量",
                            minimum=1, maximum=10, step=1, value=1,
                            info="一次生成多个变体 (使用递增 seed)",
                        )

                        with gr.Accordion("音频后处理", open=False):
                            gr.Markdown("#### 后处理选项")
                            with gr.Row():
                                normalize_checkbox = gr.Checkbox(label="音量标准化", value=True, info="归一化到 -1dB")
                                fade_checkbox = gr.Checkbox(label="淡入淡出", value=True, info="首尾各 0.5 秒")
                                trim_checkbox = gr.Checkbox(label="裁剪静音", value=False, info="移除首尾静音 (< -40dB)")
                            with gr.Row():
                                metadata_checkbox = gr.Checkbox(label="嵌入元数据", value=True, info="标题/风格/种子")

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

                    with gr.Column(scale=1):
                        with gr.Row():
                            generate_btn = gr.Button("🎵 生成歌曲", variant="primary", size="lg")
                            cancel_btn = gr.Button("取消", size="lg")
                        gr.Markdown("### 输出")
                        audio_output = gr.Audio(label="生成的歌曲", type="filepath", elem_id="gen-audio")
                        info_output = gr.Markdown()
                        gr.Markdown("### ABC 乐谱")
                        with gr.Accordion("生成的乐谱 (可编辑)", open=False):
                            abc_output = gr.Textbox(
                                label="ABC 乐谱文本",
                                placeholder="X:1",
                                lines=10,
                                interactive=True,
                                elem_id="abc-output",
                                info="生成后可编辑乐谱，点击「重新合成」使用修改后的乐谱生成新音频",
                            )
                        gr.Markdown("#### 乐谱预览")
                        gr.HTML(
                            value='<div id="abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed rgba(255,255,255,0.15);"><div style="text-align:center;color:#666;margin-bottom:12px;">生成歌曲后乐谱将在此处渲染</div><div id="abc-paper"></div><div id="abc-audio"></div></div>',
                        )
                        with gr.Row():
                            gr.Button("导出 MIDI", size="sm")
                            gr.Button("导出 PNG", size="sm")
                        abc_file_output = gr.File(label="下载乐谱")
                        flac_file_output = gr.File(label="下载 MP3")
                        lyrics_sync_data = gr.HTML(value="", visible=False)
                        with gr.Row():
                            resynthesize_btn = gr.Button("重新合成", variant="secondary")

            with gr.Tab("音频转谱"):
                gr.Markdown("### 音频转乐谱")
                gr.Markdown("上传音频文件，使用 SheetSage2 模型自动转写为 ABC 乐谱")
                
                transcribe_audio_input = gr.Audio(label="上传音频", type="filepath")
                transcribe_btn = gr.Button("开始转谱", variant="primary")
                transcribe_info = gr.Markdown()
                
                with gr.Accordion("转谱结果", open=False):
                    transcribe_abc_output = gr.Textbox(
                        label="ABC 乐谱 (可编辑)",
                        placeholder="转谱完成后乐谱将显示在这里...",
                        lines=10,
                    )
                    transcribe_abc_preview = gr.HTML(
                        label="乐谱预览",
                        value='<div id="transcribe-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed rgba(255,255,255,0.15);"><div style="text-align:center;color:#666;">转谱后乐谱预览将在此处显示</div><div id="transcribe-abc-paper"></div><div id="transcribe-abc-audio"></div></div>',
                    )
                    
                    with gr.Row():
                        transcribe_abc_download = gr.File(label="下载 ABC")
                        transcribe_midi_download = gr.File(label="下载 MIDI")
                    
                    with gr.Row():
                        transcribe_send_btn = gr.Button("→ 发送到生成页", variant="secondary", size="sm")
                
                transcribe_task_id = gr.State(value="")
                transcribe_abc_bridge = gr.Textbox(elem_id="abc-bridge", label="")

            with gr.Tab("历史") as history_tab:
                gr.Markdown("### 生成历史")
                history_state = gr.State(value=[])
                history_page = gr.State(value=0)
                history_row_trigger = gr.Number(visible=True, value=-1, elem_id="history-row-trigger", label="")
                history_df = gr.Dataframe(
                    headers=["时间", "风格", "模式", "音频时长", "生成耗时", "Task ID"],
                    datatype=["str", "str", "str", "str", "str", "str"],
                    col_count=6,
                    max_height=500,
                    interactive=False,
                    value=refresh_history()[0],
                    elem_id="history-table",
                )
                with gr.Row():
                    history_prev_btn = gr.Button("上一页", size="sm")
                    history_page_info = gr.Markdown(value=refresh_history()[1], elem_id="history-page-info")
                    history_next_btn = gr.Button("下一页", size="sm")
                history_audio = gr.Audio(label="试听", type="filepath", elem_id="history-audio")
                history_info = gr.Markdown()
                with gr.Row():
                    with gr.Column(scale=1):
                        history_lyrics = gr.Textbox(label="歌词", lines=10, interactive=False)
                        history_lyric_sync = gr.HTML(
                            label="歌词同步",
                            value='<div id="history-lyric-sync" style="padding: 12px; min-height: 100px; border-radius: 8px;"></div>',
                        )
                    with gr.Column(scale=1):
                        history_abc_preview = gr.HTML(
                            label="乐谱预览",
                            value='<div id="history-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed rgba(255,255,255,0.15);"><div style="text-align:center;color:#666;margin-bottom:12px;">点击历史记录后乐谱将在此处渲染</div><div id="history-abc-paper"></div><div id="history-abc-audio"></div></div>',
                        )
                        history_abc = gr.Textbox(label="ABC 乐谱文本", lines=6, interactive=False, elem_id="history-abc")
                history_lyrics_data = gr.HTML(value="", visible=False)
                history_duration_data = gr.HTML(value="", visible=False)
                history_style = gr.Markdown(label="风格描述")
                with gr.Row():
                    history_refresh_btn = gr.Button("刷新")
                    history_delete_btn = gr.Button("删除选中")
                    history_clear_btn = gr.Button("清空历史")

                history_df.select(fn=on_history_select, inputs=[history_state, history_page], outputs=[history_state, history_audio, history_info, history_style, history_lyrics, history_abc, history_abc_preview, history_lyrics_data, history_duration_data])
                history_row_trigger.change(fn=on_history_row_click, inputs=[history_row_trigger, history_state, history_page], outputs=[history_state, history_audio, history_info, history_style, history_lyrics, history_abc, history_abc_preview, history_lyrics_data, history_duration_data])
                history_refresh_btn.click(fn=refresh_history_full, outputs=[history_df, history_page_info, history_page])
                history_delete_btn.click(fn=on_history_delete, inputs=history_state, outputs=[history_df, history_page_info, history_info, history_state, history_page])
                history_clear_btn.click(fn=on_history_clear, outputs=[history_df, history_page_info, history_info, history_state, history_page])
                history_prev_btn.click(fn=on_history_prev_page, inputs=history_page, outputs=[history_df, history_page_info, history_page])
                history_next_btn.click(fn=on_history_next_page, inputs=history_page, outputs=[history_df, history_page_info, history_page])

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

        lyrics_input.change(fn=on_lyrics_change, inputs=lyrics_input, outputs=structure_analysis)

        random_seed_btn.click(fn=on_random_seed, outputs=seed_input)

        generate_btn.click(
            fn=on_generate,
            inputs=[
                style_input, lyrics_input, cot_input, seed_input, cfg_input, steps_input, out_format_input, batch_count_input,
                normalize_checkbox, fade_checkbox, trim_checkbox, metadata_checkbox,
                abc_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
            outputs=[audio_output, info_output, abc_output, abc_file_output, flac_file_output, lyrics_sync_data, history_df, history_page_info, history_page],
        )

        cancel_btn.click(fn=on_cancel, outputs=info_output)

        resynthesize_btn.click(
            fn=on_resynthesize,
            inputs=[
                abc_output, style_input, lyrics_input, seed_input, cfg_input, steps_input, out_format_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
            outputs=[audio_output, info_output, abc_file_output, flac_file_output, lyrics_sync_data],
        )

        transcribe_btn.click(
            fn=on_transcribe,
            inputs=[transcribe_audio_input],
            outputs=[transcribe_abc_output, transcribe_info, transcribe_abc_download, transcribe_midi_download, transcribe_task_id],
        )

        transcribe_send_btn.click(
            fn=on_send_to_generate,
            inputs=[transcribe_abc_output],
            outputs=[transcribe_abc_bridge],
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

    def _register_custom_routes():
        import time
        for _ in range(50):
            time.sleep(0.2)
            try:
                import urllib.request
                urllib.request.urlopen("http://127.0.0.1:9898", timeout=0.5)
            except Exception:
                continue
            break
        
        from starlette.responses import HTMLResponse
        
        original_index = None
        for route in demo.app.routes:
            if hasattr(route, 'path') and route.path == "/":
                original_index = route
                break
        
        if original_index:
            from fastapi import Request
            async def custom_index(request: Request):
                response = original_index.endpoint(request)
                if hasattr(response, 'body'):
                    html = response.body.decode("utf-8")
                    scripts = """
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/abcjs/6.3.0/abcjs-audio.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/abcjs/6.3.0/abcjs-basic-min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Sortable/1.15.0/Sortable.min.js"></script>
<script src="/static/js/app.js?v=7"></script>
"""
                    html = html.replace("</head>", scripts + "</head>")
                    return HTMLResponse(content=html, status_code=response.status_code)
                return response
            
            demo.app.routes.remove(original_index)
            demo.app.add_api_route("/", custom_index, methods=["GET"])
        
        demo.app.routes.insert(0, Route(
            "/static/js/app.js",
            lambda request: FileResponse(WEBUI_ROOT / "static" / "js" / "app.js", media_type="application/javascript"),
            methods=["GET"],
        ))
    threading.Thread(target=_register_custom_routes, daemon=True).start()

    demo.launch(
        server_name="127.0.0.1",
        server_port=9898,
        share=False,
        show_error=True,
    )
