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
from history import HistoryManager, HistoryRecord, sanitize_project, _FILENAME_TS_RE
from postprocess import postprocess_audio
from queue_manager import queue_manager, TaskType, TaskStatus, TaskCancelledError
from i18n import tr, normalize_lang
from voice_client import VoiceClient, check_voice_models
from voice_ui_handlers import VoiceHandlers, detect_voice

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
    db_file=WEBUI_ROOT / "history.db",
    outputs_root=WEBUI_ROOT / "outputs",
)

# 音色工坊：独立 worker 客户端 + 业务处理器（懒加载 worker 进程）
voice_client = VoiceClient(PROJECT_ROOT)
voice_handlers = VoiceHandlers(
    project_root=PROJECT_ROOT,
    webui_root=WEBUI_ROOT,
    history_mgr=history_mgr,
    voice_client=voice_client,
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
    project, style, lyrics, cot, seed, random_seed, cfg_scale, num_inference_steps, out_format, batch_count,
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
        project=project or "",
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
    project, style, lyrics, cot, seeds, cfg_scale, num_inference_steps, out_format, batch_count,
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

    # 项目名清洗 + 产物命名规范（文件管理重构）：
    #   目录 = outputs/song_<时间戳>/；文件 = <项目名>_<时间戳>[_varN].<ext>（项目名空则时间戳开头）
    project = sanitize_project(project or "")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_task_id = f"song_{timestamp}"           # 目录名 / 历史 task_id（不含项目名，改名不动目录）
    file_stem = f"{project}_{timestamp}" if project else timestamp  # 产物文件名主干
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
        # 文件名与 task_id 解耦：task_id 固定 song_<ts>[_varN]，文件主干 = <项目名>_<ts>[_varN]
        output_name = f"{file_stem}_var{i+1}" if batch_count > 1 else file_stem
        variant_dir = output_dir
        variant_dir.mkdir(parents=True, exist_ok=True)

        params.seed = current_seed
        _task.push_progress(0, f"{tr(lang, '生成变体')} {i+1}/{batch_count} (seed={current_seed})...")

        result = backend.generate(
            params=params,
            output_dir=variant_dir,
            on_progress=on_progress,
            cancel_event=_task.cancel_event,
            output_name=output_name,
            lang=lang,
        )
        results.append((task_id, output_name, variant_dir, result, current_seed))

    successful = [(tid, fname, d, r, s) for tid, fname, d, r, s in results if r.success]
    if not successful:
        if _task.cancel_event.is_set():
            raise TaskCancelledError(tr(lang, "任务已取消"))
        failed_msg = results[0][3].error_message if results else tr(lang, "未知错误")
        raise ValueError(f"{tr(lang, '生成失败')}: {failed_msg}")

    # 生成成功：用产出的乐谱（最后一个成功变体）更新「使用上一次」记录，
    # 否则记录停留在任务开始时外部输入框的内容（正常生成时为空）
    last_abc = successful[-1][3].abc_score or (abc_text or "")
    _update_last_abc(last_abc)

    format_label = FORMAT_LABELS.get(params.out_format.value, "PCM 16-bit")

    if any([normalize, fade, trim, metadata]):
        for idx, (task_id, fname, variant_dir, result, variant_seed) in enumerate(successful):
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

    total_time = sum(r.generation_time_seconds or 0 for _, _, _, r, _ in successful)
    avg_duration = sum(r.audio_duration_seconds or 0 for _, _, _, r, _ in successful) / len(successful)

    if batch_count > 1:
        duration_info = f"{tr(lang, '批量生成')} **{len(successful)}/{batch_count}** {tr(lang, '个变体')} | {tr(lang, '总耗时')} **{total_time:.1f}s** | {tr(lang, '平均音频时长')} **{avg_duration:.1f}s** | {format_label}"
    else:
        r = successful[0][3]
        duration_info = f"{tr(lang, '生成耗时')} **{r.generation_time_seconds:.1f}s** | {tr(lang, '音频时长')} **{r.audio_duration_seconds:.1f}s** | {format_label}"

    _lyrics_json = html.escape(json.dumps(params.lyrics, ensure_ascii=False), quote=True)
    lyrics_data_html = f'<div class="gen-lyrics-data" style="display:none" data-lyrics=\'{_lyrics_json}\' data-duration="{avg_duration}"></div>'

    for task_id, fname, variant_dir, result, variant_seed in successful:
        abc_path = ""
        if result.abc_score:
            abc_file = variant_dir / f"{fname}.abc"
            abc_file.write_text(result.abc_score, encoding="utf-8")
            abc_path = str(abc_file.relative_to(WEBUI_ROOT))

        lyrics_file = variant_dir / f"{fname}.txt"
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
            project=project,
        )
        history_mgr.append(record)
    history_mgr.auto_prune()

    first_result = successful[0][3]
    first_fname = successful[0][1]
    first_dir = successful[0][2]
    abc_display = first_result.abc_score or ""
    abc_download = str(first_dir / f"{first_fname}.abc") if first_result.abc_score else None

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
        for idx, (task_id, fname, variant_dir, result, variant_seed) in enumerate(successful, start=1):
            variants_payload.append({
                "label": f"{tr(lang, '变体')}{idx} (seed={variant_seed}, {result.audio_duration_seconds or 0:.1f}s)",
                "task_id": task_id,
                "audio_path": str(result.audio_path),
                "abc_text": result.abc_score or "",
                "abc_file": str(variant_dir / f"{fname}.abc") if result.abc_score else None,
                "mp3_file": result.mp3_path,
            })

        audio_paths = [str(r.audio_path) for _, _, _, r, _ in successful]
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
    task_id = f"song_{timestamp}"  # 重新合成也走 song_<ts> 项目目录（无项目名，文件以时间戳开头）
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
        output_name=timestamp,
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

        lyrics_file = output_dir / f"{timestamp}.txt"
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
    # 上传源统一留存（文件管理重构）：uploads/<源名>_<时间戳>_transcribe.<ext>，
    # Gradio 临时文件会被清理，不留存则历史无法追溯源音频
    voice_handlers.persist_upload(str(audio_path), "transcribe")
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


# =======================================================================
# 音色工坊 Tab：业务函数（分离 / 翻唱 的入队与进度转发）
# 复用 voice_handlers 的 run_in_queue + worker 回调；本层只做参数校验、
# 进度转发和结果展平，不含模型推理逻辑（见 Docs/voice-tools-plan.md）。
# =======================================================================

def _voice_ref_choices(lang="zh"):
    """生成音色库下拉选项：显示文件名，值为绝对路径。"""
    paths = voice_handlers.list_refs()
    # 组内键不得重复（Gradio choices 需 (label, value) 唯一即可）
    return [(Path(p).name or p, p) for p in paths]


def _voice_ref_names():
    """仅返回文件名列表（用于「从音色库选择」的 label）。"""
    return [Path(p).name or p for p in voice_handlers.list_refs()]


# 分离轨道类型 → 中文标签（与 worker 分离产物键一致）
_STEM_TYPE_LABELS = {"vocals": "人声", "accompaniment": "伴奏",
                     "drums": "鼓", "bass": "贝斯", "other": "其他"}


def _voice_stem_choices(lang="zh", exclude_vocals=True):
    """素材库下拉：列出轨道素材（label 标注类型）；默认排除人声轨（作伴奏用）。"""
    out = []
    for name, stype, p in voice_handlers.list_stems():
        if exclude_vocals and stype == "vocals":
            continue
        out.append((f"{name} · {tr(lang, _STEM_TYPE_LABELS.get(stype, stype))}", p))
    return out


def _voice_source_history_choices(lang="zh", limit=50):
    """生成「从历史记录选择」下拉：列出最近 generation/cover 记录（有音频者）。

    类型词经 _QUEUE_TYPE_LABELS + tr 翻译，避免中文界面出现英文类型词。
    """
    choices = []
    for rec in history_mgr.list_all():
        if rec.audio_path and Path(rec.audio_path).exists() \
                and rec.record_type in ("generation", "cover"):
            type_label = tr(lang, _QUEUE_TYPE_LABELS.get(rec.record_type, rec.record_type))
            choices.append((f"{type_label} · {Path(rec.audio_path).name}", rec.audio_path))
        if len(choices) >= limit:
            break
    return choices


def _voice_cover_source_choices(lang="zh", limit=50):
    """翻唱源下拉：仅 generation/cover 原唱历史记录（排除 separation 分离任务）。"""
    choices = []
    for rec in history_mgr.list_all():
        if rec.record_type in ("generation", "cover") and rec.audio_path \
                and Path(rec.audio_path).exists():
            type_label = tr(lang, _QUEUE_TYPE_LABELS.get(rec.record_type, rec.record_type))
            choices.append((f"{type_label} · {Path(rec.audio_path).name}", rec.audio_path))
        if len(choices) >= limit:
            break
    return choices


def _voice_dry_sep_choices(lang="zh", limit=50):
    """干声来源1：分离历史记录的人声干声（separation 记录的 audio_path 即 vocals 轨）。"""
    choices = []
    for rec in history_mgr.list_all():
        if rec.audio_path and Path(rec.audio_path).exists() \
                and rec.record_type == "separation":
            choices.append((f"{Path(rec.audio_path).name}", rec.audio_path))
        if len(choices) >= limit:
            break
    return choices


def _voice_dry_upload_choices(lang="zh", limit=50):
    """干声来源2：上传并验证为干音的留存。"""
    return [(f"上传 · {Path(p).name}", p) for p in voice_handlers.list_dry_uploads()[:limit]]


def _voice_ref_pair_acc(ref_path: str) -> str:
    """参考干声若来自分离记录，返回同一次分离的伴奏轨路径（供智能挑段算人声主导度）。

    仅「从分离人声选择」这条来源能拿到配对伴奏（分离记录自带 vocals+accompaniment
    两轨）；上传干声/音色库没有配对轨，返回空串，worker 侧会回退"能量最高段"。
    查询异常一律返回空串，不阻断翻唱流程。
    """
    try:
        target = Path(ref_path).resolve()
        for rec in history_mgr.list_all():
            if rec.record_type != "separation" or not rec.audio_path:
                continue
            if Path(rec.audio_path).resolve() != target:
                continue
            stems = {s.get("type"): s.get("path", "")
                     for s in (getattr(rec, "stems", None) or []) if isinstance(s, dict)}
            acc = stems.get("accompaniment", "")
            return acc if acc and Path(acc).exists() else ""
    except Exception:
        pass
    return ""


def _voice_task_history_choices(record_type, lang="zh", limit=50):
    """按类型列出任务历史（separation/cover），value=task_id，用于 Tab 内选择回放。

    显示名 = "项目名 · 产物文件夹名"（项目名含上传源文件名；无项目名时仅文件夹名）。
    """
    choices = []
    for rec in history_mgr.list_all():
        if rec.record_type == record_type and rec.audio_path \
                and Path(rec.audio_path).exists():
            folder = Path(rec.output_dir).name if rec.output_dir else Path(rec.audio_path).stem
            # 有项目名时前缀显示（含上传源文件名），避免多个任务仅时间戳可辨
            label = f"{rec.project} · {folder}" if getattr(rec, "project", "") else folder
            choices.append((label, rec.task_id))
        if len(choices) >= limit:
            break
    return choices


def _dd_update(choices):
    """构建 Dropdown 的 gr.update，同时传递 value 为首项（避免 Gradio 6 重置 value 为 None）。

    Gradio 6 中 gr.update(choices=[...]) 不传 value 会把当前 value 重置，
    导致 Dropdown 显示 placeholder 但前端残留旧 label，形成"假选中"状态。
    本函数自动取 choices 首项的 value 作为新默认值（choices 为空则 value=None）。
    """
    first_val = choices[0][1] if choices else None  # (label, value) 元组取 value 部分
    return gr.update(choices=choices, value=first_val)


