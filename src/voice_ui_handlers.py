"""音色工坊业务逻辑（音轨分离 + 参考音色翻唱）。

与 app.py 解耦：本模块只负责"入队 worker + 产物落盘 + 历史记录"，不包含 Gradio 布局。
app.py 仅做：Tab 布局、语言注册、事件绑定，并把按钮输入转发到本模块的函数。

核心设计（见 Docs/voice-tools-plan.md §3.4）：
- 产物独立文件夹：outputs/separations/<时间戳>_<短id>/（分离）、outputs/covers/...（翻唱）
- 产物文件名 <时间戳>_<类别>.wav（如 20260924_201805_vocals.wav），按文件名即可辨识轨道
- 每个 worker 从 VoiceClient 调用 worker，并把结果写为 HistoryRecord
"""

import logging
import random
import string
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from history import HistoryRecord
from queue_manager import queue_manager, TaskCancelledError, TaskStatus, TaskType
from voice_client import VoiceClient, VoiceResult

logger = logging.getLogger(__name__)

# 衍生目录短 id 字符集（去掉易混淆字符）
_SHORT_ID_CHARS = string.ascii_lowercase + string.digits


def new_short_id(length: int = 4) -> str:
    return "".join(random.choice(_SHORT_ID_CHARS) for _ in range(length))


# 产物键 → 轨道中文标签（与 worker 分离/翻唱产物一致）
_STEM_LABELS = {"vocals": "人声", "accompaniment": "伴奏",
                "drums": "鼓", "bass": "贝斯", "other": "其他",
                "cover": "翻唱成品", "converted_vocals": "换嗓干声",
                "separated_vocals": "分离人声"}


def _build_stems(products: dict) -> list:
    """把 worker 分离/翻唱产物整理为 [{label,type,path}, ...]，仅收录存在的音频文件。"""
    stems = []
    for key, path in (products or {}).items():
        if not path or not Path(path).exists():
            continue
        stems.append({"label": _STEM_LABELS.get(key, key), "type": key, "path": str(path)})
    return stems


