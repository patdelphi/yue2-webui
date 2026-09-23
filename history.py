"""Generation history manager with JSON persistence."""
import json
import sys
import threading
from pathlib import Path
from dataclasses import dataclass, asdict
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
        """Drop entries whose audio file no longer exists on disk."""
        with self._lock:
            kept = []
            removed = 0
            for e in self._entries:
                if e.audio_path and Path(e.audio_path).exists():
                    kept.append(e)
                else:
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

        # 兼容乐谱存于独立目录的旧记录（abc_path 为相对 WEBUI_ROOT 的路径）
        if entry.abc_path:
            abc = self.outputs_root.parent / entry.abc_path
            if abc.exists() and abc not in files:
                files.append(abc)
        return files

    def delete(self, task_id: str) -> bool:
        """删除一条历史记录及其产出文件（文件级删除，移入系统回收站）。"""
        with self._lock:
            entry = self.get(task_id)
            if not entry:
                return False

            delete_files_to_recycle(self._files_for(entry))

            self._entries = [e for e in self._entries if e.task_id != task_id]
            self._save()
            return True

    def clear(self):
        """Clear all history entries and remove their output files."""
        with self._lock:
            for entry in self._entries:
                delete_files_to_recycle(self._files_for(entry))
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
