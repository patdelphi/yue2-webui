"""Generation history manager with JSON persistence."""
import json
import sys
import threading
from pathlib import Path
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional

# 历史记录最多保留条数，超出后自动删除最旧记录（auto_prune 使用）
HISTORY_MAX_ENTRIES = 100


def _delete_to_recycle(path: Path) -> bool:
    """将单个文件移入系统回收站（Windows）。成功返回 True，否则返回 False。"""
    try:
        import ctypes
        from ctypes import wintypes

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40
        FOF_NOCONFIRMATION = 0x10
        FOF_SILENT = 0x4

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", ctypes.c_uint),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = str(path) + "\0\0"
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
        result = ctypes.windll.shell32.SHFILEOperationW(ctypes.byref(op))
        return result == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


def delete_files_to_recycle(files: list) -> int:
    """文件级删除：优先把存在的文件移入系统回收站，失败或非 Windows 时回退为直接删除。"""
    removed = 0
    paths = [f for f in files if Path(f).exists()]
    if not paths:
        return 0

    if sys.platform == "win32":
        for p in paths:
            if _delete_to_recycle(p):
                removed += 1
            else:
                # 回收站失败则回退为直接删除，避免残留
                try:
                    Path(p).unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    for p in paths:
        try:
            Path(p).unlink()
            removed += 1
        except OSError:
            pass
    return removed


@dataclass
class HistoryRecord:
    """A single generation history entry."""
    task_id: str
    created_at: str
    style: str
    lyrics: str = ""
    lyrics_preview: str = ""
    cot: str = ""
    seed: int = 0
    cfg_scale: float = 0
    num_inference_steps: int = 8
    batch_count: int = 1
    audio_duration_seconds: float = 0
    generation_time_seconds: float = 0
    audio_path: str = ""
    output_dir: str = ""
    backend: str = "gguf"
    status: str = "completed"
    abc_path: str = ""
    out_format: str = "pcm16"
    # 音色工坊衍生记录（可选字段，默认值保证旧记录兼容）
    record_type: str = "generation"   # generation | separation | cover
    derived_from: str = ""            # 直接前驱 task_id（分离/翻唱记录必填）
    root_task_id: str = ""            # 顶层源任务 id（链式翻唱定位聚合目录）
    stems: list = field(default_factory=list)  # 多轨产物：[{label, type, path}, ...]，供历史查看/回放


