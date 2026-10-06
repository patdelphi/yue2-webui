"""YuE2 Music Studio - Gradio WebUI for YuE2 Music Generation."""
import gradio as gr
import html
import random
import json
import shutil
import threading
import time
import logging
from dataclasses import asdict, dataclass
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
from history import HistoryManager, HistoryRecord, sanitize_project, _FILENAME_TS_RE, recycle_dir
from postprocess import postprocess_audio
from queue_manager import queue_manager, TaskType, TaskStatus, TaskCancelledError, TIMEOUT_ERROR
from i18n import tr, normalize_lang
from voice_client import VoiceClient, check_voice_models
from voice_ui_handlers import VoiceHandlers, detect_voice, _make_preview
# C1：生成/设置页的纯常量与无状态工具已抽到 src/app_utils.py（此处导入保持既有名字可用）
from app_utils import (
    BUILTIN_PRESETS, PRESET_PARAM_KEYS, FORMAT_LABELS, COMMENT_PREFIXES,
    strip_comment_lines,
)
# C1 第三阶段：回调按业务域拆到 src/callbacks_*.py，app.py 保留同名薄封装
import callbacks_generate
import callbacks_settings
import callbacks_transcribe
import callbacks_history
import callbacks_voice
# C1 Phase 2：UI 构建（CSS/JS/build_ui）抽到 src/ui_tabs.py，app.py 保留 build_ui 薄封装
import ui_tabs

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


# 队列外同步阶段的取消事件（如翻唱前的 ensure_separation）：
# 该阶段尚未提交队列任务、没有 task_id，故单独登记 channel → Event 供取消按钮触发
_pending_cancel: dict = {}


def _set_pending_cancel(channel: str, event: threading.Event) -> None:
    """登记队列外同步阶段的取消事件（同一 channel 后写覆盖）。"""
    with _active_tasks_lock:
        _pending_cancel[channel] = event


def _clear_pending_cancel(channel: str) -> None:
    with _active_tasks_lock:
        _pending_cancel.pop(channel, None)

# 注：BUILTIN_PRESETS / PRESET_PARAM_KEYS / FORMAT_LABELS / COMMENT_PREFIXES /
# strip_comment_lines 已抽到 src/app_utils.py，经文件顶部导入在本模块可直接使用。


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


def _localize_task_error(lang: str, error) -> str:
    """把队列层写入的哨兵错误翻译为当前语言（如任务级超时）；普通异常文本原样返回。

    queue_manager 不感知界面语言，故以 TIMEOUT_ERROR 哨兵表示超时，
    由 UI 层在此统一翻译，避免队列层硬编码面向用户的中文。
    """
    if error == TIMEOUT_ERROR:
        return tr(lang, "任务超时")
    return error or ""


# ---------------------------------------------------------------------------
# 生成页回调（C1：实现见 src/callbacks_generate.py，依赖在调用时注入当前全局）
# ---------------------------------------------------------------------------

def _gen_deps() -> "callbacks_generate.GenDeps":
    """构建生成组依赖（调用时读取当前全局，保证 app 侧猴子补丁仍生效）。"""
    return callbacks_generate.GenDeps(
        webui_root=WEBUI_ROOT, backend=backend, history_mgr=history_mgr,
        cur_lang=_CUR_LANG, refresh_history=refresh_history,
        refresh_history_full=refresh_history_full, prefer_mp3=_prefer_mp3,
        save_last_inputs=_save_last_inputs, update_last_abc=_update_last_abc,
        load_last_inputs=_load_last_inputs, generate_worker=_generate_worker,
        resynthesize_worker=_resynthesize_worker, register_task=_register_task,
        unregister_task=_unregister_task, localize_task_error=_localize_task_error,
        active_tasks=_active_tasks, active_tasks_lock=_active_tasks_lock,
    )


GenerationOutcome = callbacks_generate.GenerationOutcome
_recycle_output_dir = callbacks_generate._recycle_output_dir
on_variant_select = callbacks_generate.on_variant_select