# 音色工坊播放器组槽位数：分离最多 4 轨（+降噪变体）、翻唱 4 产物，取 6 留余量
VOICE_PLAYER_COUNT = 6


def _voice_stem_items(stems):
    """把记录/产物的 stems 列表转为 [(label, path)]，仅收录磁盘存在的轨。

    降噪轨（文件名含 _denoised）在标签上追加"已降噪"标识，便于区分源轨。
    """
    items = []
    for s in stems or []:
        p = s.get("path", "") if isinstance(s, dict) else ""
        if not p or not Path(p).exists():
            continue
        # 轨道标签走 tr 国际化（历史记录的 stems 存中文原文，显示时按当前语言翻译）
        label = tr(_CUR_LANG, s.get("label") or Path(p).stem)
        if "_denoised" in Path(p).stem:
            label = f"{label} · {tr(_CUR_LANG, '已降噪')}"
        items.append((label, p))
    return items


def _fill_voice_players(items):
    """把 [(label, path)] 填充为播放器组更新：前 n 个显示（带轨道名，可下载），其余隐藏。

    与其他 Tab 的播放器为同一 gr.Audio 组件（个性化定制由 app.js PlayerZoom 按
    elem_id 前缀统一接管）；按产物数量逐个显隐，空槽位不占界面。
    """
    updates = []
    for i in range(VOICE_PLAYER_COUNT):
        if i < len(items):
            label, path = items[i]
            updates.append(gr.update(value=path, label=label, visible=True))
        else:
            updates.append(gr.update(value=None, visible=False))
    return updates


def on_voice_task_history_pick(task_id):
    """任务历史选择（按文件夹）：整组播放器回放该任务全部轨道（每轨可下载）。"""
    entry = history_mgr.get(task_id) if task_id else None
    items = _voice_stem_items(getattr(entry, "stems", None) if entry else None)
    # 无 stems 的旧记录回退主轨
    if not items and entry and entry.audio_path and Path(entry.audio_path).exists():
        items.append((Path(entry.audio_path).name, entry.audio_path))
    return _fill_voice_players(items)


def on_voice_task_rename(task_id, new_name, record_type):
    """分离/翻唱任务历史：改项目名（重命名项目目录文件，保留时间戳）。

    返回 (历史下拉刷新, State 保持当前 task_id, 播放器组保持, 新项目名输入清空)。
    """
    if not task_id:
        raise gr.Error(tr(_CUR_LANG, "请先选择任务"))
    entry = history_mgr.get(task_id)
    if not entry or not getattr(entry, "output_dir", ""):
        raise gr.Error(tr(_CUR_LANG, "该记录没有产物目录"))
    n = history_mgr.rename_project(entry.output_dir, new_name or "")
    gr.Info(f"{tr(_CUR_LANG, '已重命名')} {n} {tr(_CUR_LANG, '个文件')}")
    # 播放器里的旧路径已失效：按记录 stems 重新填充
    items = _voice_stem_items(getattr(entry, "stems", None))
    return (gr.update(choices=_voice_task_history_choices(record_type)),
            task_id,  # State 组件：改名后 task_id 不变
            *_fill_voice_players(items), "")


def on_voice_task_delete(task_id, record_type):
    """分离/翻唱任务历史：删除项目（整目录移入回收站，移除该目录全部记录）。

    返回 (历史下拉刷新, State 更新为剩余首条记录的 task_id 或 None, 播放器组清空)。
    """
    if not task_id:
        raise gr.Error(tr(_CUR_LANG, "请先选择任务"))
    entry = history_mgr.get(task_id)
    if not entry or not getattr(entry, "output_dir", ""):
        raise gr.Error(tr(_CUR_LANG, "该记录没有产物目录"))
    n = history_mgr.delete_project(entry.output_dir)
    if n <= 0:
        raise gr.Error(tr(_CUR_LANG, "删除失败"))
    gr.Info(f"{tr(_CUR_LANG, '已删除项目')} · {n} {tr(_CUR_LANG, '条记录')}")
    # 删除后 State 应更新为剩余首条记录的 task_id（如果还有的话）
    remaining = _voice_task_history_choices(record_type)
    new_state_value = remaining[0][1] if remaining else None
    return (gr.update(choices=remaining, value=None),
            new_state_value,
            *_fill_voice_players([]))


def on_voice_save_ref(src_upload, name_input):
    """把上传的参考干声存入音色库，返回(成功提示, 音色库新下拉选项)。"""
    if not src_upload:
        raise gr.Error(tr(_CUR_LANG, "请先上传音频文件"))
    name = (name_input or "").strip() or Path(src_upload).stem
    voice_handlers.save_ref(src_upload, name)
    return tr(_CUR_LANG, "已保存到音色库"), _voice_ref_choices(_CUR_LANG)


def on_voice_ref_upload_check(path):
    """上传参考干声后的轻量人声检测（方案B）：仅提示，不拦截流程。
    检测通过的人声干音自动留存到 dry_uploads，并入「干声历史」第二来源。"""
    if not path:
        return "", gr.update(choices=_voice_dry_upload_choices(_CUR_LANG))
    # 格式校验：仅支持 torchaudio/Seed-VC 可解码的常见格式
    if Path(path).suffix.lower() not in (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".wma"):
        return tr(_CUR_LANG, "不支持的文件格式，请上传 WAV/MP3/FLAC/M4A/OGG"), \
            gr.update(choices=_voice_dry_upload_choices(_CUR_LANG))
    r = detect_voice(path, project_root=PROJECT_ROOT)
    if r is None:
        return tr(_CUR_LANG, "无法检测"), gr.update(choices=_voice_dry_upload_choices(_CUR_LANG))
    ok, score = r
    if ok:
        voice_handlers.save_dry_upload(path)
        msg = f"{tr(_CUR_LANG, '人声检测: 通过')} (p={score:.2f})"
    else:
        msg = f"{tr(_CUR_LANG, '人声检测: 疑似非人声，建议上传清唱干声')} (p={score:.2f})"
    return msg, gr.update(choices=_voice_dry_upload_choices(_CUR_LANG))


def on_voice_src_mode(src_mode):
    """源音频两入口显隐：history 下拉 或 上传。"""
    return gr.update(visible=(src_mode == "history")), gr.update(visible=(src_mode == "upload"))


def on_voice_ref_mode(ref_mode):
    """参考音色三入口显隐：音色库 / 干声历史 / 上传(含命名保存)。"""
    return (gr.update(visible=(ref_mode == "library")),
            gr.update(visible=(ref_mode == "dry")),
            gr.update(visible=(ref_mode == "upload")))


def on_voice_dry_src_mode(dry_src):
    """干声来源切换：分离人声 / 上传干声，显示对应下拉。"""
    return gr.update(visible=(dry_src == "upload")), gr.update(visible=(dry_src == "sep"))


def _resolve_voice_source(history_val, upload_val):
    """从「历史记录 / 上传」两入口取实际音频路径，返回 (路径, 是否上传源)。

    上传源标记供 worker 拷贝源副本入产物文件夹（Gradio 临时文件会被清理，
    不拷贝则历史记录无法追溯源音频）。两者均无效则抛错。
    """
    if upload_val and Path(upload_val).exists():
        return str(upload_val), True
    if history_val and Path(history_val).exists():
        return str(history_val), False
    raise gr.Error(tr(_CUR_LANG, "请先选择源音频"))


def _project_from_source(source: str) -> str:
    """解析源音频的项目名（文件管理重构）：历史记录 project 字段优先。

    记录无项目名则返回空（产物不带项目名前缀）；非历史源（上传）按文件名结构
    <项目名>_<时间戳>[_类别] 提取项目名段，无该结构则用整个文件名主干。
    """
    try:
        src = Path(source).resolve()
        for rec in history_mgr.list_all():
            if rec.audio_path and Path(rec.audio_path).resolve() == src:
                return getattr(rec, "project", "") or ""
    except OSError:
        pass
    m = _FILENAME_TS_RE.match(Path(source).stem)
    if m:
        return m.group("proj") or ""
    return Path(source).stem


def _voice_ref_name(ref_path: str) -> str:
    """参考音色名（翻唱项目名的组成部分）。

    音色库条目（名_4位短id.wav）去掉短 id 段；其余（uploads 留存/临时上传/
    分离人声轨）按 <项目名>_<时间戳>[_类别] 结构剥时间戳段，无结构则取主干。
    """
    p = Path(ref_path)
    stem = p.stem
    try:
        if p.parent.resolve() == voice_handlers.refs_dir().resolve():
            base, sep, tail = stem.rpartition("_")
            if sep and base and len(tail) == 4 and tail.isascii() and tail.isalnum():
                return base
            return stem
    except OSError:
        pass
    m = _FILENAME_TS_RE.match(stem)
    if m and m.group("proj"):
        return m.group("proj")
    return stem


def _voice_running_outputs(text):
    """任务运行中的中间态输出：播放器组+历史下拉保持现状 + 进度文案 + 按钮保持禁用。"""
    return (*[gr.update()] * VOICE_PLAYER_COUNT, text,
            gr.update(interactive=False), gr.update())


def on_voice_separate(source_history, source_upload, sep_mode="vocals",
                      denoise=False):
    """音轨分离（生成器回调）：入队 Demucs，实时显示排队/执行进度。

    提交即禁用按钮（防运行期间重复提交）并清空播放器组；info 区实时显示
    排队位置/执行秒表/阶段进度（分离中/降噪中）；完成恢复按钮、按产物数量
    填充播放器组并刷新历史下拉；失败/取消也恢复按钮。上传源自动拷贝副本入
    产物文件夹（Gradio 临时文件不持久）。
    """
    source, from_upload = _resolve_voice_source(source_history, source_upload)
    # 分离项目名（文件管理重构）：自动取源的项目名（历史记录 project / 上传文件名）
    project = _project_from_source(source)
    vote = "2" if sep_mode == "vocals" else "4"
    lang = _CUR_LANG
    # 提交前先反馈：清空播放器组 + 禁用按钮
    yield (*_fill_voice_players([]), tr(lang, "排队中..."),
           gr.update(interactive=False), gr.update())
    gen = voice_handlers.run_in_queue_stream(
        TaskType.SEPARATION, voice_handlers.separate_worker,
        lang, tr, "音轨分离",
        on_submit=lambda tid: _register_task("separation", tid),
        on_finish=lambda tid: _unregister_task("separation", tid),
        source=source, mode=vote, root_task_id="", denoise=bool(denoise),
        from_upload=from_upload, project=project)
    try:
        result = None
        while True:  # 消费状态流：每条文案 → 播放器保持 + 进度文案 + 按钮保持禁用
            try:
                text = next(gen)
            except StopIteration as stop:
                result = stop.value
                break
            yield _voice_running_outputs(text)
    except TaskCancelledError:
        # 用户主动取消：恢复按钮 + info 显示"任务已取消"，正常收尾不弹错误窗
        # （必须 return：否则会落进下方成功路径，result=None 导致 .get 崩溃）
        yield (*[gr.update()] * VOICE_PLAYER_COUNT, tr(lang, "任务已取消"),
               gr.update(interactive=True), gr.update())
        return
    except Exception:
        # 失败必须恢复按钮；info 区给出失败提示后向上抛（Gradio 弹错误）
        yield (*[gr.update()] * VOICE_PLAYER_COUNT, tr(lang, "任务失败"),
               gr.update(interactive=True), gr.update())
        raise
    # 按产物数量填充播放器组（每轨一个播放器，label 为轨道名）+ 刷新历史下拉
    items = _voice_stem_items(result.get("stems"))
    note = tr(lang, "分离完成") + " · " + tr(lang, "写入历史")
    yield (*_fill_voice_players(items), note, gr.update(interactive=True),
           gr.update(choices=_voice_task_history_choices("separation", lang)))


