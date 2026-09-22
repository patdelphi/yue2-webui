"""Generation history manager with JSON persistence."""
import json
import shutil
import threading
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


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

    def delete(self, task_id: str) -> bool:
        """Delete a history entry and its output directory."""
        with self._lock:
            entry = self.get(task_id)
            if not entry:
                return False

            output_path = self.outputs_root.parent / entry.output_dir
            if output_path.exists():
                shutil.rmtree(output_path, ignore_errors=True)

            self._entries = [e for e in self._entries if e.task_id != task_id]
            self._save()
            return True

    def clear(self):
        """Clear all history entries and output directories."""
        with self._lock:
            for entry in self._entries:
                output_path = self.outputs_root.parent / entry.output_dir
                if output_path.exists():
                    shutil.rmtree(output_path, ignore_errors=True)
            self._entries = []
            self._save()

    def auto_prune(self, max_entries: int = 100):
        """Remove oldest entries if over the limit."""
        with self._lock:
            if len(self._entries) <= max_entries:
                return

            to_remove = self._entries[max_entries:]
            self._entries = self._entries[:max_entries]

            for entry in to_remove:
                output_path = self.outputs_root.parent / entry.output_dir
                if output_path.exists():
                    shutil.rmtree(output_path, ignore_errors=True)

            self._save()

    def to_dataframe_rows(self) -> list[list]:
        """Convert entries to rows for Gradio Dataframe."""
        self.prune_missing()
        rows = []
        for e in self._entries:
            style_short = e.style[:40] + "..." if len(e.style) > 40 else e.style
            rows.append([
                e.created_at,
                style_short,
                e.cot,
                f"{e.audio_duration_seconds:.1f}s",
                f"{e.generation_time_seconds:.1f}s",
                e.task_id,
            ])
        return rows
