# -*- coding: utf-8 -*-
"""voice_ui_handlers 单元测试（mock voice_client，不触发真实 worker/进程）。

验证点：
1. separate_worker：产物目录聚合到 outputs/<root>/derived/sep_XXXX，写入 history 记录
2. cover_worker：cover 记录写入，root_task_id/derived_from 正确
3. 分离失败时（ok=False）抛 RuntimeError，不写历史
4. 音色库 save_ref/list_refs 正常落盘与列出
5. 短 id 唯一性/长度基本约束
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from voice_ui_handlers import VoiceHandlers, new_short_id  # noqa: E402
from history import HistoryManager  # noqa: E402
from queue_manager import TaskType  # noqa: E402


class _FakeResult:
    def __init__(self, ok=True, products=None, error=None):
        self.ok = ok
        self.products = products or {}
        self.error = error


class _FakeVoiceClient:
    def __init__(self, result=None):
        self._result = result or _FakeResult()
        self.calls = []

    def separate(self, *a, **k):
        self.calls.append(("separate", a, k))
        return self._result

    def convert(self, *a, **k):
        self.calls.append(("convert", a, k))
        return self._result


class _FakeTask:
    def __init__(self, tid):
        import threading
        self.task_id = tid
        self.cancel_event = threading.Event()


def _make_handlers(tmp, vc):
    webui = tmp / "yue2-webui"
    webui.mkdir(parents=True, exist_ok=True)
    # 造一个最小历史管理器（沿用真实实现）
    hm = HistoryManager(webui / "history.json", webui / "outputs")
    h = VoiceHandlers(project_root=tmp, webui_root=webui, history_mgr=hm, voice_client=vc)
    return h


def test_new_short_id_length():
    for _ in range(50):
        assert len(new_short_id()) == 4
        assert len(new_short_id(6)) == 6


def test_separate_worker_writes_history(tmp_path):
    vc = _FakeVoiceClient(_FakeResult(products={"vocals": str(tmp_path / "vocals.wav"),
                                                "accompaniment": str(tmp_path / "no.wav")}))
    h = _make_handlers(tmp_path, vc)
    t = _FakeTask("sep001")
    Path(tmp_path / "vocals.wav").write_bytes(b"\x00")
    out = h.separate_worker(t, source="a.wav", mode="2", root_task_id="root_task")
    # 产物目录聚合在 derived/sep_xxxx 下
    derived = Path(tmp_path / "yue2-webui" / "outputs" / "root_task" / "derived")
    assert derived.is_dir()
    assert (derived / "sep_") in [p.parent for p in derived.rglob("*")] or any(
        p.is_dir() and p.name.startswith("sep_") for p in derived.iterdir())
    # 历史记录写入
    recs = h.history_mgr.list_all()
    assert len(recs) == 1
    assert recs[0].record_type == "separation"
    assert recs[0].root_task_id == "root_task"
    assert recs[0].derived_from == "root_task"
    assert recs[0].audio_path.endswith("vocals.wav")


def test_cover_worker_writes_history(tmp_path):
    vc = _FakeVoiceClient(_FakeResult(products={"cover": str(tmp_path / "cover.flac"),
                                                "converted_vocals": "cv.wav",
                                                "accompaniment": "a.wav"}))
    h = _make_handlers(tmp_path, vc)
    t = _FakeTask("cov001")
    Path(tmp_path / "cover.flac").write_bytes(b"\x00")
    out = h.cover_worker(t, source="s.wav", ref="r.wav", accompaniment="a.wav",
                         semi_tone=-12, diffusion_steps=30, gain_db=0.0,
                         root_task_id="gen_root")
    recs = h.history_mgr.list_all()
    assert len(recs) == 1
    assert recs[0].record_type == "cover"
    assert recs[0].root_task_id == "gen_root"
    # 配音至 cover 产物
    assert recs[0].audio_path.endswith("cover.flac")
    # 短 id 聚合目录
    derived = Path(tmp_path / "yue2-webui" / "outputs" / "gen_root" / "derived")
    assert any(p.is_dir() and p.name.startswith("cover_") for p in derived.iterdir())


def test_separate_failure_raises(tmp_path):
    vc = _FakeVoiceClient(_FakeResult(ok=False, error="worker boom"))
    h = _make_handlers(tmp_path, vc)
    t = _FakeTask("t001")
    with pytest.raises(RuntimeError, match="boom"):
        h.separate_worker(t, source="a.wav", mode="2", root_task_id="r")
    assert h.history_mgr.list_all() == []  # 失败不写历史


def test_refs_save_and_list(tmp_path):
    vc = _FakeVoiceClient()
    h = _make_handlers(tmp_path, vc)
    src = tmp_path / "src.wav"
    src.write_bytes(b"\x00")
    saved = h.save_ref(str(src), "男声 示例")
    assert Path(saved).exists()
    refs = h.list_refs()
    assert len(refs) == 1
    assert saved in refs[0] or saved.replace("\\", "/") in refs[0].replace("\\", "/")


def test_stems_save_and_list(tmp_path):
    """素材库：乐器轨按 名__类型.wav 落盘，list_stems 正确解析名称与类型。"""
    vc = _FakeVoiceClient()
    h = _make_handlers(tmp_path, vc)
    src = tmp_path / "drum.wav"
    src.write_bytes(b"\x00")
    saved = h.save_stem(str(src), "军鼓 节奏", "drums")
    assert Path(saved).exists()
    assert "__drums" in Path(saved).name
    stems = h.list_stems()
    assert len(stems) == 1
    name, stype, path = stems[0]
    assert name == "军鼓 节奏" and stype == "drums"
    assert Path(path).exists()


def test_stems_save_invalid_type(tmp_path):
    """非法轨道类型应抛 ValueError，不落盘。"""
    vc = _FakeVoiceClient()
    h = _make_handlers(tmp_path, vc)
    src = tmp_path / "x.wav"
    src.write_bytes(b"\x00")
    with pytest.raises(ValueError):
        h.save_stem(str(src), "bad", "piano")
    assert h.list_stems() == []


def test_stems_list_fallback_for_legacy_name(tmp_path):
    """无 __类型 后缀的旧文件：名称原样、类型归为 other。"""
    vc = _FakeVoiceClient()
    h = _make_handlers(tmp_path, vc)
    legacy = h.stems_dir() / "legacy_track.wav"
    legacy.write_bytes(b"\x00")
    stems = h.list_stems()
    assert stems == [("legacy_track", "other", str(legacy))]