def on_voice_cover(source_history, source_upload, ref_library, ref_dry_upload,
                   ref_dry_sep, ref_upload,
                   semi_tone=0, steps=30, gain_db=0.0, custom_acc="",
                   denoise=False, ref_seg_mode="smart"):
    """参考音色翻唱（生成器回调）：确保分离 → 换嗓 → 混音 全流程，实时显示进度。

    与「音轨分离」Tab 统一：
    1. 用户选原唱（generation/cover/上传） → 先 ensure_separation：
       - history 里已有同 source_md5 的 separation → 直接复用（跳过 Demucs）
       - 没有 → 同步独立 Demucs 分离 + 持久化为 separation 记录
    2. 然后用 sep_task:<task_id> 模式调 cover_worker（跳过 worker 内部分离）
    3. 后续同曲翻唱自动命中第 1 步的查重，GPU 秒级启动

    用户直接选分离记录（sep_task 前缀）时：跳过 ensure_separation，直接复用。
    ref_seg_mode=参考段策略（smart=智能/默认、energy=能量最高段、full=整曲不裁剪）；
    smart 仅在参考干声来自分离记录时带配对伴奏（算人声主导度挑段），否则 worker 回退
    能量最高段。
    """
    lang = _CUR_LANG
    # 提交前先反馈：清空播放器组 + 禁用按钮
    yield (*_fill_voice_players([]), tr(lang, "排队中..."),
           gr.update(interactive=False), gr.update())

    # ====== 源解析 ======
    src_val = source_upload if source_upload and Path(source_upload).exists() \
        else source_history
    source_vocals = source_acc = ""
    root_task_id = ""
    project = ""

    if isinstance(src_val, str) and src_val.startswith("sep_task:"):
        # 用户直接选了分离记录 → 跳过 ensure_separation
        entry = history_mgr.get(src_val.split(":", 1)[1])
        stems = {s.get("type"): s.get("path", "") for s in (getattr(entry, "stems", None) or [])
                 if isinstance(s, dict)}
        source_vocals = stems.get("vocals", "")
        source_acc = stems.get("accompaniment", "")
        if not (source_vocals and Path(source_vocals).exists()
                and source_acc and Path(source_acc).exists()):
            raise gr.Error(tr(_CUR_LANG, "该分离记录缺少人声/伴奏轨，无法复用"))
        source, from_upload = source_vocals, False
        root_task_id = entry.task_id
        project = getattr(entry, "project", "") or ""
    else:
        # 原唱路径 → 先 ensure_separation（查重 / 同步分离 + 持久化）
        source_orig, from_upload = _resolve_voice_source(source_history, source_upload)
        project = _project_from_source(source_orig)
        try:
            sep_task_id = voice_handlers.ensure_separation(source_orig, project)
        except Exception as e:
            yield (*[gr.update()] * VOICE_PLAYER_COUNT,
                   tr(_CUR_LANG, "源音频分离失败") + f": {e}",
                   gr.update(interactive=True), gr.update())
            raise gr.Error(str(e))
        # sep_task_id = "sep_task:<task_id>" → 走复用分支解析
        entry = history_mgr.get(sep_task_id.split(":", 1)[1])
        stems = {s.get("type"): s.get("path", "") for s in (getattr(entry, "stems", None) or [])
                 if isinstance(s, dict)}
        source_vocals = stems.get("vocals", "")
        source_acc = stems.get("accompaniment", "")
        source, from_upload = source_vocals, False
        root_task_id = entry.task_id

    # 参考音色解析优先级：上传 → 上传干声 → 分离人声 → 音色库
    # ref_acc = 配对伴奏轨：仅「分离人声」来源能拿到，供 worker 智能挑参考段用
    ref = None
    ref_acc = ""
    for src_key, cand in (("upload", ref_upload), ("dry_upload", ref_dry_upload),
                          ("dry_sep", ref_dry_sep), ("library", ref_library)):
        if cand and Path(cand).exists():
            ref = cand
            if str(ref_seg_mode) == "smart" and src_key == "dry_sep":
                ref_acc = _voice_ref_pair_acc(str(ref))
            break
    if not ref:
        raise gr.Error(tr(_CUR_LANG, "请先选择参考音色"))
    # 自定义伴奏校验（选了但文件缺失则忽略，回退原伴奏）
    acc = custom_acc if custom_acc and Path(custom_acc).exists() else ""
    # 翻唱项目名（文件管理重构）：源项目名_音色名（源无项目名则仅音色名）
    cover_project = "_".join(x for x in (project, _voice_ref_name(str(ref))) if x)
    # ====== 提交 cover worker 队列 ======
    gen = voice_handlers.run_in_queue_stream(
        TaskType.COVER, voice_handlers.cover_worker,
        lang, tr, "参考音色翻唱",
        on_submit=lambda tid: _register_task("cover", tid),
        on_finish=lambda tid: _unregister_task("cover", tid),
        source=source, ref=str(ref),
        accompaniment=acc, semi_tone=int(semi_tone or 0),
        diffusion_steps=int(steps or 30), gain_db=float(gain_db or 0.0),
        root_task_id=root_task_id, denoise=bool(denoise),
        source_vocals=source_vocals, source_acc=source_acc,
        from_upload=False, project=cover_project,
        ref_mode=str(ref_seg_mode or "smart"), ref_acc=ref_acc,
    )
    try:
        result = None
        while True:  # 消费状态流：每条文案 → 播放器保持 + 进度文案 + 按钮保持禁用
            try:
                text = next(gen)
            except StopIteration as stop:
                result = stop.value
                break
            yield _voice_running_outputs(text)
    except TaskCancelledError:
        # 用户主动取消：恢复按钮 + info 显示"任务已取消"，正常收尾不弹错误窗
        # （必须 return：否则会落进下方成功路径，result=None 导致 .get 崩溃）
        yield (*[gr.update()] * VOICE_PLAYER_COUNT, tr(lang, "任务已取消"),
               gr.update(interactive=True), gr.update())
        return
    except Exception:
        # 失败必须恢复按钮；info 区给出失败提示后向上抛（Gradio 弹错误）
        yield (*[gr.update()] * VOICE_PLAYER_COUNT, tr(lang, "任务失败"),
               gr.update(interactive=True), gr.update())
        raise
    # 按产物数量填充播放器组（翻唱成品/换嗓干声/伴奏/分离人声）+ 刷新历史下拉
    items = _voice_stem_items(result.get("stems"))
    note = tr(lang, "翻唱完成") + " · " + tr(lang, "写入历史")
    yield (*_fill_voice_players(items), note, gr.update(interactive=True),
           gr.update(choices=_voice_task_history_choices("cover", lang)))


def on_voice_cancel(channel):
    """音色工坊取消按钮：按通道（separation/cover）取消正在排队/运行的任务。

    取消为协作式：QUEUED 直接移除；RUNNING 经 cancel_event 通知 worker
    在阶段边界中止（换嗓子进程可被 terminate，产物目录一并回收）。
    """
    with _active_tasks_lock:
        task_ids = _active_tasks.pop(channel, set())
    for task_id in task_ids:
        queue_manager.cancel_task_by_id(task_id)
    if task_ids:
        return tr(_CUR_LANG, "正在取消...")
    return tr(_CUR_LANG, "没有正在运行的任务")


def on_voice_ref_delete(path):
    """删除音色库选中条目（移系统回收站），返回(试听清空, 音色库下拉刷新)。"""
    if not path:
        raise gr.Error(tr(_CUR_LANG, "请先选择条目"))
    try:
        voice_handlers.delete_ref(path)
    except Exception as e:
        raise gr.Error(f"{tr(_CUR_LANG, '删除失败')}: {e}")
    gr.Info(tr(_CUR_LANG, "已删除"))
    return gr.update(value=None, visible=False), \
        gr.update(choices=_voice_ref_choices(_CUR_LANG), value=None)


def on_voice_ref_rename(path, new_name):
    """重命名音色库选中条目，返回(试听清空, 音色库下拉刷新)。"""
    if not path:
        raise gr.Error(tr(_CUR_LANG, "请先选择条目"))
    name = (new_name or "").strip()
    if not name:
        raise gr.Error(tr(_CUR_LANG, "请输入新名称"))
    try:
        voice_handlers.rename_ref(path, name)
    except Exception as e:
        raise gr.Error(f"{tr(_CUR_LANG, '重命名失败')}: {e}")
    gr.Info(tr(_CUR_LANG, "已重命名"))
    return gr.update(value=None, visible=False), \
        gr.update(choices=_voice_ref_choices(_CUR_LANG), value=None)


def on_voice_stem_delete(path):
    """删除素材库选中条目（移系统回收站），返回素材库下拉刷新。"""
    if not path:
        raise gr.Error(tr(_CUR_LANG, "请先选择条目"))
    try:
        voice_handlers.delete_stem(path)
    except Exception as e:
        raise gr.Error(f"{tr(_CUR_LANG, '删除失败')}: {e}")
    gr.Info(tr(_CUR_LANG, "已删除"))
    return gr.update(choices=_voice_stem_choices(_CUR_LANG), value=None)