def on_generate(
    project, style, lyrics, cot, seed, random_seed, cfg_scale, num_inference_steps, out_format, batch_count,
    normalize, fade, trim, metadata,
    abc_text,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress=gr.Progress(track_tqdm=False),
):
    """生成按钮回调（实现见 src/callbacks_generate.py）。"""
    return callbacks_generate.on_generate(
        _gen_deps(), project, style, lyrics, cot, seed, random_seed, cfg_scale,
        num_inference_steps, out_format, batch_count, normalize, fade, trim, metadata,
        abc_text, abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window,
        abc_min_tok, abc_max_tok, sem_temp, sem_top_p, sem_top_k, sem_rep_penalty,
        sem_pen_window, sem_min_tok, sem_max_tok, progress=progress,
    )


def _generate_worker(
    _task,
    project, style, lyrics, cot, seeds, cfg_scale, num_inference_steps, out_format, batch_count,
    normalize, fade, trim, metadata, abc_text,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    lang="zh",
):
    """生成 worker（实现见 src/callbacks_generate.py；供队列调用）。"""
    return callbacks_generate.generate_worker(
        _gen_deps(), _task, project, style, lyrics, cot, seeds, cfg_scale,
        num_inference_steps, out_format, batch_count, normalize, fade, trim, metadata,
        abc_text, abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window,
        abc_min_tok, abc_max_tok, sem_temp, sem_top_p, sem_top_k, sem_rep_penalty,
        sem_pen_window, sem_min_tok, sem_max_tok, lang=lang,
    )


def on_restore_last(kind: str, current: str):
    """恢复「使用上一次」文本（实现见 src/callbacks_generate.py）。"""
    return callbacks_generate.on_restore_last(_gen_deps(), kind, current)


def on_variant_finalize(label, payload):
    """选定变体为最终版并清理其余（实现见 src/callbacks_generate.py）。"""
    return callbacks_generate.on_variant_finalize(_gen_deps(), label, payload)


def on_variant_keep_all(label, payload):
    """选定变体为最终版但保留全部（实现见 src/callbacks_generate.py）。"""
    return callbacks_generate.on_variant_keep_all(_gen_deps(), label, payload)


def on_cancel():
    """取消生成任务（实现见 src/callbacks_generate.py）。"""
    return callbacks_generate.on_cancel(_gen_deps())


def on_resynthesize(
    abc_text, style, lyrics, seed, cfg_scale, num_inference_steps, out_format,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    progress=gr.Progress(track_tqdm=False),
):
    """用编辑后的 ABC 重新合成（实现见 src/callbacks_generate.py）。"""
    return callbacks_generate.on_resynthesize(
        _gen_deps(), abc_text, style, lyrics, seed, cfg_scale, num_inference_steps,
        out_format, abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window,
        abc_min_tok, abc_max_tok, sem_temp, sem_top_p, sem_top_k, sem_rep_penalty,
        sem_pen_window, sem_min_tok, sem_max_tok, progress=progress,
    )


def _resynthesize_worker(
    _task, abc_text, style, lyrics, seed, cfg_scale, num_inference_steps, out_format,
    abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window, abc_min_tok, abc_max_tok,
    sem_temp, sem_top_p, sem_top_k, sem_rep_penalty, sem_pen_window, sem_min_tok, sem_max_tok,
    lang="zh",
):
    """重新合成 worker（实现见 src/callbacks_generate.py；供队列调用）。"""
    return callbacks_generate.resynthesize_worker(
        _gen_deps(), _task, abc_text, style, lyrics, seed, cfg_scale, num_inference_steps,
        out_format, abc_temp, abc_top_p, abc_top_k, abc_rep_penalty, abc_pen_window,
        abc_min_tok, abc_max_tok, sem_temp, sem_top_p, sem_top_k, sem_rep_penalty,
        sem_pen_window, sem_min_tok, sem_max_tok, lang=lang,
    )

# ---------------------------------------------------------------------------
# 转谱页回调（C1：实现见 src/callbacks_transcribe.py，依赖在调用时注入当前全局）
# ---------------------------------------------------------------------------

