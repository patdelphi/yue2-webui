"""音色工坊 Tab 回调组（C1 拆分 app.py：第四阶段 · 音色组）。

程序说明
--------
本模块承载音色工坊（音轨分离 / 参考音色翻唱 / 音色库 / 素材库 / 任务历史回放）
相关回调。依赖全部由 app.py 在调用时以 :class:`VoiceDeps` 注入（本模块不 import
app.py，避免循环依赖），因此 app.py 侧对全局（history_mgr / _CUR_LANG /
voice_handlers / 任务登记与取消等）的猴子补丁在调用时仍然生效。

app.py 保留同名薄封装（生成器函数用 yield from 转发）或纯函数直接 re-export，
build_ui() 的裸名字引用与 Gradio inputs/outputs 绑定完全不变。
"""
import threading
from dataclasses import dataclass
from pathlib import Path

import gradio as gr

from callbacks_settings import _QUEUE_TYPE_LABELS
from history import _FILENAME_TS_RE
from i18n import tr
from queue_manager import queue_manager, TaskType, TaskCancelledError
from voice_ui_handlers import detect_voice, _make_preview


@dataclass
class VoiceDeps:
    """音色组回调的外部依赖（app.py 调用时注入当前全局值，保证猴子补丁生效）。"""
    project_root: Path        # app.PROJECT_ROOT
    cur_lang: str             # app._CUR_LANG
    history_mgr: object       # app.history_mgr
    voice_handlers: object    # app.voice_handlers
    register_task: object     # app._register_task
    unregister_task: object   # app._unregister_task
    set_pending_cancel: object   # app._set_pending_cancel
    clear_pending_cancel: object  # app._clear_pending_cancel
    active_tasks: dict           # app._active_tasks
    active_tasks_lock: object    # app._active_tasks_lock
    pending_cancel: dict         # app._pending_cancel


# ---------------------------------------------------------------------------
# 下拉与选择项构造
# ---------------------------------------------------------------------------

def _voice_ref_choices(d: VoiceDeps, lang="zh"):
    """生成音色库下拉选项：显示文件名，值为绝对路径。"""
    paths = d.voice_handlers.list_refs()
    # 组内键不得重复（Gradio choices 需 (label, value) 唯一即可）
    return [(Path(p).name or p, p) for p in paths]


def _voice_ref_names(d: VoiceDeps):
    """仅返回文件名列表（用于「从音色库选择」的 label）。"""
    return [Path(p).name or p for p in d.voice_handlers.list_refs()]


# 分离轨道类型 → 中文标签（与 worker 分离产物键一致）
_STEM_TYPE_LABELS = {"vocals": "人声", "accompaniment": "伴奏",
                     "drums": "鼓", "bass": "贝斯", "other": "其他"}


def _voice_stem_choices(d: VoiceDeps, lang="zh", exclude_vocals=True):
    """素材库下拉：列出轨道素材（label 标注类型）；默认排除人声轨（作伴奏用）。"""
    out = []
    for name, stype, p in d.voice_handlers.list_stems():
        if exclude_vocals and stype == "vocals":
            continue
        out.append((f"{name} · {tr(lang, _STEM_TYPE_LABELS.get(stype, stype))}", p))
    return out


def _stem_pick_choices(d: VoiceDeps, stems):
    """「待入库轨道」下拉选项：取本次分离的乐器/伴奏轨（人声归音色库，不入素材库）。

    value = 音频路径；显示名 = 轨道类型名（经 tr 翻译）。与素材库下拉同样排除
    vocals，保证「保存成功」后条目立刻能在素材库下拉里看到。
    """
    out = []
    for s in (stems or []):
        if not isinstance(s, dict) or s.get("type") == "vocals":
            continue
        p = s.get("path", "")
        if p:
            out.append((tr(d.cur_lang, _STEM_TYPE_LABELS.get(s.get("type"), s.get("type"))), p))
    return out