class HistoryManager:
    """Manage generation history with JSON persistence."""

    def __init__(self, history_file: Path, outputs_root: Path):
        self.history_file = Path(history_file)
        self.outputs_root = Path(outputs_root)
        self._entries: list[HistoryRecord] = []
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        """Load history from JSON file."""
        if not self.history_file.exists():
            self._entries = []
            return

        try:
            data = json.loads(self.history_file.read_text(encoding="utf-8"))
            self._entries = [HistoryRecord(**e) for e in data.get("entries", [])]
        except (json.JSONDecodeError, KeyError, TypeError):
            self._entries = []

    def prune_missing(self) -> int:
        """Drop entries whose audio file no longer exists on disk.

        【副轨保留】判定不再只凭 audio_path 是否存在：若记录含 stems（音色工坊分离/翻唱
        各轨）且任一 stem 文件仍存在于磁盘，即便主轨丢失也保留该记录，避免整条误删。
        既无 audio_path 也无任何可用的 stem 时，才视为缺失并删除。
        """
        with self._lock:
            kept = []
            removed = 0
            for e in self._entries:
                # 主轨存在 -> 保留
                if e.audio_path and Path(e.audio_path).exists():
                    kept.append(e)
                    continue
                # 主轨缺失但任一副轨仍在 -> 保留（副轨记录仍有回放价值）
                stems = getattr(e, "stems", None) or []
                if any(
                    Path(s.get("path", "")).exists()
                    for s in stems if isinstance(s, dict) and s.get("path")
                ):
                    kept.append(e)
                    continue
                removed += 1
            if removed:
                self._entries = kept
                self._save()
            return removed

    def _save(self):
        """Persist history to JSON file."""
        data = {
            "version": 1,
            "entries": [asdict(e) for e in self._entries],
        }
        self.history_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def append(self, record: HistoryRecord):
        """Add a new history entry."""
        with self._lock:
            self._entries.insert(0, record)
            self._save()

    def list_all(self) -> list[HistoryRecord]:
        """Return all entries (newest first)."""
        return list(self._entries)

    def get(self, task_id: str) -> Optional[HistoryRecord]:
        """Get a single entry by task_id."""
        for e in self._entries:
            if e.task_id == task_id:
                return e
        return None

    def get_abc_score(self, task_id: str) -> Optional[str]:
        """Get the ABC score text for a history entry."""
        entry = self.get(task_id)
        if not entry or not entry.abc_path:
            return None
        abc_file = self.outputs_root.parent / entry.abc_path
        if abc_file.exists():
            return abc_file.read_text(encoding="utf-8")
        return None

    def set_status(self, task_id: str, status: str) -> bool:
        """Update the status of a history entry."""
        with self._lock:
            entry = self.get(task_id)
            if not entry:
                return False
            entry.status = status
            self._save()
            return True

    def _derived_dir_for(self, entry) -> Optional[Path]:
        """返回音色工坊记录专属的产物目录绝对路径，非专属（不可安全回收）时返回 None。

        安全判定（避免误删共享目录／他人文件）：
        - 仅 separation/cover 记录才可能产生专属产物目录；
        - 新结构：output_dir 指向 outputs/separations/<dir> 或 outputs/covers/<dir>
          （独立产物文件夹，整目录属于该记录）；
        - 旧结构兼容：output_dir 指向 outputs/<root>/derived/<kind>_<shortid>
          （派生目录名与记录类型匹配：separation->sep，cover->cover）；
        - 该目录必须位于本 outputs_root 之下。满足才认为可整目录回收。
        """
        if getattr(entry, "record_type", "") not in ("separation", "cover"):
            return None
        if not getattr(entry, "output_dir", ""):
            return None
        # output_dir 为相对 webui_root（=outputs_root.parent）的路径
        out_abs = (self.outputs_root.parent / entry.output_dir)
        try:
            out_abs = out_abs.resolve()
        except OSError:
            return None
        # 必须位于 outputs_root 下
        try:
            rel = out_abs.relative_to(self.outputs_root)
        except ValueError:
            return None
        # 新结构：outputs/<separations|covers>/<dir>（两段，独立产物文件夹）
        kind_dir = "separations" if entry.record_type == "separation" else "covers"
        if len(rel.parts) == 2 and rel.parts[0] == kind_dir:
            if out_abs.is_dir():
                return out_abs
            return None
        # 旧结构兼容：outputs/<root>/derived/<kind>_<xxx>（历史存量记录仍可整目录回收）
        if len(rel.parts) >= 3 and rel.parts[-3] == "derived":
            kind = "sep" if entry.record_type == "separation" else "cover"
            dname = rel.parts[-2]  # 派生目录名
            if not dname.startswith(f"{kind}_"):
                return None
            if out_abs.is_dir():
                return out_abs
        return None

    def _recycle_derived(self, entry) -> int:
        """整目录回收音色工坊记录专属的 derived 子目录，返回删除文件数。

        对比逐文件 _files_for：覆盖 derived 内散落但未登记在 stems 的中间产物
        （denoise 孤儿轨、换嗓中间文件等），避免目录无限堆积。
        约束（项目要求）：不以 rm 直接删目录 —— 先逐个删除内部文件（移回收站），
        再从最深到最浅 rmdir 空目录；中间任一非空目录 rmdir 失败则无害跳过。
        """
        out_abs = self._derived_dir_for(entry)
        if out_abs is None:
            return 0
        files = [p for p in out_abs.rglob("*") if p.is_file()]
        removed = delete_files_to_recycle(files)
        # 自底向上删除变为空目录；非空目录 rmdir 会失败，天然避免误删他人文件
        dirs = sorted((p for p in out_abs.rglob("*") if p.is_dir()),
                      key=lambda p: len(p.parts), reverse=True)
        for d in dirs:
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            out_abs.rmdir()
        except OSError:
            pass
        return removed

    def _files_for(self, entry) -> list:
        """收集一条记录关联的产出文件（音频 / MP3 / 乐谱 / 歌词 / 元数据）。"""
        files = []
        wav = Path(entry.audio_path)
        if wav.exists():
            files.append(wav)
        for suffix in (".mp3", ".abc", ".txt", ".json"):
            candidate = wav.with_suffix(suffix)
            if candidate.exists():
                files.append(candidate)

        # 多轨产物（音色工坊分离/翻唱各轨），一并纳入删除范围
        for stem in getattr(entry, "stems", None) or []:
            p = Path(stem.get("path", "")) if isinstance(stem, dict) else Path()
            if p.exists() and p not in files:
                files.append(p)

        # 兼容乐谱存于独立目录的旧记录（abc_path 为相对 WEBUI_ROOT 的路径）
        if entry.abc_path:
            abc = self.outputs_root.parent / entry.abc_path
            if abc.exists() and abc not in files:
                files.append(abc)
        return files

    def delete(self, task_id: str) -> bool:
        """删除一条历史记录及其产出文件（文件级删除，移入系统回收站）。

        separation/cover 记录在逐文件回收后，再整目录回收其专属 derived 子目录，
        兜底清理未登记在 stems 的孤儿中间文件（denoise 轨、换嗓中间文件等）。
        """
        with self._lock:
            entry = self.get(task_id)
            if not entry:
                return False

            delete_files_to_recycle(self._files_for(entry))
            self._recycle_derived(entry)

            self._entries = [e for e in self._entries if e.task_id != task_id]
            self._save()
            return True

    def clear(self):
        """Clear all history entries and remove their output files."""
        with self._lock:
            for entry in self._entries:
                delete_files_to_recycle(self._files_for(entry))
                self._recycle_derived(entry)
            self._entries = []
            self._save()

    def auto_prune(self, max_entries: int = HISTORY_MAX_ENTRIES):
        """Remove oldest entries if over the limit."""
        with self._lock:
            if len(self._entries) <= max_entries:
                return

            to_remove = self._entries[max_entries:]
            self._entries = self._entries[:max_entries]

            for entry in to_remove:
                delete_files_to_recycle(self._files_for(entry))
                self._recycle_derived(entry)

            self._save()

    def to_dataframe_rows(self) -> list[list]:
        """Convert entries to rows for Gradio Dataframe."""
        self.prune_missing()
        rows = []
        for e in self._entries:
            style_short = e.style[:40] + "..." if len(e.style) > 40 else e.style
            if e.status == "final":
                style_short = f"🏆 {style_short}"
            rows.append([
                e.created_at,
                style_short,
                e.cot,
                f"{e.audio_duration_seconds:.1f}s",
                f"{e.generation_time_seconds:.1f}s",
                e.task_id,
            ])
        return rows