def on_voice_stem_rename(path, new_name):
    """重命名素材库选中条目，返回素材库下拉刷新。"""
    if not path:
        raise gr.Error(tr(_CUR_LANG, "请先选择条目"))
    name = (new_name or "").strip()
    if not name:
        raise gr.Error(tr(_CUR_LANG, "请输入新名称"))
    try:
        voice_handlers.rename_stem(path, name)
    except Exception as e:
        raise gr.Error(f"{tr(_CUR_LANG, '重命名失败')}: {e}")
    gr.Info(tr(_CUR_LANG, "已重命名"))
    return gr.update(choices=_voice_stem_choices(_CUR_LANG), value=None)


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
    # 歌曲历史页只展示生成记录；分离/翻唱记录在各自 Tab 的历史区查看
    rows = history_mgr.to_dataframe_rows(record_types=("generation",))
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
    # 与 refresh_history 一致：仅生成记录进入歌曲历史分页
    rows = history_mgr.to_dataframe_rows(record_types=("generation",))
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
    # 页数按过滤后的记录集计算（与 refresh_history/_get_history_page 一致），
    # 否则存在分离/翻唱记录时总页数会偏大
    rows = history_mgr.to_dataframe_rows(record_types=("generation",))
    pages = max(1, (len(rows) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    new_page = min(pages - 1, int(current_page) + 1)
    return _get_history_page(new_page) + (new_page,)


def _load_history_entry(row_index, current_state):
    """Load a history entry by row index.
    返回 state, audio, info, style, lyrics, abc, preview, lyrics_data, duration_data, stem_dd, stem_audio。

    注意：行号映射基于与表格一致的过滤记录集（generation），否则选中行会错位
    （曾导致点击生成记录实际选中 separation 记录）。
    """
    no_stem = gr.update(visible=False, choices=[]), gr.update(visible=False, value=None)
    rows = history_mgr.to_dataframe_rows(record_types=("generation",))
    if row_index < 0 or row_index >= len(rows):
        return current_state, None, tr(_CUR_LANG, "请选择一条记录"), "", "", "", "", "", "", *no_stem
    task_id = rows[row_index][6]  # 列序：时间/项目名/风格/模式/时长/耗时/TaskID
    entry = history_mgr.get(task_id)
    if not entry:
        return current_state, None, tr(_CUR_LANG, "记录不存在"), "", "", "", "", "", "", *no_stem
    audio_path = Path(entry.audio_path)
    abc_score = history_mgr.get_abc_score(task_id) or ""
    # 多轨产物回放：仅有 separation/cover 且存在条目标签才显示
    stem_choices = []
    stem_first = None
    for s in getattr(entry, "stems", None) or []:
        p = s.get("path", "") if isinstance(s, dict) else ""
        if p and Path(p).exists():
            stem_choices.append((s.get("label", p), p))
            stem_first = stem_first or p
    has_stem = bool(stem_choices)
    stem_dd = gr.update(choices=stem_choices, value=stem_first if stem_choices else None,
                        visible=has_stem)
    stem_audio = gr.update(value=stem_first, visible=has_stem)
    if audio_path.exists():
        lyrics = entry.lyrics or entry.lyrics_preview or ""
        _lyrics_json = html.escape(json.dumps(lyrics, ensure_ascii=False), quote=True)
        lyrics_data = f'<div class="history-lyrics-data" style="display:none" data-lyrics=\'{_lyrics_json}\' data-duration="{entry.audio_duration_seconds}"></div>'
        abc_preview = '<div id="history-abc-preview-container" style="padding: 20px; border-radius: 8px; min-height: 200px;"><div id="history-abc-paper"></div><div id="history-abc-audio"></div></div>'
        return [task_id], str(audio_path), f"**{entry.task_id}**", entry.style, lyrics, abc_score, abc_preview, lyrics_data, f'<div class="history-duration-data" style="display:none" data-duration="{entry.audio_duration_seconds}"></div>', stem_dd, stem_audio
    return [task_id], None, tr(_CUR_LANG, "音频文件不存在"), "", "", "", "", "", "", stem_dd, stem_audio


def on_history_select(evt: gr.SelectData, current_state: list, current_page):
    """Handle history row selection via Dataframe.select (fallback)."""
    actual_index = evt.index[0] + int(current_page) * HISTORY_PAGE_SIZE
    return _load_history_entry(actual_index, current_state)


def on_history_row_click(row_index, current_state, current_page):
    """Handle history row selection via JS click handler."""
    actual_index = int(row_index) + int(current_page) * HISTORY_PAGE_SIZE
    return _load_history_entry(actual_index, current_state)


def _hist_player_keep():
    """历史页播放器三元组：保持现状（未选中/操作失败时使用）。"""
    return gr.update(), gr.update(), gr.update()


def _hist_player_clear():
    """历史页播放器三元组：清空并隐藏轨道回放（删除/清空成功后使用）。"""
    return (gr.update(value=None),
            gr.update(visible=False, choices=[], value=None),
            gr.update(value=None, visible=False))


def on_history_delete(selected_state):
    """Delete the currently selected history entry.

    返回末尾三元组同步刷新播放器（试听/轨道回放），避免仍指向已删除文件
    （与分离/翻唱页 on_voice_task_delete 清空播放器的行为一致）。
    """
    if not selected_state:
        rows, info = refresh_history()
        return rows, info, tr(_CUR_LANG, "请先点击选择要删除的记录"), selected_state, 0, *_hist_player_keep()
    task_id = selected_state[0]
    if history_mgr.delete(task_id):
        rows, info = refresh_history()
        return rows, info, f"{tr(_CUR_LANG, '已删除')} {task_id}", [], 0, *_hist_player_clear()
    rows, info = refresh_history()
    return rows, info, tr(_CUR_LANG, "删除失败"), selected_state, 0, *_hist_player_keep()


def on_history_clear():
    """Clear all history. 清空后播放器一并清空。"""
    history_mgr.clear()
    rows, info = refresh_history()
    return rows, info, tr(_CUR_LANG, "已清空所有历史"), [], 0, *_hist_player_clear()


def on_history_rename_project(selected_state, new_name):
    """改项目名（文件管理重构）：重命名选中记录所在项目目录的全部文件。

    只替换文件名中的项目名段，保留时间戳与后缀（_varN 等）；目录名不动。
    输入留空 = 清除项目名（文件回退为时间戳开头）。
    返回末尾三元组按记录新路径重填播放器（旧路径已随重命名失效）。
    """
    if not selected_state:
        rows, info = refresh_history()
        return rows, info, tr(_CUR_LANG, "请先点击选择要修改的记录"), selected_state, 0, *_hist_player_keep()
    entry = history_mgr.get(selected_state[0])
    if not entry or not getattr(entry, "output_dir", ""):
        rows, info = refresh_history()
        return rows, info, tr(_CUR_LANG, "该记录没有产物目录"), selected_state, 0, *_hist_player_keep()
    n = history_mgr.rename_project(entry.output_dir, new_name or "")
    rows, info = refresh_history()
    # rename_project 已同步 db 路径：重新取记录，按新路径重填播放器
    entry = history_mgr.get(selected_state[0]) or entry
    stem_choices, stem_first = [], None
    for s in getattr(entry, "stems", None) or []:
        p = s.get("path", "") if isinstance(s, dict) else ""
        if p and Path(p).exists():
            stem_choices.append((s.get("label", p), p))
            stem_first = stem_first or p
    has_stem = bool(stem_choices)
    audio = entry.audio_path if entry.audio_path and Path(entry.audio_path).exists() else None
    stem_dd = gr.update(choices=stem_choices, value=stem_first if has_stem else None,
                        visible=has_stem)
    stem_audio = gr.update(value=stem_first, visible=has_stem)
    return rows, info, f"{tr(_CUR_LANG, '已重命名')} {n} {tr(_CUR_LANG, '个文件')}", [], 0, audio, stem_dd, stem_audio


def on_history_delete_project(selected_state):
    """删除项目（文件管理重构）：选中记录所在的整个产物目录移入回收站。

    同目录的批量变体等多条记录一并移除（目录整体回收，文件可从回收站还原）。
    返回末尾三元组清空播放器，避免仍指向已删除目录。
    """
    if not selected_state:
        rows, info = refresh_history()
        return rows, info, tr(_CUR_LANG, "请先点击选择要删除的记录"), selected_state, 0, *_hist_player_keep()
    entry = history_mgr.get(selected_state[0])
    if not entry or not getattr(entry, "output_dir", ""):
        rows, info = refresh_history()
        return rows, info, tr(_CUR_LANG, "该记录没有产物目录"), selected_state, 0, *_hist_player_keep()
    n = history_mgr.delete_project(entry.output_dir)
    rows, info = refresh_history()
    if n <= 0:
        return rows, info, tr(_CUR_LANG, "删除失败"), selected_state, 0, *_hist_player_keep()
    return rows, info, f"{tr(_CUR_LANG, '已删除项目')} · {n} {tr(_CUR_LANG, '条记录')}", [], 0, *_hist_player_clear()


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

    # 音色工坊三模型状态（Demucs / Seed-VC / campplus，文件级检查不加载模型）
    try:
        vc = check_voice_models(PROJECT_ROOT)
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

    total, used, free = shutil.disk_usage(str(PROJECT_ROOT))
    lines.append(f"\n### {tr(lang, '磁盘')}\n- {tr(lang, '剩余')}: {free / (1024**3):.1f} GB / {total / (1024**3):.1f} GB")

    return "\n".join(lines)


# 队列任务类型 → 展示名（中文原文走 tr 翻译）
_QUEUE_TYPE_LABELS = {"generation": "生成", "transcription": "转谱",
                      "separation": "分离", "cover": "翻唱"}


def _queue_status_html():
    """生成设置页「当前队列」状态 Markdown（运行中/排队中/最近任务，按当前语言渲染）。

    数据来自 queue_manager.get_queue_snapshot()（只读快照，不消费 drain 进度流），
    由 gr.Timer 周期刷新 + 语言切换时即时重渲染。
    """
    lang = _CUR_LANG
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
    """Load a preset and return param values.

    返回 23 个字段（17 旧字段 + CFG/批量 + 4 个后处理开关）。
    预设中缺失的字段返回 gr.update()（前端保持当前值），保证旧格式
    预设文件与仅覆盖部分字段的内置预设（如「快速demo」）不被默认值覆盖。
    """
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
            return [gr.update() for _ in range(23)]

    p = preset.get("params", {})

    def _pv(key):
        """声明字段返回存储值，缺失字段返回 gr.update()（保持当前值）。"""
        return p[key] if key in p else gr.update()

    return [
        _pv("cot") if "cot" in p else "full",
        p.get("num_inference_steps", 8),
        p.get("out_format", "pcm16"),
        _pv("abc_temp"), _pv("abc_top_p"), _pv("abc_top_k"),
        _pv("abc_rep_penalty"), _pv("abc_pen_window"), _pv("abc_min_tok"), _pv("abc_max_tok"),
        _pv("sem_temp"), _pv("sem_top_p"), _pv("sem_top_k"),
        _pv("sem_rep_penalty"), _pv("sem_pen_window"), _pv("sem_min_tok"), _pv("sem_max_tok"),
        _pv("cfg_scale"), _pv("batch_count"),
        _pv("pp_normalize"), _pv("pp_fade"), _pv("pp_trim"), _pv("pp_metadata"),
    ]


def on_preset_save(name, cot, steps, out_format, abc_temp, abc_top_p, abc_top_k, abc_rep, abc_pen, abc_min, abc_max,
                    sem_temp, sem_top_p, sem_top_k, sem_rep, sem_pen, sem_min, sem_max,
                    cfg_scale, batch_count, pp_normalize, pp_fade, pp_trim, pp_metadata):
    """Save current params as a preset（含 CFG/批量数量/后处理开关等全部生成参数）。"""
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
            "cfg_scale": cfg_scale, "batch_count": batch_count,
            "pp_normalize": pp_normalize, "pp_fade": pp_fade,
            "pp_trim": pp_trim, "pp_metadata": pp_metadata,
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
    /* 系统设置页区块卡片：外框分隔 + 内边距，颜色随明暗主题自动切换 */
    .settings-panel {
        border: 1px solid var(--border-color-primary);
        border-radius: 10px;
        padding: 2px 14px 12px;
        background: var(--background-fill-primary);
    }
    /* GitHub 图标：随主题色，hover 亮起；flex 收缩避免撑满整行 */
    #gh-icon { width: auto; flex: 0 0 auto; display: flex; align-items: center; margin: 0; }
    #gh-link { display: inline-flex; align-items: center; color: var(--body-text-color); opacity: .65; transition: opacity .15s; }
    #gh-link:hover { opacity: 1; }
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
    /* —— 改名/库管理行：统一结构 label | 输入框 | 主动作按钮 | 删除按钮 ——
       做法：把 Gradio Textbox 的原生 <label> 由纵向改为横向 flex，
       label 文字固定宽度，从而各行输入框/按钮起点完全对齐。 */
    #sep-rename-row, #cover-rename-row, #hist-rename-row,
    #lib-stem-row, #lib-ref-row {
        flex-wrap: nowrap !important; gap: 10px !important; align-items: center !important;
    }
    /* Textbox 外层 block 去内边距，高度贴合输入框，便于与按钮垂直居中 */
    #sep-rename-row .block, #cover-rename-row .block, #hist-rename-row .block,
    #lib-stem-row .block, #lib-ref-row .block { padding: 0 !important; }
    /* 原生 label 改为横向 flex：文字在左，输入框在右，同一行垂直居中 */
    #sep-rename-row label.container, #cover-rename-row label.container,
    #hist-rename-row label.container, #lib-stem-row label.container,
    #lib-ref-row label.container {
        display: flex !important; flex-direction: row !important;
        align-items: center !important; gap: 10px !important;
        border: none !important; padding: 0 0 0 12px !important;
    }
    /* label 文字：固定宽度，保证多行左对齐一致 */
    #sep-rename-row label.container > span[data-testid="block-info"],
    #cover-rename-row label.container > span[data-testid="block-info"],
    #hist-rename-row label.container > span[data-testid="block-info"],
    #lib-stem-row label.container > span[data-testid="block-info"],
    #lib-ref-row label.container > span[data-testid="block-info"] {
        flex: 0 0 76px !important; margin: 0 !important;
        white-space: nowrap !important; font-size: 14px !important;
    }
    /* 输入框容器占满剩余宽度，边框移到这里 */
    #sep-rename-row .input-container, #cover-rename-row .input-container,
    #hist-rename-row .input-container, #lib-stem-row .input-container,
    #lib-ref-row .input-container {
        flex: 1 1 auto !important;
        border: 1px solid var(--border-color-primary) !important;
        border-radius: 6px !important;
        background: var(--background-fill-primary) !important;
    }
    /* Textarea 单行高度 */
    #sep-rename-row textarea, #cover-rename-row textarea, #hist-rename-row textarea,
    #lib-stem-row textarea, #lib-ref-row textarea {
        min-height: 38px !important; height: 38px !important;
    }
    /* 按钮缩小 */
    #sep-rename-row button, #cover-rename-row button, #hist-rename-row button,
    #lib-stem-row button, #lib-ref-row button {
        flex: 0 0 auto !important; min-width: auto !important; padding: 4px 14px !important;
    }