def _voice_source_history_choices(d: VoiceDeps, lang="zh", limit=50):
    """生成「从历史记录选择」下拉：列出最近 generation/cover 记录（有音频者）。

    类型词经 _QUEUE_TYPE_LABELS + tr 翻译，避免中文界面出现英文类型词。
    """
    choices = []
    for rec in d.history_mgr.list_all():
        if rec.audio_path and Path(rec.audio_path).exists() \
                and rec.record_type in ("generation", "cover"):
            type_label = tr(lang, _QUEUE_TYPE_LABELS.get(rec.record_type, rec.record_type))
            choices.append((f"{type_label} · {Path(rec.audio_path).name}", rec.audio_path))
        if len(choices) >= limit:
            break
    return choices


def _voice_cover_source_choices(d: VoiceDeps, lang="zh", limit=50):
    """翻唱源下拉：仅 generation/cover 原唱历史记录（排除 separation 分离任务）。"""
    choices = []
    for rec in d.history_mgr.list_all():
        if rec.record_type in ("generation", "cover") and rec.audio_path \
                and Path(rec.audio_path).exists():
            type_label = tr(lang, _QUEUE_TYPE_LABELS.get(rec.record_type, rec.record_type))
            choices.append((f"{type_label} · {Path(rec.audio_path).name}", rec.audio_path))
        if len(choices) >= limit:
            break
    return choices


def _voice_dry_sep_choices(d: VoiceDeps, lang="zh", limit=50):
    """干声来源1：分离历史记录的人声干声（separation 记录的 audio_path 即 vocals 轨）。"""
    choices = []
    for rec in d.history_mgr.list_all():
        if rec.audio_path and Path(rec.audio_path).exists() \
                and rec.record_type == "separation":
            choices.append((f"{Path(rec.audio_path).name}", rec.audio_path))
        if len(choices) >= limit:
            break
    return choices


def _voice_dry_upload_choices(d: VoiceDeps, lang="zh", limit=50):
    """干声来源2：上传并验证为干音的留存。"""
    return [(f"上传 · {Path(p).name}", p) for p in d.voice_handlers.list_dry_uploads()[:limit]]


def _voice_ref_pair_acc(d: VoiceDeps, ref_path: str) -> str:
    """参考干声若来自分离记录，返回同一次分离的伴奏轨路径（供智能挑段算人声主导度）。

    仅「从分离人声选择」这条来源能拿到配对伴奏（分离记录自带 vocals+accompaniment
    两轨）；上传干声/音色库没有配对轨，返回空串，worker 侧会回退"能量最高段"。
    查询异常一律返回空串，不阻断翻唱流程。
    """
    try:
        target = Path(ref_path).resolve()
        for rec in d.history_mgr.list_all():
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


def _voice_task_history_choices(d: VoiceDeps, record_type, lang="zh", limit=50):
    """按类型列出任务历史（separation/cover），value=task_id，用于 Tab 内选择回放。

    显示名 = "项目名 · 产物文件夹名"（项目名含上传源文件名；无项目名时仅文件夹名）。
    """
    choices = []
    for rec in d.history_mgr.list_all():
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


def _preview_first_update(choices):
    """切 Tab 时按下拉默认首项刷新「试听」播放器。

    Gradio 的下拉 .change 只在用户交互时触发，程序化 gr.update 设值不会触发；
    若切 Tab 只刷新下拉（_dd_update 已默认选中首项）而不刷新试听播放器，
    就会停在「下拉显示着选中项、试听却是空的」假选中状态（与任务历史回放同理）。
    """
    p = choices[0][1] if choices else None
    return gr.update(value=_preview_for_library(p) if p else None, visible=bool(p))


# 音色工坊播放器组槽位数：分离最多 4 轨（+降噪变体）、翻唱 4 产物，取 6 留余量
VOICE_PLAYER_COUNT = 6


# ---------------------------------------------------------------------------
# 播放器与回放小件
# ---------------------------------------------------------------------------

