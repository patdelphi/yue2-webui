"""YuE2 Music Studio - Gradio WebUI for YuE2 Music Generation."""
import gradio as gr
import html
import random
import json
import shutil
import threading
import time
import logging
from dataclasses import asdict
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

# 核心模块已整理到 src/ 子目录（app.py 作为唯一入口留在根目录）
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent / "src"))

from config import GenerationParams, CotMode, SamplingParams, OutFormat, validate_params, TranscriptionResult
from backend_gguf import GGUFBackend
from style_presets import STYLE_PRESETS
from vocal_presets import VOCAL_PRESETS, INSTRUMENT_PRESETS, MOOD_PRESETS, LANGUAGE_PRESETS, GENRE_PRESETS
from lyrics_templates import LYRICS_TEMPLATES
from history import HistoryManager, HistoryRecord
from postprocess import postprocess_audio
from queue_manager import queue_manager, TaskType, TaskStatus, TaskCancelledError
from i18n import tr, normalize_lang

PROJECT_ROOT = Path(__file__).parent.parent
WEBUI_ROOT = PROJECT_ROOT / "yue2-webui"
LAST_INPUTS_FILE = WEBUI_ROOT / "last_inputs.json"
# 语言状态持久化文件：服务重启后恢复上次选择，避免旧英文页面与重置为 zh 的
# 服务端 _CUR_LANG 不同步（表现为生成结果状态栏/变体标签回退中文）
LANG_STATE_FILE = WEBUI_ROOT / "lang_state.json"