def _transcribe_deps() -> "callbacks_transcribe.TranscribeDeps":
    """构建转谱组依赖（调用时读取当前全局，保证 app 侧猴子补丁仍生效）。"""
    return callbacks_transcribe.TranscribeDeps(
        webui_root=WEBUI_ROOT, backend=backend, cur_lang=_CUR_LANG,
        voice_handlers=voice_handlers, transcribe_worker=_transcribe_worker,
        register_task=_register_task, unregister_task=_unregister_task,
        localize_task_error=_localize_task_error,
    )


def on_transcribe(audio_file, progress=gr.Progress(track_tqdm=False)):
    """转谱按钮回调（实现见 src/callbacks_transcribe.py）。"""
    return callbacks_transcribe.on_transcribe(_transcribe_deps(), audio_file, progress=progress)


def _transcribe_worker(_task, audio_path, lang="zh"):
    """转谱 worker（实现见 src/callbacks_transcribe.py；供队列调用）。"""
    return callbacks_transcribe.transcribe_worker(_transcribe_deps(), _task, audio_path, lang=lang)


def on_send_to_generate(abc_text):
    """发送乐谱到生成页（实现见 src/callbacks_transcribe.py）。"""
    return callbacks_transcribe.on_send_to_generate(_transcribe_deps(), abc_text)


# =======================================================================
# 音色工坊 Tab：回调薄封装（C1：实现见 src/callbacks_voice.py）
# 依赖在调用时读取当前全局（_voice_deps），保证 app 侧猴子补丁仍生效；
# 纯函数/常量直接 re-export。build_ui() 的裸名字引用保持不变。
# =======================================================================

def _voice_deps() -> "callbacks_voice.VoiceDeps":
    """构建音色组依赖（调用时读取当前全局，保证 app 侧猴子补丁仍生效）。"""
    return callbacks_voice.VoiceDeps(
        project_root=PROJECT_ROOT, cur_lang=_CUR_LANG, history_mgr=history_mgr,
        voice_handlers=voice_handlers, register_task=_register_task,
        unregister_task=_unregister_task,
        set_pending_cancel=_set_pending_cancel,
        clear_pending_cancel=_clear_pending_cancel,
        active_tasks=_active_tasks, active_tasks_lock=_active_tasks_lock,
        pending_cancel=_pending_cancel,
    )


# 纯函数 / 常量直接 re-export（无 app 全局依赖）
VOICE_PLAYER_COUNT = callbacks_voice.VOICE_PLAYER_COUNT
_STEM_TYPE_LABELS = callbacks_voice._STEM_TYPE_LABELS
_dd_update = callbacks_voice._dd_update
_preview_first_update = callbacks_voice._preview_first_update
_prefer_mp3 = callbacks_voice._prefer_mp3
_preview_for_library = callbacks_voice._preview_for_library
_fill_voice_players = callbacks_voice._fill_voice_players
_voice_running_outputs = callbacks_voice._voice_running_outputs
_sep_running_outputs = callbacks_voice._sep_running_outputs
on_voice_src_mode = callbacks_voice.on_voice_src_mode
on_voice_ref_mode = callbacks_voice.on_voice_ref_mode
on_voice_dry_src_mode = callbacks_voice.on_voice_dry_src_mode


def _voice_ref_choices(lang="zh"):
    """音色库下拉选项（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_ref_choices(_voice_deps(), lang)


def _voice_ref_names():
    """音色库文件名列表（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_ref_names(_voice_deps())


def _voice_stem_choices(lang="zh", exclude_vocals=True):
    """素材库下拉选项（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_stem_choices(_voice_deps(), lang, exclude_vocals=exclude_vocals)


def _stem_pick_choices(stems):
    """「待入库轨道」下拉选项（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._stem_pick_choices(_voice_deps(), stems)


def _voice_source_history_choices(lang="zh", limit=50):
    """历史记录源下拉（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_source_history_choices(_voice_deps(), lang, limit=limit)


def _voice_cover_source_choices(lang="zh", limit=50):
    """翻唱源下拉（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_cover_source_choices(_voice_deps(), lang, limit=limit)


def _voice_dry_sep_choices(lang="zh", limit=50):
    """干声来源1下拉（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_dry_sep_choices(_voice_deps(), lang, limit=limit)


def _voice_dry_upload_choices(lang="zh", limit=50):
    """干声来源2下拉（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_dry_upload_choices(_voice_deps(), lang, limit=limit)