def _voice_stem_items(d: VoiceDeps, stems):
    """把记录/产物的 stems 列表转为 [(label, 回放路径)]，仅收录磁盘存在的轨。

    回放优先取预览小件（preview，MP3）：远端经隧道回放时原件是 32bit float WAV
    （单轨 60–100MB），要整文件下完才出波形；小件体积约为其 1/12。
    预览件缺失时退回原件（合成链路始终用原件 path，不受影响）。
    降噪轨（文件名含 _denoised）在标签上追加"已降噪"标识，便于区分源轨。
    """
    items = []
    for s in stems or []:
        if not isinstance(s, dict):
            continue
        full = s.get("path", "")
        preview = s.get("preview", "")
        # 存量记录（本次改动前写入）的 stems 无 preview 键：按命名约定从原件名推导
        # <原名>_preview.mp3，存在即用，避免为大件 WAV 重新迁移数据库。
        if not preview and full:
            cand = Path(full).with_name(f"{Path(full).stem}_preview.mp3")
            if cand.exists():
                preview = str(cand)
        play = preview if preview and Path(preview).exists() else full
        if not play or not Path(play).exists():
            continue
        # 轨道标签走 tr 国际化（历史记录的 stems 存中文原文，显示时按当前语言翻译）
        label = tr(d.cur_lang, s.get("label") or Path(full).stem)
        if "_denoised" in Path(full).stem:
            label = f"{label} · {tr(d.cur_lang, '已降噪')}"
        items.append((label, play))
    return items


def _prefer_mp3(path):
    """主播放器回放路径：优先取同目录同名 MP3（体积约为 WAV 的 1/10，远端加载快）。

    生成产物目录里通常已有 export_mp3 导出的同名 .mp3；缺失时回退原路径。
    仅影响回放/下载槽位——合成、分离、混音等后端链路始终使用原件路径。
    """
    if not path:
        return path
    p = Path(path)
    if p.suffix.lower() == ".mp3":
        return str(p)
    cand = p.with_suffix(".mp3")
    return str(cand) if cand.exists() else str(p)


def _preview_for_library(path):
    """库试听路径：按需生成/复用 MP3 预览小件（库文件可能是大 WAV）。

    生成失败（无 ffmpeg 等）或源不存在时回退原件，不阻断试听。
    """
    if not path:
        return path
    prev = _make_preview(str(path))
    return prev or str(path)


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


def on_voice_task_history_pick(d: VoiceDeps, task_id):
    """任务历史选择（按文件夹）：整组播放器回放该任务全部轨道（每轨可下载）。"""
    entry = d.history_mgr.get(task_id) if task_id else None
    items = _voice_stem_items(d, getattr(entry, "stems", None) if entry else None)
    # 无 stems 的旧记录回退主轨
    if not items and entry and entry.audio_path and Path(entry.audio_path).exists():
        items.append((Path(entry.audio_path).name, entry.audio_path))
    return _fill_voice_players(items)


def _voice_task_first_players(d: VoiceDeps, record_type):
    """按下拉默认首条任务回填播放器组（切 Tab 时用）。

    下拉 refreshing 后 value 已是首条任务（_dd_update），若不同步回填播放器，
    界面会停在"下拉显示着任务名、播放器却是空的"的假选中状态。
    """
    choices = _voice_task_history_choices(d, record_type)
    return on_voice_task_history_pick(d, choices[0][1] if choices else None)


# ---------------------------------------------------------------------------
# 任务历史管理（改项目名 / 删除项目）
# ---------------------------------------------------------------------------

def on_voice_task_rename(d: VoiceDeps, task_id, new_name, record_type):
    """分离/翻唱任务历史：改项目名（重命名项目目录文件，保留时间戳）。

    返回 (历史下拉刷新, State 保持当前 task_id, 播放器组保持, 新项目名输入清空)。
    """
    if not task_id:
        raise gr.Error(tr(d.cur_lang, "请先选择任务"))
    entry = d.history_mgr.get(task_id)
    if not entry or not getattr(entry, "output_dir", ""):
        raise gr.Error(tr(d.cur_lang, "该记录没有产物目录"))
    n = d.history_mgr.rename_project(entry.output_dir, new_name or "")
    gr.Info(f"{tr(d.cur_lang, '已重命名')} {n} {tr(d.cur_lang, '个文件')}")
    # 播放器里的旧路径已失效：按记录 stems 重新填充
    items = _voice_stem_items(d, getattr(entry, "stems", None))
    return (gr.update(choices=_voice_task_history_choices(d, record_type)),
            task_id,  # State 组件：改名后 task_id 不变
            *_fill_voice_players(items), "")