@dataclass
class VoiceHandlers:
    """持有历史管理器与产物根路径，提供音色工坊的队列 worker 回调。"""
    project_root: Path
    webui_root: Path
    history_mgr: object
    voice_client: VoiceClient

    # 供主 app 注入的取消事件提供者（可选）
    cancel_provider: Optional[Callable[[], threading.Event]] = None

    # ---------------------------------------------------------------- 工具
    def _derived_dir(self, kind: str) -> tuple:
        """创建独立产物文件夹，返回 (目录绝对路径, 时间戳前缀)。

        每次任务一个独立文件夹：outputs/separations/<时间戳>_<短id>/（分离）或
        outputs/covers/<时间戳>_<短id>/（翻唱）。时间戳前缀供 worker 命名产物
        <时间戳>_<类别>.wav，文件夹名与产物名对齐，便于按文件夹整组回放/下载。
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        kind_dir = "separations" if kind == "sep" else "covers"
        base = self.webui_root / "outputs" / kind_dir
        d = base / f"{ts}_{new_short_id()}"
        while d.exists():  # 极低概率冲突，见缝插针避免覆盖
            d = base / f"{ts}_{new_short_id()}"
        d.mkdir(parents=True, exist_ok=True)
        return d, ts

    def _recycle_created_derived(self, out_dir: Path) -> None:
        """回收本次任务已创建但未入历史的衍生目录（孤儿回收）。

        与 history._recycle_derived 规则一致：文件逐个移系统回收站 + 自底向上 rmdir 空目录，
        不使用 rm 整目录。回收失败仅记日志，不掩盖原始任务错误。
        """
        if not out_dir or not out_dir.is_dir():
            return
        try:
            from history import delete_files_to_recycle
            files = [p for p in out_dir.rglob("*") if p.is_file()]
            delete_files_to_recycle(files)
            dirs = sorted((p for p in out_dir.rglob("*") if p.is_dir()),
                          key=lambda p: len(p.parts), reverse=True)
            for d in dirs:
                try:
                    d.rmdir()
                except OSError:
                    pass
            try:
                out_dir.rmdir()
            except OSError:
                pass
        except Exception:
            logger.exception("孤儿衍生目录回收失败(已忽略): %s", out_dir)

    def _record(self, task_id: str, rec_type: str, derived_from: str,
                root_task_id: str, audio_path: str, output_dir: Path,
                duration: float = 0.0, elapsed: float = 0.0,
                style: str = "", status: str = "completed",
                stems: Optional[list] = None) -> None:
        """追加一条音色工坊历史记录（不覆盖现有生成/转谱记录）。"""
        record = HistoryRecord(
            task_id=task_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            style=style or f"[{rec_type}]",
            cot=f"voice:{rec_type}",
            audio_duration_seconds=duration,
            generation_time_seconds=elapsed,
            audio_path=str(audio_path),
            output_dir=str(output_dir.relative_to(self.webui_root)),
            backend="voice",
            status=status,
            record_type=rec_type,
            derived_from=derived_from,
            root_task_id=root_task_id,
            stems=list(stems or []),
        )
        self.history_mgr.append(record)
        self.history_mgr.auto_prune()

    # ---------------------------------------------------------------- 队列提交
    def run_in_queue_stream(self, task_type: TaskType, worker_fn: Callable,
                            lang: str, tr_fn: Callable, task_id_name: str,
                            **kwargs):
        """生成器版队列提交：yield 实时状态文案，完成时 return worker 结果 dict。

        worker_fn(task, **kwargs) -> dict；内部复用 queue_manager 的顺序执行保证显存安全。
        与回调式不同，本方法把排队/执行状态 yield 给 Gradio 生成器回调，
        前端组件（info 区 + 按钮）可实时反馈进度，避免任务运行期间界面无响应感。
        - 执行中文案附已用秒数（秒表）：worker 无细粒度进度，用耗时提供反馈
        - 相邻重复文案自动去重，减少无效前端更新
        """
        # 捕获语言快照，供任务内文案使用（运行中切换语言不影响进行中任务）
        t = queue_manager.submit(task_type, worker_fn, cancel_event=threading.Event(),
                                 lang=lang, tr=tr_fn, **kwargs)
        run_started = None  # 首次进入 RUNNING 的时间戳，用于秒表
        last_text = None    # 上次已 yield 的文案，相同则跳过（去重）
        try:
            while True:
                status_info = queue_manager.get_status(t)
                status = status_info["status"]
                if status == TaskStatus.QUEUED:
                    pos = status_info["position"]
                    head = tr_fn(lang, "前面还有 {n} 个任务").replace("{n}", str(pos - 1)) \
                        if pos > 1 else tr_fn(lang, "正在等待...")
                    text = f"{tr_fn(lang, task_id_name)} · {head}"
                elif status == TaskStatus.RUNNING:
                    if run_started is None:
                        run_started = time.time()
                    text = (f"{tr_fn(lang, task_id_name)} · {tr_fn(lang, '执行中...')}"
                            f" · {int(time.time() - run_started)}s")
                elif status == TaskStatus.COMPLETED:
                    break
                elif status == TaskStatus.FAILED:
                    err = status_info.get("error", tr_fn(lang, "未知错误"))
                    raise RuntimeError(f"{tr_fn(lang, '任务失败')}: {err}")
                else:  # CANCELLED
                    raise TaskCancelledError(tr_fn(lang, "任务已取消"))
                # worker 侧旁路进度（若 worker 有上报则优先展示，取最后一条）
                for pv, pd in t.drain_progress():
                    text = f"{tr_fn(lang, task_id_name)} · {pd}"
                if text != last_text:
                    last_text = text
                    yield text
                time.sleep(0.5)
            result = t.result
            if result is None:
                raise RuntimeError(tr_fn(lang, "任务无返回结果"))
            return result if isinstance(result, dict) else {"result": result}
        except TaskCancelledError:
            raise
        except Exception:
            logger.exception(f"{task_id_name} 任务异常")
            raise

    # ---------------------------------------------------------------- 队列 workers
    def separate_worker(self, _task, source: str, mode: str, root_task_id: str = "",
                        denoise: bool = False, lang: str = "zh", tr=None, **kwargs) -> dict:
        """音轨分离 worker：调用 worker 分离，独立产物文件夹 + 写历史记录，返回产物 dict。

        入队约定（见 run_in_queue_stream）：首个参数名为 _task，并吸收 lang/tr。
        denoise=True 时对输出人声降噪。产物落 outputs/separations/<时间戳>_<短id>/，
        文件名 <时间戳>_<类别>.wav，按文件名即可辨识人声/伴奏等轨。
        """
        derived_from = root_task_id
        root = root_task_id or f"upload_{new_short_id()}"  # 上传源无生成任务目录，仅作记录
        out_dir, ts = self._derived_dir("sep")
        try:
            result = self.voice_client.separate(source, mode=mode, output_dir=str(out_dir),
                                                denoise=denoise, prefix=ts)
        except Exception:
            # worker 抛异常：回收本次已建但未入历史的衍生目录，再向上抛原始错误
            self._recycle_created_derived(out_dir)
            raise
        if _task.cancel_event.is_set():
            self._recycle_created_derived(out_dir)
            raise TaskCancelledError("任务已取消")
        if not result.ok:
            self._recycle_created_derived(out_dir)
            raise RuntimeError(result.error or "音轨分离失败")
        # 换算音频时长（取 vocals 轨）；并收集各轨供历史查看/回放
        products = result.products or {}
        duration = _probe_duration(products.get("vocals", ""))
        stems = _build_stems(products)
        self._record(_task.task_id, "separation", derived_from, root,
                     products.get("vocals", ""), out_dir,
                     duration=duration, style=_source_label(source), stems=stems)
        return {"products": products, "stems": stems,
                "output_dir": str(out_dir), "root_task_id": root}

    def cover_worker(self, _task, source: str, ref: str, accompaniment: str,
                     semi_tone: int, diffusion_steps: int, gain_db: float,
                     denoise: bool = False,
                     root_task_id: str = "", lang: str = "zh", tr=None, **kwargs) -> dict:
        """参考音色翻唱 worker：完整管线（换嗓+混音）+ 写历史记录。denoise=True 时对换嗓人声降噪。

        产物落 outputs/covers/<时间戳>_<短id>/，全部轨道平铺命名 <时间戳>_<类别>。
        """
        derived_from = root_task_id
        root = root_task_id or f"upload_{new_short_id()}"
        out_dir, ts = self._derived_dir("cover")
        try:
            result = self.voice_client.convert(
                source, ref, semi_tone=semi_tone, diffusion_steps=diffusion_steps,
                accompaniment=accompaniment, gain_db=gain_db, output_dir=str(out_dir),
                denoise=denoise, prefix=ts,
            )
        except Exception:
            # worker 抛异常：回收本次已建但未入历史的衍生目录，再向上抛原始错误
            self._recycle_created_derived(out_dir)
            raise
        if _task.cancel_event.is_set():
            self._recycle_created_derived(out_dir)
            raise TaskCancelledError("任务已取消")
        if not result.ok:
            self._recycle_created_derived(out_dir)
            raise RuntimeError(result.error or "翻唱失败")
        cover_path = result.products.get("cover", "")
        duration = _probe_duration(cover_path)
        stems = _build_stems(result.products)
        self._record(_task.task_id, "cover", derived_from, root,
                     cover_path, out_dir, duration=duration,
                     style=f"cover+{semi_tone:+.0f}", stems=stems)
        return {"products": result.products, "stems": stems,
                "output_dir": str(out_dir), "root_task_id": root}

    # ---------------------------------------------------------------- 音色库
    def refs_dir(self) -> Path:
        """音色参考库目录（voice-tools/refs），不存在则创建。"""
        d = self.webui_root / "voice-tools" / "refs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_ref(self, src_path: str, name: str) -> str:
        """把参考干声存入音色库，返回落盘路径。"""
        import shutil
        refs = self.refs_dir()
        safe = "".join(c for c in name.strip() if c.isalnum() or c in "_- ").strip() or "ref"
        dest = refs / f"{safe}_{new_short_id(4)}.wav"
        shutil.copyfile(src_path, dest)
        return str(dest)

    def list_refs(self) -> list[str]:
        """列出音色库中所有参考音频绝对路径。"""
        refs = self.refs_dir()
        exts = (".wav", ".mp3", ".flac", ".m4a", ".ogg")
        return [str(p) for p in sorted(refs.glob("*")) if p.suffix.lower() in exts]

    # ---------------------------------------------------------------- 上传干音历史（第二来源）
    def dry_dir(self) -> Path:
        """上传已验证干音留存目录（voice-tools/dry_uploads），不存在则创建。"""
        d = self.webui_root / "voice-tools" / "dry_uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_dry_upload(self, src_path: str) -> str:
        """把上传且检测通过的人声干音留存到 dry_uploads，返回落盘路径（重复上传覆盖）。"""
        import shutil
        src = Path(src_path)
        dest = self.dry_dir() / f"dry_{new_short_id(4)}{src.suffix.lower() or '.wav'}"
        shutil.copyfile(src, dest)
        return str(dest)

    def list_dry_uploads(self) -> list[str]:
        """列出所有已验证干音绝对路径（按修改时间倒序）。"""
        exts = (".wav", ".mp3", ".flac", ".m4a", ".ogg")
        return [str(p) for p in sorted(self.dry_dir().glob("*"),
                                       key=lambda p: p.stat().st_mtime, reverse=True)
                if p.suffix.lower() in exts]

    # ---------------------------------------------------------------- 素材库（乐器轨留存）
    # 合法轨道类型：与 worker 分离产物键一致（2轨: vocals/accompaniment；4轨: vocals/drums/bass/other）
    STEM_TYPES = ("vocals", "accompaniment", "drums", "bass", "other")

    def stems_dir(self) -> Path:
        """轨道素材库目录（voice-tools/stems），不存在则创建。"""
        d = self.webui_root / "voice-tools" / "stems"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_stem(self, src_path: str, name: str, stem_type: str) -> str:
        """把分离轨道存入素材库（命名 名__类型.wav），返回落盘路径。"""
        import shutil
        if stem_type not in self.STEM_TYPES:
            raise ValueError(f"未知轨道类型: {stem_type}")
        stems = self.stems_dir()
        safe = "".join(c for c in name.strip() if c.isalnum() or c in "_- ").strip() or "stem"
        dest = stems / f"{safe}__{stem_type}{Path(src_path).suffix or '.wav'}"
        shutil.copyfile(src_path, dest)
        return str(dest)

    def list_stems(self) -> list[tuple]:
        """列出素材库条目，返回 (名称, 轨道类型, 路径) 列表（按名称排序）。"""
        exts = (".wav", ".mp3", ".flac", ".m4a", ".ogg")
        out = []
        for p in sorted(self.stems_dir().glob("*")):
            if p.suffix.lower() not in exts:
                continue
            # 文件名约定 名__类型.ext；无 __ 分隔时归为 other
            stem = p.stem
            name, _, stype = stem.rpartition("__")
            if not name or stype not in self.STEM_TYPES:
                name, stype = stem, "other"
            out.append((name, stype, str(p)))
        return out


def _probe_duration(path: str) -> float:
    """用 ffmpeg/音速获取音频时长；失败返回 0。仅做展示用途，异常不抛出。"""
    import subprocess
    if not path or not Path(path).exists():
        return 0.0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        return float(out.stdout.strip()) if out.stdout.strip() else 0.0
    except Exception:
        return 0.0


def _source_label(path: str) -> str:
    """从源路径抽取可读标签（取文件名）。"""
    return Path(path).name or "voice"


# ---------------------------------------------------------------- 轻量人声检测（方案B）
def _parse_cfg_defaults(cfg: Optional[dict] = None) -> dict:
    """从语音工具配置派生人声检测参数（魔法数字外置为配置键，缺失/非法回退默认）。

    cfg 应为 load_voice_config() 的规范化返回值（键含 voice_detect_threshold /
    detect_sample_rate / detect_frame_len / voice_band(tuple) / detect_seconds）。
    cfg 为 None 时回退静态默认，保证不传项目根的调用（如测试）行为不变。
    """
    from voice_config import DEFAULT_VOICE_CONFIG
    base = cfg if cfg is not None else dict(DEFAULT_VOICE_CONFIG)
    def _f(key, default):
        try:
            return float(base[key])
        except (KeyError, TypeError, ValueError):
            return default
    def _i(key, default):
        try:
            return int(base[key])
        except (KeyError, TypeError, ValueError):
            return default
    # voice_band 规范化后为 tuple (lo,hi)；字符串形态兜底解析并防 lo>hi
    band = base.get("voice_band")
    lo, hi = 80, 1000
    if isinstance(band, (tuple, list)) and len(band) == 2:
        lo, hi = int(band[0]), int(band[1])
    else:
        try:
            parsed = str(band).split("-")
            lo, hi = int(float(parsed[0])), int(float(parsed[1]))
        except (ValueError, TypeError, IndexError):
            pass
    if lo > hi:
        lo, hi = hi, lo
    return {
        "threshold": _f("voice_detect_threshold", 0.5),
        "sample_rate": _i("detect_sample_rate", 16000),
        "frame_len": _i("detect_frame_len", 2048),
        "band": (lo, hi),
        "max_seconds": _f("detect_seconds", 30.0),
    }


def _voice_score(x, sr: int, *, threshold: float = 0.5,
                 frame_len: int = 2048, band: tuple = (80, 1000)) -> tuple:
    """纯 DSP 打分：返回 (是否人声, 置信度0~1)。

    特征：人声频带(默认 80-1000Hz)能量占比 0.65 + 谱平坦度反向 0.35。
    人声干声基频及谐波集中在中低频且周期性强（平坦度低），得分高；
    白噪声/高频乐器能量分散或集中高频，得分低。默认阈值取自配置键 0.5。
    """
    import numpy as np
    n = frame_len  # 帧长（配置键 detect_frame_len，默认 2048）
    freqs = np.fft.rfftfreq(n, 1 / sr)
    lo, hi = band
    band_mask = (freqs >= lo) & (freqs <= hi)   # 人声频带掩码（配置键 voice_band）
    ratios, flats = [], []
    frames = [x[i:i + n] for i in range(0, len(x) - n, n // 2)]
    if not frames:
        return (False, 0.0)
    energies = [float(np.sum(f * f)) for f in frames]
    e_max = max(energies) or 1.0
    for f, e in zip(frames, energies):
        if e < e_max * 0.05:  # 跳过静音帧
            continue
        spec = np.abs(np.fft.rfft(f)) + 1e-10
        power = spec * spec
        ratios.append(float(power[band_mask].sum() / power.sum()))
        # 谱平坦度 = 几何均值/算术均值；周期信号(人声)接近0，噪声接近1
        flats.append(float(np.exp(np.log(spec).mean()) / spec.mean()))
    if not ratios:
        return (False, 0.0)
    voice_ratio = sum(ratios) / len(ratios)
    flatness = sum(flats) / len(flats)
    score = 0.65 * voice_ratio + 0.35 * (1 - flatness)
    return (score >= threshold, min(max(score, 0.0), 1.0))


def detect_voice(path: str, max_seconds: float = 30.0,
                 project_root: Path | None = None):
    """轻量判断音频是否人声干声：ffmpeg 解码为 mono PCM 后打分。

    采样率/帧长/频带/阈值/时长均取自 voice_config 配置键（默认 16kHz、2048、80-1000、0.5、30s）。
    project_root 缺省为 None 时回退静态默认；传入时读取用户 config.cfg 的 [voice] 段，
    让用户配置优先（缺失的键再回退默认）。秒级完成、不占显存（粗判）；
    解码或分析失败返回 None（不判定、不拦截）。
    """
    import subprocess
    from voice_config import load_voice_config
    cfg = _parse_cfg_defaults(load_voice_config(project_root)) \
        if project_root is not None else _parse_cfg_defaults()
    ts = max_seconds if max_seconds is not None else cfg["max_seconds"]
    sr = cfg["sample_rate"]
    if not path or not Path(path).exists():
        return None
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-t", str(ts), "-i", str(path),
             "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
            capture_output=True, timeout=60,
        )
        if len(out.stdout) < sr // 2:  # 少于约 0.5 秒数据，无法判断
            return None
        import numpy as np
        x = np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        return _voice_score(x, sr, threshold=cfg["threshold"],
                            frame_len=cfg["frame_len"], band=cfg["band"])
    except Exception:
        logger.exception("人声检测失败: %s", path)
        return None