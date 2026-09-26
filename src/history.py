"""生成历史管理器：SQLite 持久化（WAL 模式）。

存储设计：
- 一行一条记录，自增 id 保持插入顺序；list_all 按 id 倒序（最新在前），
  与旧 JSON 版行为一致。
- stems（多轨产物 list[dict]）以 JSON 文本列存储，读写时自动序列化/反序列化。
- 所有写操作在 RLock + `with conn` 事务中执行（异常自动回滚），线程安全。
- PRAGMA journal_mode=WAL + synchronous=NORMAL（WAL 替代回滚日志，减少 IO 开销）。
- source_md5：源音频文件前 1MB 的 MD5，用于查重避免重复 Demucs 分离
  （翻唱时若源音频已存在 separation 记录，直接复用跳过分离）。
"""
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

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
        # 注意：API 名为 SHFileOperationW（仅 SH 大写）。此前误写为 SHFILEOperationW，
        # ctypes 抛 AttributeError 被下方 except 吞掉、静默回退为直接删除，导致
        # "移入回收站"从未真正生效（文件被 unlink 直删，违背可还原约束）
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
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


# 项目产物文件名规范：<项目名>_<时间戳>[_后缀].<ext>（项目名可空 -> 时间戳开头）
# 解析用途：rename_project 按 ts 段定位，仅替换项目名段，保留时间戳/后缀
_FILENAME_TS_RE = re.compile(r"^(?:(?P<proj>.+)_)?(?P<ts>\d{8}_\d{6})(?P<rest>.*)$")

# 项目目录安全前缀（outputs 下单层目录），delete_project 仅回收带这些前缀的目录
_PROJECT_DIR_PREFIXES = ("song_", "separations_", "cover_")


def sanitize_project(name: str) -> str:
    """清洗项目名：替换文件系统非法字符/控制符为下划线，限 60 字符，去尾部空白与点。"""
    cleaned = "".join("_" if (c in '\\/:*?"<>|' or ord(c) < 32) else c
                      for c in (name or "").strip())
    return cleaned[:60].rstrip(" .")


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
    project: str = ""                 # 项目名（文件名前缀；空 = 无项目名，文件以时间戳开头）
    source_md5: str = ""              # 源音频前 1MB MD5（分离/翻唱记录写，查重复用）


