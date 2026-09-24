"""音色工坊业务逻辑（音轨分离 + 参考音色翻唱）。

与 app.py 解耦：本模块只负责"入队 worker + 产物落盘 + 历史记录"，不包含 Gradio 布局。
app.py 仅做：Tab 布局、语言注册、事件绑定，并把按钮输入转发到本模块的函数。

核心设计（见 Docs/voice-tools-plan.md §3.4）：
- 产物聚合在源任务目录：outputs/<root_task_id>/derived/sep_XXXX 或 cover_XXXX
- 短 id（4 位随机）避免 Windows 260 路径限制
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


# 产物键 → 轨道中文标签（与 worker 分离产物一致）
_STEM_LABELS = {"vocals": "人声", "accompaniment": "伴奏",
                "drums": "鼓", "bass": "贝斯", "other": "其他",
                "cover": "翻唱成品", "converted_vocals": "换嗓干声"}


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
    def _derived_dir(self, root_task_id: str, kind: str) -> Path:
        """定位根任务目录下的衍生子目录：outputs/<root>/derived/<kind>_<shortid>。"""
        out_root = self.webui_root / "outputs" / root_task_id / "derived"
        # 目录名含较短 id 便于辨识；冲突时自增
        d = out_root / f"{kind}_{new_short_id()}"
        while d.exists():  # 极低概率冲突，见缝插针避免覆盖
            d = out_root / f"{kind}_{new_short_id()}"
        d.mkdir(parents=True, exist_ok=True)
        return d

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
    def run_in_queue(self, task_type: TaskType, worker_fn: Callable,
                     lang: str, tr_fn: Callable, task_id_name: str,
                     progress_cb: Callable, **kwargs) -> dict:
        """把 worker_fn 提交到共享队列并轮询完成，返回其结果 dict。

        worker_fn(task, **kwargs) -> dict；内部复用 queue_manager 的顺序执行保证显存安全。
        进度通过 queue_manager 的 drain_progress 转发到 progress_cb(值, 文案)。
        """
        # 捕获语言快照，供任务内文案使用（运行中切换语言不影响进行中任务）
        t = queue_manager.submit(task_type, worker_fn, cancel_event=threading.Event(),
                                 lang=lang, tr=tr_fn, **kwargs)
        try:
            while True:
                status_info = queue_manager.get_status(t)
                status = status_info["status"]
                if status == TaskStatus.QUEUED:
                    pos = status_info["position"]
                    head = tr_fn(lang, "前面还有 {n} 个任务").replace("{n}", str(pos - 1)) \
                        if pos > 1 else tr_fn(lang, "正在等待...")
                    progress_cb(0, f"{tr_fn(lang, task_id_name)} · {head}")
                elif status == TaskStatus.RUNNING:
                    progress_cb(0, f"{tr_fn(lang, task_id_name)} · {tr_fn(lang, '执行中...')}")
                elif status == TaskStatus.COMPLETED:
                    break
                elif status == TaskStatus.FAILED:
                    err = status_info.get("error", tr_fn(lang, "未知错误"))
                    raise RuntimeError(f"{tr_fn(lang, '任务失败')}: {err}")
                elif status == TaskStatus.CANCELLED:
                    raise TaskCancelledError(tr_fn(lang, "任务已取消"))
                for pv, pd in t.drain_progress():
                    progress_cb(pv, pd)
                time.sleep(0.5)
            for pv, pd in t.drain_progress():
                progress_cb(pv, pd)
            result = t.result
            if result is None:
                raise RuntimeError(tr_fn(lang, "任务无返回结果"))
            return result if isinstance(result, dict) else {"result": result}
        except TaskCancelledError:
            raise
        except Exception as e:
            logger.exception(f"{task_id_name} 任务异常")
            raise

    # ---------------------------------------------------------------- 队列 workers
    def separate_worker(self, _task, source: str, mode: str, root_task_id: str = "",
                        lang: str = "zh", tr=None, **kwargs) -> dict:
        """音轨分离 worker：调用 worker 分离，产出目录 + 写历史记录，返回产物 dict。

        入队约定（见 run_in_queue）：首个参数名为 _task，并吸收 lang/tr。
        """
        derived_from = root_task_id
        root = root_task_id or f"upload_{new_short_id()}"
        if not root_task_id:
            # 外部上传源：在 outputs/upload_xxxx 创建源目录
            src_dir = self.webui_root / "outputs" / root
            src_dir.mkdir(parents=True, exist_ok=True)
        out_dir = self._derived_dir(root, "sep")
        result = self.voice_client.separate(source, mode=mode, output_dir=str(out_dir))
        if _task.cancel_event.is_set():
            raise TaskCancelledError("任务已取消")
        if not result.ok:
            raise RuntimeError(result.error or "音轨分离失败")
        # 换算音频时长（取 vocals 轨）；并收集各轨供历史查看/回放
        products = result.products or {}
        duration = _probe_duration(products.get("vocals", ""))
        stems = _build_stems(products)
        self._record(_task.task_id, "separation", derived_from, root,
                     products.get("vocals", ""), out_dir,
                     duration=duration, style=_source_label(source), stems=stems)
        return {"products": products, "output_dir": str(out_dir), "root_task_id": root}

    def cover_worker(self, _task, source: str, ref: str, accompaniment: str,
                     semi_tone: int, diffusion_steps: int, gain_db: float,
                     root_task_id: str = "", lang: str = "zh", tr=None, **kwargs) -> dict:
        """参考音色翻唱 worker：完整管线（换嗓+混音）+ 写历史记录。"""
        derived_from = root_task_id
        root = root_task_id or f"upload_{new_short_id()}"
        out_dir = self._derived_dir(root, "cover")
        result = self.voice_client.convert(
            source, ref, semi_tone=semi_tone, diffusion_steps=diffusion_steps,
            accompaniment=accompaniment, gain_db=gain_db, output_dir=str(out_dir),
        )
        if _task.cancel_event.is_set():
            raise TaskCancelledError("任务已取消")
        if not result.ok:
            raise RuntimeError(result.error or "翻唱失败")
        cover_path = result.products.get("cover", "")
        duration = _probe_duration(cover_path)
        stems = _build_stems(result.products)
        self._record(_task.task_id, "cover", derived_from, root,
                     cover_path, out_dir, duration=duration,
                     style=f"cover+{semi_tone:+.0f}", stems=stems)
        return {"products": result.products, "output_dir": str(out_dir), "root_task_id": root}

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