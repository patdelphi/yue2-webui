"""生成页回调组（C1 拆分 app.py：第三阶段 · 生成组）。

程序说明
--------
本模块承载「歌曲生成 / 重新合成 / 批量变体 / 取消 / 使用上一次」相关回调。
依赖全部由 app.py 在调用时以 :class:`GenDeps` 注入（本模块不 import app.py，
避免循环依赖），因此 app.py 侧的猴子补丁（WEBUI_ROOT / backend / history_mgr /
refresh_history / LAST_INPUTS_FILE 等）在调用时仍然生效。

app.py 保留同名薄封装：Gradio 的 inputs/outputs 绑定与函数签名完全不变，
既有测试与前端绑定无需改动。
"""
import html
import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import gradio as gr

from config import (GenerationParams, CotMode, SamplingParams, OutFormat,
                    validate_params)
from history import HistoryRecord, sanitize_project, recycle_dir
from postprocess import postprocess_audio
from queue_manager import (queue_manager, TaskType, TaskStatus,
                           TaskCancelledError)
from i18n import tr
from app_utils import FORMAT_LABELS, strip_comment_lines
from style_presets import STYLE_PRESETS
from vocal_presets import (VOCAL_PRESETS, INSTRUMENT_PRESETS, MOOD_PRESETS,
                           LANGUAGE_PRESETS, GENRE_PRESETS)
from lyrics_templates import LYRICS_TEMPLATES

logger = logging.getLogger(__name__)


@dataclass
class GenDeps:
    """生成组回调的外部依赖（app.py 调用时注入当前全局值，保证猴子补丁生效）。"""
    webui_root: Path          # app.WEBUI_ROOT
    backend: object           # app.backend
    history_mgr: object       # app.history_mgr
    cur_lang: str             # app._CUR_LANG
    refresh_history: object   # app.refresh_history
    refresh_history_full: object  # app.refresh_history_full
    prefer_mp3: object        # app._prefer_mp3
    save_last_inputs: object  # app._save_last_inputs
    update_last_abc: object   # app._update_last_abc
    load_last_inputs: object  # app._load_last_inputs
    generate_worker: object   # app._generate_worker（薄封装，供入队）
    resynthesize_worker: object   # app._resynthesize_worker（薄封装，供入队）
    register_task: object     # app._register_task
    unregister_task: object   # app._unregister_task
    localize_task_error: object   # app._localize_task_error
    active_tasks: dict        # app._active_tasks
    active_tasks_lock: object  # app._active_tasks_lock


# ---------------------------------------------------------------------------
# 生成入口
# ---------------------------------------------------------------------------