def _voice_ref_pair_acc(ref_path: str) -> str:
    """参考干声配对伴奏轨（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_ref_pair_acc(_voice_deps(), ref_path)


def _voice_task_history_choices(record_type, lang="zh", limit=50):
    """任务历史下拉（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_task_history_choices(_voice_deps(), record_type, lang, limit=limit)


def _voice_stem_items(stems):
    """stems → [(label, 回放路径)]（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_stem_items(_voice_deps(), stems)


def on_voice_task_history_pick(task_id):
    """任务历史选择回放（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_task_history_pick(_voice_deps(), task_id)


def on_voice_task_rename(task_id, new_name, record_type):
    """任务历史改项目名（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_task_rename(_voice_deps(), task_id, new_name, record_type)


def on_voice_task_delete(task_id, record_type):
    """任务历史删除项目（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_task_delete(_voice_deps(), task_id, record_type)


def on_voice_save_ref(src_upload, name_input):
    """保存参考干声到音色库（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_save_ref(_voice_deps(), src_upload, name_input)


def on_voice_ref_upload_check(path):
    """上传参考干声的人声检测（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_ref_upload_check(_voice_deps(), path)


def _resolve_voice_source(history_val, upload_val):
    """解析源音频路径（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._resolve_voice_source(_voice_deps(), history_val, upload_val)


def _project_from_source(source: str) -> str:
    """解析源音频项目名（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._project_from_source(_voice_deps(), source)


def _voice_ref_name(ref_path: str) -> str:
    """参考音色名（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice._voice_ref_name(_voice_deps(), ref_path)


def on_voice_separate(source_history, source_upload, sep_mode="vocals", denoise=False):
    """音轨分离生成器回调（实现见 src/callbacks_voice.py）。"""
    yield from callbacks_voice.on_voice_separate(
        _voice_deps(), source_history, source_upload, sep_mode=sep_mode, denoise=denoise)


def on_voice_cover(source_history, source_upload, ref_library, ref_dry_upload,
                   ref_dry_sep, ref_upload,
                   semi_tone=0, steps=30, gain_db=0.0, custom_acc="",
                   denoise=False, ref_seg_mode="smart"):
    """参考音色翻唱生成器回调（实现见 src/callbacks_voice.py）。"""
    yield from callbacks_voice.on_voice_cover(
        _voice_deps(), source_history, source_upload, ref_library, ref_dry_upload,
        ref_dry_sep, ref_upload, semi_tone=semi_tone, steps=steps, gain_db=gain_db,
        custom_acc=custom_acc, denoise=denoise, ref_seg_mode=ref_seg_mode)


def on_voice_cancel(channel):
    """音色工坊取消按钮（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_cancel(_voice_deps(), channel)


def on_voice_ref_delete(path):
    """删除音色库条目（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_ref_delete(_voice_deps(), path)


def on_voice_ref_rename(path, new_name):
    """重命名音色库条目（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_ref_rename(_voice_deps(), path, new_name)


def on_voice_stem_delete(path):
    """删除素材库条目（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_stem_delete(_voice_deps(), path)


def on_voice_stem_rename(path, new_name):
    """重命名素材库条目（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_stem_rename(_voice_deps(), path, new_name)


def on_voice_save_stem_to_lib(pick_path, name, stems_state):
    """把分离轨道存入素材库（实现见 src/callbacks_voice.py）。"""
    return callbacks_voice.on_voice_save_stem_to_lib(_voice_deps(), pick_path, name, stems_state)


# 创作页小回调（C1：实现见 src/callbacks_generate.py，纯函数直接 re-export）
on_random_seed = callbacks_generate.on_random_seed
on_style_preset = callbacks_generate.on_style_preset
append_to_style = callbacks_generate.append_to_style
on_vocal_preset = callbacks_generate.on_vocal_preset
on_instrument_preset = callbacks_generate.on_instrument_preset
on_mood_preset = callbacks_generate.on_mood_preset
on_language_preset = callbacks_generate.on_language_preset
on_genre_preset = callbacks_generate.on_genre_preset
on_lyrics_template = callbacks_generate.on_lyrics_template
on_cot_change = callbacks_generate.on_cot_change

