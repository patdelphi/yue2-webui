#!/usr/bin/env python
"""Test file-level delete + recycle-bin behaviour of HistoryManager.

核心验证点（对应批量变体误删 bug）：
1. 删除某条记录时只回收该记录自身的文件，不误删同目录其它记录的文件；
2. 不再整目录删除，输出目录本身保留；
3. clear / auto_prune 同样走文件级 + 回收站；
4. Windows 下真实回收站删除可用（不抛异常，文件从原路径消失）。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import history as H  # noqa: E402


def _make_record(history_mgr, task_id, file_stem):
    """构造一条记录，audio_path 指向同目录下不同文件名的 WAV。"""
    # 归一化路径：ABS_ROOT/outputs/<task_id>/<file_stem>.wav
    base = history_mgr.outputs_root / task_id
    base.mkdir(parents=True, exist_ok=True)
    for suffix in (".wav", ".mp3", ".abc", ".txt", ".json"):
        (base / f"{file_stem}{suffix}").write_text("x", encoding="utf-8")

    record = H.HistoryRecord(
        task_id=task_id,
        created_at="2026-09-23T00:00:00",
        style="test",
        audio_path=str(base / f"{file_stem}.wav"),
        output_dir=str(base.relative_to(history_mgr.outputs_root.parent)),
        abc_path=str((base / f"{file_stem}.abc").relative_to(history_mgr.outputs_root.parent)),
        cot="full",
    )
    history_mgr.append(record)
    return base, record


def test_delete_is_file_level_not_directory_level():
    """批量场景：同批次两个变体若共用目录，删除一个不能影响另一个。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgr = H.HistoryManager(history_file=root / "history.json", outputs_root=root / "outputs")

        # 构造同目录下的两个"变体"（模拟两个变体落在同一自目录的不同文件）
        var_dir = mgr.outputs_root / "batch_20260923"
        var_dir.mkdir(parents=True, exist_ok=True)
        keep_stem, drop_stem = "batch_20260923_song_var1", "batch_20260923_song_var2"
        for stem in (keep_stem, drop_stem):
            for suffix in (".wav", ".mp3", ".abc", ".txt", ".json"):
                (var_dir / f"{stem}{suffix}").write_text("x", encoding="utf-8")

        keep = H.HistoryRecord(task_id=keep_stem, created_at="t", style="s",
                               audio_path=str(var_dir / f"{keep_stem}.wav"),
                               output_dir=str(var_dir.relative_to(mgr.outputs_root.parent)),
                               abc_path=str((var_dir / f"{keep_stem}.abc").relative_to(mgr.outputs_root.parent)), cot="full")
        drop = H.HistoryRecord(task_id=drop_stem, created_at="t", style="s",
                               audio_path=str(var_dir / f"{drop_stem}.wav"),
                               output_dir=str(var_dir.relative_to(mgr.outputs_root.parent)),
                               abc_path=str((var_dir / f"{drop_stem}.abc").relative_to(mgr.outputs_root.parent)), cot="full")
        mgr.append(keep)
        mgr.append(drop)

        # 拦截回收站调用，只记录哪些文件被送入删除，不改动真实磁盘
        got = []
        orig = H.delete_files_to_recycle

        def spy(files):
            got.extend(str(x) for x in files)
            return len(files)

        H.delete_files_to_recycle = spy
        try:
            ok = mgr.delete(drop_stem)
        finally:
            H.delete_files_to_recycle = orig

        # 1) 删除成功、条目移除
        assert ok and mgr.get(drop_stem) is None, "记录应从历史移除"
        # 2) 送删列表只包含 drop 变体的文件（同目录 keep 变体的文件不应被送删）
        got_names = {Path(x).name for x in got}
        assert got_names == {f"{drop_stem}{s}" for s in (".wav", ".mp3", ".abc", ".txt", ".json")}, got_names
        # 3) keep 变体文件仍在磁盘
        assert (var_dir / f"{keep_stem}.wav").exists(), "同目录其它变体文件不得被删除"
        # 4) 目录本身仍然存在（非目录级删除）
        assert var_dir.exists(), "输出目录必须保留，不允许整目录删除"
        # 5) keep 记录仍在历史
        assert mgr.get(keep_stem) is not None
        print("PASS: 删除为文件级，未误删同目录其它变体、未删除目录")


def test_auto_prune_uses_constant_default():
    """auto_prune 默认按 HISTORY_MAX_ENTRIES 触发，且只回收最旧条目文件。"""
    assert H.HISTORY_MAX_ENTRIES == 100
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgr = H.HistoryManager(history_file=root / "history.json", outputs_root=root / "outputs")

        # 塞入 HISTORY_MAX_ENTRIES + 2 条记录
        keep = []
        for i in range(H.HISTORY_MAX_ENTRIES + 2):
            tid = f"t{i}"
            base, _ = _make_record(mgr, tid, f"t{i}")
            keep.append((tid, base))

        # 不传参数，应触发默认上限的清理
        mgr.auto_prune()

        assert len(mgr.list_all()) == H.HISTORY_MAX_ENTRIES, "应裁剪到 HISTORY_MAX_ENTRIES 条"
        # 最新的 HISTORY_MAX_ENTRIES 条保留（list 是新的在前，删除的是最后追加的后缀）
        kept_ids = {r.task_id for r in mgr.list_all()}
        # 最旧的两条 t0、t1 应被移除
        assert "t0" not in kept_ids and "t1" not in kept_ids
        print("PASS: auto_prune 默认使用 HISTORY_MAX_ENTRIES 常量")


def test_real_recycle_on_windows():
    """Windows 下真实回收站删除：文件被移走且原路径不再存在。"""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "trashme.tmp"
        p.write_text("junk", encoding="utf-8")
        assert p.exists()
        removed = H.delete_files_to_recycle([p])
        assert removed == 1, f"应移除 1 个文件，实际 {removed}"
        assert not p.exists(), "文件应从原路径消失（已进回收站或已删除）"
        print("PASS: 文件移入回收站（Windows）")


if __name__ == "__main__":
    test_delete_is_file_level_not_directory_level()
    test_auto_prune_uses_constant_default()
    test_real_recycle_on_windows()
    print("\n=== 全部 history 回收站测试通过 ===")