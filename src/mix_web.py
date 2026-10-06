"""多轨混音 Web 接口（M2）：素材清单、波形峰值、音频下发、提交渲染、状态查询。

本模块只提供与 Web 框架无关的纯 Python 函数，由 app.py 注册为 Starlette 路由，
便于脱离 Gradio 做单元测试。对外能力：
    resolve_audio(webui_root, rel, must_exist) -> Path|None   # outputs/ 白名单解析
    audio_mime(path) -> str                                   # 扩展名 → MIME
    compute_peaks(path, buckets) -> dict                      # ffmpeg 解码后分桶求 min/max
    list_sources(history_mgr, webui_root) -> dict              # 分离素材 + 已有混音记录
    page_texts(lang) -> dict                                   # 编辑页文案（走 i18n.tr）
    submit_render(payload, webui_root, history_mgr, project) -> dict
    task_status(task_id, webui_root) -> dict
    cancel_render(task_id) -> dict
    save_project(payload, webui_root, name) -> dict             # 工程持久化（M3）
    list_projects(webui_root) -> dict
    load_project(webui_root, rel) -> dict
    rename_mix(history_mgr, task_id, new_name) -> dict          # 混音记录改名/删除（M3）
    delete_mix(history_mgr, task_id) -> dict

安全与健壮性约束：
- 所有下发/读取的音频路径都必须经 resolve_audio 校验落在 <webui_root>/outputs 下，
  且扩展名在白名单内，避免任意路径被读取或暴露给浏览器。
- 波形峰值用 ffmpeg 解码为 8kHz 单声道 s16le 再分桶，与源格式无关（wav/flac 均可）；
  结果按 (路径, mtime, 桶数) 缓存，避免重复解码。
- 渲染复用既有 queue_manager（TaskType.MIX）；因 queue_manager 只能按 Task 对象查询，
  本模块维护一个轻量 task_id → Task 注册表（FIFO，上限 20 条）。
- 全部接口不抛异常：失败统一返回 {"ok": False, "error": 中文说明}。
"""
import json
import logging
import shutil
import subprocess
import threading
import time
from array import array
from collections import OrderedDict
from pathlib import Path

from i18n import tr
from mix_render import MixProjectError, mix_worker, parse_mix_project
from queue_manager import TaskStatus, TaskType, queue_manager

logger = logging.getLogger(__name__)

# 混音产物目录前缀（须与 history._PROJECT_DIR_PREFIXES 中的 "mix_" 保持一致）
MIX_DIR_PREFIX = "mix_"

# 保存的混音工程目录（相对 outputs/）；文件名即工程名，同名覆盖
MIX_PROJECT_DIR = "mix_projects"
# 工程名清洗：这些字符会破坏文件名/路径，统一替换为下划线
_BAD_NAME_CHARS = '\\/:*?"<>|'
_NAME_MAX = 60

# 允许下发给浏览器的音频扩展名 → MIME（其余一律拒绝）
AUDIO_MIME = {
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
}

# 峰值分桶数区间
PEAK_BUCKETS_MIN, PEAK_BUCKETS_MAX, PEAK_BUCKETS_DEFAULT = 200, 4000, 1200
_PEAK_SR = 8000            # 峰值解码采样率：足够画波形，解码量小
_PEAK_TIMEOUT = 120        # 单轨解码超时（秒）
_PEAK_CACHE_MAX = 16
_peak_cache: "OrderedDict" = OrderedDict()
_peak_lock = threading.Lock()
# 每个缓存键一把锁：避免并发请求对同一 key 重复解码（check-then-compute 竞态）
_peak_key_locks: dict = {}

_TASKS_MAX = 20
_tasks: "OrderedDict" = OrderedDict()
_tasks_lock = threading.Lock()


# ------------------------------------------------------------------ 路径与峰值
def resolve_audio(webui_root, rel, must_exist: bool = True):
    """把 outputs/ 下的路径解析为绝对路径；越界/扩展名不允许/不存在时返回 None。"""
    if not isinstance(rel, str) or not rel.strip():
        return None
    root = Path(webui_root)
    raw = Path(rel)
    cand = raw if raw.is_absolute() else (root / raw)
    try:
        resolved = cand.resolve()
        resolved.relative_to((root / "outputs").resolve())
    except (OSError, ValueError):
        return None
    if resolved.suffix.lower() not in AUDIO_MIME:
        return None
    if must_exist and not resolved.is_file():
        return None
    return resolved


