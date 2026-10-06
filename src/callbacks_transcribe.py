"""转谱页回调组（C1 拆分 app.py：第三阶段 · 转谱组）。

程序说明
--------
本模块承载「上传音频 → SheetSage2 转谱 → 发送乐谱到生成页」相关回调。
依赖全部由 app.py 在调用时以 :class:`TranscribeDeps` 注入（本模块不 import app.py，
避免循环依赖），因此 app.py 侧的猴子补丁（WEBUI_ROOT / backend / _CUR_LANG /
voice_handlers 等）在调用时仍然生效。

app.py 保留同名薄封装：Gradio 的 inputs/outputs 绑定与函数签名完全不变，
既有测试与前端绑定无需改动。
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import gradio as gr

from i18n import tr
from queue_manager import queue_manager, TaskType, TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class TranscribeDeps:
    """转谱组回调的外部依赖（app.py 调用时注入当前全局值，保证猴子补丁生效）。"""
    webui_root: Path          # app.WEBUI_ROOT
    backend: object           # app.backend
    cur_lang: str             # app._CUR_LANG
    voice_handlers: object    # app.voice_handlers（上传统一留存）
    transcribe_worker: object  # app._transcribe_worker（薄封装，供入队）
    register_task: object     # app._register_task
    unregister_task: object   # app._unregister_task
    localize_task_error: object  # app._localize_task_error


# ---------------------------------------------------------------------------
# 转谱入口
# ---------------------------------------------------------------------------

def on_transcribe(d: TranscribeDeps, audio_file, progress=gr.Progress(track_tqdm=False)):
    """Transcribe audio to ABC score using SheetSage2 - submits to queue."""
    lang = d.cur_lang
    if not audio_file:
        raise gr.Error(tr(d.cur_lang, "请先上传音频文件"))

    audio_path = Path(audio_file)
    if not audio_path.exists():
        raise gr.Error(tr(d.cur_lang, "音频文件不存在"))

    supported_formats = {'.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac', '.wma'}
    if audio_path.suffix.lower() not in supported_formats:
        raise gr.Error(f"{tr(d.cur_lang, '不支持的音频格式')}：{audio_path.suffix}。{tr(d.cur_lang, '支持的格式')}：{', '.join(sorted(supported_formats))}")

    task = queue_manager.submit(
        TaskType.TRANSCRIPTION,
        d.transcribe_worker,
        lang=lang,
        audio_path=str(audio_path),
    )

    d.register_task("transcription", task.task_id)
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
                error_msg = d.localize_task_error(lang, status_info.get("error")) or tr(lang, "未知错误")
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
        d.unregister_task("transcription", task.task_id)


def transcribe_worker(d: TranscribeDeps, _task, audio_path, lang="zh"):
    """Worker function for transcription, runs in queue thread."""
    audio_path = Path(audio_path)
    # 上传源统一留存（文件管理重构）：uploads/<源名>_<时间戳>_transcribe.<ext>，
    # Gradio 临时文件会被清理，不留存则历史无法追溯源音频
    d.voice_handlers.persist_upload(str(audio_path), "transcribe")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"transcribe_{timestamp}"
    output_dir = d.webui_root / "outputs" / task_id

    def on_progress(p):
        if p and p.get("message"):
            _task.push_progress(p.get("progress", 0), p["message"])

    result = d.backend.transcribe(audio_path, output_dir, on_progress=on_progress, lang=lang)

    if result.success:
        abc_score = result.abc_score or ""
        midi_path = result.midi_path
        info = f"{tr(lang, '转谱耗时')} **{result.transcription_time_seconds:.1f}s**"

        abc_file = output_dir / f"{output_dir.name}.abc"
        abc_file_path = str(abc_file.relative_to(d.webui_root)) if abc_file.exists() else ""

        return abc_score, info, abc_file_path, midi_path, task_id
    else:
        raise ValueError(f"{tr(lang, '转谱失败')}：{result.error_message}")


def on_send_to_generate(d: TranscribeDeps, abc_text):
    """Send ABC score to generation tab via bridge."""
    if not abc_text or not abc_text.strip():
        raise gr.Error(tr(d.cur_lang, "没有可发送的乐谱内容"))
    return abc_text