# ---------------------------------------------------------------------------
# 歌曲历史页回调（C1：实现见 src/callbacks_history.py，依赖在调用时注入当前全局）
# ---------------------------------------------------------------------------

HISTORY_PAGE_SIZE = callbacks_history.HISTORY_PAGE_SIZE
_hist_player_keep = callbacks_history._hist_player_keep
_hist_player_clear = callbacks_history._hist_player_clear


def _history_deps() -> "callbacks_history.HistoryDeps":
    """构建历史组依赖（调用时读取当前全局，保证 app 侧猴子补丁仍生效）。"""
    return callbacks_history.HistoryDeps(
        webui_root=WEBUI_ROOT, history_mgr=history_mgr, cur_lang=_CUR_LANG,
        prefer_mp3=_prefer_mp3,
    )


def refresh_history():
    """刷新历史表格首页（实现见 src/callbacks_history.py）。"""
    return callbacks_history.refresh_history(_history_deps())


def refresh_history_full():
    """刷新历史并重置页码（实现见 src/callbacks_history.py）。"""
    return callbacks_history.refresh_history_full(_history_deps())


def _get_history_page(page):
    """取指定页历史（实现见 src/callbacks_history.py）。"""
    return callbacks_history._get_history_page(_history_deps(), page)


def on_history_prev_page(current_page):
    """上一页（实现见 src/callbacks_history.py）。"""
    return callbacks_history.on_history_prev_page(_history_deps(), current_page)


def on_history_next_page(current_page):
    """下一页（实现见 src/callbacks_history.py）。"""
    return callbacks_history.on_history_next_page(_history_deps(), current_page)


def on_history_select(evt: gr.SelectData, current_state: list, current_page):
    """选中历史行加载记录（实现见 src/callbacks_history.py）。"""
    return callbacks_history.on_history_select(_history_deps(), evt, current_state, current_page)


def on_history_delete(selected_state):
    """删除选中记录（实现见 src/callbacks_history.py）。"""
    return callbacks_history.on_history_delete(_history_deps(), selected_state)


def on_history_clear():
    """清空全部历史（实现见 src/callbacks_history.py）。"""
    return callbacks_history.on_history_clear(_history_deps())


def on_history_rename_project(selected_state, new_name):
    """改项目名（实现见 src/callbacks_history.py）。"""
    return callbacks_history.on_history_rename_project(_history_deps(), selected_state, new_name)


def on_history_delete_project(selected_state):
    """删除项目（实现见 src/callbacks_history.py）。"""
    return callbacks_history.on_history_delete_project(_history_deps(), selected_state)


# ---------------------------------------------------------------------------
# 设置页状态回调（C1：实现见 src/callbacks_settings.py，依赖在调用时注入当前全局）
# ---------------------------------------------------------------------------

def on_check_models():
    """检查模型文件并返回状态 Markdown（实现见 src/callbacks_settings.py）。"""
    return callbacks_settings.on_check_models(backend, _CUR_LANG, PROJECT_ROOT)


def _queue_status_html():
    """当前队列状态 Markdown（实现见 src/callbacks_settings.py）。"""
    return callbacks_settings._queue_status_html(_CUR_LANG)


def on_lyrics_change(lyrics):
    """歌词结构分析（实现见 src/callbacks_settings.py）。"""
    return callbacks_settings.on_lyrics_change(lyrics, _CUR_LANG)


def _preset_display_names(lang: str) -> list:
    """预设下拉框显示名（实现见 src/callbacks_settings.py）。"""
    return callbacks_settings.preset_display_names(lang, WEBUI_ROOT)


def on_preset_load(name):
    """加载预设（实现见 src/callbacks_settings.py）：WEBUI_ROOT/_CUR_LANG 调用时读取。"""
    return callbacks_settings.preset_load(name, _CUR_LANG, WEBUI_ROOT)


def on_preset_save(name, *values):
    """保存当前参数为预设（实现见 src/callbacks_settings.py）。"""
    return callbacks_settings.preset_save(name, values, _CUR_LANG, WEBUI_ROOT)