def on_voice_task_delete(d: VoiceDeps, task_id, record_type):
    """分离/翻唱任务历史：删除项目（整目录移入回收站，移除该目录全部记录）。

    返回 (历史下拉刷新, State 更新为剩余首条记录的 task_id 或 None, 播放器组清空)。
    """
    if not task_id:
        raise gr.Error(tr(d.cur_lang, "请先选择任务"))
    entry = d.history_mgr.get(task_id)
    if not entry or not getattr(entry, "output_dir", ""):
        raise gr.Error(tr(d.cur_lang, "该记录没有产物目录"))
    n = d.history_mgr.delete_project(entry.output_dir)
    if n <= 0:
        raise gr.Error(tr(d.cur_lang, "删除失败"))
    gr.Info(f"{tr(d.cur_lang, '已删除项目')} · {n} {tr(d.cur_lang, '条记录')}")
    # 删除后 State 应更新为剩余首条记录的 task_id（如果还有的话）
    remaining = _voice_task_history_choices(d, record_type)
    new_state_value = remaining[0][1] if remaining else None
    return (gr.update(choices=remaining, value=None),
            new_state_value,
            *_fill_voice_players([]))


# ---------------------------------------------------------------------------
# 库管理（音色库 / 素材库 / 干声留存）
# ---------------------------------------------------------------------------

def on_voice_save_ref(d: VoiceDeps, src_upload, name_input):
    """把上传的参考干声存入音色库，返回(成功提示, 音色库新下拉选项)。"""
    if not src_upload:
        raise gr.Error(tr(d.cur_lang, "请先上传音频文件"))
    name = (name_input or "").strip() or Path(src_upload).stem
    d.voice_handlers.save_ref(src_upload, name)
    return tr(d.cur_lang, "已保存到音色库"), _voice_ref_choices(d, d.cur_lang)


def on_voice_ref_upload_check(d: VoiceDeps, path):
    """上传参考干声后的轻量人声检测（方案B）：仅提示，不拦截流程。
    检测通过的人声干音自动留存到 dry_uploads，并入「干声历史」第二来源。"""
    if not path:
        return "", gr.update(choices=_voice_dry_upload_choices(d, d.cur_lang))
    # 格式校验：仅支持 torchaudio/Seed-VC 可解码的常见格式
    if Path(path).suffix.lower() not in (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".wma"):
        return tr(d.cur_lang, "不支持的文件格式，请上传 WAV/MP3/FLAC/M4A/OGG"), \
            gr.update(choices=_voice_dry_upload_choices(d, d.cur_lang))
    r = detect_voice(path, project_root=d.project_root)
    if r is None:
        return tr(d.cur_lang, "无法检测"), gr.update(choices=_voice_dry_upload_choices(d, d.cur_lang))
    ok, score = r
    if ok:
        d.voice_handlers.save_dry_upload(path)
        msg = f"{tr(d.cur_lang, '人声检测: 通过')} (p={score:.2f})"
    else:
        msg = f"{tr(d.cur_lang, '人声检测: 疑似非人声，建议上传清唱干声')} (p={score:.2f})"
    return msg, gr.update(choices=_voice_dry_upload_choices(d, d.cur_lang))


def on_voice_ref_delete(d: VoiceDeps, path):
    """删除音色库选中条目（移系统回收站），返回(试听清空, 音色库下拉刷新)。"""
    if not path:
        raise gr.Error(tr(d.cur_lang, "请先选择条目"))
    try:
        d.voice_handlers.delete_ref(path)
    except Exception as e:
        raise gr.Error(f"{tr(d.cur_lang, '删除失败')}: {e}")
    gr.Info(tr(d.cur_lang, "已删除"))
    return gr.update(value=None, visible=False), \
        gr.update(choices=_voice_ref_choices(d, d.cur_lang), value=None)


def on_voice_ref_rename(d: VoiceDeps, path, new_name):
    """重命名音色库选中条目，返回(试听清空, 音色库下拉刷新)。"""
    if not path:
        raise gr.Error(tr(d.cur_lang, "请先选择条目"))
    name = (new_name or "").strip()
    if not name:
        raise gr.Error(tr(d.cur_lang, "请输入新名称"))
    try:
        d.voice_handlers.rename_ref(path, name)
    except Exception as e:
        raise gr.Error(f"{tr(d.cur_lang, '重命名失败')}: {e}")
    gr.Info(tr(d.cur_lang, "已重命名"))
    return gr.update(value=None, visible=False), \
        gr.update(choices=_voice_ref_choices(d, d.cur_lang), value=None)