def on_generate(
    d: GenDeps,
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
    lang = d.cur_lang

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
        d.generate_worker,
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

    d.register_task("generation", task.task_id)
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
                error_msg = d.localize_task_error(lang, status_info.get("error")) or tr(lang, "未知错误")
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

        result = task.result
        if isinstance(result, GenerationOutcome):
            result = result.to_gradio()
        elif not isinstance(result, (tuple, list)):
            result = [result]
        return (*result, seed)
    except gr.Error:
        raise
    except Exception as e:
        logger.exception(f"生成失败: {e}")
        raise
    finally:
        d.unregister_task("generation", task.task_id)


def _recycle_output_dir(output_dir: Path) -> None:
    """回收生成失败的孤儿产物目录（转交 history.recycle_dir，异常仅记日志）。

    与 voice 侧 _recycle_created_derived 行为一致（不 rm 整目录，保证可还原）；
    回收失败不掩盖原始生成错误。
    """
    try:
        recycle_dir(output_dir)
    except Exception:
        logger.exception("孤儿生成目录回收失败(已忽略): %s", output_dir)


@dataclass
class GenerationOutcome:
    """生成 worker 的返回值结构（替代易错位的 12 元组）。

    字段顺序与 on_generate 的 outputs 绑定一一对应；to_gradio() 按序展开，
    UI 侧无需再数位置。单变体与多变体仅在 variants_* 三项上有差异。
    """
    audio_path: str                  # 主音频（优先 MP3 预览件）
    duration_info: str               # 时长/耗时展示文案
    abc_display: str                 # ABC 乐谱文本
    abc_download: object             # ABC 文件下载路径（可为 None）
    mp3_download: object             # MP3 下载路径（多变体时为 None，走变体选择器）
    lyrics_data_html: str            # 歌词同步数据（HTML）
    history_rows: object             # 历史表格行
    history_info: str                # 历史分页信息
    history_page: int                # 历史页码（重置为 0）
    variants_visible: object         # 变体分组显隐（gr.update）
    variants_dropdown: object        # 变体选择器（gr.update）
    variants_payload: object         # 变体载荷（供 on_variant_select 使用）

    def to_gradio(self) -> tuple:
        """按 Gradio outputs 顺序展开为元组。"""
        return (self.audio_path, self.duration_info, self.abc_display,
                self.abc_download, self.mp3_download, self.lyrics_data_html,
                self.history_rows, self.history_info, self.history_page,
                self.variants_visible, self.variants_dropdown, self.variants_payload)


def generate_worker(
    d: GenDeps,
    _task,
    project, style, lyrics, cot, seeds, cfg_scale, num_inference_steps, out_format, batch_count,
    normalize, fade, trim, metadata, abc_text,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    lang="zh",
):
    """Worker function that runs in the queue thread. Returns the result tuple."""

    # 防御：种子列表与批量数对齐（UI 已保证成对；worker 层兜底，避免 seeds[i] 越界）。
    # 种子为空时给一个随机种子，批量数不超过实际种子数。
    seeds = [int(s) for s in (seeds or [])] or [random.randint(0, 2**31 - 1)]
    batch_count = max(1, min(int(batch_count), len(seeds)))

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

    d.save_last_inputs(params.style, params.lyrics, abc_text or "")

    # 项目名清洗 + 产物命名规范（文件管理重构）：
    #   目录 = outputs/song_<时间戳>/；文件 = <项目名>_<时间戳>[_varN].<ext>（项目名空则时间戳开头）
    project = sanitize_project(project or "")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_task_id = f"song_{timestamp}"           # 目录名 / 历史 task_id（不含项目名，改名不动目录）
    file_stem = f"{project}_{timestamp}" if project else timestamp  # 产物文件名主干
    output_dir = d.webui_root / "outputs" / base_task_id
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

    results = []

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

        result = d.backend.generate(
            params=params,
            output_dir=variant_dir,
            on_progress=on_progress,
            cancel_event=_task.cancel_event,
            output_name=output_name,
            lang=lang,
        )
        results.append((task_id, output_name, variant_dir, result, current_seed))

    successful = [(tid, fname, d_, r, s) for tid, fname, d_, r, s in results if r.success]
    if not successful:
        if _task.cancel_event.is_set():
            raise TaskCancelledError(tr(lang, "任务已取消"))
        # 全部变体失败（非取消）：回收本次生成的孤儿目录（含半成品），避免残留且无历史记录
        _recycle_output_dir(output_dir)
        failed_msg = results[0][3].error_message if results else tr(lang, "未知错误")
        raise ValueError(f"{tr(lang, '生成失败')}: {failed_msg}")

    # 生成成功：用产出的乐谱（最后一个成功变体）更新「使用上一次」记录，
    # 否则记录停留在任务开始时外部输入框的内容（正常生成时为空）
    last_abc = successful[-1][3].abc_score or (abc_text or "")
    d.update_last_abc(last_abc)

    format_label = FORMAT_LABELS.get(params.out_format.value, "PCM 16-bit")

    if any([normalize, fade, trim, metadata]):
        for idx, (task_id, fname, variant_dir, result, variant_seed) in enumerate(successful):
            wav_path = Path(result.audio_path)
            if wav_path.exists():
                _task.push_progress(0, tr(lang, "后处理音频..."))
                # 携带完整生成/采样参数写入 sidecar JSON（cfg/ODE/批量/采样等）
                try:
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
                            "model_gguf": d.backend.main_model,
                            "vae_gguf": d.backend.vae_model,
                            "abc_sampling": asdict(params.abc_sampling),
                            "semantic_sampling": asdict(params.semantic_sampling),
                        },
                    )
                except Exception:
                    # 后处理失败不阻断历史写入：音频已落盘，继续登记记录，避免孤儿文件
                    logger.exception("后处理失败(已忽略，音频已落盘): %s", wav_path)
                    continue
                if result.mp3_path:
                    new_mp3 = d.backend.re_export_mp3(wav_path)
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
            abc_path = str(abc_file.relative_to(d.webui_root))

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
            output_dir=str(variant_dir.relative_to(d.webui_root)),
            abc_path=abc_path,
            out_format=params.out_format.value,
            project=project,
        )
        d.history_mgr.append(record)
    d.history_mgr.auto_prune()

    first_result = successful[0][3]
    first_fname = successful[0][1]
    first_dir = successful[0][2]
    abc_display = first_result.abc_score or ""
    abc_download = str(first_dir / f"{first_fname}.abc") if first_result.abc_score else None

    if batch_count == 1:
        mp3_download = first_result.mp3_path
        h_rows, h_info = d.refresh_history()
        return GenerationOutcome(
            audio_path=d.prefer_mp3(str(first_result.audio_path)),
            duration_info=duration_info, abc_display=abc_display, abc_download=abc_download,
            mp3_download=mp3_download, lyrics_data_html=lyrics_data_html,
            history_rows=h_rows, history_info=h_info, history_page=0,
            variants_visible=gr.update(visible=False),
            variants_dropdown=gr.update(visible=False, choices=[], value=None),
            variants_payload=[],
        )
    else:
        variants_payload = []
        for idx, (task_id, fname, variant_dir, result, variant_seed) in enumerate(successful, start=1):
            variants_payload.append({
                "label": f"{tr(lang, '变体')}{idx} (seed={variant_seed}, {result.audio_duration_seconds or 0:.1f}s)",
                "task_id": task_id,
                "audio_path": d.prefer_mp3(str(result.audio_path)),
                "abc_text": result.abc_score or "",
                "abc_file": str(variant_dir / f"{fname}.abc") if result.abc_score else None,
                "mp3_file": result.mp3_path,
            })

        audio_paths = [d.prefer_mp3(str(r.audio_path)) for _, _, _, r, _ in successful]
        h_rows, h_info = d.refresh_history()
        return GenerationOutcome(
            audio_path=audio_paths[0],
            duration_info=duration_info, abc_display=abc_display, abc_download=abc_download,
            mp3_download=None, lyrics_data_html=lyrics_data_html,
            history_rows=h_rows, history_info=h_info, history_page=0,
            variants_visible=gr.update(visible=True),
            variants_dropdown=gr.update(visible=True,
                                        choices=[v["label"] for v in variants_payload],
                                        value=variants_payload[0]["label"]),
            variants_payload=variants_payload,
        )