def audio_mime(path) -> str:
    """按扩展名返回音频 MIME；未知扩展名回退 application/octet-stream。"""
    return AUDIO_MIME.get(Path(path).suffix.lower(), "application/octet-stream")


def _rel_to_root(root: Path, path) -> "str | None":
    """返回相对 webui_root 的正斜杠路径；空路径或越界返回 None。"""
    if path is None or not str(path).strip():
        return None
    p = Path(path)
    try:
        resolved = p.resolve() if p.is_absolute() else (root / p).resolve()
        return resolved.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _clamp(value, low, high):
    return max(low, min(high, value))


def _decode_mono(path: Path):
    """用 ffmpeg 解码为 8kHz 单声道 s16le，返回 array('h')；失败返回 None。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
             "-ac", "1", "-ar", str(_PEAK_SR), "-f", "s16le", "-"],
            capture_output=True, timeout=_PEAK_TIMEOUT,
        )
    except Exception:
        logger.exception("波形解码失败: %s", path)
        return None
    if proc.returncode != 0 or not proc.stdout:
        logger.warning("波形解码无输出: %s", path)
        return None
    raw = proc.stdout[: len(proc.stdout) - (len(proc.stdout) % 2)]
    samples = array("h")
    samples.frombytes(raw)
    return samples


def compute_peaks(path, buckets: int = PEAK_BUCKETS_DEFAULT) -> dict:
    """返回 {"peaks": [[min,max], ...], "duration": float}；失败返回空峰值。

    min/max 归一到 -1..1（s16 满量程 32768），供前端绘制包络。
    """
    path = Path(path)
    try:
        buckets = int(buckets)
    except (TypeError, ValueError):
        buckets = PEAK_BUCKETS_DEFAULT
    buckets = int(_clamp(buckets, PEAK_BUCKETS_MIN, PEAK_BUCKETS_MAX))

    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {"peaks": [], "duration": 0.0}

    key = (str(path), mtime, buckets)
    with _peak_lock:
        cached = _peak_cache.get(key)
        if cached is not None:
            return cached
        # 同一 key 共用一把锁：并发请求串行化，只有一个真正解码
        key_lock = _peak_key_locks
        lock = key_lock.get(key)
        if lock is None:
            lock = threading.Lock()
            key_lock[key] = lock

    with lock:
        # 双重检查：等锁期间可能已被先到的请求填充
        with _peak_lock:
            cached = _peak_cache.get(key)
            if cached is not None:
                return cached

        samples = _decode_mono(path)
        if not samples:
            return {"peaks": [], "duration": 0.0}

        total = len(samples)
        duration = total / float(_PEAK_SR)
        step = max(1, total // buckets)
        scale = 1.0 / 32768.0
        peaks = []
        for start in range(0, total, step):
            chunk = samples[start:start + step]
            if not chunk:
                break
            peaks.append([min(chunk) * scale, max(chunk) * scale])
            if len(peaks) >= buckets:
                break

        result = {"peaks": peaks, "duration": duration}
        with _peak_lock:
            _peak_cache[key] = result
            while len(_peak_cache) > _PEAK_CACHE_MAX:
                _peak_cache.popitem(last=False)
            # 锁表随缓存回收，避免无界增长
            if len(_peak_key_locks) > _PEAK_CACHE_MAX:
                _peak_key_locks.clear()
        return result


# ------------------------------------------------------------------ 素材清单
def _source_label(rec) -> str:
    """素材/记录的显示名：有项目名时「项目名 · 目录名」，否则仅目录名。"""
    folder = Path(rec.output_dir).name if getattr(rec, "output_dir", "") else Path(
        getattr(rec, "audio_path", "") or "").stem
    return f"{rec.project} · {folder}" if getattr(rec, "project", "") else folder


def list_sources(history_mgr, webui_root) -> dict:
    """列出可作混音素材的分离记录，以及已生成的混音记录（均只含磁盘存在的文件）。"""
    root = Path(webui_root)
    sources, mixes = [], []
    try:
        records = list(history_mgr.list_all())
    except Exception:
        logger.exception("读取历史记录失败")
        return {"ok": False, "error": "读取历史记录失败", "sources": [], "mixes": []}

    for rec in records:
        rtype = getattr(rec, "record_type", "")
        if rtype == "separation":
            tracks = []
            for stem in getattr(rec, "stems", None) or []:
                if not isinstance(stem, dict):
                    continue
                p = Path(stem.get("path", ""))
                rel = _rel_to_root(root, p)
                if rel is None or not p.is_file():
                    continue
                tracks.append({
                    "label": stem.get("label") or p.stem,
                    "type": stem.get("type") or "",
                    "path": rel,
                    "name": p.name,
                })
            if tracks:
                sources.append({
                    "task_id": rec.task_id,
                    "name": _source_label(rec),
                    "created_at": rec.created_at,
                    "tracks": tracks,
                })
        elif rtype == "mix":
            rel = _rel_to_root(root, getattr(rec, "audio_path", ""))
            if not rel:
                continue
            mixes.append({
                "task_id": rec.task_id,
                "name": _source_label(rec),
                "created_at": rec.created_at,
                "path": rel,
                "duration": getattr(rec, "audio_duration_seconds", 0.0) or 0.0,
            })
    return {"ok": True, "sources": sources, "mixes": mixes}


# ------------------------------------------------------------------ 工程持久化（M3）
def _safe_name(name: str) -> str:
    """清洗工程名：替换非法字符、去首尾点/空格、限长；为空则返回空串。"""
    s = "".join("_" if ch in _BAD_NAME_CHARS else ch for ch in (name or "").strip())
    return s.strip(". ")[:_NAME_MAX]


def _project_dir(root: Path) -> Path:
    return Path(root) / "outputs" / MIX_PROJECT_DIR


def _project_to_json(project, webui_root) -> dict:
    """把校验后的 MixProject 序列化为前端可回灌的规范 JSON（src 转为相对路径）。"""
    root = Path(webui_root)
    return {
        "version": project.version,
        "sample_rate": project.sample_rate,
        "duration": project.duration,
        "source_root_task_id": project.source_root_task_id,
        "tracks": [{
            "id": t.id,
            "name": t.name,
            "src": _rel_to_root(root, t.src) or str(t.src),
            "gain_db": t.gain_db,
            "mute": bool(t.mute),
            # 音质参数（P1）：与编辑页 buildPayload 字段名一致，保证「保存→载入→再保存」幂等
            "pan": t.pan, "eq_low": t.eq_low, "eq_mid": t.eq_mid, "eq_high": t.eq_high,
            "hpf": t.hpf, "lpf": t.lpf,
            # 音效参数（P2）：压缩（阈值/比率）+ 回声（延迟/反馈/混合）
            "comp_th": t.comp_th, "comp_ratio": t.comp_ratio,
            "echo_delay": t.echo_delay, "echo_fb": t.echo_fb, "echo_mix": t.echo_mix,
            "clips": [{
                "start": c.start, "in": c.in_, "out": c.out,
                "fade_in": c.fade_in, "fade_out": c.fade_out,
            } for c in (t.clips or [])],
        } for t in project.tracks],
        "master": dict(project.master or {}),
    }


def save_project(payload, webui_root, name: str = "") -> dict:
    """保存混音工程为 JSON（先校验归一化，同名覆盖）；name 为空则用时间戳。"""
    try:
        project = parse_mix_project(payload, webui_root)
    except MixProjectError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception("混音工程校验异常")
        return {"ok": False, "error": f"工程校验失败: {e}"}

    safe = _safe_name(name) or time.strftime("%Y%m%d_%H%M%S")
    target = _project_dir(Path(webui_root)) / f"{safe}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(_project_to_json(project, webui_root), ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as e:
        logger.exception("工程保存失败")
        return {"ok": False, "error": f"工程保存失败: {e}"}
    return {"ok": True, "name": safe, "path": _rel_to_root(Path(webui_root), target)}


def list_projects(webui_root) -> dict:
    """列出已保存的混音工程（按保存时间倒序）；损坏文件跳过。"""
    d = _project_dir(Path(webui_root))
    items = []
    try:
        files = sorted(d.glob("*.json")) if d.is_dir() else []
    except OSError:
        logger.exception("工程目录读取失败")
        files = []
    for f in files:
        try:
            stat = f.stat()
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append({
            "name": f.stem,
            "path": _rel_to_root(Path(webui_root), f),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            "mtime": stat.st_mtime,
            "track_count": len((data or {}).get("tracks") or []),
            "duration": (data or {}).get("duration", 0.0),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"ok": True, "projects": items}


def load_project(webui_root, rel) -> dict:
    """读取一个已保存工程；路径必须落在 outputs/mix_projects/ 下，且校验可通过。"""
    root = Path(webui_root)
    if not isinstance(rel, str) or not rel.strip():
        return {"ok": False, "error": "工程路径为空"}
    cand = Path(rel)
    cand = cand if cand.is_absolute() else (root / cand)
    try:
        resolved = cand.resolve()
        resolved.relative_to(_project_dir(root).resolve())
    except (OSError, ValueError):
        return {"ok": False, "error": "工程路径越界"}
    if resolved.suffix.lower() != ".json" or not resolved.is_file():
        return {"ok": False, "error": "工程文件不存在"}
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"工程文件损坏: {e}"}
    try:
        project = parse_mix_project(data, webui_root)
    except MixProjectError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "name": resolved.stem, "project": _project_to_json(project, webui_root)}


# ------------------------------------------------------------------ 混音记录管理（M3）
def rename_mix(history_mgr, task_id, new_name: str = "") -> dict:
    """修改混音记录的项目名（文件级重命名，保留时间戳）；返回重命名文件数。"""
    if not task_id:
        return {"ok": False, "error": "未指定记录"}
    try:
        rec = history_mgr.get(str(task_id))
        if rec is None:
            return {"ok": False, "error": "记录不存在"}
        n = history_mgr.rename_project(rec.output_dir, new_name or "")
    except Exception as e:
        logger.exception("混音记录改名失败")
        return {"ok": False, "error": f"改名失败: {e}"}
    return {"ok": True, "renamed": n}


def delete_mix(history_mgr, task_id) -> dict:
    """删除一条混音记录：整目录移入系统回收站，并移除该目录全部记录。"""
    if not task_id:
        return {"ok": False, "error": "未指定记录"}
    try:
        rec = history_mgr.get(str(task_id))
        if rec is None:
            return {"ok": False, "error": "记录不存在"}
        n = history_mgr.delete_project(rec.output_dir)
    except Exception as e:
        logger.exception("混音记录删除失败")
        return {"ok": False, "error": f"删除失败: {e}"}
    if not n:
        return {"ok": False, "error": "删除失败（目录不存在或不允许删除）"}
    return {"ok": True, "removed": n}


# ------------------------------------------------------------------ 文案
# 编辑页需要的文案键（值即中文原文，统一经 tr 翻译，禁止前端硬编码文案）
PAGE_TEXT_KEYS = (
    "多轨编辑器", "选择分离素材", "载入", "刷新", "音轨", "音量", "静音", "独奏",
    "全局选区", "起点(秒)", "终点(秒)", "应用选区到全部轨", "恢复全部整轨",
    "裁为选区", "恢复整轨", "淡入(秒)", "淡出(秒)", "在波形上拖拽选择区间",
    "母带", "响度目标(LUFS)", "真峰值(dBTP)", "项目名", "项目名(可选)",
    "渲染成品", "取消渲染", "渲染中", "排队中", "渲染完成", "渲染失败", "已取消",
    "试听", "下载", "混音记录", "暂无混音记录", "请先选择素材", "正在载入波形", "区间无效",
    "无可用素材，请先执行一次音轨分离",
    # —— M3：工程持久化 + 混音记录管理 ——
    "工程", "已保存工程", "保存工程", "打开", "工程已保存", "工程已载入",
    "改名", "新名称", "删除该混音记录？文件将移入回收站。", "记录不存在", "删除失败",
    # —— 单轨试听 ——
    "试听本轨",
    # —— 统一播放（DAW transport） ——
    "播放", "暂停", "停止", "循环", "正在解码音频", "正在下载音频", "解码失败", "播放失败",
    "播放控制", "取消选择区", "起始", "时长",
    # —— 每轨音质（P1：声像 + 三段 EQ + 高通/低通） ——
    "音质", "声像", "低频", "中频", "高频", "高通", "低通", "分贝", "Hz（0 = 关闭）",
    # —— 每轨音效（P2：压缩 + 回声） ——
    "压缩", "阈值", "比率", "回声", "延迟", "延迟（0 = 自动 250ms）", "反馈", "混合",
    # —— 电平表 / GR 表（音频术语，中英一致） ——
    "IN", "OUT", "GR",
    # —— 模块归零 + FX 工具（旁通 / 预设 / 复制到其他轨） ——
    "归零", "全部复位到中性值", "FX 工具", "开关", "正常", "旁通",
    "旁通本轨全部音质与音效", "预设", "应用", "保存为预设…", "预设名称", "中性",
    "复制到", "复制", "全部其他轨",
)


def page_texts(lang: str) -> dict:
    """返回编辑页全部文案（键=中文原文，值=按语言翻译后的文本）。"""
    return {key: tr(lang, key) for key in PAGE_TEXT_KEYS}


# ------------------------------------------------------------------ 渲染任务
def _remember(task) -> None:
    """登记任务供状态查询；超出上限时淘汰最旧的（其状态不再可查）。"""
    with _tasks_lock:
        _tasks[task.task_id] = task
        while len(_tasks) > _TASKS_MAX:
            _tasks.popitem(last=False)


def _lookup(task_id):
    if not task_id:
        return None
    with _tasks_lock:
        return _tasks.get(str(task_id))


def submit_render(payload, webui_root, history_mgr, project: str = "") -> dict:
    """校验工程并提交渲染队列；返回 {"ok": True, "task_id": ...} 或 {"ok": False, ...}。"""
    try:
        parse_mix_project(payload, webui_root)     # 提交前先校验，尽早把错误反馈给页面
    except MixProjectError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception("混音工程校验异常")
        return {"ok": False, "error": f"工程校验失败: {e}"}

    out_dir = Path(webui_root) / "outputs" / f"{MIX_DIR_PREFIX}{time.strftime('%Y%m%d_%H%M%S')}"
    try:
        task = queue_manager.submit(
            TaskType.MIX, mix_worker,
            payload_json=json.dumps(payload, ensure_ascii=False),
            webui_root=Path(webui_root), out_dir=out_dir,
            project=project or "", history_mgr=history_mgr,
        )
    except Exception as e:
        logger.exception("混音任务提交失败")
        return {"ok": False, "error": f"任务提交失败: {e}"}

    _remember(task)
    return {"ok": True, "task_id": task.task_id}


def task_status(task_id, webui_root) -> dict:
    """查询渲染任务状态；含进度、排队位次、失败原因与完成后的产物相对路径。"""
    task = _lookup(task_id)
    if task is None:
        return {"ok": False, "error": "任务不存在或已过期"}

    try:
        info = queue_manager.get_status(task)
    except Exception as e:
        logger.exception("任务状态查询失败")
        return {"ok": False, "error": f"状态查询失败: {e}"}

    status = info.get("status")
    status = status.value if isinstance(status, TaskStatus) else str(status)
    out = {"ok": True, "status": status, "position": info.get("position", 0)}

    if status == TaskStatus.RUNNING.value:
        progress = getattr(task, "last_progress", None)
        if progress:
            out["progress"] = {"value": progress[0], "desc": progress[1]}
    elif status == TaskStatus.COMPLETED.value:
        result = task.result if isinstance(task.result, dict) else {}
        if result.get("ok"):
            out["output"] = _rel_to_root(Path(webui_root), result.get("output", ""))
            out["duration"] = result.get("duration", 0.0)
        else:
            # worker 内部业务失败：队列状态是 completed，但结果不可用
            out["status"] = TaskStatus.FAILED.value
            out["error"] = result.get("error") or "渲染失败"
    elif status == TaskStatus.FAILED.value:
        out["error"] = info.get("error") or task.error or "渲染失败"
    return out


def cancel_render(task_id) -> dict:
    """对排队中/运行中的渲染任务下发协作取消信号。"""
    if _lookup(task_id) is None:
        return {"ok": False, "error": "任务不存在或已过期"}
    try:
        ok = queue_manager.cancel_task_by_id(str(task_id))
    except Exception as e:
        logger.exception("任务取消失败")
        return {"ok": False, "error": f"取消失败: {e}"}
    return {"ok": bool(ok)}