def _save_lang_state(lang: str) -> None:
    """将当前语言选择写入 lang_state.json（异常时静默忽略，不影响切换）。"""
    try:
        import json as _json
        LANG_STATE_FILE.write_text(_json.dumps({"lang": lang}), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存语言状态失败: {e}")


def _load_lang_state() -> str:
    """读取持久化的语言选择；文件缺失/内容非法时回退 zh。"""
    try:
        import json as _json
        data = _json.loads(LANG_STATE_FILE.read_text(encoding="utf-8"))
        lang = data.get("lang", "zh")
        return lang if lang in ("zh", "en") else "zh"
    except FileNotFoundError:
        return "zh"
    except Exception as e:
        logger.warning(f"读取语言状态失败，回退中文: {e}")
        return "zh"


# 当前界面语言：启动时从 lang_state.json 恢复上次选择
_CUR_LANG = _load_lang_state()

backend = GGUFBackend(PROJECT_ROOT)
history_mgr = HistoryManager(
    history_file=WEBUI_ROOT / "history.json",
    outputs_root=WEBUI_ROOT / "outputs",
)

# Per-channel active task registry, so concurrent requests (e.g. generation
# and transcription) don't clobber each other's cancel handles.
_active_tasks: dict = {}
_active_tasks_lock = threading.Lock()


def _register_task(channel: str, task_id: str):
    with _active_tasks_lock:
        _active_tasks.setdefault(channel, set()).add(task_id)


def _unregister_task(channel: str, task_id: str):
    with _active_tasks_lock:
        tasks = _active_tasks.get(channel)
        if tasks:
            tasks.discard(task_id)
            if not tasks:
                _active_tasks.pop(channel, None)

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

COMMENT_PREFIXES = ("//", "**")


def strip_comment_lines(lyrics: str) -> str:
    """Remove comment lines (starting with // or **) from lyrics."""
    if not lyrics:
        return lyrics
    return "\n".join(
        line for line in lyrics.split("\n")
        if not line.strip().startswith(COMMENT_PREFIXES)
    )


def _load_last_inputs() -> dict:
    if not LAST_INPUTS_FILE.exists():
        return {}
    try:
        data = json.loads(LAST_INPUTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_last_inputs(style: str, lyrics: str, abc: str):
    data = {
        "style": style or "",
        "lyrics": lyrics or "",
        "abc": abc or "",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = LAST_INPUTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LAST_INPUTS_FILE)


def _update_last_abc(abc: str):
    """生成成功后用产出的乐谱更新「使用上一次」记录（仅改 abc，保留风格/歌词）。

    背景：_save_last_inputs 在任务开始时存的是外部 ABC 输入框内容（正常生成时为空），
    若不更新，生成后点「使用上一次」乐谱将恢复为空。
    """
    if not abc:
        return
    data = _load_last_inputs()
    data["abc"] = abc
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = LAST_INPUTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LAST_INPUTS_FILE)


def on_generate(
    style, lyrics, cot, seed, random_seed, cfg_scale, num_inference_steps, out_format, batch_count,
    normalize, fade, trim, metadata,
    abc_text,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress=gr.Progress(track_tqdm=False),
):
    """Generate button callback - submits to queue and polls for results."""
    logger.info(f"on_generate: style={style!r}, cot={cot!r}, seed={seed!r}, cfg_scale={cfg_scale!r}, steps={num_inference_steps!r}, batch={batch_count!r}")
    # 任务入队时快照当前界面语言，供 worker 线程内使用（运行时切换不影响已入队任务）
    lang = _CUR_LANG
    
    # Handle None values from frontend (Gradio may send None for uninitialized sliders)
    cot = cot if cot is not None else "full"
    if isinstance(cot, bool):
        cot = "off" if not cot else "full"
    if random_seed is None:
        random_seed = True
    if random_seed or seed is None:
        seed = random.randint(0, 2**31 - 1)
    else:
        seed = int(seed)
    cfg_scale = cfg_scale if cfg_scale is not None else 0
    num_inference_steps = num_inference_steps if num_inference_steps is not None else 8
    batch_count = int(batch_count) if batch_count is not None else 1
    if batch_count > 1:
        # Batch variants get independent random seeds; a pinned seed only
        # governs single generation.
        seeds = [random.randint(0, 2**31 - 1) for _ in range(batch_count)]
        seed = seeds[0]
    else:
        seeds = [seed]
    
    abc_temp = abc_temp if abc_temp is not None else 0.7
    abc_top_p = abc_top_p if abc_top_p is not None else 0.9
    abc_top_k = abc_top_k if abc_top_k is not None else 30
    abc_rep_penalty = abc_rep_penalty if abc_rep_penalty is not None else 1.005
    abc_pen_window = abc_pen_window if abc_pen_window is not None else 100
    abc_min_tok = abc_min_tok if abc_min_tok is not None else 32
    abc_max_tok = abc_max_tok if abc_max_tok is not None else 4096
    
    sem_temp = sem_temp if sem_temp is not None else 1.0
    sem_top_p = sem_top_p if sem_top_p is not None else 0.95
    sem_top_k = sem_top_k if sem_top_k is not None else 100
    sem_rep_penalty = sem_rep_penalty if sem_rep_penalty is not None else 1.2
    sem_pen_window = sem_pen_window if sem_pen_window is not None else 50
    sem_min_tok = sem_min_tok if sem_min_tok is not None else 200
    sem_max_tok = sem_max_tok if sem_max_tok is not None else 9000
    
    # Validate inputs before queuing
    lyrics = strip_comment_lines(lyrics)
    if not style or not style.strip():
        raise gr.Error(tr(lang, "请输入风格描述"))
    if not lyrics or not lyrics.strip():
        raise gr.Error(tr(lang, "请输入歌词"))
    
    # Submit to queue
    task = queue_manager.submit(
        TaskType.GENERATION,
        _generate_worker,
        lang=lang,
        style=style, lyrics=lyrics, cot=cot, seeds=seeds, cfg_scale=cfg_scale,
        num_inference_steps=num_inference_steps, out_format=out_format, batch_count=batch_count,
        normalize=normalize, fade=fade, trim=trim, metadata=metadata, abc_text=abc_text,
        abc_temp=abc_temp, abc_top_p=abc_top_p, abc_top_k=abc_top_k,
        abc_rep_penalty=abc_rep_penalty, abc_pen_window=abc_pen_window,
        abc_min_tok=abc_min_tok, abc_max_tok=abc_max_tok,
        sem_temp=sem_temp, sem_top_p=sem_top_p, sem_top_k=sem_top_k,
        sem_rep_penalty=sem_rep_penalty, sem_pen_window=sem_pen_window,
        sem_min_tok=sem_min_tok, sem_max_tok=sem_max_tok,
    )
    
    _register_task("generation", task.task_id)
    logger.info(f"Task {task.task_id} submitted to queue")
    
    # Poll for completion, forwarding progress to Gradio
    try:
        while True:
            status_info = queue_manager.get_status(task)
            status = status_info["status"]
            
            if status == TaskStatus.QUEUED:
                pos = status_info["position"]
                queue_ahead = tr(lang, "前面还有 {n} 个任务").replace("{n}", str(pos - 1))
                progress(0, desc=f"{tr(lang, '排队中...')} {queue_ahead}")
            elif status == TaskStatus.RUNNING:
                running_time = status_info.get("running_time", 0)
                progress(0, desc=f"{tr(lang, '执行中...')} ({running_time:.0f}s)")
            elif status == TaskStatus.COMPLETED:
                break
            elif status == TaskStatus.FAILED:
                error_msg = status_info.get("error", tr(lang, "未知错误"))
                raise gr.Error(f"{tr(lang, '生成失败')}: {error_msg}")
            elif status == TaskStatus.CANCELLED:
                raise gr.Error(tr(lang, "任务已取消"))
            
            # Forward progress updates from worker
            for prog_val, desc in task.drain_progress():
                progress(prog_val, desc=desc)
            
            time.sleep(0.5)
        
        # Forward any remaining progress
        for prog_val, desc in task.drain_progress():
            progress(prog_val, desc=desc)
        
        result = task.result if isinstance(task.result, (tuple, list)) else [task.result]
        return (*result, seed)
    except gr.Error:
        raise
    except Exception as e:
        logger.exception(f"生成失败: {e}")
        raise
    finally:
        _unregister_task("generation", task.task_id)


def _generate_worker(
    _task,
    style, lyrics, cot, seeds, cfg_scale, num_inference_steps, out_format, batch_count,
    normalize, fade, trim, metadata, abc_text,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    lang="zh",
):
    """Worker function that runs in the queue thread. Returns the result tuple."""

    params = GenerationParams(
        style=style.strip(),
        lyrics=lyrics.strip(),
        cot=CotMode(cot),
        seed=int(seeds[0]),
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

    error = validate_params(params, lang=lang)
    if error:
        raise ValueError(error)

    _save_last_inputs(params.style, params.lyrics, abc_text or "")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_task_id = f"{timestamp}_{params.id}"
    batch_count = int(batch_count)
    output_dir = WEBUI_ROOT / "outputs" / base_task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    def on_progress(p):
        phase_labels = {
            "loading": tr(lang, "加载模型..."),
            "planning": tr(lang, "规划乐谱..."),
            "generating": tr(lang, "生成音乐..."),
            "synthesizing": tr(lang, "合成音频..."),
            "decoding": tr(lang, "解码音频..."),
            "done": tr(lang, "完成"),
        }
        label = phase_labels.get(p.get("phase", ""), tr(lang, "处理中..."))
        _task.push_progress(0, label)

    batch_count = int(batch_count)
    results = []
    seeds = [int(s) for s in seeds]

    for i in range(batch_count):
        if _task.cancel_event and _task.cancel_event.is_set():
            break

        current_seed = seeds[i]
        task_id = f"{base_task_id}_var{i+1}" if batch_count > 1 else base_task_id
        variant_dir = output_dir
        variant_dir.mkdir(parents=True, exist_ok=True)

        params.seed = current_seed
        _task.push_progress(0, f"{tr(lang, '生成变体')} {i+1}/{batch_count} (seed={current_seed})...")

        result = backend.generate(
            params=params,
            output_dir=variant_dir,
            on_progress=on_progress,
            cancel_event=_task.cancel_event,
            output_name=task_id,
            lang=lang,
        )
        results.append((task_id, variant_dir, result, current_seed))

    successful = [(tid, d, r, s) for tid, d, r, s in results if r.success]
    if not successful:
        if _task.cancel_event.is_set():
            raise TaskCancelledError(tr(lang, "任务已取消"))
        failed_msg = results[0][2].error_message if results else tr(lang, "未知错误")
        raise ValueError(f"{tr(lang, '生成失败')}: {failed_msg}")

    # 生成成功：用产出的乐谱（最后一个成功变体）更新「使用上一次」记录，
    # 否则记录停留在任务开始时外部输入框的内容（正常生成时为空）
    last_abc = successful[-1][2].abc_score or (abc_text or "")
    _update_last_abc(last_abc)

    format_label = FORMAT_LABELS.get(params.out_format.value, "PCM 16-bit")

    if any([normalize, fade, trim, metadata]):
        for idx, (task_id, variant_dir, result, variant_seed) in enumerate(successful):
            wav_path = Path(result.audio_path)
            if wav_path.exists():
                _task.push_progress(0, tr(lang, "后处理音频..."))
                # 携带完整生成/采样参数写入 sidecar JSON（cfg/ODE/批量/采样等）
                postprocess_audio(
                    wav_path,
                    normalize=normalize, fade=fade, trim=trim, metadata=metadata,
                    style=params.style, seed=variant_seed,
                    out_format=params.out_format.value,
                    extra_meta={
                        "cot": params.cot.value,
                        "cfg_scale": params.cfg_scale if params.cfg_scale is not None else 0,
                        "num_inference_steps": params.num_inference_steps,
                        "batch_count": batch_count,
                        "variant_index": idx + 1,
                        "out_format": params.out_format.value,
                        "model_gguf": backend.main_model,
                        "vae_gguf": backend.vae_model,
                        "abc_sampling": asdict(params.abc_sampling),
                        "semantic_sampling": asdict(params.semantic_sampling),
                    },
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
        duration_info = f"{tr(lang, '批量生成')} **{len(successful)}/{batch_count}** {tr(lang, '个变体')} | {tr(lang, '总耗时')} **{total_time:.1f}s** | {tr(lang, '平均音频时长')} **{avg_duration:.1f}s** | {format_label}"
    else:
        r = successful[0][2]
        duration_info = f"{tr(lang, '生成耗时')} **{r.generation_time_seconds:.1f}s** | {tr(lang, '音频时长')} **{r.audio_duration_seconds:.1f}s** | {format_label}"

    _lyrics_json = html.escape(json.dumps(params.lyrics, ensure_ascii=False), quote=True)
    lyrics_data_html = f'<div class="gen-lyrics-data" style="display:none" data-lyrics=\'{_lyrics_json}\' data-duration="{avg_duration}"></div>'

    for task_id, variant_dir, result, variant_seed in successful:
        abc_path = ""
        if result.abc_score:
            abc_file = variant_dir / f"{task_id}.abc"
            abc_file.write_text(result.abc_score, encoding="utf-8")
            abc_path = str(abc_file.relative_to(WEBUI_ROOT))

        lyrics_file = variant_dir / f"{task_id}.txt"
        lyrics_file.write_text(params.lyrics, encoding="utf-8")

        record = HistoryRecord(
            task_id=task_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            style=params.style,
            lyrics=params.lyrics,
            lyrics_preview=params.lyrics[:60],
            cot=params.cot.value,
            seed=variant_seed,
            cfg_scale=params.cfg_scale if params.cfg_scale is not None else 0,
            num_inference_steps=params.num_inference_steps,
            batch_count=batch_count,
            audio_duration_seconds=result.audio_duration_seconds or 0,
            generation_time_seconds=result.generation_time_seconds or 0,
            audio_path=str(result.audio_path),
            output_dir=str(variant_dir.relative_to(WEBUI_ROOT)),
            abc_path=abc_path,
            out_format=params.out_format.value,
        )
        history_mgr.append(record)
    history_mgr.auto_prune()

    first_result = successful[0][2]
    first_task_id = successful[0][0]
    first_dir = successful[0][1]
    abc_display = first_result.abc_score or ""
    abc_download = str(first_dir / f"{first_task_id}.abc") if first_result.abc_score else None

    if batch_count == 1:
        mp3_download = first_result.mp3_path
        h_rows, h_info = refresh_history()
        return (
            first_result.audio_path, duration_info, abc_display, abc_download, mp3_download,
            lyrics_data_html, h_rows, h_info, 0,
            gr.update(visible=False), gr.update(visible=False, choices=[], value=None), [],
        )
    else:
        variants_payload = []
        for idx, (task_id, variant_dir, result, variant_seed) in enumerate(successful, start=1):
            variants_payload.append({
                "label": f"{tr(lang, '变体')}{idx} (seed={variant_seed}, {result.audio_duration_seconds or 0:.1f}s)",
                "task_id": task_id,
                "audio_path": str(result.audio_path),
                "abc_text": result.abc_score or "",
                "abc_file": str(variant_dir / f"{task_id}.abc") if result.abc_score else None,
                "mp3_file": result.mp3_path,
            })

        audio_paths = [str(r.audio_path) for _, _, r, _ in successful]
        h_rows, h_info = refresh_history()
        return (
            audio_paths[0], duration_info, abc_display, abc_download, None,
            lyrics_data_html, h_rows, h_info, 0,
            gr.update(visible=True),
            gr.update(visible=True, choices=[v["label"] for v in variants_payload], value=variants_payload[0]["label"]),
            variants_payload,
        )


def on_restore_last(kind: str, current: str):
    """Fill an input box with the last-saved text of the same kind."""
    value = _load_last_inputs().get(kind, "")
    return value if value else (current or "")


def on_variant_select(label, payload):
    """Switch main outputs to the selected batch variant."""
    for v in payload:
        if v["label"] == label:
            return v["audio_path"], v["abc_text"], v["abc_file"], v["mp3_file"]
    # Label/state desync (e.g. after a page reload or while the selector is
    # being reset) — keep the current outputs instead of erroring.
    return gr.update(), gr.update(), gr.update(), gr.update()


def on_variant_finalize(label, payload):
    """Mark selected variant as final and delete the others."""
    return _finalize_variant(label, payload, keep_all=False)


def on_variant_keep_all(label, payload):
    """Mark selected variant as final but keep all variants."""
    return _finalize_variant(label, payload, keep_all=True)


def _finalize_variant(label, payload, keep_all):
    if not payload or not label:
        raise gr.Error(tr(_CUR_LANG, "没有可用的批量变体"))
    selected = next((v for v in payload if v["label"] == label), None)
    if not selected:
        raise gr.Error(tr(_CUR_LANG, "变体不存在"))

    history_mgr.set_status(selected["task_id"], "final")
    removed = 0
    if not keep_all:
        for v in payload:
            if v["task_id"] != selected["task_id"]:
                history_mgr.delete(v["task_id"])
                removed += 1

    h_rows, h_info, _ = refresh_history_full()
    lang = _CUR_LANG
    msg = tr(lang, "🏆 已选定") + f" **{label}** " + tr(lang, "为最终版")
    if removed:
        msg += tr(lang, "，已清理其余") + f" {removed} " + tr(lang, "个变体")
    else:
        msg += tr(lang, "，全部变体已保留")
    if keep_all:
        return (
            msg, h_rows, h_info, 0,
            gr.update(visible=True), gr.update(visible=True, choices=[v["label"] for v in payload], value=label), payload,
        )
    return (
        msg, h_rows, h_info, 0,
        gr.update(visible=False), gr.update(visible=False, choices=[], value=None), [],
    )


def on_cancel():
    """Cancel button callback."""
    with _active_tasks_lock:
        task_ids = _active_tasks.pop("generation", set())
    for task_id in task_ids:
        queue_manager.cancel_task_by_id(task_id)
    if task_ids:
        return tr(_CUR_LANG, "正在取消...")
    return tr(_CUR_LANG, "没有正在运行的任务")


def on_resynthesize(
    abc_text, style, lyrics, seed, cfg_scale, num_inference_steps, out_format,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress=gr.Progress(track_tqdm=False),
):
    """Resynthesize with edited ABC score - submits to queue."""
    lang = _CUR_LANG
    seed = seed if seed is not None else 831001
    cfg_scale = cfg_scale if cfg_scale is not None else 0
    num_inference_steps = num_inference_steps if num_inference_steps is not None else 8

    if not abc_text or not abc_text.strip():
        raise gr.Error(tr(_CUR_LANG, "ABC 乐谱不能为空"))

    lyrics = strip_comment_lines(lyrics)

    task = queue_manager.submit(
        TaskType.GENERATION,
        _resynthesize_worker,
        lang=lang,
        abc_text=abc_text, style=style, lyrics=lyrics, seed=seed,
        cfg_scale=cfg_scale, num_inference_steps=num_inference_steps, out_format=out_format,
        abc_temp=abc_temp, abc_top_p=abc_top_p, abc_top_k=abc_top_k,
        abc_rep_penalty=abc_rep_penalty, abc_pen_window=abc_pen_window,
        abc_min_tok=abc_min_tok, abc_max_tok=abc_max_tok,
        sem_temp=sem_temp, sem_top_p=sem_top_p, sem_top_k=sem_top_k,
        sem_rep_penalty=sem_rep_penalty, sem_pen_window=sem_pen_window,
        sem_min_tok=sem_min_tok, sem_max_tok=sem_max_tok,
    )

    _register_task("generation", task.task_id)
    logger.info(f"Resynthesize task {task.task_id} submitted to queue")

    try:
        while True:
            status_info = queue_manager.get_status(task)
            status = status_info["status"]

            if status == TaskStatus.QUEUED:
                pos = status_info["position"]
                queue_ahead = tr(lang, "前面还有 {n} 个任务").replace("{n}", str(pos - 1))
                progress(0, desc=f"{tr(lang, '排队中...')} {queue_ahead}")
            elif status == TaskStatus.RUNNING:
                running_time = status_info.get("running_time", 0)
                progress(0, desc=f"{tr(lang, '重新合成中...')} ({running_time:.0f}s)")
            elif status == TaskStatus.COMPLETED:
                break
            elif status == TaskStatus.FAILED:
                error_msg = status_info.get("error", tr(lang, "未知错误"))
                raise gr.Error(f"{tr(lang, '重新合成失败')}: {error_msg}")
            elif status == TaskStatus.CANCELLED:
                raise gr.Error(tr(lang, "任务已取消"))

            for prog_val, desc in task.drain_progress():
                progress(prog_val, desc=desc)

            time.sleep(0.5)

        for prog_val, desc in task.drain_progress():
            progress(prog_val, desc=desc)

        return task.result
    except gr.Error:
        raise
    except Exception as e:
        logger.exception(f"重新合成失败: {e}")
        raise
    finally:
        _unregister_task("generation", task.task_id)


def _resynthesize_worker(
    _task, abc_text, style, lyrics, seed, cfg_scale, num_inference_steps, out_format,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    lang="zh",
):
    """Worker function for resynthesis, runs in queue thread."""

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

    error = validate_params(params, lang=lang)
    if error:
        raise ValueError(error)

    _save_last_inputs(params.style, params.lyrics, abc_text or "")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"{timestamp}_resynth_{params.id}"
    output_dir = WEBUI_ROOT / "outputs" / task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    def on_progress(p):
        phase_labels = {
            "loading": tr(lang, "加载模型..."),
            "generating": tr(lang, "合成音乐..."),
            "synthesizing": tr(lang, "合成音频..."),
            "decoding": tr(lang, "解码音频..."),
            "done": tr(lang, "完成"),
        }
        label = phase_labels.get(p.get("phase", ""), tr(lang, "处理中..."))
        _task.push_progress(0, label)

    _task.push_progress(0, tr(lang, "开始重新合成..."))

    result = backend.generate(
        params=params,
        output_dir=output_dir,
        on_progress=on_progress,
        cancel_event=_task.cancel_event,
        lang=lang,
    )

    if result.success:
        format_label = FORMAT_LABELS.get(params.out_format.value, "PCM 16-bit")
        duration_info = f"{tr(lang, '重新合成耗时')} **{result.generation_time_seconds:.1f}s** | {tr(lang, '音频时长')} **{result.audio_duration_seconds:.1f}s** | {format_label}"

        record = HistoryRecord(
            task_id=task_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            style=params.style,
            lyrics=params.lyrics,
            lyrics_preview=tr(lang, "重新合成"),
            cot="melody",
            seed=params.seed,
            cfg_scale=params.cfg_scale if params.cfg_scale is not None else 0,
            num_inference_steps=params.num_inference_steps,
            audio_duration_seconds=result.audio_duration_seconds or 0,
            generation_time_seconds=result.generation_time_seconds or 0,
            audio_path=str(result.audio_path),
            output_dir=str(output_dir.relative_to(WEBUI_ROOT)),
            abc_path="",
            status="resynthesized",
            out_format=params.out_format.value,
        )
        history_mgr.append(record)
        history_mgr.auto_prune()

        lyrics_file = output_dir / f"{output_dir.name}.txt"
        lyrics_file.write_text(params.lyrics, encoding="utf-8")

        # 重新合成成功：产出乐谱更新「使用上一次」记录
        _update_last_abc(result.abc_score or abc_text or "")

        abc_download = str(output_dir / f"{output_dir.name}.abc") if result.abc_score else None
        mp3_download = result.mp3_path
        resynth_lyrics_data = f'<div class="gen-lyrics-data" style="display:none" data-lyrics=\'{html.escape(json.dumps(params.lyrics, ensure_ascii=False), quote=True)}\' data-duration="{result.audio_duration_seconds}"></div>'
        return result.audio_path, duration_info, abc_download, mp3_download, resynth_lyrics_data
    else:
        if _task.cancel_event.is_set():
            raise TaskCancelledError(tr(lang, "任务已取消"))
        raise ValueError(f"{tr(lang, '重新合成失败')}：{result.error_message}")


def on_transcribe(audio_file, progress=gr.Progress(track_tqdm=False)):
    """Transcribe audio to ABC score using SheetSage2 - submits to queue."""
    lang = _CUR_LANG
    if not audio_file:
        raise gr.Error(tr(_CUR_LANG, "请先上传音频文件"))
    
    audio_path = Path(audio_file)
    if not audio_path.exists():
        raise gr.Error(tr(_CUR_LANG, "音频文件不存在"))
    
    supported_formats = {'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac', '.wma'}
    if audio_path.suffix.lower() not in supported_formats:
        raise gr.Error(f"{tr(_CUR_LANG, '不支持的音频格式')}：{audio_path.suffix}。{tr(_CUR_LANG, '支持的格式')}：{', '.join(sorted(supported_formats))}")
    
    task = queue_manager.submit(
        TaskType.TRANSCRIPTION,
        _transcribe_worker,
        lang=lang,
        audio_path=str(audio_path),
    )
    
    _register_task("transcription", task.task_id)
    logger.info(f"Transcription task {task.task_id} submitted to queue")

    try:
        while True:
            status_info = queue_manager.get_status(task)
            status = status_info["status"]

            if status == TaskStatus.QUEUED:
                pos = status_info["position"]
                queue_ahead = tr(lang, "前面还有 {n} 个任务").replace("{n}", str(pos - 1))
                progress(0, desc=f"{tr(lang, '排队中...')} {queue_ahead}")
            elif status == TaskStatus.RUNNING:
                running_time = status_info.get("running_time", 0)
                progress(0, desc=f"{tr(lang, '转谱中...')} ({running_time:.0f}s)")
            elif status == TaskStatus.COMPLETED:
                break
            elif status == TaskStatus.FAILED:
                error_msg = status_info.get("error", tr(lang, "未知错误"))
                raise gr.Error(f"{tr(lang, '转谱失败')}: {error_msg}")
            elif status == TaskStatus.CANCELLED:
                raise gr.Error(tr(lang, "任务已取消"))

            for prog_val, desc in task.drain_progress():
                progress(prog_val, desc=desc)

            time.sleep(0.5)

        for prog_val, desc in task.drain_progress():
            progress(prog_val, desc=desc)

        return task.result
    except gr.Error:
        raise
    except Exception as e:
        logger.exception(f"转谱失败: {e}")
        raise
    finally:
        _unregister_task("transcription", task.task_id)


def _transcribe_worker(_task, audio_path, lang="zh"):
    """Worker function for transcription, runs in queue thread."""
    audio_path = Path(audio_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"transcribe_{timestamp}"
    output_dir = WEBUI_ROOT / "outputs" / task_id
    
    def on_progress(p):
        if p and p.get("message"):
            _task.push_progress(p.get("progress", 0), p["message"])
    
    result = backend.transcribe(audio_path, output_dir, on_progress=on_progress, lang=lang)
    
    if result.success:
        abc_score = result.abc_score or ""
        midi_path = result.midi_path
        info = f"{tr(lang, '转谱耗时')} **{result.transcription_time_seconds:.1f}s**"
        
        abc_file = output_dir / f"{output_dir.name}.abc"
        abc_file_path = str(abc_file.relative_to(WEBUI_ROOT)) if abc_file.exists() else ""
        
        return abc_score, info, abc_file_path, midi_path, task_id
    else:
        raise ValueError(f"{tr(lang, '转谱失败')}：{result.error_message}")


def on_send_to_generate(abc_text):
    """Send ABC score to generation tab via bridge."""
    if not abc_text or not abc_text.strip():
        raise gr.Error(tr(_CUR_LANG, "没有可发送的乐谱内容"))
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
    return gr.update(visible=not is_off), gr.update(visible=not is_off)


HISTORY_PAGE_SIZE = 10


def _history_page_info_text(page, pages, total):
    """按当前语言生成分页信息文案（整体句式，避免逐词翻译导致中英标点混杂）。"""
    if _CUR_LANG == "en":
        return f"Page {page} / {pages}, {total} entries"
    return f"第 {page} / {pages} 页，共 {total} 条"


def refresh_history():
    """Refresh history dataframe (first page)."""
    rows = history_mgr.to_dataframe_rows()
    page_rows = rows[:HISTORY_PAGE_SIZE]
    total = len(rows)
    pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    return page_rows, _history_page_info_text(1, pages, total)


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
    return page_rows, _history_page_info_text(page + 1, pages, total)


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
        return current_state, None, tr(_CUR_LANG, "请选择一条记录"), "", "", "", "", "", ""
    task_id = rows[row_index][5]
    entry = history_mgr.get(task_id)
    if not entry:
        return current_state, None, tr(_CUR_LANG, "记录不存在"), "", "", "", "", "", ""
    audio_path = Path(entry.audio_path)
    abc_score = history_mgr.get_abc_score(task_id) or ""
    if audio_path.exists():
        lyrics = entry.lyrics or entry.lyrics_preview or ""
        _lyrics_json = html.escape(json.dumps(lyrics, ensure_ascii=False), quote=True)
        lyrics_data = f'<div class="history-lyrics-data" style="display:none" data-lyrics=\'{_lyrics_json}\' data-duration="{entry.audio_duration_seconds}"></div>'
        abc_preview = '<div id="history-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px;"><div id="history-abc-paper"></div><div id="history-abc-audio"></div></div>'
        return [task_id], str(audio_path), f"**{entry.task_id}**", entry.style, lyrics, abc_score, abc_preview, lyrics_data, f'<div class="history-duration-data" style="display:none" data-duration="{entry.audio_duration_seconds}"></div>'
    return [task_id], None, tr(_CUR_LANG, "音频文件不存在"), "", "", "", "", "", ""


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
        return rows, info, tr(_CUR_LANG, "请先点击选择要删除的记录"), selected_state, 0
    task_id = selected_state[0]
    if history_mgr.delete(task_id):
        rows, info = refresh_history()
        return rows, info, f"{tr(_CUR_LANG, '已删除')} {task_id}", [], 0
    rows, info = refresh_history()
    return rows, info, tr(_CUR_LANG, "删除失败"), selected_state, 0


def on_history_clear():
    """Clear all history."""
    history_mgr.clear()
    rows, info = refresh_history()
    return rows, info, tr(_CUR_LANG, "已清空所有历史"), [], 0


def on_check_models():
    """Check model files and return status."""
    checks = backend.check_models()
    lang = _CUR_LANG
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

    total, used, free = shutil.disk_usage(str(PROJECT_ROOT))
    lines.append(f"\n### {tr(lang, '磁盘')}\n- {tr(lang, '剩余')}: {free / (1024**3):.1f} GB / {total / (1024**3):.1f} GB")

    return "\n".join(lines)


def on_lyrics_change(lyrics):
    """Return structure analysis HTML when lyrics change."""
    lang = _CUR_LANG
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


def _preset_display_names(lang: str) -> list:
    """预设下拉框显示名：内置预设名按语言翻译，用户自定义预设保持文件名原样。"""
    names = [tr(lang, k) for k in BUILTIN_PRESETS]
    user_dir = WEBUI_ROOT / "presets"
    if user_dir.exists():
        names += [p.stem for p in sorted(user_dir.glob("*.json"))]
    return names


def on_preset_load(name):
    """Load a preset and return param values."""
    preset = BUILTIN_PRESETS.get(name)
    if not preset:
        # 英文界面下选中翻译名（如 Default）时，反查回中文内置键
        for k in BUILTIN_PRESETS:
            if tr(_CUR_LANG, k) == name:
                preset = BUILTIN_PRESETS[k]
                break
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
        return tr(_CUR_LANG, "请输入预设名称")
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
    return f"{tr(_CUR_LANG, '已保存预设')}: {name}"


# 标题行紧凑样式：语言下拉框伪装为原生控件（无组件外框），配色用主题变量自动适配明暗
# 关键点：Gradio 给 .block 设了 width:100%，而 flex-basis:auto 会回退读 width，
# 因此必须显式 width:auto 才能让 hint/dd 按内容收缩，否则各自撑满整行换行堆叠。
_TITLE_ROW_CSS = """
#title-row { align-items: center; gap: 8px; }
#lang-hint { width: auto; display: flex; align-items: center; margin: 0; flex: 0 0 auto; }
#lang-hint label { font-size: 13px; color: var(--body-text-color); opacity: .75; white-space: nowrap; }
#lang-dd { width: auto; flex: 0 0 auto; margin: 0; }
#lang-dd > div { display: flex; align-items: center; height: 26px; }
#lang-dd .wrap { border: 1px solid var(--border-color-primary); border-radius: 6px; background: var(--background-fill-primary); box-shadow: none; min-height: 26px; padding: 0 2px 0 8px; }
#lang-dd .wrap-inner, #lang-dd .secondary-wrap { min-height: 24px; }
#lang-dd input { font-size: 12px; min-height: 24px; height: 24px; width: 80px; }
#lang-dd .icon-wrap { padding: 0 4px; }
#lang-dd .icon-wrap svg { width: 12px; height: 12px; }
#lang-signal { display: none; }
"""

# Gradio 内置文案（上传组件"将音频拖放到此处/点击上传"、页脚等）跟随浏览器 locale，
# 后端无参数可控制。此处通过隐藏信号组件 #lang-signal（值=zh/en，CSS display:none
# 隐藏而非 visible=False——后者不渲染 DOM，前端将无法监听）+ 本脚本联动：
# 轮询等待信号组件渲染 -> 调用 Gradio 前端内部 changeLocale 切换其内置文案语言。
# core-*.js 文件名带构建 hash，运行时从 <script> 标签或 performance 资源记录动态
# 获取（Gradio 模块多为动态 import，不一定存在于 script 标签），避免硬编码。
_LOCALE_SYNC_JS = """
(function () {
    var GRADIO_LOCALE = { zh: "zh-CN", en: "en" };
    function findCoreModuleUrl() {
        var s = document.querySelector('script[src*="/core-"]');
        if (s) return s.src;
        var res = performance.getEntriesByType('resource').map(function (e) { return e.name; });
        for (var i = 0; i < res.length; i++) {
            if (/\\/core-[^/]+\\.js$/.test(res[i])) return res[i];
        }
        return null;
    }
    function applyGradioLocale(node) {
        var lang = (node.textContent || "").trim();
        var locale = GRADIO_LOCALE[lang];
        if (!locale) return;
        var coreUrl = findCoreModuleUrl();
        if (!coreUrl) return;
        import(coreUrl).then(function (m) {
            if (m && m.changeLocale) m.changeLocale(locale);
        }).catch(function () {});
    }
    // 轮询等待信号组件渲染（最长约 30 秒），出现后先做初始同步，再持续监听语言变化
    var tries = 0;
    var timer = setInterval(function () {
        var node = document.querySelector("#lang-signal");
        tries += 1;
        if (node) {
            clearInterval(timer);
            applyGradioLocale(node);
            new MutationObserver(function () { applyGradioLocale(node); })
                .observe(node, { childList: true, characterData: true, subtree: true });
        } else if (tries > 150) {
            clearInterval(timer);
        }
    }, 200);
})();
"""


def build_ui():
    """Build the Gradio UI."""
    with gr.Blocks(title="YuE2 Music Studio") as demo:

        def _t(s: str) -> str:
            """按当前界面语言翻译单条文案（zh 直接返回原文）。"""
            return tr(_CUR_LANG, s)

        # 标题行：主标题+副标题 Markdown，右侧原生风格 "Lang/语言" 说明 + 紧凑下拉框
        with gr.Row(elem_id="title-row"):
            title_md = gr.Markdown("### YuE2 Music Studio · " + _t("AI音乐创作 — 输入歌词和风格，生成完整歌曲"))
            # 说明文字用原生 HTML label（无 Gradio block 底色）
            gr.HTML('<label>Lang/语言</label>', elem_id="lang-hint", container=False)
            lang_select = gr.Dropdown(
                choices=["中文", "English"], value=("English" if _CUR_LANG == "en" else "中文"),
                show_label=False, container=False,
                elem_id="lang-dd", scale=0, min_width=90,
            )
        # 语言即时切换控件：下拉框选择中文/English，State 保存归一化语言标识。
        # translatables/_updaters 一一对应（同一组件各占一位），保证 apply_lang
        # 返回值数量与 outputs=[lang_state]+translatables 完全一致。
        # 初始值取自 lang_state.json 恢复的 _CUR_LANG，重启后界面语言不回退。
        lang_state = gr.State(_CUR_LANG)
        translatables: list = []
        _updaters: list = []

        def _reg(comp, updater):
            """注册一个可切换语言组件：comp 进 outputs，updater 生成对应 gr.update。"""
            translatables.append(comp)
            _updaters.append(updater)

        def apply_lang(lang):
            """语言切换回调：全局更新 _CUR_LANG 并为每个 translatable 生成 gr.update。"""
            global _CUR_LANG
            nlang = normalize_lang(lang)
            _CUR_LANG = nlang
            _save_lang_state(nlang)  # 持久化，服务重启后恢复
            return [nlang] + [u(nlang) for u in _updaters]

        _reg(title_md, lambda lang: gr.update(value="### YuE2 Music Studio · " + tr(lang, "AI音乐创作 — 输入歌词和风格，生成完整歌曲")))

        # 语言信号组件（CSS display:none 隐藏，visible=False 不渲染 DOM 会导致前端无法监听）：
        # 值=当前语言(zh/en)。前端 _LOCALE_SYNC_JS 脚本监听其变化并调用 Gradio 内部
        # changeLocale，同步上传组件/页脚等 Gradio 内置文案语言。
        lang_signal = gr.HTML(value=_CUR_LANG, elem_id="lang-signal")
        _reg(lang_signal, lambda lang: gr.update(value=lang))

        with gr.Tabs():
            tab_create = gr.Tab(_t("创作"))
            _reg(tab_create, lambda lang: gr.update(label=tr(lang, "创作")))
            with tab_create:
                with gr.Row():
                    with gr.Column(scale=1):
                        style_input = gr.Textbox(
                            label=_t("风格描述"),
                            placeholder="English, warm piano pop, expressive female voice, acoustic piano, 88 BPM",
                            lines=2,
                            info=_t("语言 + 流派 + 乐器 + 人声 + 速度"),
                        )
                        _reg(style_input, lambda lang: gr.update(label=tr(lang, "风格描述"), info=tr(lang, "语言 + 流派 + 乐器 + 人声 + 速度")))
                        with gr.Row(elem_classes="last-btn-row"):
                            last_style_btn = gr.Button(_t("使用上一次"), size="sm", scale=0, min_width=110)
                            _reg(last_style_btn, lambda lang: gr.update(value=tr(lang, "使用上一次")))

                        style_tag_acc = gr.Accordion(_t("风格标签"), open=False)
                        _reg(style_tag_acc, lambda lang: gr.update(label=tr(lang, "风格标签")))
                        with style_tag_acc:
                            quick_tags_md = gr.Markdown(_t("#### 风格快捷标签"))
                            _reg(quick_tags_md, lambda lang: gr.update(value=tr(lang, "#### 风格快捷标签")))
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

                            vocal_acc = gr.Accordion(_t("人声标签"), open=False)
                            _reg(vocal_acc, lambda lang: gr.update(label=tr(lang, "人声标签")))
                            with vocal_acc:
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

                            inst_acc = gr.Accordion(_t("乐器标签"), open=False)
                            _reg(inst_acc, lambda lang: gr.update(label=tr(lang, "乐器标签")))
                            with inst_acc:
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

                            mood_acc = gr.Accordion(_t("情绪标签"), open=False)
                            _reg(mood_acc, lambda lang: gr.update(label=tr(lang, "情绪标签")))
                            with mood_acc:
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

                            lang_tag_acc = gr.Accordion(_t("语言标签"), open=False)
                            _reg(lang_tag_acc, lambda lang: gr.update(label=tr(lang, "语言标签")))
                            with lang_tag_acc:
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

                            genre_acc = gr.Accordion(_t("流派标签"), open=False)
                            _reg(genre_acc, lambda lang: gr.update(label=tr(lang, "流派标签")))
                            with genre_acc:
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
                            label=_t("歌词"),
                            placeholder=f"[Verse]\n{_t('在这里输入歌词...')}\n\n[Chorus]\n{_t('副歌歌词...')}",
                            lines=10,
                            info=_t("支持 [Verse] [Chorus] [Bridge] 段落标记，可拖拽排序"),
                        )
                        _reg(lyrics_input, lambda lang: gr.update(label=tr(lang, "歌词"), placeholder=f"[Verse]\n{tr(lang, '在这里输入歌词...')}\n\n[Chorus]\n{tr(lang, '副歌歌词...')}", info=tr(lang, "支持 [Verse] [Chorus] [Bridge] 段落标记，可拖拽排序")))
                        with gr.Row(elem_classes="last-btn-row"):
                            last_lyrics_btn = gr.Button(_t("使用上一次"), size="sm", scale=0, min_width=110)
                            _reg(last_lyrics_btn, lambda lang: gr.update(value=tr(lang, "使用上一次")))

                        lyrics_tools_acc = gr.Accordion(_t("歌词工具"), open=False)
                        _reg(lyrics_tools_acc, lambda lang: gr.update(label=tr(lang, "歌词工具")))
                        with lyrics_tools_acc:
                            with gr.Row():
                                gr.Button("+ Verse", size="sm")
                                gr.Button("+ Chorus", size="sm")
                                gr.Button("+ Bridge", size="sm")
                                gr.Button("+ Intro", size="sm")
                                gr.Button("+ Outro", size="sm")
                                gr.Button("+ Pre-Chorus", size="sm")

                            # 段落标记说明表格：按语言组装，切语言时由 _reg 重新生成
                            def _section_notes_md(t):
                                return (
                                    f"{t('#### 段落标记说明')}\n"
                                    f"| {t('标记')} | {t('用途')} |\n"
                                    "| --- | --- |\n"
                                    f"| [Intro] | {t('前奏/器乐引入')} |\n"
                                    f"| [Verse] | {t('主歌段落')} |\n"
                                    f"| [Pre-Chorus] | {t('预副歌，制造期待感')} |\n"
                                    f"| [Chorus] | {t('副歌，全曲最抓耳的部分')} |\n"
                                    f"| [Bridge] | {t('桥段，打破重复，情感转折')} |\n"
                                    f"| [Outro] | {t('尾声/渐弱收尾')} |\n\n"
                                    f"{t('注释行：以 `//` 或 `**` 开头的行视为注释，不会送入模型生成。')}"
                                )

                            section_notes_md = gr.Markdown(_section_notes_md(_t))
                            _reg(section_notes_md, lambda lang: gr.update(value=_section_notes_md(lambda k: tr(lang, k))))

                            structure_tpl_md = gr.Markdown(_t("#### 歌曲结构模板"))
                            _reg(structure_tpl_md, lambda lang: gr.update(value=tr(lang, "#### 歌曲结构模板")))
                            with gr.Row():
                                gr.Button("Verse-Chorus", size="sm")
                                gr.Button("V-C-V-C", size="sm")
                                gr.Button("V-C-V-C-B-C", size="sm")
                                gr.Button("V-V-C", size="sm")
                                gr.Button("A-A-B-A", size="sm")

                            segment_cards = gr.HTML(
                                label=_t("段落拖拽排序"),
                                value='<div id="segment-cards" style="padding:4px 0;"></div>',
                            )
                            structure_analysis = gr.HTML(
                                label=_t("结构分析"),
                                value=f'<div id="lyrics-structure" style="padding:4px 8px;color:#888;">{_t("输入歌词后显示结构分析")}</div>',
                            )
                            _reg(structure_analysis, lambda lang: gr.update(value=f'<div id="lyrics-structure" style="padding:4px 8px;color:#888;">{tr(lang, "输入歌词后显示结构分析")}</div>'))

                            template_dropdown = gr.Dropdown(
                                label=_t("歌词模板 (内容)"),
                                choices=list(LYRICS_TEMPLATES.keys()),
                                value=None,
                                info=_t("选择模板将填充歌词内容（覆盖现有内容）"),
                            )
                            _reg(template_dropdown, lambda lang: gr.update(label=tr(lang, "歌词模板 (内容)"), info=tr(lang, "选择模板将填充歌词内容（覆盖现有内容）")))
                            template_dropdown.change(fn=on_lyrics_template, inputs=template_dropdown, outputs=lyrics_input)

                        workmode_md = gr.Markdown(_t("### 工作模式"))
                        _reg(workmode_md, lambda lang: gr.update(value=tr(lang, "### 工作模式")))
                        cot_input = gr.Radio(
                            label=_t("模式"),
                            choices=[
                                (_t("完整创作 (生成乐谱+和弦)"), "full"),
                                (_t("旋律创作 (仅旋律，适合翻唱)"), "melody"),
                                (_t("直接生成 (跳过乐谱，最快)"), "off"),
                            ],
                            value="full",
                        )
                        _reg(cot_input, lambda lang: gr.update(label=tr(lang, "模式"), choices=[(tr(lang, "完整创作 (生成乐谱+和弦)"), "full"), (tr(lang, "旋律创作 (仅旋律，适合翻唱)"), "melody"), (tr(lang, "直接生成 (跳过乐谱，最快)"), "off")]))

                        abc_input = gr.Textbox(
                            label=_t("ABC 乐谱 (外部输入)"),
                            placeholder="X:1\nM:4/4\nL:1/16\nK:C\n...",
                            lines=8,
                            visible=True,
                            info=_t("提供外部ABC乐谱文本。仅在 full/melody 模式下生效。留空则自动生成。"),
                        )
                        _reg(abc_input, lambda lang: gr.update(label=tr(lang, "ABC 乐谱 (外部输入)"), info=tr(lang, "提供外部ABC乐谱文本。仅在 full/melody 模式下生效。留空则自动生成。")))
                        with gr.Row(elem_classes="last-btn-row"):
                            last_abc_btn = gr.Button(_t("使用上一次"), size="sm", scale=0, min_width=110)
                            _reg(last_abc_btn, lambda lang: gr.update(value=tr(lang, "使用上一次")))
                        cot_input.change(fn=on_cot_change, inputs=cot_input, outputs=[abc_input, last_abc_btn])

                        last_style_btn.click(fn=lambda cur: on_restore_last("style", cur), inputs=style_input, outputs=style_input)
                        last_lyrics_btn.click(fn=lambda cur: on_restore_last("lyrics", cur), inputs=lyrics_input, outputs=lyrics_input)
                        # 恢复上次乐谱：off（直接生成）模式下 ABC 输入框隐藏，
                        # 恢复到乐谱时自动切回 full 以显示输入框（cot 值变化触发 on_cot_change 联动显示）
                        def _restore_last_abc(cur_abc, cur_cot):
                            value = on_restore_last("abc", cur_abc)
                            return value, ("full" if (value and cur_cot == "off") else cur_cot)
                        last_abc_btn.click(fn=_restore_last_abc, inputs=[abc_input, cot_input], outputs=[abc_input, cot_input])

                        with gr.Row():
                            seed_input = gr.Number(label=_t("随机种子"), value=831001, precision=0, info=_t("勾选「随机种子变化」时每次生成自动换新，此处显示实际使用的种子"))
                            _reg(seed_input, lambda lang: gr.update(label=tr(lang, "随机种子"), info=tr(lang, "勾选「随机种子变化」时每次生成自动换新，此处显示实际使用的种子")))
                            random_seed_btn = gr.Button(_t("🎲 随机"), size="sm")
                            _reg(random_seed_btn, lambda lang: gr.update(value=tr(lang, "🎲 随机")))
                        random_seed_checkbox = gr.Checkbox(label=_t("随机种子变化"), value=True, info=_t("勾选: 每次点击「生成歌曲」自动换新种子; 取消勾选: 使用上方固定种子"))
                        _reg(random_seed_checkbox, lambda lang: gr.update(label=tr(lang, "随机种子变化"), info=tr(lang, "勾选: 每次点击「生成歌曲」自动换新种子; 取消勾选: 使用上方固定种子")))

                        cfg_input = gr.Slider(
                            label=_t("CFG 引导强度"),
                            minimum=0, maximum=20, step=0.1, value=0,
                            info=_t("0=Auto (off模式=1.01, 其他=1.0)"),
                        )
                        _reg(cfg_input, lambda lang: gr.update(label=tr(lang, "CFG 引导强度"), info=tr(lang, "0=Auto (off模式=1.01, 其他=1.0)")))

                        steps_input = gr.Slider(
                            label=_t("ODE 求解步数"),
                            minimum=1, maximum=64, step=1, value=8,
                            info=_t("8=快速, 16=标准, 32=高质量"),
                        )
                        _reg(steps_input, lambda lang: gr.update(label=tr(lang, "ODE 求解步数"), info=tr(lang, "8=快速, 16=标准, 32=高质量")))

                        out_format_input = gr.Dropdown(
                            label=_t("输出格式"),
                            choices=[
                                (_t("PCM 16-bit (标准)"), "pcm16"),
                                (_t("PCM 24-bit (高动态)"), "pcm24"),
                                (_t("Float 32-bit (最大动态)"), "float32"),
                            ],
                            value="pcm16",
                            info=_t("PCM16=标准质量, PCM24=更高动态范围, Float32=最大动态范围(文件更大)"),
                        )
                        _reg(out_format_input, lambda lang: gr.update(label=tr(lang, "输出格式"), choices=[(tr(lang, "PCM 16-bit (标准)"), "pcm16"), (tr(lang, "PCM 24-bit (高动态)"), "pcm24"), (tr(lang, "Float 32-bit (最大动态)"), "float32")], info=tr(lang, "PCM16=标准质量, PCM24=更高动态范围, Float32=最大动态范围(文件更大)")))

                        batch_count_input = gr.Slider(
                            label=_t("批量生成数量"),
                            minimum=1, maximum=10, step=1, value=1,
                            info=_t("一次生成多个变体 (每个变体使用独立随机种子)"),
                        )
                        _reg(batch_count_input, lambda lang: gr.update(label=tr(lang, "批量生成数量"), info=tr(lang, "一次生成多个变体 (每个变体使用独立随机种子)")))

                        postprocess_acc = gr.Accordion(_t("音频后处理"), open=False)
                        _reg(postprocess_acc, lambda lang: gr.update(label=tr(lang, "音频后处理")))
                        with postprocess_acc:
                            postopt_md = gr.Markdown(_t("#### 后处理选项"))
                            _reg(postopt_md, lambda lang: gr.update(value=tr(lang, "#### 后处理选项")))
                            with gr.Row():
                                normalize_checkbox = gr.Checkbox(label=_t("音量标准化"), value=True, info=_t("归一化到 -1dB"))
                                _reg(normalize_checkbox, lambda lang: gr.update(label=tr(lang, "音量标准化"), info=tr(lang, "归一化到 -1dB")))
                                fade_checkbox = gr.Checkbox(label=_t("淡入淡出"), value=True, info=_t("首尾各 0.5 秒"))
                                _reg(fade_checkbox, lambda lang: gr.update(label=tr(lang, "淡入淡出"), info=tr(lang, "首尾各 0.5 秒")))
                                trim_checkbox = gr.Checkbox(label=_t("裁剪静音"), value=False, info=_t("移除首尾静音 (< -40dB)"))
                                _reg(trim_checkbox, lambda lang: gr.update(label=tr(lang, "裁剪静音"), info=tr(lang, "移除首尾静音 (< -40dB)")))
                            with gr.Row():
                                metadata_checkbox = gr.Checkbox(label=_t("嵌入元数据"), value=True, info=_t("标题/风格/种子"))
                                _reg(metadata_checkbox, lambda lang: gr.update(label=tr(lang, "嵌入元数据"), info=tr(lang, "标题/风格/种子")))

                        advanced_acc = gr.Accordion(_t("高级采样参数"), open=True)
                        _reg(advanced_acc, lambda lang: gr.update(label=tr(lang, "高级采样参数")))
                        with advanced_acc:
                            abc_stage1_md = gr.Markdown(_t("#### ABC 乐谱采样 (Stage 1)"))
                            _reg(abc_stage1_md, lambda lang: gr.update(value=tr(lang, "#### ABC 乐谱采样 (Stage 1)")))
                            with gr.Row():
                                abc_temp_input = gr.Slider(label=_t("ABC 温度"), minimum=0, maximum=5, step=0.1, value=0.7)
                                abc_top_p_input = gr.Slider(label="ABC Top-P", minimum=0, maximum=1, step=0.01, value=0.9)
                                abc_top_k_input = gr.Slider(label="ABC Top-K", minimum=1, maximum=500, step=1, value=30)
                            with gr.Row():
                                abc_rep_input = gr.Slider(label=_t("ABC 重复惩罚"), minimum=0.001, maximum=3, step=0.001, value=1.005)
                                abc_pen_window_input = gr.Slider(label=_t("ABC 惩罚窗口"), minimum=1, maximum=100, step=1, value=100)
                            with gr.Row():
                                abc_min_tok_input = gr.Slider(label="ABC Min Tokens", minimum=0, maximum=8192, step=1, value=32)
                                abc_max_tok_input = gr.Slider(label="ABC Max Tokens", minimum=1, maximum=8192, step=1, value=4096)
                            _reg(abc_temp_input, lambda lang: gr.update(label=tr(lang, "ABC 温度")))
                            _reg(abc_rep_input, lambda lang: gr.update(label=tr(lang, "ABC 重复惩罚")))
                            _reg(abc_pen_window_input, lambda lang: gr.update(label=tr(lang, "ABC 惩罚窗口")))

                            sem_stage2_md = gr.Markdown(_t("#### 语义 Token 采样 (Stage 2)"))
                            _reg(sem_stage2_md, lambda lang: gr.update(value=tr(lang, "#### 语义 Token 采样 (Stage 2)")))
                            with gr.Row():
                                sem_temp_input = gr.Slider(label=_t("语义 温度"), minimum=0, maximum=5, step=0.1, value=1.0)
                                sem_top_p_input = gr.Slider(label=_t("语义 Top-P"), minimum=0, maximum=1, step=0.01, value=0.95)
                                sem_top_k_input = gr.Slider(label=_t("语义 Top-K"), minimum=1, maximum=500, step=1, value=100)
                            with gr.Row():
                                sem_rep_input = gr.Slider(label=_t("语义 重复惩罚"), minimum=0.001, maximum=3, step=0.01, value=1.2)
                                sem_pen_window_input = gr.Slider(label=_t("语义 惩罚窗口"), minimum=1, maximum=100, step=1, value=50)
                            with gr.Row():
                                sem_min_tok_input = gr.Slider(label=_t("语义 Min Tokens"), minimum=0, maximum=9000, step=1, value=200)
                                sem_max_tok_input = gr.Slider(label=_t("语义 Max Tokens"), minimum=1, maximum=9000, step=1, value=9000)
                            _reg(sem_temp_input, lambda lang: gr.update(label=tr(lang, "语义 温度")))
                            _reg(sem_top_p_input, lambda lang: gr.update(label=tr(lang, "语义 Top-P")))
                            _reg(sem_top_k_input, lambda lang: gr.update(label=tr(lang, "语义 Top-K")))
                            _reg(sem_rep_input, lambda lang: gr.update(label=tr(lang, "语义 重复惩罚")))
                            _reg(sem_pen_window_input, lambda lang: gr.update(label=tr(lang, "语义 惩罚窗口")))
                            _reg(sem_min_tok_input, lambda lang: gr.update(label=tr(lang, "语义 Min Tokens")))
                            _reg(sem_max_tok_input, lambda lang: gr.update(label=tr(lang, "语义 Max Tokens")))

                    with gr.Column(scale=1):
                        with gr.Row():
                            generate_btn = gr.Button(_t("🎵 生成歌曲"), variant="primary", size="lg")
                            _reg(generate_btn, lambda lang: gr.update(value=tr(lang, "🎵 生成歌曲")))
                            cancel_btn = gr.Button(_t("取消"), size="lg")
                            _reg(cancel_btn, lambda lang: gr.update(value=tr(lang, "取消")))
                        output_md = gr.Markdown(_t("### 输出"))
                        _reg(output_md, lambda lang: gr.update(value=tr(lang, "### 输出")))
                        audio_output = gr.Audio(label=_t("生成的歌曲"), type="filepath", elem_id="gen-audio")
                        _reg(audio_output, lambda lang: gr.update(label=tr(lang, "生成的歌曲")))
                        info_output = gr.Markdown()

                        with gr.Group(visible=False) as variant_group:
                            variant_selector = gr.Radio(label=_t("批量变体选择"), choices=[], interactive=True)
                            _reg(variant_selector, lambda lang: gr.update(label=tr(lang, "批量变体选择")))
                            with gr.Row():
                                variant_finalize_btn = gr.Button(_t("✅ 选定为最终版"), variant="primary", size="sm")
                                _reg(variant_finalize_btn, lambda lang: gr.update(value=tr(lang, "✅ 选定为最终版")))
                                variant_keep_btn = gr.Button(_t("保留全部变体"), size="sm")
                                _reg(variant_keep_btn, lambda lang: gr.update(value=tr(lang, "保留全部变体")))
                        variant_state = gr.State([])

                        abc_md = gr.Markdown(_t("### ABC 乐谱"))
                        _reg(abc_md, lambda lang: gr.update(value=tr(lang, "### ABC 乐谱")))
                        gen_abc_acc = gr.Accordion(_t("生成的乐谱 (可编辑)"), open=False)
                        _reg(gen_abc_acc, lambda lang: gr.update(label=tr(lang, "生成的乐谱 (可编辑)")))
                        with gen_abc_acc:
                            abc_output = gr.Textbox(
                                label=_t("ABC 乐谱文本"),
                                placeholder="X:1",
                                lines=10,
                                interactive=True,
                                elem_id="gen-abc-output",
                                info=_t("生成后可编辑乐谱，点击「重新合成」使用修改后的乐谱生成新音频"),
                            )
                            _reg(abc_output, lambda lang: gr.update(label=tr(lang, "ABC 乐谱文本"), info=tr(lang, "生成后可编辑乐谱，点击「重新合成」使用修改后的乐谱生成新音频")))
                        preview_md = gr.Markdown(_t("#### 乐谱预览"))
                        _reg(preview_md, lambda lang: gr.update(value=tr(lang, "#### 乐谱预览")))
                        # 乐谱预览占位容器：提示文案按语言翻译，切语言时更新
                        def _abc_preview_html(container_id, paper_id, audio_id, msg):
                            return (
                                f'<div id="{container_id}" style="padding: 20px; border-radius: 8px; min-height: 200px; '
                                f'border: 1px dashed rgba(255,255,255,0.15);">'
                                f'<div style="text-align:center;color:#666;margin-bottom:12px;">{msg}</div>'
                                f'<div id="{paper_id}"></div><div id="{audio_id}"></div></div>'
                            )

                        gen_abc_preview = gr.HTML(
                            value=_abc_preview_html("abc-preview-container", "abc-paper", "abc-audio", _t("生成歌曲后乐谱将在此处渲染")),
                        )
                        _reg(gen_abc_preview, lambda lang: gr.update(value=_abc_preview_html("abc-preview-container", "abc-paper", "abc-audio", tr(lang, "生成歌曲后乐谱将在此处渲染"))))
                        with gr.Row():
                            export_midi_btn = gr.Button(_t("导出 MIDI"), size="sm")
                            _reg(export_midi_btn, lambda lang: gr.update(value=tr(lang, "导出 MIDI")))
                            export_png_btn = gr.Button(_t("导出 PNG"), size="sm")
                            _reg(export_png_btn, lambda lang: gr.update(value=tr(lang, "导出 PNG")))
                        abc_file_output = gr.File(label=_t("下载乐谱"))
                        _reg(abc_file_output, lambda lang: gr.update(label=tr(lang, "下载乐谱")))
                        flac_file_output = gr.File(label=_t("下载 MP3"))
                        _reg(flac_file_output, lambda lang: gr.update(label=tr(lang, "下载 MP3")))
                        lyrics_sync_data = gr.HTML(value="", visible=False)
                        with gr.Row():
                            resynthesize_btn = gr.Button(_t("重新合成"), variant="secondary")
                            _reg(resynthesize_btn, lambda lang: gr.update(value=tr(lang, "重新合成")))

            with gr.Tab(_t("音频转谱")) as tab_transcribe:
                _reg(tab_transcribe, lambda lang: gr.update(label=tr(lang, "音频转谱")))
                transcribe_md = gr.Markdown(_t("### 音频转乐谱"))
                _reg(transcribe_md, lambda lang: gr.update(value=tr(lang, "### 音频转乐谱")))
                transcribe_intro_md = gr.Markdown(_t("上传音频文件，使用 SheetSage2 模型自动转写为 ABC 乐谱"))
                _reg(transcribe_intro_md, lambda lang: gr.update(value=tr(lang, "上传音频文件，使用 SheetSage2 模型自动转写为 ABC 乐谱")))
                
                with gr.Row():
                    with gr.Column():
                        transcribe_audio_input = gr.Audio(label=_t("上传音频 (支持 WAV/MP3/FLAC/OGG/M4A 等)"), type="filepath", elem_id="transcribe-audio-input")
                        _reg(transcribe_audio_input, lambda lang: gr.update(label=tr(lang, "上传音频 (支持 WAV/MP3/FLAC/OGG/M4A 等)")))
                        with gr.Row():
                            transcribe_btn = gr.Button(_t("开始转谱"), variant="primary")
                            _reg(transcribe_btn, lambda lang: gr.update(value=tr(lang, "开始转谱")))
                            transcribe_send_btn = gr.Button(_t("→ 发送到生成页"), variant="secondary")
                            _reg(transcribe_send_btn, lambda lang: gr.update(value=tr(lang, "→ 发送到生成页")))
                        transcribe_info = gr.Markdown()
                    
                    with gr.Column():
                        transcribe_abc_output = gr.Textbox(
                            label=_t("ABC 乐谱 (可编辑)"),
                            placeholder=_t("转谱完成后乐谱将显示在这里..."),
                            lines=10,
                        )
                        _reg(transcribe_abc_output, lambda lang: gr.update(label=tr(lang, "ABC 乐谱 (可编辑)"), placeholder=tr(lang, "转谱完成后乐谱将显示在这里...")))
                        transcribe_abc_preview = gr.HTML(
                            label=_t("乐谱预览"),
                            value=f'<div id="transcribe-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed rgba(255,255,255,0.15);"><div style="text-align:center;color:#666;">{_t("转谱后乐谱预览将在此处显示")}</div><div id="transcribe-abc-paper"></div><div id="transcribe-abc-audio"></div></div>',
                        )
                        _reg(transcribe_abc_preview, lambda lang: gr.update(label=tr(lang, "乐谱预览"), value=f'<div id="transcribe-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed rgba(255,255,255,0.15);"><div style="text-align:center;color:#666;">{tr(lang, "转谱后乐谱预览将在此处显示")}</div><div id="transcribe-abc-paper"></div><div id="transcribe-abc-audio"></div></div>'))
                        
                        with gr.Row():
                            transcribe_abc_download = gr.File(label=_t("下载 ABC"))
                            _reg(transcribe_abc_download, lambda lang: gr.update(label=tr(lang, "下载 ABC")))
                            transcribe_midi_download = gr.File(label=_t("下载 MIDI"))
                            _reg(transcribe_midi_download, lambda lang: gr.update(label=tr(lang, "下载 MIDI")))

                transcribe_task_id = gr.State(value="")
                transcribe_abc_bridge = gr.Textbox(elem_id="abc-bridge", label="")

            with gr.Tab(_t("历史")) as tab_history:
                _reg(tab_history, lambda lang: gr.update(label=tr(lang, "历史")))
                history_md = gr.Markdown(_t("### 生成历史"))
                _reg(history_md, lambda lang: gr.update(value=tr(lang, "### 生成历史")))
                history_state = gr.State(value=[])
                history_page = gr.State(value=0)
                history_row_trigger = gr.Number(visible=True, value=-1, elem_id="history-row-trigger", label="")
                history_df = gr.Dataframe(
                    # 列头采用中英双语（Gradio 静态表格的 headers 不支持运行时切换）
                    headers=["时间 Time", "风格 Style", "模式 Mode", "音频时长 Duration", "生成耗时 Elapsed", "Task ID"],
                    datatype=["str", "str", "str", "str", "str", "str"],
                    col_count=6,
                    max_height=500,
                    interactive=False,
                    value=refresh_history()[0],
                    elem_id="history-table",
                )
                with gr.Row():
                    history_prev_btn = gr.Button(_t("上一页"), size="sm")
                    _reg(history_prev_btn, lambda lang: gr.update(value=tr(lang, "上一页")))
                    history_page_info = gr.Markdown(value=refresh_history()[1], elem_id="history-page-info")
                    history_next_btn = gr.Button(_t("下一页"), size="sm")
                    _reg(history_next_btn, lambda lang: gr.update(value=tr(lang, "下一页")))
                history_audio = gr.Audio(label=_t("试听"), type="filepath", elem_id="history-audio")
                _reg(history_audio, lambda lang: gr.update(label=tr(lang, "试听")))
                history_info = gr.Markdown()
                with gr.Row():
                    with gr.Column(scale=1):
                        history_lyrics = gr.Textbox(label=_t("歌词"), lines=10, interactive=False)
                        _reg(history_lyrics, lambda lang: gr.update(label=tr(lang, "歌词")))
                        history_lyric_sync = gr.HTML(
                            label=_t("歌词同步"),
                            value='<div id="history-lyric-sync" style="padding: 12px; min-height: 100px; border-radius: 8px;"></div>',
                        )
                        _reg(history_lyric_sync, lambda lang: gr.update(label=tr(lang, "歌词同步")))
                    with gr.Column(scale=1):
                        history_abc_preview = gr.HTML(
                            label=_t("乐谱预览"),
                            value=f'<div id="history-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed rgba(255,255,255,0.15);"><div style="text-align:center;color:#666;margin-bottom:12px;">{_t("点击历史记录后乐谱将在此处渲染")}</div><div id="history-abc-paper"></div><div id="history-abc-audio"></div></div>',
                        )
                        _reg(history_abc_preview, lambda lang: gr.update(label=tr(lang, "乐谱预览"), value=f'<div id="history-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px; border: 1px dashed rgba(255,255,255,0.15);"><div style="text-align:center;color:#666;margin-bottom:12px;">{tr(lang, "点击历史记录后乐谱将在此处渲染")}</div><div id="history-abc-paper"></div><div id="history-abc-audio"></div></div>'))
                        history_abc = gr.Textbox(label=_t("ABC 乐谱文本"), lines=6, interactive=False, elem_id="history-abc")
                        _reg(history_abc, lambda lang: gr.update(label=tr(lang, "ABC 乐谱文本")))
                history_lyrics_data = gr.HTML(value="", visible=False)
                history_duration_data = gr.HTML(value="", visible=False)
                history_style = gr.Markdown(label=_t("风格描述"))
                _reg(history_style, lambda lang: gr.update(label=tr(lang, "风格描述")))
                with gr.Row():
                    history_refresh_btn = gr.Button(_t("刷新"))
                    _reg(history_refresh_btn, lambda lang: gr.update(value=tr(lang, "刷新")))
                    history_delete_btn = gr.Button(_t("删除选中"))
                    _reg(history_delete_btn, lambda lang: gr.update(value=tr(lang, "删除选中")))
                    history_clear_btn = gr.Button(_t("清空历史"))
                    _reg(history_clear_btn, lambda lang: gr.update(value=tr(lang, "清空历史")))

                history_df.select(fn=on_history_select, inputs=[history_state, history_page], outputs=[history_state, history_audio, history_info, history_style, history_lyrics, history_abc, history_abc_preview, history_lyrics_data, history_duration_data])
                history_row_trigger.change(fn=on_history_row_click, inputs=[history_row_trigger, history_state, history_page], outputs=[history_state, history_audio, history_info, history_style, history_lyrics, history_abc, history_abc_preview, history_lyrics_data, history_duration_data])
                history_refresh_btn.click(fn=refresh_history_full, outputs=[history_df, history_page_info, history_page])
                history_delete_btn.click(fn=on_history_delete, inputs=history_state, outputs=[history_df, history_page_info, history_info, history_state, history_page])
                history_clear_btn.click(fn=on_history_clear, outputs=[history_df, history_page_info, history_info, history_state, history_page])
                history_prev_btn.click(fn=on_history_prev_page, inputs=history_page, outputs=[history_df, history_page_info, history_page])
                history_next_btn.click(fn=on_history_next_page, inputs=history_page, outputs=[history_df, history_page_info, history_page])
                demo.load(fn=refresh_history_full, outputs=[history_df, history_page_info, history_page])

            with gr.Tab(_t("设置")) as tab_settings:
                _reg(tab_settings, lambda lang: gr.update(label=tr(lang, "设置")))
                sysstatus_md = gr.Markdown(_t("### 系统状态"))
                _reg(sysstatus_md, lambda lang: gr.update(value=tr(lang, "### 系统状态")))
                model_status = gr.Markdown(value=on_check_models())
                # 切语言时重新渲染模型状态（apply_lang 先更新 _CUR_LANG 再执行 updater）
                _reg(model_status, lambda lang: gr.update(value=on_check_models()))
                check_models_btn = gr.Button(_t("检查模型"))
                _reg(check_models_btn, lambda lang: gr.update(value=tr(lang, "检查模型")))
                check_models_btn.click(fn=on_check_models, outputs=model_status)

                presets_md = gr.Markdown(_t("### 参数预设"))
                _reg(presets_md, lambda lang: gr.update(value=tr(lang, "### 参数预设")))
                preset_dropdown = gr.Dropdown(
                    label=_t("加载预设"),
                    choices=_preset_display_names(_CUR_LANG),
                    value=None,
                )
                _reg(preset_dropdown, lambda lang: gr.update(label=tr(lang, "加载预设"), choices=_preset_display_names(lang)))
                preset_name_input = gr.Textbox(label=_t("保存预设名称"), placeholder=_t("我的预设"))
                _reg(preset_name_input, lambda lang: gr.update(label=tr(lang, "保存预设名称"), placeholder=tr(lang, "我的预设")))
                with gr.Row():
                    preset_load_btn = gr.Button(_t("加载"))
                    _reg(preset_load_btn, lambda lang: gr.update(value=tr(lang, "加载")))
                    preset_save_btn = gr.Button(_t("保存当前参数"))
                    _reg(preset_save_btn, lambda lang: gr.update(value=tr(lang, "保存当前参数")))
                preset_info = gr.Markdown()

        lyrics_input.change(fn=on_lyrics_change, inputs=lyrics_input, outputs=structure_analysis)

        random_seed_btn.click(fn=on_random_seed, outputs=seed_input)

        generate_btn.click(
            fn=on_generate,
            inputs=[
                style_input, lyrics_input, cot_input, seed_input, random_seed_checkbox, cfg_input, steps_input, out_format_input, batch_count_input,
                normalize_checkbox, fade_checkbox, trim_checkbox, metadata_checkbox,
                abc_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
            ],
            outputs=[audio_output, info_output, abc_output, abc_file_output, flac_file_output, lyrics_sync_data, history_df, history_page_info, history_page, variant_group, variant_selector, variant_state, seed_input],
        )

        variant_selector.change(
            fn=on_variant_select,
            inputs=[variant_selector, variant_state],
            outputs=[audio_output, abc_output, abc_file_output, flac_file_output],
        )
        variant_finalize_btn.click(
            fn=on_variant_finalize,
            inputs=[variant_selector, variant_state],
            outputs=[info_output, history_df, history_page_info, history_page, variant_group, variant_selector, variant_state],
        )
        variant_keep_btn.click(
            fn=on_variant_keep_all,
            inputs=[variant_selector, variant_state],
            outputs=[info_output, history_df, history_page_info, history_page, variant_group, variant_selector, variant_state],
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

        # 语言即时切换：lang_select.change -> apply_lang -> 更新 lang_state + 所有 translatable 组件
        lang_select.change(
            fn=apply_lang,
            inputs=lang_select,
            outputs=[lang_state] + translatables,
        )
        # 历史页翻页信息是动态文案（含当前页码），无法静态注册 updater；
        # 追加第二个 change 绑定，按当前页重新生成。本绑定注册在 apply_lang 之后，
        # 执行时 _CUR_LANG 已更新为新语言，故直接复用 _get_history_page。
        lang_select.change(
            fn=lambda page: _get_history_page(page)[1],
            inputs=history_page,
            outputs=history_page_info,
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
<style>
.last-btn-row { justify-content: flex-end; margin-top: -10px; }
.last-btn-row > * { flex-grow: 0 !important; }
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/abcjs/6.3.0/abcjs-audio.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/abcjs/6.3.0/abcjs-basic-min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Sortable/1.15.0/Sortable.min.js"></script>
<script src="/static/js/vendor/wavesurfer.min.js?v=1"></script>
<script src="/static/js/app.js?v=10"></script>
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
        demo.app.routes.insert(0, Route(
            "/static/js/vendor/wavesurfer.min.js",
            lambda request: FileResponse(WEBUI_ROOT / "static" / "js" / "vendor" / "wavesurfer.min.js", media_type="application/javascript"),
            methods=["GET"],
        ))
    threading.Thread(target=_register_custom_routes, daemon=True).start()

    demo.launch(
        server_name="127.0.0.1",
        server_port=9898,
        share=False,
        show_error=True,
        # Gradio 6.0 起 css/js 从 Blocks 构造器移至 launch()（5.x 两者兼容，按新规范统一放此处）
        css=_TITLE_ROW_CSS,
        js=_LOCALE_SYNC_JS,
    )