def on_voice_stem_delete(d: VoiceDeps, path):
    """删除素材库选中条目（移系统回收站），返回素材库下拉刷新。"""
    if not path:
        raise gr.Error(tr(d.cur_lang, "请先选择条目"))
    try:
        d.voice_handlers.delete_stem(path)
    except Exception as e:
        raise gr.Error(f"{tr(d.cur_lang, '删除失败')}: {e}")
    gr.Info(tr(d.cur_lang, "已删除"))
    return gr.update(choices=_voice_stem_choices(d, d.cur_lang), value=None)


def on_voice_stem_rename(d: VoiceDeps, path, new_name):
    """重命名素材库选中条目，返回素材库下拉刷新。"""
    if not path:
        raise gr.Error(tr(d.cur_lang, "请先选择条目"))
    name = (new_name or "").strip()
    if not name:
        raise gr.Error(tr(d.cur_lang, "请输入新名称"))
    try:
        d.voice_handlers.rename_stem(path, name)
    except Exception as e:
        raise gr.Error(f"{tr(d.cur_lang, '重命名失败')}: {e}")
    gr.Info(tr(d.cur_lang, "已重命名"))
    return gr.update(choices=_voice_stem_choices(d, d.cur_lang), value=None)


def on_voice_save_stem_to_lib(d: VoiceDeps, pick_path, name, stems_state):
    """把本次分离产物中的某一轨存入素材库——素材库的唯一写入入口。

    轨道类型从 sep_stems_state（本次分离的 stems）回查，保证 save_stem 的
    「名__类型」命名正确。返回 (素材库下拉刷新, 名称输入清空)。
    """
    if not pick_path:
        raise gr.Error(tr(d.cur_lang, "请先选择要入库的轨道"))
    src = Path(pick_path)
    if not src.exists():
        raise gr.Error(tr(d.cur_lang, "音频文件不存在"))
    stype = ""
    for s in (stems_state or []):
        if isinstance(s, dict) and str(s.get("path", "")) == str(pick_path):
            stype = s.get("type", "")
            break
    if stype not in d.voice_handlers.STEM_TYPES:
        raise gr.Error(tr(d.cur_lang, "无法识别该轨的类型"))
    # 名称留空时回退轨道类型名（保证素材库条目始终有可读名称）
    nm = (name or "").strip() or tr(d.cur_lang, _STEM_TYPE_LABELS.get(stype, stype))
    try:
        dest = d.voice_handlers.save_stem(str(src), nm, stype)
    except Exception as e:
        raise gr.Error(f"{tr(d.cur_lang, '保存失败')}: {e}")
    gr.Info(tr(d.cur_lang, "已存入素材库") + " · " + Path(dest).name)
    return gr.update(choices=_voice_stem_choices(d, d.cur_lang), value=None), ""


# ---------------------------------------------------------------------------
# 源/参考解析
# ---------------------------------------------------------------------------

def _resolve_voice_source(d: VoiceDeps, history_val, upload_val):
    """从「历史记录 / 上传」两入口取实际音频路径，返回 (路径, 是否上传源)。

    上传源标记供 worker 拷贝源副本入产物文件夹（Gradio 临时文件会被清理，
    不拷贝则历史记录无法追溯源音频）。两者均无效则抛错。
    """
    if upload_val and Path(upload_val).exists():
        return str(upload_val), True
    if history_val and Path(history_val).exists():
        return str(history_val), False
    raise gr.Error(tr(d.cur_lang, "请先选择源音频"))


def _project_from_source(d: VoiceDeps, source: str) -> str:
    """解析源音频的项目名（文件管理重构）：历史记录 project 字段优先。

    记录无项目名则返回空（产物不带项目名前缀）；非历史源（上传）按文件名结构
    <项目名>_<时间戳>[_类别] 提取项目名段，无该结构则用整个文件名主干。
    """
    try:
        src = Path(source).resolve()
        for rec in d.history_mgr.list_all():
            if rec.audio_path and Path(rec.audio_path).resolve() == src:
                return getattr(rec, "project", "") or ""
    except OSError:
        pass
    m = _FILENAME_TS_RE.match(Path(source).stem)
    if m:
        return m.group("proj") or ""
    return Path(source).stem


