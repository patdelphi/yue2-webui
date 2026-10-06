"""音色工坊业务逻辑（音轨分离 + 参考音色翻唱）。

与 app.py 解耦：本模块只负责"入队 worker + 产物落盘 + 历史记录"，不包含 Gradio 布局。
app.py 仅做：Tab 布局、语言注册、事件绑定，并把按钮输入转发到本模块的函数。

核心设计（文件管理重构，见 Docs/）：
- 产物独立文件夹：outputs/separations_<时间戳>/（分离）、outputs/cover_<时间戳>/（翻唱）
- 产物文件名 <项目名>_<时间戳>_<类别>.wav（项目名空则时间戳开头），按文件名即可辨识轨道
- 上传文件统一留存 uploads/：<源文件名>_<时间戳>_<类别>.<ext>（sep_src/cover_src/dry_ref/transcribe）
- 每个 worker 从 VoiceClient 调用 worker，并把结果写为 HistoryRecord
"""

import json
import logging
import random
import shutil
import string
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from history import HistoryRecord, HistoryManager, sanitize_project, delete_files_to_recycle
from queue_manager import queue_manager, TaskCancelledError, TaskStatus, TaskType
from voice_client import VoiceClient, VoiceResult

logger = logging.getLogger(__name__)

# 衍生目录短 id 字符集（去掉易混淆字符）
_SHORT_ID_CHARS = string.ascii_lowercase + string.digits


def new_short_id(length: int = 4) -> str:
    return "".join(random.choice(_SHORT_ID_CHARS) for _ in range(length))


# worker 阶段键 → 展示文案（worker 写入进度文件的 stage 值；文案经 tr 国际化）
_STAGE_TEXTS = {"separating": "分离中...", "converting": "换嗓中...",
                "denoising": "降噪中...", "mixing": "混音中..."}


def _progress_poller(task, progress_file: Path, lang: str, tr_fn,
                     stop_event: threading.Event) -> None:
    """轮询 worker 阶段进度文件，转发到任务进度通道（run_in_queue_stream 消费）。

    文件由 worker 在阶段边界重写（JSON：{"stage": ...}）；读取失败/未创建跳过本轮。
    阶段变化才推送，避免重复刷屏。daemon 线程，stop_event 置位即退出。
    """
    last_stage = None
    tr_fn = tr_fn or (lambda l, s: s)  # tr 缺失时回退原文（测试场景）
    while not stop_event.wait(0.5):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                stage = json.load(f).get("stage", "")
        except Exception:
            continue  # 文件未创建/写入中，本轮跳过
        if stage and stage != last_stage:
            last_stage = stage
            try:
                task.push_progress(0, tr_fn(lang, _STAGE_TEXTS.get(stage, stage)))
            except Exception:
                pass  # 任务已结束等场景，进度推送失败无碍


# 产物键 → 轨道中文标签（与 worker 分离/翻唱产物一致）
_STEM_LABELS = {"vocals": "人声", "accompaniment": "伴奏",
                "drums": "鼓", "bass": "贝斯", "other": "其他",
                "cover": "翻唱成品", "converted_vocals": "换嗓干声",
                "separated_vocals": "分离人声"}

# 回放预览件：MP3 192k（约为 32bit float WAV 原件的 1/12 体积）
_PREVIEW_SUFFIX = "_preview"
_PREVIEW_BITRATE = "192k"


