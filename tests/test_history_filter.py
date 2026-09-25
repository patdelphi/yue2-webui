#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""歌曲历史页类型过滤测试：只展示 generation，分离/翻唱记录不进入历史表格。

验证点：
1. to_dataframe_rows() 不传参返回全部记录（向后兼容，翻唱 Tab 等处仍可全量取）；
2. record_types=("generation",) 只返回生成记录；
3. record_types 组合过滤（generation+cover）正确；
4. app 侧历史页两处取数（refresh_history / _get_history_page）均传 generation 过滤。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import history as H  # noqa: E402


def _add_record(mgr: "H.HistoryManager", task_id: str, record_type: str):
    """构造一条指定类型的记录（audio 文件真实存在，避免被 prune_missing 清理）。"""
    base = mgr.outputs_root / task_id
    base.mkdir(parents=True, exist_ok=True)
    (base / "audio.wav").write_bytes(b"x")
    rec = H.HistoryRecord(
        task_id=task_id,
        created_at="2026-09-25T00:00:00",
        style="s",
        audio_path=str(base / "audio.wav"),
        output_dir=str(base.relative_to(mgr.outputs_root.parent)),
        abc_path="",
        cot="full",
        record_type=record_type,
    )
    mgr.append(rec)


def test_to_dataframe_rows_filters_by_record_type():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mgr = H.HistoryManager(db_file=root / "history.db",
                               outputs_root=root / "outputs")
        _add_record(mgr, "gen_1", "generation")
        _add_record(mgr, "sep_1", "separation")
        _add_record(mgr, "cov_1", "cover")

        # 不传参：全部 3 条（向后兼容）
        assert len(mgr.to_dataframe_rows()) == 3
        # 仅生成：1 条
        rows = mgr.to_dataframe_rows(record_types=("generation",))
        assert len(rows) == 1
        assert rows[0][6] == "gen_1"  # 第 7 列为 task_id（新增"项目名"列后右移）
        # 组合：生成 + 翻唱
        assert len(mgr.to_dataframe_rows(record_types=("generation", "cover"))) == 2
        mgr.close()  # 释放 SQLite 句柄，避免 TemporaryDirectory 清理被锁


def test_app_history_page_uses_generation_filter():
    """app 侧历史页四处取数均应传 record_types=("generation",)。

    四处：refresh_history / _get_history_page（表格取数与分页）、
    on_history_next_page（总页数计算）、_load_history_entry（行号映射）。
    其中 _load_history_entry 若不过滤会导致表格行号与全量记录错位
    （点击生成记录实际选中 separation 记录）。
    """
    app_path = Path(__file__).parent.parent / "app.py"
    src = app_path.read_text(encoding="utf-8-sig")
    assert 'to_dataframe_rows(record_types=("generation",))' in src
    # 四处调用都必须带过滤（与表格显示的记录集一致）
    assert src.count('to_dataframe_rows(record_types=("generation",))') == 4