# ---------------------------------------------------------------------------
# 「使用上一次」与批量变体
# ---------------------------------------------------------------------------

def on_restore_last(d: GenDeps, kind: str, current: str):
    """Fill an input box with the last-saved text of the same kind."""
    value = d.load_last_inputs().get(kind, "")
    return value if value else (current or "")


def on_variant_select(label, payload):
    """Switch main outputs to the selected batch variant."""
    for v in payload:
        if v["label"] == label:
            return v["audio_path"], v["abc_text"], v["abc_file"], v["mp3_file"]
    # Label/state desync (e.g. after a page reload or while the selector is
    # being reset) — keep the current outputs instead of erroring.
    return gr.update(), gr.update(), gr.update(), gr.update()


def on_variant_finalize(d: GenDeps, label, payload):
    """Mark selected variant as final and delete the others."""
    return _finalize_variant(d, label, payload, keep_all=False)


def on_variant_keep_all(d: GenDeps, label, payload):
    """Mark selected variant as final but keep all variants."""
    return _finalize_variant(d, label, payload, keep_all=True)


def _finalize_variant(d: GenDeps, label, payload, keep_all):
    if not payload or not label:
        raise gr.Error(tr(d.cur_lang, "没有可用的批量变体"))
    selected = next((v for v in payload if v["label"] == label), None)
    if not selected:
        raise gr.Error(tr(d.cur_lang, "变体不存在"))

    d.history_mgr.set_status(selected["task_id"], "final")
    removed = 0
    if not keep_all:
        for v in payload:
            if v["task_id"] != selected["task_id"]:
                d.history_mgr.delete(v["task_id"])
                removed += 1

    h_rows, h_info, _ = d.refresh_history_full()
    lang = d.cur_lang
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