class HistoryManager:
    """生成历史管理器：SQLite 持久化（WAL 模式 + 事务）。

    一行一条记录，自增 id 保持插入顺序；list_all 按 id 倒序（最新在前），
    与旧 JSON 版行为一致。stems（多轨产物）以 JSON 文本列存储。
    """

    # 除自增 id 外的列（与 HistoryRecord 字段一一对应；stems 为 JSON 文本列）
    _COLS = ("task_id", "created_at", "style", "lyrics", "lyrics_preview", "cot",
             "seed", "cfg_scale", "num_inference_steps", "batch_count",
             "audio_duration_seconds", "generation_time_seconds",
             "audio_path", "output_dir", "backend", "status", "abc_path",
             "out_format", "record_type", "derived_from", "root_task_id",
             "stems", "project", "source_md5")

    def __init__(self, db_file: Path, outputs_root: Path):
        self.db_file = Path(db_file)
        self.outputs_root = Path(outputs_root)
        self._lock = threading.RLock()
        # 单连接 + RLock 保证线程安全；check_same_thread=False 允许队列线程共用
        self._conn = sqlite3.connect(str(self.db_file), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        """建表 + 性能 PRAGMA（WAL 替代回滚日志，synchronous=NORMAL 减少 IO）。"""
        try:
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        created_at TEXT DEFAULT '',
                        style TEXT DEFAULT '',
                        lyrics TEXT DEFAULT '',
                        lyrics_preview TEXT DEFAULT '',
                        cot TEXT DEFAULT '',
                        seed INTEGER DEFAULT 0,
                        cfg_scale REAL DEFAULT 0,
                        num_inference_steps INTEGER DEFAULT 8,
                        batch_count INTEGER DEFAULT 1,
                        audio_duration_seconds REAL DEFAULT 0,
                        generation_time_seconds REAL DEFAULT 0,
                        audio_path TEXT DEFAULT '',
                        output_dir TEXT DEFAULT '',
                        backend TEXT DEFAULT 'gguf',
                        status TEXT DEFAULT 'completed',
                        abc_path TEXT DEFAULT '',
                        out_format TEXT DEFAULT 'pcm16',
                        record_type TEXT DEFAULT 'generation',
                        derived_from TEXT DEFAULT '',
                        root_task_id TEXT DEFAULT '',
                        stems TEXT DEFAULT '[]',
                        project TEXT DEFAULT '',
                        source_md5 TEXT DEFAULT ''
                    )
                """)
                # 旧库迁移：自动追加缺失列（IF NOT EXISTS 避免新库出错）
                try:
                    self._conn.execute("ALTER TABLE history ADD COLUMN source_md5 TEXT DEFAULT ''")
                    logger.info("已添加 source_md5 列（旧库迁移）")
                except sqlite3.OperationalError:
                    pass  # 列已存在
                self._conn.commit()
        except sqlite3.Error:
            logger.exception("历史数据库初始化失败: %s", self.db_file)
            raise

    def close(self):
        """显式关闭数据库连接（测试清理 / 进程退出前调用，释放文件句柄）。"""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __del__(self):
        # 兜底：对象回收时关闭连接，避免 Windows 下句柄滞留锁住 .db 文件
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ---------------------------------------------------------------- 存取基础
    @staticmethod
    def _row_to_record(row) -> HistoryRecord:
        """sqlite3.Row -> HistoryRecord（stems 列反序列化为 list）。"""
        d = {c: row[c] for c in row.keys() if c != "id"}
        d["stems"] = json.loads(d.get("stems") or "[]")
        return HistoryRecord(**d)

    @staticmethod
    def _record_values(record: HistoryRecord) -> dict:
        """HistoryRecord -> 列值 dict（stems 序列化为 JSON 文本）。"""
        d = asdict(record)
        d["stems"] = json.dumps(d.get("stems") or [], ensure_ascii=False)
        return d

    def _insert(self, record: HistoryRecord) -> None:
        """插入一条记录（事务：异常自动回滚）。"""
        with self._conn:
            self._conn.execute(
                f"INSERT INTO history ({', '.join(self._COLS)}) "
                f"VALUES ({', '.join(':' + c for c in self._COLS)})",
                self._record_values(record))

    def _update(self, row_id: int, record: HistoryRecord) -> None:
        """按行 id 更新一条记录（事务：异常自动回滚）。"""
        with self._conn:
            self._conn.execute(
                f"UPDATE history SET {', '.join(c + ' = :' + c for c in self._COLS)} "
                "WHERE id = :rid", {**self._record_values(record), "rid": row_id})

    def _delete_ids(self, row_ids: list) -> None:
        """按行 id 批量删除（事务：异常自动回滚）。"""
        if not row_ids:
            return
        with self._conn:
            self._conn.execute(
                f"DELETE FROM history WHERE id IN ({','.join('?' * len(row_ids))})",
                row_ids)

    def _select_all(self) -> list:
        """全部记录 [(row_id, record), ...]，最新在前（id 倒序）。"""
        rows = self._conn.execute(
            "SELECT * FROM history ORDER BY id DESC").fetchall()
        return [(r["id"], self._row_to_record(r)) for r in rows]

    @staticmethod
    def _record_files_alive(e: HistoryRecord) -> bool:
        """记录是否仍有可用文件：主轨存在，或任一 stem 副轨仍在磁盘。"""
        if e.audio_path and Path(e.audio_path).exists():
            return True
        stems = getattr(e, "stems", None) or []
        return any(Path(s.get("path", "")).exists()
                   for s in stems if isinstance(s, dict) and s.get("path"))

    def prune_missing(self) -> int:
        """Drop entries whose audio file no longer exists on disk.

        【副轨保留】判定不再只凭 audio_path 是否存在：若记录含 stems（音色工坊分离/翻唱
        各轨）且任一 stem 文件仍存在于磁盘，即便主轨丢失也保留该记录，避免整条误删。
        既无 audio_path 也无任何可用的 stem 时，才视为缺失并删除。
        """
        with self._lock:
            removed_ids = [rid for rid, e in self._select_all()
                           if not self._record_files_alive(e)]
            self._delete_ids(removed_ids)
            return len(removed_ids)

    def append(self, record: HistoryRecord):
        """Add a new history entry."""
        with self._lock:
            self._insert(record)

    def list_all(self) -> list[HistoryRecord]:
        """Return all entries (newest first)."""
        with self._lock:
            return [rec for _rid, rec in self._select_all()]

    def get(self, task_id: str) -> Optional[HistoryRecord]:
        """Get a single entry by task_id (latest if duplicated)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM history WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,)).fetchone()
            return self._row_to_record(row) if row else None

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
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE history SET status = ? WHERE task_id = ?",
                    (status, task_id))
            return cur.rowcount > 0

    def _resolve_output_dir(self, output_dir: str) -> Optional[Path]:
        """解析记录的 output_dir（相对 webui_root）为绝对路径；越界（不在 outputs 下）返回 None。"""
        if not output_dir:
            return None
        try:
            d = (self.outputs_root.parent / output_dir).resolve()
            d.relative_to(self.outputs_root.resolve())
        except (OSError, ValueError):
            return None
        return d

    def _same_output_dir(self, a: str, b: str) -> bool:
        """判断两个 output_dir（相对路径）是否指向同一目录（规范化比较）。"""
        if not a or not b:
            return False
        try:
            return (self.outputs_root.parent / a).resolve() \
                == (self.outputs_root.parent / b).resolve()
        except OSError:
            return False

    def _derived_dir_for(self, entry) -> Optional[Path]:
        """返回分离/翻唱记录专属的产物目录绝对路径，非专属（不可安全回收）时返回 None。

        新结构：output_dir 指向 outputs/<separations_<ts>|cover_<ts>>（outputs 下
        单层目录，整目录属于该任务）；目录必须位于本 outputs_root 之下才可整目录回收。
        """
        if getattr(entry, "record_type", "") not in ("separation", "cover"):
            return None
        if not getattr(entry, "output_dir", ""):
            return None
        out_abs = self._resolve_output_dir(entry.output_dir)
        if out_abs is None:
            return None
        try:
            rel = out_abs.relative_to(self.outputs_root)
        except ValueError:
            return None
        # 新结构：outputs/<separations_<ts>|cover_<ts>>（单层，独立产物文件夹）
        prefix = "separations_" if entry.record_type == "separation" else "cover_"
        if len(rel.parts) == 1 and rel.parts[0].startswith(prefix):
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
            matched = [(rid, e) for rid, e in self._select_all()
                       if e.task_id == task_id]
            if not matched:
                return False

            for _rid, entry in matched:
                delete_files_to_recycle(self._files_for(entry))
                self._recycle_derived(entry)
                # 目录已空则一并移除（如 song_<ts> 内最后一个变体被删后）
                self._rmdir_if_empty(entry)

            self._delete_ids([rid for rid, _ in matched])
            return True

    def rename_project(self, output_dir: str, new_project: str) -> int:
        """修改一个产物项目的项目名：重命名目录内文件并同步历史记录。

        文件名规范 <项目名>_<时间戳>[_后缀].<ext>：仅替换项目名段，保留时间戳与
        后缀（批量变体 _varN、轨道类别等）；new_project 为空则去掉项目名段。
        目录名不动（song_<ts> 等固定前缀+时间戳），改名只影响文件名。
        同步更新该目录全部记录的 project / audio_path / abc_path / stems[].path。
        返回重命名的文件数（0 也可能仅更新了记录中的项目名）。
        """
        new_project = sanitize_project(new_project)
        with self._lock:
            d = self._resolve_output_dir(output_dir)
            if d is None or not d.is_dir():
                return 0
            webui_root = self.outputs_root.parent
            renames: dict = {}  # normcase(旧绝对路径) -> 新绝对路径
            for f in sorted(d.iterdir()):
                if not f.is_file():
                    continue
                m = _FILENAME_TS_RE.match(f.stem)
                if not m:
                    continue  # 不符合命名规范（如 _progress.json）的文件不动
                stem = f"{new_project}_{m.group('ts')}{m.group('rest')}" if new_project \
                    else f"{m.group('ts')}{m.group('rest')}"
                target = f.with_name(stem + f.suffix)
                if target == f or target.exists():
                    continue
                try:
                    f.rename(target)
                except OSError:
                    continue
                renames[os.path.normcase(str(f))] = str(target)

            def _remap(path: str) -> str:
                """把记录里的旧路径映射为新路径（绝对/相对两种存储形态都兼容）。"""
                if not path:
                    return path
                p = Path(path)
                if p.is_absolute():
                    return renames.get(os.path.normcase(str(p)), path)
                new = renames.get(os.path.normcase(str(webui_root / p)))
                return str(Path(new).relative_to(webui_root)) if new else path

            for rid, e in self._select_all():
                if not self._same_output_dir(e.output_dir, output_dir):
                    continue
                e.project = new_project
                e.audio_path = _remap(e.audio_path)
                e.abc_path = _remap(e.abc_path)
                for s in getattr(e, "stems", None) or []:
                    if isinstance(s, dict):
                        s["path"] = _remap(s.get("path", ""))
                self._update(rid, e)  # 逐条回写数据库（事务）
            return len(renames)

    def delete_project(self, output_dir: str) -> int:
        """删除一个产物项目：整目录移入系统回收站，并移除该目录全部历史记录。

        安全判定：目录必须位于 outputs 根下且为单层 song_/separations_/cover_
        前缀目录，避免误删任意目录；批量变体同目录多条记录一并移除。
        回收站失败回退：逐文件回收 + 自底向上删除空目录。返回移除的记录条数。
        """
        with self._lock:
            d = self._resolve_output_dir(output_dir)
            if d is None or not d.is_dir():
                return 0
            try:
                rel = d.relative_to(self.outputs_root)
            except ValueError:
                return 0
            if len(rel.parts) != 1 or not rel.parts[0].startswith(_PROJECT_DIR_PREFIXES):
                return 0
            matched = [(rid, e) for rid, e in self._select_all()
                       if self._same_output_dir(e.output_dir, output_dir)]
            if not _delete_to_recycle(d):
                # 回收站失败回退：逐文件移回收站 + 自底向上删空目录
                files = [p for p in d.rglob("*") if p.is_file()]
                delete_files_to_recycle(files)
                for sub in sorted((p for p in d.rglob("*") if p.is_dir()),
                                  key=lambda p: len(p.parts), reverse=True):
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
                try:
                    d.rmdir()
                except OSError:
                    pass
            self._delete_ids([rid for rid, _ in matched])
            return len(matched)

    def _rmdir_if_empty(self, entry) -> None:
        """记录文件删除后尝试移除已空的项目目录（rmdir 仅能删空目录，天然安全）。"""
        d = self._resolve_output_dir(getattr(entry, "output_dir", ""))
        if d and d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass

    def clear(self):
        """Clear all history entries and remove their output files."""
        with self._lock:
            for _rid, entry in self._select_all():
                delete_files_to_recycle(self._files_for(entry))
                self._recycle_derived(entry)
                self._rmdir_if_empty(entry)
            with self._conn:
                self._conn.execute("DELETE FROM history")

    def auto_prune(self, max_entries: int = HISTORY_MAX_ENTRIES):
        """Remove oldest entries if over the limit."""
        with self._lock:
            all_rows = self._select_all()
            if len(all_rows) <= max_entries:
                return

            to_remove = all_rows[max_entries:]  # id 倒序排列，尾部为最旧

            for _rid, entry in to_remove:
                delete_files_to_recycle(self._files_for(entry))
                self._recycle_derived(entry)
                self._rmdir_if_empty(entry)

            self._delete_ids([rid for rid, _ in to_remove])

    # ---------------------------------------------------------------- 查重工具
    @staticmethod
    def compute_source_md5(path: Path) -> str:
        """计算源音频文件前 1MB 的 MD5，快速区分不同音频。

        只读开头 1MB 足够识别绝大多数不同音频（哪怕同文件换了编码，头信息也会不同），
        比全文件 hash 快得多（长音频可能几十 MB）。文件不存在返回空串。
        """
        try:
            with open(path, "rb") as f:
                head = f.read(1024 * 1024)
            return hashlib.md5(head).hexdigest()
        except (OSError, IOError):
            return ""

    def find_separation_by_source(self, md5: str) -> Optional[HistoryRecord]:
        """查有没有现成的 separation 记录（人声+伴奏双轨齐全）可复用。

        只匹配 status=completed 且 vocals/accompaniment 文件都在磁盘的记录，
        避免返回无效分离。返回最新一条（id 最大）。
        """
        if not md5:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM history WHERE record_type = 'separation' "
                "AND source_md5 = ? AND status = 'completed' "
                "ORDER BY id DESC", (md5,)).fetchall()
            for row in rows:
                rec = self._row_to_record(row)
                # 双轨都必须在磁盘
                stems = {s.get("type"): s.get("path", "")
                         for s in (rec.stems or []) if isinstance(s, dict)}
                if stems.get("vocals") and Path(stems["vocals"]).exists() \
                        and stems.get("accompaniment") \
                        and Path(stems["accompaniment"]).exists():
                    return rec
            return None

    def to_dataframe_rows(self, record_types=None) -> list[list]:
        """Convert entries to rows for Gradio Dataframe.

        record_types: 可选类型过滤（如 ("generation",)）；None 返回全部（向后兼容）。
        歌曲历史页只展示生成记录，分离/翻唱记录在各自 Tab 的历史区查看。
        列序：时间, 项目名, 风格, 模式, 音频时长, 生成耗时, Task ID。
        """
        self.prune_missing()
        rows = []
        for e in self.list_all():  # RLock 可重入：持锁状态下调 list_all 安全
            # 类型过滤：不在指定类型内的记录跳过（record_type 缺省视为 generation）
            if record_types is not None and \
                    getattr(e, "record_type", "generation") not in record_types:
                continue
            style_short = e.style[:40] + "..." if len(e.style) > 40 else e.style
            if e.status == "final":
                style_short = f"🏆 {style_short}"
            rows.append([
                e.created_at,
                getattr(e, "project", "") or "",
                style_short,
                e.cot,
                f"{e.audio_duration_seconds:.1f}s",
                f"{e.generation_time_seconds:.1f}s",
                e.task_id,
            ])
        return rows