def _voice_ref_name(d: VoiceDeps, ref_path: str) -> str:
    """参考音色名（翻唱项目名的组成部分）。

    音色库条目（名_4位短id.wav）去掉短 id 段；其余（uploads 留存/临时上传/
    分离人声轨）按 <项目名>_<时间戳>[_类别] 结构剥时间戳段，无结构则取主干。
    """
    p = Path(ref_path)
    stem = p.stem
    try:
        if p.parent.resolve() == d.voice_handlers.refs_dir().resolve():
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


# ---------------------------------------------------------------------------
# 模式切换（三入口显隐）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 主流程（分离 / 翻唱 / 取消）
# ---------------------------------------------------------------------------

def _voice_running_outputs(text):
    """任务运行中的中间态输出：播放器组+历史下拉保持现状 + 进度文案 + 按钮保持禁用。"""
    return (*[gr.update()] * VOICE_PLAYER_COUNT, text,
            gr.update(interactive=False), gr.update())


def _sep_running_outputs(text):
    """分离任务运行中的中间态：在共享中间态后追加「待入库轨道下拉 + stems State」保持现状。"""
    return (*_voice_running_outputs(text), gr.update(), [])


def on_voice_separate(d: VoiceDeps, source_history, source_upload, sep_mode="vocals",
                      denoise=False):
    """音轨分离（生成器回调）：入队 Demucs，实时显示排队/执行进度。

    提交即禁用按钮（防运行期间重复提交）并清空播放器组；info 区实时显示
    排队位置/执行秒表/阶段进度（分离中/降噪中）；完成恢复按钮、按产物数量
    填充播放器组并刷新历史下拉；失败/取消也恢复按钮。上传源自动拷贝副本入
    产物文件夹（Gradio 临时文件不持久）。
    """
    source, from_upload = _resolve_voice_source(d, source_history, source_upload)
    # 分离项目名（文件管理重构）：自动取源的项目名（历史记录 project / 上传文件名）
    project = _project_from_source(d, source)
    vote = "2" if sep_mode == "vocals" else "4"
    lang = d.cur_lang
    # 提交前先反馈：清空播放器组 + 禁用按钮
    yield (*_fill_voice_players([]), tr(lang, "排队中..."),
           gr.update(interactive=False), gr.update(),
           gr.update(choices=[], value=None), [])
    gen = d.voice_handlers.run_in_queue_stream(
        TaskType.SEPARATION, d.voice_handlers.separate_worker,
        lang, tr, "音轨分离",
        on_submit=lambda tid: d.register_task("separation", tid),
        on_finish=lambda tid: d.unregister_task("separation", tid),
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
            yield _sep_running_outputs(text)
    except TaskCancelledError:
        # 用户主动取消：恢复按钮 + info 显示"任务已取消"，正常收尾不弹错误窗
        # （必须 return：否则会落进下方成功路径，result=None 导致 .get 崩溃）
        yield (*[gr.update()] * VOICE_PLAYER_COUNT, tr(lang, "任务已取消"),
               gr.update(interactive=True), gr.update(), gr.update(), [])
        return
    except Exception:
        # 失败必须恢复按钮；info 区给出失败提示后向上抛（Gradio 弹错误）
        yield (*[gr.update()] * VOICE_PLAYER_COUNT, tr(lang, "任务失败"),
               gr.update(interactive=True), gr.update(), gr.update(), [])
        raise
    # 按产物数量填充播放器组（每轨一个播放器，label 为轨道名）+ 刷新历史下拉
    # + 填「待入库轨道」下拉并缓存本次 stems（供「保存到素材库」回查轨道类型）
    items = _voice_stem_items(d, result.get("stems"))
    stems = result.get("stems") or []
    note = tr(lang, "分离完成") + " · " + tr(lang, "写入历史")
    yield (*_fill_voice_players(items), note, gr.update(interactive=True),
           gr.update(choices=_voice_task_history_choices(d, "separation", lang)),
           _dd_update(_stem_pick_choices(d, stems)), stems)


def on_voice_cover(d: VoiceDeps, source_history, source_upload, ref_library, ref_dry_upload,
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
    lang = d.cur_lang
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
        entry = d.history_mgr.get(src_val.split(":", 1)[1])
        stems = {s.get("type"): s.get("path", "") for s in (getattr(entry, "stems", None) or [])
                 if isinstance(s, dict)}
        source_vocals = stems.get("vocals", "")
        source_acc = stems.get("accompaniment", "")
        if not (source_vocals and Path(source_vocals).exists()
                and source_acc and Path(source_acc).exists()):
            raise gr.Error(tr(d.cur_lang, "该分离记录缺少人声/伴奏轨，无法复用"))
        source, from_upload = source_vocals, False
        root_task_id = entry.task_id
        project = getattr(entry, "project", "") or ""
    else:
        # 原唱路径 → 先 ensure_separation（查重 / 同步分离 + 持久化）
        source_orig, from_upload = _resolve_voice_source(d, source_history, source_upload)
        project = _project_from_source(d, source_orig)
        # 该分离在进入队列前同步执行，不带 task_id：登记一个取消事件，
        # 让「取消」按钮在此阶段也能立即中止（否则点取消无效）。
        precancel = threading.Event()
        d.set_pending_cancel("cover", precancel)
        try:
            sep_task_id = d.voice_handlers.ensure_separation(source_orig, project,
                                                             cancel_event=precancel)
        except TaskCancelledError:
            yield (*[gr.update()] * VOICE_PLAYER_COUNT, tr(d.cur_lang, "任务已取消"),
                   gr.update(interactive=True), gr.update())
            return
        except Exception as e:
            yield (*[gr.update()] * VOICE_PLAYER_COUNT,
                   tr(d.cur_lang, "源音频分离失败") + f": {e}",
                   gr.update(interactive=True), gr.update())
            raise gr.Error(str(e))
        finally:
            d.clear_pending_cancel("cover")
        # sep_task_id = "sep_task:<task_id>" → 走复用分支解析
        entry = d.history_mgr.get(sep_task_id.split(":", 1)[1])
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
                ref_acc = _voice_ref_pair_acc(d, str(ref))
            break
    if not ref:
        raise gr.Error(tr(d.cur_lang, "请先选择参考音色"))
    # 自定义伴奏校验（选了但文件缺失则忽略，回退原伴奏）
    acc = custom_acc if custom_acc and Path(custom_acc).exists() else ""
    # 翻唱项目名（文件管理重构）：源项目名_音色名（源无项目名则仅音色名）
    cover_project = "_".join(x for x in (project, _voice_ref_name(d, str(ref))) if x)
    # ====== 提交 cover worker 队列 ======
    gen = d.voice_handlers.run_in_queue_stream(
        TaskType.COVER, d.voice_handlers.cover_worker,
        lang, tr, "参考音色翻唱",
        on_submit=lambda tid: d.register_task("cover", tid),
        on_finish=lambda tid: d.unregister_task("cover", tid),
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
    items = _voice_stem_items(d, result.get("stems"))
    note = tr(lang, "翻唱完成") + " · " + tr(lang, "写入历史")
    yield (*_fill_voice_players(items), note, gr.update(interactive=True),
           gr.update(choices=_voice_task_history_choices(d, "cover", lang)))


def on_voice_cancel(d: VoiceDeps, channel):
    """音色工坊取消按钮：按通道（separation/cover）取消正在排队/运行的任务。

    取消为协作式：QUEUED 直接移除；RUNNING 经 cancel_event 通知 worker
    在阶段边界中止（换嗓子进程可被 terminate，产物目录一并回收）。
    """
    with d.active_tasks_lock:
        task_ids = d.active_tasks.pop(channel, set())
        pending = d.pending_cancel.get(channel)
    # 队列外同步阶段（如翻唱前的 ensure_separation）：设置事件让该阶段尽快中止
    if pending is not None:
        pending.set()
    for task_id in task_ids:
        queue_manager.cancel_task_by_id(task_id)
    if task_ids or pending is not None:
        return tr(d.cur_lang, "正在取消...")
    return tr(d.cur_lang, "没有正在运行的任务")