def on_cancel(d: GenDeps):
    """Cancel button callback."""
    with d.active_tasks_lock:
        task_ids = d.active_tasks.pop("generation", set())
    for task_id in task_ids:
        queue_manager.cancel_task_by_id(task_id)
    if task_ids:
        return tr(d.cur_lang, "正在取消...")
    return tr(d.cur_lang, "没有正在运行的任务")


# ---------------------------------------------------------------------------
# 重新合成
# ---------------------------------------------------------------------------

def on_resynthesize(
    d: GenDeps,
    abc_text, style, lyrics, seed, cfg_scale, num_inference_steps, out_format,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress=gr.Progress(track_tqdm=False),
):
    """Resynthesize with edited ABC score - submits to queue."""
    lang = d.cur_lang
    seed = seed if seed is not None else 831001
    cfg_scale = cfg_scale if cfg_scale is not None else 0
    num_inference_steps = num_inference_steps if num_inference_steps is not None else 8

    if not abc_text or not abc_text.strip():
        raise gr.Error(tr(d.cur_lang, "ABC 乐谱不能为空"))

    lyrics = strip_comment_lines(lyrics)

    task = queue_manager.submit(
        TaskType.GENERATION,
        d.resynthesize_worker,
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

    d.register_task("generation", task.task_id)
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
                error_msg = d.localize_task_error(lang, status_info.get("error")) or tr(lang, "未知错误")
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
        d.unregister_task("generation", task.task_id)


def resynthesize_worker(
    d: GenDeps,
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

    d.save_last_inputs(params.style, params.lyrics, abc_text or "")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"song_{timestamp}"  # 重新合成也走 song_<ts> 项目目录（无项目名，文件以时间戳开头）
    output_dir = d.webui_root / "outputs" / task_id
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

    result = d.backend.generate(
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
            output_dir=str(output_dir.relative_to(d.webui_root)),
            abc_path="",
            status="resynthesized",
            out_format=params.out_format.value,
        )
        d.history_mgr.append(record)
        d.history_mgr.auto_prune()

        lyrics_file = output_dir / f"{timestamp}.txt"
        lyrics_file.write_text(params.lyrics, encoding="utf-8")

        # 重新合成成功：产出乐谱更新「使用上一次」记录
        d.update_last_abc(result.abc_score or abc_text or "")

        abc_download = str(output_dir / f"{output_dir.name}.abc") if result.abc_score else None
        mp3_download = result.mp3_path
        resynth_lyrics_data = f'<div class="gen-lyrics-data" style="display:none" data-lyrics=\'{html.escape(json.dumps(params.lyrics, ensure_ascii=False), quote=True)}\' data-duration="{result.audio_duration_seconds}"></div>'
        return d.prefer_mp3(str(result.audio_path)), duration_info, abc_download, mp3_download, resynth_lyrics_data
    else:
        if _task.cancel_event.is_set():
            raise TaskCancelledError(tr(lang, "任务已取消"))
        raise ValueError(f"{tr(lang, '重新合成失败')}：{result.error_message}")


# ---------------------------------------------------------------------------
# 创作页小回调（无状态，app.py 直接 re-export）
# ---------------------------------------------------------------------------

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