def _make_preview(src: str) -> str:
    """为大件 WAV 生成网页回放用小音频 <原名>_preview.mp3，失败返回空串。

    远端经隧道回放时，32bit float WAV 单轨 60–100MB，整文件下完要几十秒才出波形；
    这里随产物预生成 MP3 小件，历史回放只取小件，合成（翻唱/混音）仍用原件。
    ffmpeg 缺失或转码失败都静默返回空串，回放自动退回原件，不影响主流程。
    """
    p = Path(src)
    if not p.exists():
        return ""
    dst = p.with_name(f"{p.stem}{_PREVIEW_SUFFIX}.mp3")
    if dst.exists() and dst.stat().st_mtime >= p.stat().st_mtime:
        return str(dst)  # 已生成且不旧于原件：直接复用
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return ""
    try:
        subprocess.run([ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-i", str(p),
                        "-vn", "-c:a", "libmp3lame", "-b:a", _PREVIEW_BITRATE, str(dst)],
                       check=True, timeout=900,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.warning("生成回放预览件失败（回放退回原件）: %s %s", p.name, e)
        return ""
    return str(dst) if dst.exists() else ""


def _preview_sibling(p: Path) -> Optional[Path]:
    """返回原件对应的预览小件路径（存在则返回，否则 None）。"""
    cand = p.with_name(f"{p.stem}{_PREVIEW_SUFFIX}.mp3")
    return cand if cand.exists() else None


def _is_preview_file(p: Path) -> bool:
    """是否为自动生成的预览小件（列表展示时需排除，避免污染库下拉）。"""
    return p.stem.endswith(_PREVIEW_SUFFIX)


def _rename_preview_sibling(old: Path, new: Path) -> None:
    """原件改名后同步搬移预览小件；失败仅记日志（下次试听会重建）。"""
    prev = _preview_sibling(old)
    if not prev:
        return
    try:
        prev.rename(new.with_name(f"{new.stem}{_PREVIEW_SUFFIX}.mp3"))
    except OSError:
        logger.warning("预览小件随原件改名失败（下次试听重建）: %s", prev.name)


def _build_stems(products: dict) -> list:
    """把 worker 分离/翻唱产物整理为 [{label,type,path,preview}, ...]，仅收录存在的音频文件。

    path=原件（合成用），preview=回放用小件（MP3，可为空串）。
    """
    stems = []
    for key, path in (products or {}).items():
        if not path or not Path(path).exists():
            continue
        stems.append({"label": _STEM_LABELS.get(key, key), "type": key, "path": str(path),
                      "preview": _make_preview(str(path))})
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

        新命名规范（文件管理重构）：outputs/separations_<时间戳>/（分离）或
        outputs/cover_<时间戳>/（翻唱）—— outputs 下单层目录，目录名 = 固定前缀
        +时间戳（不含项目名，改名只改文件名不动目录）。同秒撞名时等待取新时间戳。
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "separations_" if kind == "sep" else "cover_"
        d = self.webui_root / "outputs" / f"{prefix}{ts}"
        while d.exists():  # 同秒撞名（队列串行下罕见）：等待后取新时间戳避免覆盖
            time.sleep(1.0)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            d = self.webui_root / "outputs" / f"{prefix}{ts}"
        d.mkdir(parents=True, exist_ok=True)
        return d, ts

    # ---------------------------------------------------------------- 上传统一留存
    # 上传类别标识（文件名后缀段）：分离源 / 翻唱源 / 参考干声 / 转谱
    UPLOAD_CATEGORIES = ("sep_src", "cover_src", "dry_ref", "transcribe")
    # 可留存的音频扩展名白名单
    UPLOAD_EXTS = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".wma")

    def uploads_dir(self) -> Path:
        """上传文件统一留存目录（webui_root/uploads），不存在则创建。"""
        d = self.webui_root / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def persist_upload(self, src_path: str, category: str) -> str:
        """上传文件留存到 uploads/：<源文件名>_<时间戳>_<类别>.<ext>，返回落盘路径。

        同内容 md5 去重：与已有同类别留存内容一致时直接复用（源名/时间戳以首次
        留存为准），避免同一文件多次上传产生多份副本；比对失败回退直接拷贝。
        留存是尽力而为：任何异常仅记日志并返回原路径，不阻断任务。
        """
        import hashlib

        src = Path(src_path)
        if not src.exists() or category not in self.UPLOAD_CATEGORIES:
            return str(src)
        try:
            def _md5(p: Path) -> str:
                # 分块读取计算 md5，避免大文件一次读入内存
                h = hashlib.md5()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                return h.hexdigest()

            try:
                digest = _md5(src)
                src_size = src.stat().st_size
                for p in self.list_uploads(category):
                    try:
                        # 先比 size（廉价）：size 不同必然内容不同，跳过全量 md5 重算
                        if p.stat().st_size != src_size:
                            continue
                        if _md5(p) == digest:
                            return str(p)  # 内容相同：复用已有留存
                    except OSError:
                        continue
            except Exception:
                logger.exception("上传 md5 去重比对失败，回退直接拷贝")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = self.uploads_dir() / \
                f"{sanitize_project(src.stem)}_{ts}_{category}{src.suffix.lower() or '.wav'}"
            shutil.copyfile(src, dest)
            return str(dest)
        except Exception:
            logger.exception("上传留存失败(已忽略): %s", src_path)
            return str(src)

    def list_uploads(self, category: str) -> list:
        """列出 uploads/ 中指定类别的留存文件（按修改时间倒序，仅音频白名单）。"""
        out = []
        for p in sorted(self.uploads_dir().glob("*"),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            if p.is_file() and p.suffix.lower() in self.UPLOAD_EXTS \
                    and p.stem.endswith(f"_{category}"):
                out.append(p)
        return out

    def _recycle_created_derived(self, out_dir: Path) -> None:
        """回收本次任务已创建但未入历史的衍生目录（孤儿回收）。

        与 history._recycle_derived 规则一致：文件逐个移系统回收站 + 自底向上 rmdir 空目录，
        不使用 rm 整目录。回收失败仅记日志，不掩盖原始任务错误。
        """
        if not out_dir or not out_dir.is_dir():
            return
        try:
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
                stems: Optional[list] = None, project: str = "",
                source_md5: str = "") -> None:
        """追加一条音色工坊历史记录（不覆盖现有生成/转谱记录）。

        source_md5：源音频前 1MB MD5（separation/cover 记录写，用于查重跳过重复 Demucs）。
        """
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
            project=project,
            source_md5=source_md5,
        )
        self.history_mgr.append(record)
        self.history_mgr.auto_prune()

    # ---------------------------------------------------------------- 队列提交
    def run_in_queue_stream(self, task_type: TaskType, worker_fn: Callable,
                            lang: str, tr_fn: Callable, task_id_name: str,
                            on_submit: Optional[Callable[[str], None]] = None,
                            on_finish: Optional[Callable[[str], None]] = None,
                            **kwargs):
        """生成器版队列提交：yield 实时状态文案，完成时 return worker 结果 dict。

        worker_fn(task, **kwargs) -> dict；内部复用 queue_manager 的顺序执行保证显存安全。
        与回调式不同，本方法把排队/执行状态 yield 给 Gradio 生成器回调，
        前端组件（info 区 + 按钮）可实时反馈进度，避免任务运行期间界面无响应感。
        - 执行中文案附已用秒数（秒表）：worker 无细粒度进度，用耗时提供反馈
        - 相邻重复文案自动去重，减少无效前端更新
        - on_submit/on_finish：任务 id 的注册/注销回调（供 app 层挂接取消按钮的
          活动任务登记；模块不 import app，避免循环依赖）
        """
        # 捕获语言快照，供任务内文案使用（运行中切换语言不影响进行中任务）
        t = queue_manager.submit(task_type, worker_fn, cancel_event=threading.Event(),
                                 lang=lang, tr=tr_fn, **kwargs)
        if on_submit is not None:
            try:
                on_submit(t.task_id)
            except Exception:
                logger.exception("on_submit 回调失败(已忽略)")
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
        finally:
            if on_finish is not None:
                try:
                    on_finish(t.task_id)
                except Exception:
                    pass

    # ---------------------------------------------------------------- 队列 workers
    def separate_worker(self, _task, source: str, mode: str, root_task_id: str = "",
                        denoise: bool = False, lang: str = "zh", tr=None,
                        from_upload: bool = False, project: str = "",
                        **kwargs) -> dict:
        """音轨分离 worker：调用 worker 分离，独立产物文件夹 + 写历史记录，返回产物 dict。

        入队约定（见 run_in_queue_stream）：首个参数名为 _task，并吸收 lang/tr。
        产物落 outputs/separations_<时间戳>/，文件名 <项目名>_<时间戳>_<类别>.wav
        （项目名空则时间戳开头），按文件名即可辨识人声/伴奏等轨。
        from_upload=True 时源文件留存到 uploads/（<源名>_<ts>_sep_src.<ext>）。
        阶段进度经 out_dir/_progress.json 轮询上报（分离中/降噪中）。
        """
        project = sanitize_project(project or "")
        derived_from = root_task_id
        root = root_task_id or f"upload_{new_short_id()}"  # 上传源无生成任务目录，仅作记录
        out_dir, ts = self._derived_dir("sep")
        if from_upload:
            self.persist_upload(source, "sep_src")
        prefix = f"{project}_{ts}" if project else ts  # 产物文件名主干（worker 产物 = <prefix>_<类别>.wav）
        # 阶段进度文件 + 轮询线程（worker 写阶段，线程转发到任务进度通道）
        progress_file = out_dir / "_progress.json"
        stop_evt = threading.Event()
        poller = threading.Thread(target=_progress_poller,
                                  args=(_task, progress_file, lang, tr, stop_evt),
                                  daemon=True)
        poller.start()
        try:
            result = self.voice_client.separate(
                source, mode=mode, output_dir=str(out_dir), denoise=denoise,
                prefix=prefix, progress_file=str(progress_file),
                cancel_event=_task.cancel_event)
        except Exception:
            # worker 抛异常：回收本次已建但未入历史的衍生目录，再向上抛原始错误
            self._recycle_created_derived(out_dir)
            raise
        finally:
            stop_evt.set()
            try:
                progress_file.unlink(missing_ok=True)  # 清理进度文件，产物文件夹不留中间状态
            except Exception:
                pass
        if _task.cancel_event.is_set():
            self._recycle_created_derived(out_dir)
            raise TaskCancelledError("任务已取消")
        if not result.ok:
            self._recycle_created_derived(out_dir)
            # 取消来源两路：主 app cancel_event（UI 取消按钮）或 worker 侧 cancelled
            # （直接 POST /api/cancel 等）——任一命中都映射为"已取消"而非"任务失败"
            if _task.cancel_event.is_set() or result.cancelled:
                raise TaskCancelledError("任务已取消")
            raise RuntimeError(result.error or "音轨分离失败")
        # 换算音频时长（取 vocals 轨）；并收集各轨供历史查看/回放
        products = result.products or {}
        duration = _probe_duration(products.get("vocals", ""))
        stems = _build_stems(products)
        # 计算源音频 MD5（分离记录留痕，查重跳过后续翻唱的 Demucs）
        src_md5 = HistoryManager.compute_source_md5(Path(source))
        self._record(_task.task_id, "separation", derived_from, root,
                     products.get("vocals", ""), out_dir,
                     duration=duration, style=_source_label(source), stems=stems,
                     project=project, source_md5=src_md5)
        return {"products": products, "stems": stems,
                "output_dir": str(out_dir), "root_task_id": root}

    def cover_worker(self, _task, source: str, ref: str, accompaniment: str,
                     semi_tone: int, diffusion_steps: int, gain_db: float,
                     denoise: bool = False,
                     root_task_id: str = "", lang: str = "zh", tr=None,
                     source_vocals: str = "", source_acc: str = "",
                     from_upload: bool = False, project: str = "",
                     ref_mode: str = "smart", ref_acc: str = "",
                     **kwargs) -> dict:
        """参考音色翻唱 worker：完整管线（换嗓+混音）+ 写历史记录。denoise=True 时对换嗓人声降噪。

        产物落 outputs/cover_<时间戳>/，文件名 <项目名>_<时间戳>_<类别>.wav
        （项目名 = 源项目名_音色名，由 app 层拼好传入）。
        source_vocals/source_acc 非空时复用已有分离结果（跳过 Demucs 重复分离）。
        ref_mode=参考段策略（smart=智能/默认、energy=能量最高段、full=整曲不裁剪）；
        ref_acc=参考干声的配对伴奏轨（仅"分离人声"来源有，smart 挑段用）。
        from_upload=True 时源文件留存到 uploads/（<源名>_<ts>_cover_src.<ext>）。
        阶段进度经 out_dir/_progress.json 轮询上报（分离中/换嗓中/降噪中/混音中）。
        """
        project = sanitize_project(project or "")
        derived_from = root_task_id
        root = root_task_id or f"upload_{new_short_id()}"
        out_dir, ts = self._derived_dir("cover")
        if from_upload:
            self.persist_upload(source, "cover_src")
        prefix = f"{project}_{ts}" if project else ts  # 产物文件名主干
        # 阶段进度文件 + 轮询线程（worker 写阶段，线程转发到任务进度通道）
        progress_file = out_dir / "_progress.json"
        stop_evt = threading.Event()
        poller = threading.Thread(target=_progress_poller,
                                  args=(_task, progress_file, lang, tr, stop_evt),
                                  daemon=True)
        poller.start()
        try:
            result = self.voice_client.convert(
                source, ref, semi_tone=semi_tone, diffusion_steps=diffusion_steps,
                accompaniment=accompaniment, gain_db=gain_db, output_dir=str(out_dir),
                denoise=denoise, prefix=prefix,
                source_vocals=source_vocals, source_acc=source_acc,
                progress_file=str(progress_file), cancel_event=_task.cancel_event,
                ref_mode=ref_mode, ref_acc=ref_acc,
            )
        except Exception:
            # worker 抛异常：回收本次已建但未入历史的衍生目录，再向上抛原始错误
            self._recycle_created_derived(out_dir)
            raise
        finally:
            stop_evt.set()
            try:
                progress_file.unlink(missing_ok=True)  # 清理进度文件，产物文件夹不留中间状态
            except Exception:
                pass
        if _task.cancel_event.is_set():
            self._recycle_created_derived(out_dir)
            raise TaskCancelledError("任务已取消")
        if not result.ok:
            self._recycle_created_derived(out_dir)
            # 取消来源两路：主 app cancel_event（UI 取消按钮）或 worker 侧 cancelled
            # （直接 POST /api/cancel 等）——任一命中都映射为"已取消"而非"任务失败"
            if _task.cancel_event.is_set() or result.cancelled:
                raise TaskCancelledError("任务已取消")
            raise RuntimeError(result.error or "翻唱失败")
        cover_path = result.products.get("cover", "")
        duration = _probe_duration(cover_path)
        stems = _build_stems(result.products)
        self._record(_task.task_id, "cover", derived_from, root,
                     cover_path, out_dir, duration=duration,
                     style=f"cover+{semi_tone:+.0f}", stems=stems,
                     project=project)
        return {"products": result.products, "stems": stems,
                "output_dir": str(out_dir), "root_task_id": root}

    def ensure_separation(self, source: str, project: str = "",
                          cancel_event: Optional[threading.Event] = None) -> str:
        """确保源音频有持久化的 separation 记录，返回 sep_task:<task_id>。

        先查 history（source_md5 前 1MB），有现成且双轨齐全就直接复用；
        没有就同步调 worker separate + 手动写 history 记录。
        供翻唱 Tab 调用 —— 让 Demucs 分离流程与「音轨分离」Tab 完全一致
        （同样的目录结构、同样的 HistoryRecord 字段），后续同曲翻唱自动跳过分离。
        cancel_event 透传给 worker 协作取消（该同步阶段此前不响应 UI 取消）。
        """
        src_path = Path(source)
        if not src_path.exists():
            raise RuntimeError(f"源音频不存在: {source}")

        # 1. 查重：算 md5 + 查 history
        md5 = HistoryManager.compute_source_md5(src_path)
        existing = self.history_mgr.find_separation_by_source(md5)
        if existing:
            return f"sep_task:{existing.task_id}"

        # 2. 没有 → 同步分离 + 写 history（与 separate_worker 流程一致）
        task_id = f"separation_{new_short_id(10)}"
        out_dir, ts = self._derived_dir("sep")
        project = sanitize_project(project or "")
        prefix = f"{project}_{ts}" if project else ts  # 产物文件名主干

        try:
            result = self.voice_client.separate(
                source, mode="2", output_dir=str(out_dir), prefix=prefix,
                cancel_event=cancel_event)
        except Exception:
            # worker 抛异常：回收本次已建但未入历史的衍生目录
            self._recycle_created_derived(out_dir)
            raise

        if not result.ok:
            self._recycle_created_derived(out_dir)
            # 取消来源两路：UI 取消（cancel_event）或 worker 侧 cancelled —— 命中即视为取消
            if (cancel_event is not None and cancel_event.is_set()) or result.cancelled:
                raise TaskCancelledError("任务已取消")
            raise RuntimeError(result.error or "同步分离失败")

        products = result.products or {}
        stems = _build_stems(products)
        duration = _probe_duration(products.get("vocals", ""))
        self._record(task_id, "separation", "", "",
                     products.get("vocals", ""), out_dir,
                     duration=duration, style=_source_label(source), stems=stems,
                     project=project, source_md5=md5)
        return f"sep_task:{task_id}"

    # ---------------------------------------------------------------- 音色库
    def refs_dir(self) -> Path:
        """音色参考库目录（voice-tools/refs），不存在则创建。"""
        d = self.webui_root / "voice-tools" / "refs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_ref(self, src_path: str, name: str) -> str:
        """把参考干声存入音色库，返回落盘路径。"""
        refs = self.refs_dir()
        safe = "".join(c for c in name.strip() if c.isalnum() or c in "_- ").strip() or "ref"
        dest = refs / f"{safe}_{new_short_id(4)}.wav"
        shutil.copyfile(src_path, dest)
        return str(dest)

    def list_refs(self) -> list[str]:
        """列出音色库中所有参考音频绝对路径。"""
        refs = self.refs_dir()
        exts = (".wav", ".mp3", ".flac", ".m4a", ".ogg")
        # 排除自动生成的 _preview.mp3 小件（试听用，非用户条目）
        return [str(p) for p in sorted(refs.glob("*"))
                if p.suffix.lower() in exts and not _is_preview_file(p)]

    def delete_ref(self, path: str) -> None:
        """删除音色库条目：移入系统回收站（文件级，不直接删除），预览小件一并回收。"""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        prev = _preview_sibling(p)
        delete_files_to_recycle([p] + ([prev] if prev else []))

    def rename_ref(self, path: str, new_name: str) -> str:
        """重命名音色库条目（保留 名_短id.wav 格式），返回新路径。

        保留原文件名的短 id 后缀（_xxxx），保证唯一性约束不变；重名冲突时抛异常。
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        safe = "".join(c for c in new_name.strip() if c.isalnum() or c in "_- ").strip()
        if not safe:
            raise ValueError("名称不能为空")
        # 提取原短 id（文件名最后一个 _ 后的 4 位短 id）；提取不到则新生成
        stem = p.stem
        old_id = stem.rsplit("_", 1)[-1] if "_" in stem else ""
        short_id = old_id if len(old_id) == 4 and all(c in _SHORT_ID_CHARS for c in old_id) \
            else new_short_id(4)
        dest = p.parent / f"{safe}_{short_id}{p.suffix.lower()}"
        if dest.exists() and dest.resolve() != p.resolve():
            raise FileExistsError(f"已存在同名条目: {dest.name}")
        p.rename(dest)
        _rename_preview_sibling(p, dest)   # 同步搬移预览小件，避免留下孤儿
        return str(dest)

    # ---------------------------------------------------------------- 上传干音历史（第二来源）
    def save_dry_upload(self, src_path: str) -> str:
        """把上传且检测通过的人声干音留存到 uploads/（类别 dry_ref），返回落盘路径。

        文件管理重构：与所有上传统一走 persist_upload（<源名>_<时间戳>_dry_ref.<ext>），
        内容 md5 去重避免重复副本。
        """
        return self.persist_upload(src_path, "dry_ref")

    def list_dry_uploads(self) -> list[str]:
        """列出所有已验证干音绝对路径（uploads/ 下 *_dry_ref.*，按修改时间倒序）。"""
        return [str(p) for p in self.list_uploads("dry_ref")]

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
            if p.suffix.lower() not in exts or _is_preview_file(p):
                continue
            # 文件名约定 名__类型.ext；无 __ 分隔时归为 other
            stem = p.stem
            name, _, stype = stem.rpartition("__")
            if not name or stype not in self.STEM_TYPES:
                name, stype = stem, "other"
            out.append((name, stype, str(p)))
        return out

    def delete_stem(self, path: str) -> None:
        """删除素材库条目：移入系统回收站（文件级，不直接删除），预览小件一并回收。"""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        prev = _preview_sibling(p)
        delete_files_to_recycle([p] + ([prev] if prev else []))

    def rename_stem(self, path: str, new_name: str) -> str:
        """重命名素材库条目（保留 名__轨道类型.ext 格式），返回新路径。

        轨道类型从原文件名解析并保留（重命名只改显示名，不改轨道归属）。
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {path}")
        safe = "".join(c for c in new_name.strip() if c.isalnum() or c in "_- ").strip()
        if not safe:
            raise ValueError("名称不能为空")
        # 从原文件名解析轨道类型；解析不到回退 other
        _, _, stype = p.stem.rpartition("__")
        if stype not in self.STEM_TYPES:
            stype = "other"
        dest = p.parent / f"{safe}__{stype}{p.suffix.lower()}"
        if dest.exists() and dest.resolve() != p.resolve():
            raise FileExistsError(f"已存在同名条目: {dest.name}")
        p.rename(dest)
        _rename_preview_sibling(p, dest)   # 同步搬移预览小件，避免留下孤儿
        return str(dest)


def _probe_duration(path: str) -> float:
    """用 ffmpeg/音速获取音频时长；失败返回 0。仅做展示用途，异常不抛出。"""
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