def build_ui():
    """构建 Gradio UI（实现见 src/ui_tabs.py）：调用时注入本模块命名空间。"""
    import sys as _sys
    return ui_tabs.build_ui(_sys.modules[__name__])


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
.last-btn-row { justify-content: flex-end; margin-top: 0; }
.last-btn-row > * { flex-grow: 0 !important; }
</style>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/abcjs/6.3.0/abcjs-audio.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/abcjs/6.3.0/abcjs-basic-min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Sortable/1.15.0/Sortable.min.js"></script>
<script src="/static/js/vendor/wavesurfer.min.js?v=1"></script>
<script src="/static/js/app.js?v=18"></script>
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
        # 编辑页拆为 html + 外链 css/js（C6），no-store 避免改版后命中旧缓存
        from starlette.responses import JSONResponse
        import mix_web

        def _mix_page(request):
            return FileResponse(
                WEBUI_ROOT / "static" / "multitrack" / "index.html",
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        def _mix_asset(name: str, media_type: str):
            """多轨编辑页外链子资源（同目录），走静态下发（不带 no-store，靠 ?v= 破缓存）。"""
            return FileResponse(WEBUI_ROOT / "static" / "multitrack" / name, media_type=media_type)

        demo.app.routes.insert(0, Route("/static/multitrack/", _mix_page, methods=["GET"]))
        demo.app.routes.insert(0, Route("/static/multitrack", _mix_page, methods=["GET"]))
        demo.app.routes.insert(0, Route(
            "/static/multitrack/multitrack.css",
            lambda request: _mix_asset("multitrack.css", "text/css"),
            methods=["GET"],
        ))
        demo.app.routes.insert(0, Route(
            "/static/multitrack/multitrack.js",
            lambda request: _mix_asset("multitrack.js", "application/javascript"),
            methods=["GET"],
        ))

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

        # —— M3：工程持久化 + 混音记录改名/删除 ——
        async def _mix_projects(request):
            """已保存工程清单（按保存时间倒序）。"""
            try:
                data = mix_web.list_projects(WEBUI_ROOT)
            except Exception as e:
                logger.exception("工程清单读取失败")
                data = {"ok": False, "error": f"工程清单读取失败: {e}", "projects": []}
            return JSONResponse(data)

        async def _mix_project(request):
            """GET 载入工程（?path=）；POST 保存工程（体 {project, name}）。"""
            if request.method == "GET":
                return JSONResponse(mix_web.load_project(
                    WEBUI_ROOT, request.query_params.get("path", "")))
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"})
            if not isinstance(body, dict) or not isinstance(body.get("project"), dict):
                return JSONResponse({"ok": False, "error": "缺少工程数据"})
            return JSONResponse(mix_web.save_project(
                body["project"], WEBUI_ROOT, body.get("name", "")))

        async def _mix_record(request):
            """混音记录改名/删除（体 {action: rename|delete, task_id, name}）。"""
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"})
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "error": "请求体必须是 JSON 对象"})
            action = body.get("action", "")
            if action == "rename":
                return JSONResponse(mix_web.rename_mix(
                    history_mgr, body.get("task_id", ""), body.get("name", "")))
            if action == "delete":
                return JSONResponse(mix_web.delete_mix(history_mgr, body.get("task_id", "")))
            return JSONResponse({"ok": False, "error": f"未知操作: {action}"})

        for _path, _ep, _methods in (
            ("/api/mix/sources", _mix_sources, ["GET"]),
            ("/api/mix/peaks", _mix_peaks, ["GET"]),
            ("/api/mix/audio", _mix_audio, ["GET"]),
            ("/api/mix/render", _mix_render, ["POST"]),
            ("/api/mix/status", _mix_status, ["GET"]),
            ("/api/mix/cancel", _mix_cancel, ["POST"]),
            ("/api/mix/projects", _mix_projects, ["GET"]),
            ("/api/mix/project", _mix_project, ["GET", "POST"]),
            ("/api/mix/record", _mix_record, ["POST"]),
        ):
            demo.app.routes.insert(0, Route(_path, _ep, methods=_methods))
    threading.Thread(target=_register_custom_routes, daemon=True).start()

    demo.launch(
        server_name="127.0.0.1",
        server_port=9898,
        share=False,
        show_error=True,
    )