"""

# Gradio 内置文案（上传组件"将音频拖放到此处/点击上传"、页脚等）跟随浏览器 locale，
# 后端无参数可控制。此处通过隐藏信号组件 #lang-signal（值=zh/en，CSS display:none
# 隐藏而非 visible=False——后者不渲染 DOM，前端将无法监听）+ 本脚本联动：
# 轮询等待信号组件渲染 -> 调用 Gradio 前端内部 changeLocale 切换其内置文案语言。
# core-*.js 文件名带构建 hash，运行时从 <script> 标签或 performance 资源记录动态
# 获取（Gradio 模块多为动态 import，不一定存在于 script 标签），避免硬编码。
# 注意：Gradio 5.x 的 Blocks 级 js= 会被包装为 await (js)(); 要求「函数表达式」，
# 禁止写成 IIFE (function(){...})(); ——尾部分号会导致前端 SyntaxError 并中断组件挂载。
_LOCALE_SYNC_JS = """
() => {
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
}
"""


def build_ui():
    """Build the Gradio UI."""
    with gr.Blocks(title="YuE2 Music Studio",
                    css=_TITLE_ROW_CSS, js=_LOCALE_SYNC_JS) as demo:

        def _t(s: str) -> str:
            """按当前界面语言翻译单条文案（zh 直接返回原文）。"""
            return tr(_CUR_LANG, s)

        # 标题行：主标题+副标题 Markdown，右侧原生风格 "Lang/语言" 说明 + 紧凑下拉框
        with gr.Row(elem_id="title-row"):
            title_md = gr.Markdown("### YuE2 Music Studio · " + _t("AI音乐创作 — 输入歌词和风格，生成完整歌曲"))
            # GitHub 图标：点击新窗口打开项目仓库（无文案，语言切换无需注册 updater）
            gr.HTML(
                '<a id="gh-link" href="https://github.com/patdelphi/yue2-webui" target="_blank" rel="noopener" title="GitHub">'
                '<svg viewBox="0 0 16 16" width="17" height="17" aria-hidden="true"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>'
                '</a>',
                elem_id="gh-icon", container=False,
            )
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
            tab_create = gr.Tab(_t("歌曲创作"))
            _reg(tab_create, lambda lang: gr.update(label=tr(lang, "歌曲创作")))
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

                        # 项目名（文件管理重构）：产物文件名前缀 <项目名>_<时间戳>，留空则仅时间戳
                        project_input = gr.Textbox(
                            label=_t("项目名 (可选)"),
                            placeholder=_t("例如: 夜曲demo"),
                            info=_t("产物文件名 = 项目名_时间戳；留空则仅用时间戳"),
                        )
                        _reg(project_input, lambda lang: gr.update(
                            label=tr(lang, "项目名 (可选)"),
                            placeholder=tr(lang, "例如: 夜曲demo"),
                            info=tr(lang, "产物文件名 = 项目名_时间戳；留空则仅用时间戳")))

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

            with gr.Tab(_t("歌曲历史")) as tab_history:
                _reg(tab_history, lambda lang: gr.update(label=tr(lang, "歌曲历史")))
                history_md = gr.Markdown(_t("### 生成历史"))
                _reg(history_md, lambda lang: gr.update(value=tr(lang, "### 生成历史")))
                history_state = gr.State(value=[])
                history_page = gr.State(value=0)
                history_row_trigger = gr.Number(visible=True, value=-1, elem_id="history-row-trigger", label="")
                history_df = gr.Dataframe(
                    # 列头采用中英双语（Gradio 静态表格的 headers 不支持运行时切换）
                    headers=["时间 Time", "项目名 Project", "风格 Style", "模式 Mode", "音频时长 Duration", "生成耗时 Elapsed", "Task ID"],
                    datatype=["str", "str", "str", "str", "str", "str", "str"],
                    col_count=7,
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
                history_stem_dd = gr.Dropdown(label=_t("轨道回放(分离/翻唱)"),
                                              choices=[], interactive=True, visible=False)
                _reg(history_stem_dd, lambda lang: gr.update(label=tr(lang, "轨道回放(分离/翻唱)")))
                history_stem_audio = gr.Audio(type="filepath", elem_id="history-stem-audio",
                                              visible=False)
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
                # 项目管理（文件管理重构）：改项目名只改文件名段（保留时间戳）；
                # 删除项目 = 整个产物目录移入回收站 + 移除该目录全部记录
                with gr.Row(elem_id="hist-rename-row"):
                    history_project_input = gr.Textbox(
                        label=_t("新项目名"), placeholder=_t("留空则清除项目名"), scale=3, lines=1)
                    _reg(history_project_input, lambda lang: gr.update(
                        label=tr(lang, "新项目名"), placeholder=tr(lang, "留空则清除项目名")))
                    history_rename_btn = gr.Button(_t("改项目名"), size="sm", scale=1)
                    _reg(history_rename_btn, lambda lang: gr.update(value=tr(lang, "改项目名")))
                    history_del_project_btn = gr.Button(_t("删除项目"), variant="stop", size="sm", scale=1)
                    _reg(history_del_project_btn, lambda lang: gr.update(value=tr(lang, "删除项目")))

                history_df.select(fn=on_history_select, inputs=[history_state, history_page], outputs=[history_state, history_audio, history_info, history_style, history_lyrics, history_abc, history_abc_preview, history_lyrics_data, history_duration_data, history_stem_dd, history_stem_audio])
                history_row_trigger.change(fn=on_history_row_click, inputs=[history_row_trigger, history_state, history_page], outputs=[history_state, history_audio, history_info, history_style, history_lyrics, history_abc, history_abc_preview, history_lyrics_data, history_duration_data, history_stem_dd, history_stem_audio])
                history_stem_dd.change(fn=lambda v: gr.update(value=v, visible=True), inputs=history_stem_dd, outputs=history_stem_audio)
                history_refresh_btn.click(fn=refresh_history_full, outputs=[history_df, history_page_info, history_page])
                # outputs 末尾统一追加播放器三元组（试听/轨道回放），与回调返回值对应
                history_delete_btn.click(fn=on_history_delete, inputs=history_state,
                                         outputs=[history_df, history_page_info, history_info, history_state, history_page, history_audio, history_stem_dd, history_stem_audio])
                history_clear_btn.click(fn=on_history_clear,
                                        outputs=[history_df, history_page_info, history_info, history_state, history_page, history_audio, history_stem_dd, history_stem_audio])
                # 项目管理：改项目名（文件级重命名，播放器按新路径重填）/ 删除项目（整目录入回收站，播放器清空）
                history_rename_btn.click(fn=on_history_rename_project, inputs=[history_state, history_project_input],
                                         outputs=[history_df, history_page_info, history_info, history_state, history_page, history_audio, history_stem_dd, history_stem_audio])
                # 删除项目确认弹窗（前端 js，取消则中止回调不触发 Python 端删除）。
                # 注意：Gradio 5.x 中 js 返回 false 不能阻止 fn 执行（实测），
                # 必须在用户取消时 throw 中断；弹窗文案中英双语以兼容两种界面语言。
                _DEL_PROJECT_CONFIRM_JS = (
                    "() => { if (!confirm("
                    "'确定删除整个项目目录？文件将移入回收站。\\n"
                    "Delete the whole project folder? Files will be moved to the Recycle Bin.'"
                    ")) throw new Error('cancelled'); }"
                )
                history_del_project_btn.click(fn=on_history_delete_project, inputs=history_state,
                                              js=_DEL_PROJECT_CONFIRM_JS,
                                              outputs=[history_df, history_page_info, history_info, history_state, history_page, history_audio, history_stem_dd, history_stem_audio])
                history_prev_btn.click(fn=on_history_prev_page, inputs=history_page, outputs=[history_df, history_page_info, history_page])
                history_next_btn.click(fn=on_history_next_page, inputs=history_page, outputs=[history_df, history_page_info, history_page])
                demo.load(fn=refresh_history_full, outputs=[history_df, history_page_info, history_page])

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

            with gr.Tab(_t("音轨分离")) as tab_sep:
                _reg(tab_sep, lambda lang: gr.update(label=tr(lang, "音轨分离")))
                # —— 左右分栏：左=源音频+分离参数，右=执行与输出 ——
                with gr.Row():
                    with gr.Column(scale=5):
                        sep_src_md = gr.Markdown(_t("### 源音频"))
                        _reg(sep_src_md, lambda lang: gr.update(value=tr(lang, "### 源音频")))

                        # 源音频：历史记录 或 上传（value 固定 history/upload）
                        sep_src_mode = gr.Radio(choices=[
                            (_t("从历史记录选择"), "history"), (_t("上传音频"), "upload"),
                        ], value="history", label=_t("当前源"))
                        _reg(sep_src_mode, lambda lang: gr.update(
                            choices=[(tr(lang, "从历史记录选择"), "history"), (tr(lang, "上传音频"), "upload")],
                            label=tr(lang, "当前源")))

                        sep_src_history = gr.Dropdown(
                            choices=_voice_source_history_choices(_CUR_LANG),
                            label=_t("从历史记录选择"), interactive=True)
                        _reg(sep_src_history, lambda lang: gr.update(
                            choices=_voice_source_history_choices(lang),
                            label=tr(lang, "从历史记录选择")))

                        sep_src_upload = gr.Audio(label=_t("上传音频"), type="filepath",
                                                  elem_id="sep-src-upload", visible=False)
                        _reg(sep_src_upload, lambda lang: gr.update(label=tr(lang, "上传音频")))

                        sep_param_md = gr.Markdown(_t("### 音轨分离"))
                        _reg(sep_param_md, lambda lang: gr.update(value=tr(lang, "### 音轨分离")))
                        sep_stem_mode = gr.Radio(choices=[
                            (_t("人声/伴奏"), "vocals"), (_t("人声/鼓/贝斯/其他"), "full"),
                        ], value="vocals", label=_t("分离模式"))
                        _reg(sep_stem_mode, lambda lang: gr.update(
                            choices=[(tr(lang, "人声/伴奏"), "vocals"), (tr(lang, "人声/鼓/贝斯/其他"), "full")],
                            label=tr(lang, "分离模式")))

                        # —— 执行与输出：降噪、开始分离、结果（移到左列，紧跟参数） ——
                        sep_denoise = gr.Checkbox(label=_t("降噪"), value=False,
                                                  info=_t("开启后对输出人声降噪"))
                        _reg(sep_denoise, lambda lang: gr.update(
                            label=tr(lang, "降噪"), info=tr(lang, "开启后对输出人声降噪")))
                        with gr.Row():
                            sep_btn = gr.Button(_t("开始分离"), variant="primary", scale=3)
                            _reg(sep_btn, lambda lang: gr.update(value=tr(lang, "开始分离")))
                            sep_cancel_btn = gr.Button(_t("取消任务"), variant="stop", scale=2)
                            _reg(sep_cancel_btn, lambda lang: gr.update(value=tr(lang, "取消任务")))
                        sep_info = gr.Markdown()
                        # 多轨编辑入口：新窗口打开独立编辑页（前端只做交互，成品由后端 ffmpeg 渲染）
                        sep_mix_btn = gr.Button(_t("多轨编辑"), size="sm")
                        _reg(sep_mix_btn, lambda lang: gr.update(value=tr(lang, "多轨编辑")))
                        # 输出产物播放器组：按产物数量逐个显示（与其他 Tab 播放器同组件，
                        # PlayerZoom 个性化定制按 elem_id 前缀 sep-audio- 统一接管）；label 动态为轨道名
                        sep_audios = [
                            gr.Audio(type="filepath", label="", elem_id=f"sep-audio-{i}",
                                     visible=False)
                            for i in range(VOICE_PLAYER_COUNT)
                        ]

                    # —— 右栏：分离任务历史（按文件夹选择，整组播放器回放全部轨道） ——
                    with gr.Column(scale=4):
                        # —— 分离任务历史：选择历次分离任务并回放结果 ——
                        sep_hist_md = gr.Markdown(_t("### 分离任务历史"))
                        _reg(sep_hist_md, lambda lang: gr.update(value=tr(lang, "### 分离任务历史")))
                        sep_history_dd = gr.Dropdown(
                            choices=_voice_task_history_choices("separation"),
                            label=_t("选择分离任务"), interactive=True)
                        _reg(sep_history_dd, lambda lang: gr.update(
                            choices=_voice_task_history_choices("separation", lang),
                            label=tr(lang, "选择分离任务")))
                        # 隐藏 State 组件：保存当前选中的 task_id，供删除/改名按钮正确读取
                        # （Gradio 6 中 Tab 切换更新 choices 会把 Dropdown 的 value 重置为 None，
                        # 必须用 State 组件显式保存用户选中的值，与历史页 history_state 模式一致）
                        sep_selected_task = gr.State(value=None)
                        # 历史回放播放器组：选中任务后按文件夹填充全部轨道（每轨可下载）
                        sep_hist_audios = [
                            gr.Audio(type="filepath", label="", elem_id=f"sep-history-audio-{i}",
                                     visible=False)
                            for i in range(VOICE_PLAYER_COUNT)
                        ]
                        # 项目管理（文件管理重构）：改项目名（保留时间戳）/ 删除项目（整目录入回收站）
                        # 统一结构：label | 输入框 | 主动作按钮 | 删除按钮（label 由 CSS 压在输入框同一行）
                        with gr.Row(elem_id="sep-rename-row"):
                            sep_rename_input = gr.Textbox(
                                label=_t("新项目名"), placeholder=_t("留空则清除项目名"),
                                scale=3, lines=1)
                            _reg(sep_rename_input, lambda lang: gr.update(
                                label=tr(lang, "新项目名"), placeholder=tr(lang, "留空则清除项目名")))
                            sep_rename_btn = gr.Button(_t("改项目名"), size="sm", scale=1)
                            _reg(sep_rename_btn, lambda lang: gr.update(value=tr(lang, "改项目名")))
                            sep_del_btn = gr.Button(_t("删除项目"), variant="stop", size="sm", scale=1)
                            _reg(sep_del_btn, lambda lang: gr.update(value=tr(lang, "删除项目")))

                        # —— 库管理区：素材库（乐器轨）与音色库（参考干声）统一在此管理 ——
                        # （翻唱页仅保留选择+试听；删除/重命名集中到分离页）
                        lib_md = gr.Markdown(_t("### 库管理"))
                        _reg(lib_md, lambda lang: gr.update(value=tr(lang, "### 库管理")))

                        lib_stem_dd = gr.Dropdown(
                            choices=_voice_stem_choices(_CUR_LANG),
                            label=_t("素材库(乐器轨)"), interactive=True)
                        _reg(lib_stem_dd, lambda lang: gr.update(
                            choices=_voice_stem_choices(lang), label=tr(lang, "素材库(乐器轨)")))
                        lib_stem_preview = gr.Audio(
                            label=_t("试听"), type="filepath",
                            elem_id="lib-stem-preview", visible=False,
                           )
                        _reg(lib_stem_preview, lambda lang: gr.update(label=tr(lang, "试听")))
                        with gr.Row(elem_id="lib-stem-row"):
                            lib_stem_rename_input = gr.Textbox(
                                label=_t("重命名为"), placeholder=_t("新名字"), elem_id="lib-stem-rename",
                                scale=3, lines=1)
                            _reg(lib_stem_rename_input, lambda lang: gr.update(
                                label=tr(lang, "重命名为"), placeholder=tr(lang, "新名字")))
                            lib_stem_rename_btn = gr.Button(_t("重命名"), size="sm", scale=1)
                            _reg(lib_stem_rename_btn, lambda lang: gr.update(value=tr(lang, "重命名")))
                            lib_stem_del_btn = gr.Button(_t("删除选中"), size="sm", variant="stop", scale=1)
                            _reg(lib_stem_del_btn, lambda lang: gr.update(value=tr(lang, "删除选中")))

                        lib_ref_dd = gr.Dropdown(
                            choices=_voice_ref_choices(_CUR_LANG),
                            label=_t("音色库(参考干声)"), interactive=True)
                        _reg(lib_ref_dd, lambda lang: gr.update(
                            choices=_voice_ref_choices(lang), label=tr(lang, "音色库(参考干声)")))
                        lib_ref_preview = gr.Audio(
                            label=_t("试听"), type="filepath",
                            elem_id="lib-ref-preview", visible=False,
                           )
                        _reg(lib_ref_preview, lambda lang: gr.update(label=tr(lang, "试听")))
                        with gr.Row(elem_id="lib-ref-row"):
                            lib_ref_rename_input = gr.Textbox(
                                label=_t("重命名为"), placeholder=_t("新名字"), elem_id="lib-ref-rename",
                                scale=3, lines=1)
                            _reg(lib_ref_rename_input, lambda lang: gr.update(
                                label=tr(lang, "重命名为"), placeholder=tr(lang, "新名字")))
                            lib_ref_rename_btn = gr.Button(_t("重命名"), size="sm", scale=1)
                            _reg(lib_ref_rename_btn, lambda lang: gr.update(value=tr(lang, "重命名")))
                            lib_ref_del_btn = gr.Button(_t("删除选中"), size="sm", variant="stop", scale=1)
                            _reg(lib_ref_del_btn, lambda lang: gr.update(value=tr(lang, "删除选中")))

                # 事件绑定
                sep_src_mode.change(fn=on_voice_src_mode, inputs=sep_src_mode,
                                    outputs=[sep_src_history, sep_src_upload])
                sep_btn.click(fn=on_voice_separate,
                              inputs=[sep_src_history, sep_src_upload, sep_stem_mode, sep_denoise],
                              outputs=[*sep_audios, sep_info, sep_btn, sep_history_dd])
                # 取消按钮：协作式取消本 Tab 排队中/运行中的任务（info 区反馈结果）
                sep_cancel_btn.click(fn=lambda: on_voice_cancel("separation"),
                                     outputs=[sep_info])
                # 多轨编辑入口：纯前端事件（fn=None），新窗口打开独立编辑页（不做后端往返）
                sep_mix_btn.click(fn=None, js="() => window.open('/static/multitrack/', '_blank')")

            with gr.Tab(_t("音色翻唱")) as tab_cover:
                _reg(tab_cover, lambda lang: gr.update(label=tr(lang, "音色翻唱")))
                # —— 左右分栏：左=被翻唱歌曲+参考音色+翻唱参数，右=执行与输出 ——
                with gr.Row():
                    with gr.Column(scale=5):
                        cover_src_md = gr.Markdown(_t("### 被翻唱歌曲"))
                        _reg(cover_src_md, lambda lang: gr.update(value=tr(lang, "### 被翻唱歌曲")))

                        # 被翻唱歌曲：历史记录 或 上传（value 固定 history/upload）
                        cover_src_mode = gr.Radio(choices=[
                            (_t("从历史记录选择"), "history"), (_t("上传音频"), "upload"),
                        ], value="history", label=_t("当前源"))
                        _reg(cover_src_mode, lambda lang: gr.update(
                            choices=[(tr(lang, "从历史记录选择"), "history"), (tr(lang, "上传音频"), "upload")],
                            label=tr(lang, "当前源")))

                        cover_src_history = gr.Dropdown(
                            choices=_voice_cover_source_choices(_CUR_LANG),
                            label=_t("从历史记录选择"), interactive=True)
                        _reg(cover_src_history, lambda lang: gr.update(
                            choices=_voice_cover_source_choices(lang),
                            label=tr(lang, "从历史记录选择")))

                        cover_src_upload = gr.Audio(label=_t("上传音频"), type="filepath",
                                                    elem_id="cover-src-upload", visible=False)
                        _reg(cover_src_upload, lambda lang: gr.update(label=tr(lang, "上传音频")))

                        # —— 参考音色三入口：音色库 / 干声历史 / 上传 ——
                        cover_ref_md = gr.Markdown(_t("### 参考音色"))
                        _reg(cover_ref_md, lambda lang: gr.update(value=tr(lang, "### 参考音色")))

                        cover_ref_mode = gr.Radio(choices=[
                            (_t("从音色库选择"), "library"), (_t("从干声历史选择"), "dry"),
                            (_t("上传参考干声(1-30秒)"), "upload"),
                        ], value="library", label=_t("参考音色"))
                        _reg(cover_ref_mode, lambda lang: gr.update(
                            choices=[(tr(lang, "从音色库选择"), "library"),
                                     (tr(lang, "从干声历史选择"), "dry"),
                                     (tr(lang, "上传参考干声(1-30秒)"), "upload")],
                            label=tr(lang, "参考音色")))

                        # 音色库入口（默认可见）
                        cover_ref_dropdown = gr.Dropdown(
                            choices=_voice_ref_choices(_CUR_LANG), label=_t("音色库选择"),
                            interactive=True)
                        _reg(cover_ref_dropdown, lambda lang: gr.update(
                            choices=_voice_ref_choices(lang), label=tr(lang, "音色库选择")))
                        # —— 音色库入口：仅选择；选中即试听（管理功能在分离页库管理区） ——
                        cover_ref_preview = gr.Audio(
                            label=_t("试听"), type="filepath",
                            elem_id="cover-ref-preview", visible=False,
                           )
                        _reg(cover_ref_preview, lambda lang: gr.update(label=tr(lang, "试听")))

                        # 干声历史入口：radio 选来源（分离人声/上传干声），再在下拉选文件（默认隐藏）
                        with gr.Column(visible=False) as cover_ref_dry_panel:
                            cover_ref_dry_src = gr.Radio(
                                choices=[(_t("从分离人声选择"), "sep"), (_t("从上传干声选择"), "upload")],
                                value="sep", label=_t("干声来源"))
                            _reg(cover_ref_dry_src, lambda lang: gr.update(
                                choices=[(tr(lang, "从分离人声选择"), "sep"),
                                         (tr(lang, "从上传干声选择"), "upload")],
                                label=tr(lang, "干声来源")))
                            cover_ref_dry_sep = gr.Dropdown(
                                choices=_voice_dry_sep_choices(_CUR_LANG),
                                label=_t("分离人声"), interactive=True)
                            _reg(cover_ref_dry_sep, lambda lang: gr.update(
                                choices=_voice_dry_sep_choices(lang),
                                label=tr(lang, "分离人声")))
                            cover_ref_dry_upload = gr.Dropdown(
                                choices=_voice_dry_upload_choices(_CUR_LANG),
                                label=_t("上传干声"), interactive=True, visible=False)
                            _reg(cover_ref_dry_upload, lambda lang: gr.update(
                                choices=_voice_dry_upload_choices(lang),
                                label=tr(lang, "上传干声")))

                        # 上传入口（含命名保存，仅 upload 模式可见）
                        with gr.Column(visible=False) as cover_ref_upload_panel:
                            cover_ref_upload = gr.Audio(label=_t("上传参考干声(1-30秒)"), type="filepath",
                                                        elem_id="cover-ref-upload")
                            _reg(cover_ref_upload, lambda lang: gr.update(label=tr(lang, "上传参考干声(1-30秒)")))
                            # 轻量人声检测结果提示（上传后自动判定，仅提示不拦截）
                            cover_ref_check_md = gr.Markdown()
                            with gr.Row():
                                cover_ref_name = gr.Textbox(label=_t("输入音色库名称"), elem_id="cover-ref-name")
                                _reg(cover_ref_name, lambda lang: gr.update(label=tr(lang, "输入音色库名称")))
                                cover_ref_save_btn = gr.Button(_t("保存到音色库"), size="sm")
                                _reg(cover_ref_save_btn, lambda lang: gr.update(value=tr(lang, "保存到音色库")))
                            cover_ref_info = gr.Markdown(_t("保存参考音色提示"))
                            _reg(cover_ref_info, lambda lang: gr.update(value=tr(lang, "保存参考音色提示")))

                        # —— 自定义伴奏（可选）：从素材库选乐器轨替换原曲伴奏 ——
                        cover_acc_dd = gr.Dropdown(
                            choices=_voice_stem_choices(_CUR_LANG),
                            label=_t("自定义伴奏(可选)"), interactive=True,
                            info=_t("留空自动使用源伴奏"))
                        _reg(cover_acc_dd, lambda lang: gr.update(
                            choices=_voice_stem_choices(lang),
                            label=tr(lang, "自定义伴奏(可选)"),
                            info=tr(lang, "留空自动使用源伴奏")))
                        # —— 素材库选择：仅选择；选中即试听（管理功能在分离页库管理区） ——
                        cover_acc_preview = gr.Audio(
                            label=_t("试听"), type="filepath",
                            elem_id="cover-acc-preview", visible=False,
                           )
                        _reg(cover_acc_preview, lambda lang: gr.update(label=tr(lang, "试听")))

                        # —— 翻唱参数 ——
                        cover_semi = gr.Slider(-12, 12, value=0, step=1, label=_t("半音偏移"))
                        _reg(cover_semi, lambda lang: gr.update(label=tr(lang, "半音偏移")))
                        with gr.Row():
                            cover_semi_orig = gr.Button(_t("半音快捷原调"), size="sm")
                            _reg(cover_semi_orig, lambda lang: gr.update(value=tr(lang, "半音快捷原调")))
                            cover_semi_m12 = gr.Button(_t("−12"), size="sm")
                            cover_semi_p12 = gr.Button(_t("+12"), size="sm")
                        # 默认 40：Seed-VC 官方称质量最佳区为 30-50（30 是质量区下限），
                        # 默认取 40 兼顾质量与耗时
                        cover_steps = gr.Slider(10, 50, value=40, step=1, label=_t("扩散步数"))
                        _reg(cover_steps, lambda lang: gr.update(label=tr(lang, "扩散步数")))
                        cover_gain = gr.Slider(-6, 6, value=0, step=0.5, label=_t("伴奏增益(dB)"))
                        _reg(cover_gain, lambda lang: gr.update(label=tr(lang, "伴奏增益(dB)")))
                        # 参考段策略（P5C 盲听验证）：默认「智能」——参考干声来自分离记录时
                        # 用配对伴奏挑"人声主导度最高 10s"（串音最少）作音色参考；拿不到配对
                        # 伴奏（上传干声/音色库）时自动回退「能量最高段」（旧行为）。
                        # 「整曲不裁剪」等价关闭该优化。
                        cover_ref_seg = gr.Dropdown(choices=[
                            (_t("智能 (推荐)"), "smart"),
                            (_t("能量最高段"), "energy"),
                            (_t("整曲不裁剪"), "full"),
                        ], value="smart", label=_t("参考段"),
                            info=_t("智能：取人声最干净的 10 秒作参考；整曲不裁剪可能音色漂移"))
                        _reg(cover_ref_seg, lambda lang: gr.update(
                            choices=[(tr(lang, "智能 (推荐)"), "smart"),
                                     (tr(lang, "能量最高段"), "energy"),
                                     (tr(lang, "整曲不裁剪"), "full")],
                            label=tr(lang, "参考段"),
                            info=tr(lang, "智能：取人声最干净的 10 秒作参考；整曲不裁剪可能音色漂移")))

                    # —— 右栏：执行与输出 ——
                    with gr.Column(scale=4):
                        cover_denoise = gr.Checkbox(label=_t("降噪"), value=False,
                                                    info=_t("开启后对输出人声降噪"))
                        _reg(cover_denoise, lambda lang: gr.update(
                            label=tr(lang, "降噪"), info=tr(lang, "开启后对输出人声降噪")))
                        with gr.Row():
                            cover_btn = gr.Button(_t("开始翻唱"), variant="primary", scale=3)
                            _reg(cover_btn, lambda lang: gr.update(value=tr(lang, "开始翻唱")))
                            cover_cancel_btn = gr.Button(_t("取消任务"), variant="stop", scale=2)
                            _reg(cover_cancel_btn, lambda lang: gr.update(value=tr(lang, "取消任务")))
                        cover_info = gr.Markdown()
                        # 输出产物播放器组：按产物数量逐个显示（与其他 Tab 播放器同组件，
                        # PlayerZoom 按 elem_id 前缀 cover-audio- 接管）；label 动态为轨道名
                        cover_audios = [
                            gr.Audio(type="filepath", label="", elem_id=f"cover-audio-{i}",
                                     visible=False)
                            for i in range(VOICE_PLAYER_COUNT)
                        ]
                        # —— 翻唱任务历史：按文件夹选择，整组播放器回放全部轨道 ——
                        cover_hist_md = gr.Markdown(_t("### 翻唱任务历史"))
                        _reg(cover_hist_md, lambda lang: gr.update(value=tr(lang, "### 翻唱任务历史")))
                        cover_history_dd = gr.Dropdown(
                            choices=_voice_task_history_choices("cover"),
                            label=_t("选择翻唱任务"), interactive=True)
                        _reg(cover_history_dd, lambda lang: gr.update(
                            choices=_voice_task_history_choices("cover", lang),
                            label=tr(lang, "选择翻唱任务")))
                        cover_selected_task = gr.State(value=None)
                        # 历史回放播放器组：选中任务后按文件夹填充全部轨道（每轨可下载）
                        cover_hist_audios = [
                            gr.Audio(type="filepath", label="", elem_id=f"cover-history-audio-{i}",
                                     visible=False)
                            for i in range(VOICE_PLAYER_COUNT)
                        ]
                        # 项目管理（文件管理重构）：改项目名（保留时间戳）/ 删除项目（整目录入回收站）
                        with gr.Row(elem_id="cover-rename-row"):
                            cover_rename_input = gr.Textbox(
                                label=_t("新项目名"), placeholder=_t("留空则清除项目名"),
                                scale=3, lines=1)
                            _reg(cover_rename_input, lambda lang: gr.update(
                                label=tr(lang, "新项目名"), placeholder=tr(lang, "留空则清除项目名")))
                            cover_rename_btn = gr.Button(_t("改项目名"), size="sm", scale=1)
                            _reg(cover_rename_btn, lambda lang: gr.update(value=tr(lang, "改项目名")))
                            cover_del_btn = gr.Button(_t("删除项目"), variant="stop", size="sm", scale=1)
                            _reg(cover_del_btn, lambda lang: gr.update(value=tr(lang, "删除项目")))

                # 事件绑定
                cover_src_mode.change(fn=on_voice_src_mode, inputs=cover_src_mode,
                                      outputs=[cover_src_history, cover_src_upload])
                cover_ref_mode.change(fn=on_voice_ref_mode, inputs=cover_ref_mode,
                                    outputs=[cover_ref_dropdown, cover_ref_dry_panel,
                                             cover_ref_upload_panel])
                cover_ref_dry_src.change(fn=on_voice_dry_src_mode, inputs=cover_ref_dry_src,
                                       outputs=[cover_ref_dry_upload, cover_ref_dry_sep])
                cover_ref_save_btn.click(fn=on_voice_save_ref, inputs=[cover_ref_upload, cover_ref_name],
                                         outputs=[cover_ref_info, cover_ref_dropdown])
                # 上传参考干声后自动轻量人声检测（方案B，仅提示）+ 通过者留存并入干声历史
                cover_ref_upload.change(fn=on_voice_ref_upload_check,
                                        inputs=cover_ref_upload,
                                        outputs=[cover_ref_check_md, cover_ref_dry_upload])
                cover_semi_orig.click(fn=lambda: 0, outputs=cover_semi)
                cover_semi_m12.click(fn=lambda: -12, outputs=cover_semi)
                cover_semi_p12.click(fn=lambda: 12, outputs=cover_semi)
                cover_btn.click(fn=on_voice_cover,
                                inputs=[cover_src_history, cover_src_upload,
                                        cover_ref_dropdown, cover_ref_dry_upload,
                                        cover_ref_dry_sep,
                                        cover_ref_upload,
                                        cover_semi, cover_steps, cover_gain, cover_acc_dd,
                                        cover_denoise, cover_ref_seg],
                                outputs=[*cover_audios, cover_info, cover_btn, cover_history_dd])
                # 取消按钮：协作式取消本 Tab 排队中/运行中的任务（info 区反馈结果）
                cover_cancel_btn.click(fn=lambda: on_voice_cancel("cover"),
                                       outputs=[cover_info])
                # 音色库/素材库选择：选中即试听（管理功能在分离页库管理区）
                cover_ref_dropdown.change(fn=lambda p: gr.update(value=p, visible=bool(p)),
                                          inputs=cover_ref_dropdown,
                                          outputs=[cover_ref_preview])
                cover_acc_dd.change(fn=lambda p: gr.update(value=p, visible=bool(p)),
                                    inputs=cover_acc_dd, outputs=[cover_acc_preview])

                # 库管理（分离页）：选中即试听；删除/重命名（回收站）后刷新下拉
                lib_stem_dd.change(fn=lambda p: gr.update(value=p, visible=bool(p)),
                                   inputs=lib_stem_dd, outputs=[lib_stem_preview])
                lib_stem_del_btn.click(fn=on_voice_stem_delete, inputs=[lib_stem_dd],
                                       outputs=[lib_stem_dd])
                lib_stem_rename_btn.click(fn=on_voice_stem_rename,
                                          inputs=[lib_stem_dd, lib_stem_rename_input],
                                          outputs=[lib_stem_dd])
                lib_ref_dd.change(fn=lambda p: gr.update(value=p, visible=bool(p)),
                                  inputs=lib_ref_dd, outputs=[lib_ref_preview])
                lib_ref_del_btn.click(fn=on_voice_ref_delete, inputs=[lib_ref_dd],
                                      outputs=[lib_ref_preview, lib_ref_dd])
                lib_ref_rename_btn.click(fn=on_voice_ref_rename,
                                         inputs=[lib_ref_dd, lib_ref_rename_input],
                                         outputs=[lib_ref_preview, lib_ref_dd])

                # 任务历史回放（按文件夹）：选任务 → 整组播放器填充全部轨道
                sep_history_dd.change(fn=on_voice_task_history_pick, inputs=sep_history_dd,
                                      outputs=[*sep_hist_audios])
                # 额外绑定：用户手动选下拉时，同步更新隐藏 State 组件
                # （后续删除/改名按钮从 State 读 task_id，避免 Dropdown 被重置）
                sep_history_dd.change(fn=lambda tid: tid, inputs=sep_history_dd,
                                      outputs=sep_selected_task)
                cover_history_dd.change(fn=on_voice_task_history_pick, inputs=cover_history_dd,
                                        outputs=[*cover_hist_audios])
                cover_history_dd.change(fn=lambda tid: tid, inputs=cover_history_dd,
                                        outputs=cover_selected_task)

                # 项目管理（文件管理重构）：改项目名（重命名文件保留时间戳）/ 删除项目（整目录入回收站）
                # 关键修复：inputs 用隐藏 State 组件而非 Dropdown，
                # 因为 Gradio 6 中 Tab 切换更新 choices 会把 Dropdown.value 重置为 None
                sep_rename_btn.click(fn=lambda tid, name: on_voice_task_rename(tid, name, "separation"),
                                     inputs=[sep_selected_task, sep_rename_input],
                                     outputs=[sep_history_dd, sep_selected_task, *sep_hist_audios, sep_rename_input])
                sep_del_btn.click(fn=lambda tid: on_voice_task_delete(tid, "separation"),
                                  inputs=sep_selected_task,
                                  js=_DEL_PROJECT_CONFIRM_JS,
                                  outputs=[sep_history_dd, sep_selected_task, *sep_hist_audios])
                cover_rename_btn.click(fn=lambda tid, name: on_voice_task_rename(tid, name, "cover"),
                                       inputs=[cover_selected_task, cover_rename_input],
                                       outputs=[cover_history_dd, cover_selected_task, *cover_hist_audios, cover_rename_input])
                cover_del_btn.click(fn=lambda tid: on_voice_task_delete(tid, "cover"),
                                    inputs=cover_selected_task,
                                    js=_DEL_PROJECT_CONFIRM_JS,
                                    outputs=[cover_history_dd, cover_selected_task, *cover_hist_audios])

                # 每次切到分离 Tab 时刷新源下拉 + 分离任务历史 + 库管理两下拉
                # （翻唱页删除后保持同步；新生成的歌曲也要能立即作为分离源，无需刷新页面）
                # 注意：Gradio 6 中 gr.update(choices=...) 不传 value 会把 Dropdown 值重置，
                # 必须用 _dd_update 同时传递 value=首项值。
                # 同时更新隐藏 State：保存第一条任务记录的 task_id，供删除/改名按钮正确读取
                tab_sep.select(fn=lambda: (
                    _dd_update(_voice_source_history_choices(_CUR_LANG)),
                    _dd_update(_voice_task_history_choices("separation")),
                    _dd_update(_voice_stem_choices(_CUR_LANG)),
                    _dd_update(_voice_ref_choices(_CUR_LANG)),
                    (_voice_task_history_choices("separation")[0][1]
                     if _voice_task_history_choices("separation") else None)),
                               outputs=[sep_src_history, sep_history_dd, lib_stem_dd, lib_ref_dd,
                                        sep_selected_task])
                # 每次切到翻唱 Tab 时刷新翻唱源/音色库/伴奏/干声两来源/翻唱历史下拉
                tab_cover.select(fn=lambda: (
                    _dd_update(_voice_cover_source_choices(_CUR_LANG)),
                    _dd_update(_voice_ref_choices(_CUR_LANG)),
                    _dd_update(_voice_stem_choices(_CUR_LANG)),
                    _dd_update(_voice_dry_sep_choices(_CUR_LANG)),
                    _dd_update(_voice_dry_upload_choices(_CUR_LANG)),
                    _dd_update(_voice_task_history_choices("cover")),
                    (_voice_task_history_choices("cover")[0][1]
                     if _voice_task_history_choices("cover") else None)),
                                 outputs=[cover_src_history, cover_ref_dropdown, cover_acc_dd,
                                          cover_ref_dry_sep, cover_ref_dry_upload,
                                          cover_history_dd, cover_selected_task])

            with gr.Tab(_t("系统设置")) as tab_settings:
                _reg(tab_settings, lambda lang: gr.update(label=tr(lang, "系统设置")))
                # 左右分栏：左列系统状态（模型检查），右列当前队列；下方参数预设整行
                with gr.Row():
                    with gr.Column(scale=3, elem_classes="settings-panel"):
                        # 标题与「检查模型」按钮同行，压缩纵向占用
                        with gr.Row():
                            sysstatus_md = gr.Markdown(_t("### 系统状态"))
                            _reg(sysstatus_md, lambda lang: gr.update(value=tr(lang, "### 系统状态")))
                            check_models_btn = gr.Button(_t("检查模型"), scale=0, min_width=110, size="sm")
                            _reg(check_models_btn, lambda lang: gr.update(value=tr(lang, "检查模型")))
                        model_status = gr.Markdown(value=on_check_models())
                        # 切语言时重新渲染模型状态（apply_lang 先更新 _CUR_LANG 再执行 updater）
                        _reg(model_status, lambda lang: gr.update(value=on_check_models()))
                        check_models_btn.click(fn=on_check_models, outputs=model_status)

                    with gr.Column(scale=2, elem_classes="settings-panel"):
                        # 当前队列状态窗口：Timer 每 2s 轮询只读快照（不影响任务进度流）
                        queue_md = gr.Markdown(_t("### 当前队列"))
                        _reg(queue_md, lambda lang: gr.update(value=tr(lang, "### 当前队列")))
                        queue_status_md = gr.Markdown(value=_queue_status_html())
                        # 切语言即时重渲染（apply_lang 先更新 _CUR_LANG 再执行 updater）
                        _reg(queue_status_md, lambda lang: gr.update(value=_queue_status_html()))
                        queue_timer = gr.Timer(2.0)
                        queue_timer.tick(fn=_queue_status_html, outputs=[queue_status_md])

                # 参数预设区：Group 承载卡片外框（与上方两列同样式）
                with gr.Group(elem_classes="settings-panel"):
                    presets_md = gr.Markdown(_t("### 参数预设"))
                    _reg(presets_md, lambda lang: gr.update(value=tr(lang, "### 参数预设")))
                    # 下拉与「加载」按钮同行、名称输入与「保存当前参数」按钮同行（紧凑化）
                    with gr.Row():
                        preset_dropdown = gr.Dropdown(
                            label=_t("加载预设"),
                            choices=_preset_display_names(_CUR_LANG),
                            value=None,
                            scale=4,
                        )
                        _reg(preset_dropdown, lambda lang: gr.update(label=tr(lang, "加载预设"), choices=_preset_display_names(lang)))
                        preset_load_btn = gr.Button(_t("加载"), scale=1)
                        _reg(preset_load_btn, lambda lang: gr.update(value=tr(lang, "加载")))
                    with gr.Row():
                        preset_name_input = gr.Textbox(label=_t("保存预设名称"), placeholder=_t("我的预设"), scale=4)
                        _reg(preset_name_input, lambda lang: gr.update(label=tr(lang, "保存预设名称"), placeholder=tr(lang, "我的预设")))
                        preset_save_btn = gr.Button(_t("保存当前参数"), scale=1)
                        _reg(preset_save_btn, lambda lang: gr.update(value=tr(lang, "保存当前参数")))
                    preset_info = gr.Markdown()

        lyrics_input.change(fn=on_lyrics_change, inputs=lyrics_input, outputs=structure_analysis)

        random_seed_btn.click(fn=on_random_seed, outputs=seed_input)

        generate_btn.click(
            fn=on_generate,
            inputs=[
                project_input,
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
                cfg_input, batch_count_input,
                normalize_checkbox, fade_checkbox, trim_checkbox, metadata_checkbox,
            ],
        )
        preset_save_btn.click(
            fn=on_preset_save,
            inputs=[
                preset_name_input, cot_input, steps_input, out_format_input,
                abc_temp_input, abc_top_p_input, abc_top_k_input, abc_rep_input, abc_pen_window_input, abc_min_tok_input, abc_max_tok_input,
                sem_temp_input, sem_top_p_input, sem_top_k_input, sem_rep_input, sem_pen_window_input, sem_min_tok_input, sem_max_tok_input,
                cfg_input, batch_count_input,
                normalize_checkbox, fade_checkbox, trim_checkbox, metadata_checkbox,
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
<script src="/static/js/app.js?v=12"></script>
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

        # —— 多轨混音（M2）：静态编辑页 + JSON 接口 ——
        # 编辑页为单文件自包含（CSS/JS 内联），no-store 避免改版后命中旧缓存
        from starlette.responses import JSONResponse
        import mix_web

        def _mix_page(request):
            return FileResponse(
                WEBUI_ROOT / "static" / "multitrack" / "index.html",
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        demo.app.routes.insert(0, Route("/static/multitrack/", _mix_page, methods=["GET"]))
        demo.app.routes.insert(0, Route("/static/multitrack", _mix_page, methods=["GET"]))

        async def _mix_sources(request):
            """素材清单 + 已有混音记录 + 编辑页文案（文案统一由后端 i18n 下发）。"""
            try:
                data = mix_web.list_sources(history_mgr, WEBUI_ROOT)
            except Exception as e:
                logger.exception("混音素材清单读取失败")
                data = {"ok": False, "error": f"素材清单读取失败: {e}", "sources": [], "mixes": []}
            data["lang"] = _CUR_LANG
            data["strings"] = mix_web.page_texts(_CUR_LANG)
            return JSONResponse(data)

        async def _mix_peaks(request):
            """按 outputs/ 白名单解析素材并返回波形峰值（ffmpeg 解码分桶）。"""
            path = mix_web.resolve_audio(WEBUI_ROOT, request.query_params.get("path", ""))
            if path is None:
                return JSONResponse({"ok": False, "error": "音频不存在或路径越界"})
            try:
                buckets = int(request.query_params.get("buckets", "0") or 0)
            except ValueError:
                buckets = 0
            try:
                info = mix_web.compute_peaks(path, buckets or mix_web.PEAK_BUCKETS_DEFAULT)
            except Exception as e:
                logger.exception("波形峰值计算失败")
                return JSONResponse({"ok": False, "error": f"波形读取失败: {e}"})
            return JSONResponse({"ok": True, **info})

        async def _mix_audio(request):
            """下发 outputs/ 下的音频（供波形页试听与下载）。"""
            path = mix_web.resolve_audio(WEBUI_ROOT, request.query_params.get("path", ""))
            if path is None:
                return JSONResponse({"ok": False, "error": "音频不存在或路径越界"}, status_code=404)
            download = request.query_params.get("download") == "1"
            return FileResponse(
                path,
                media_type=mix_web.audio_mime(path),
                filename=path.name if download else None,
            )

        async def _mix_render(request):
            """提交多轨混音渲染任务（工程 JSON 走请求体，项目名走 query）。"""
            try:
                payload = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"})
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "混音工程必须是 JSON 对象"})
            return JSONResponse(mix_web.submit_render(
                payload, WEBUI_ROOT, history_mgr,
                request.query_params.get("project", "")))

        async def _mix_status(request):
            return JSONResponse(mix_web.task_status(
                request.query_params.get("task_id", ""), WEBUI_ROOT))

        async def _mix_cancel(request):
            try:
                body = await request.json()
            except Exception:
                body = {}
            task_id = (body or {}).get("task_id", "") if isinstance(body, dict) else ""
            return JSONResponse(mix_web.cancel_render(task_id))

        for _path, _ep, _methods in (
            ("/api/mix/sources", _mix_sources, ["GET"]),
            ("/api/mix/peaks", _mix_peaks, ["GET"]),
            ("/api/mix/audio", _mix_audio, ["GET"]),
            ("/api/mix/render", _mix_render, ["POST"]),
            ("/api/mix/status", _mix_status, ["GET"]),
            ("/api/mix/cancel", _mix_cancel, ["POST"]),
        ):
            demo.app.routes.insert(0, Route(_path, _ep, methods=_methods))
    threading.Thread(target=_register_custom_routes, daemon=True).start()

    demo.launch(
        server_name="127.0.0.1",
        server_port=9898,
        share=False,
        show_error=True,
    )
