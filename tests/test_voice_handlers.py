# -*- coding: utf-8 -*-
"""voice_ui_handlers 单元测试（mock voice_client，不触发真实 worker/进程）。

验证点：
1. separate_worker：产物落独立文件夹 outputs/separations/<时间戳>_<短id>/，写入 history 记录；
   时间戳前缀传给 worker（产物命名 <时间戳>_<类别>.wav），返回 dict 含 stems
2. cover_worker：cover 记录写入 outputs/covers/...，root_task_id/derived_from 正确
3. 分离失败时（ok=False）抛 RuntimeError，不写历史
4. 音色库 save_ref/list_refs 正常落盘与列出
5. 短 id 唯一性/长度基本约束
6. run_in_queue_stream（生成器版队列提交）：yield 排队/执行进度文案并 return 结果；失败抛错
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import voice_ui_handlers  # noqa: E402  供 monkeypatch 替换模块级 queue_manager
from voice_ui_handlers import VoiceHandlers, new_short_id, detect_voice, _voice_score  # noqa: E402
from history import HistoryManager  # noqa: E402
from queue_manager import TaskType, TaskStatus, TaskCancelledError  # noqa: E402


class _FakeResult:
    def __init__(self, ok=True, products=None, error=None, cancelled=False):
        self.ok = ok
        self.products = products or {}
        self.error = error
        self.cancelled = cancelled


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
    # 产物落独立文件夹 outputs/separations/<时间戳>_<短id>/
    seps = tmp_path / "yue2-webui" / "outputs" / "separations"
    assert seps.is_dir() and any(p.is_dir() for p in seps.iterdir())
    # 历史记录写入，output_dir 指向该独立文件夹
    recs = h.history_mgr.list_all()
    assert len(recs) == 1
    assert recs[0].record_type == "separation"
    assert recs[0].root_task_id == "root_task"
    assert recs[0].derived_from == "root_task"
    assert recs[0].audio_path.endswith("vocals.wav")
    # output_dir 为相对 webui_root 的路径，指向独立产物文件夹
    assert (tmp_path / "yue2-webui" / recs[0].output_dir).is_dir()
    # 时间戳前缀传给 worker（产物命名 <时间戳>_<类别>.wav），目录名与之对齐
    call = vc.calls[0]
    assert call[0] == "separate"
    prefix = call[2].get("prefix", "")
    assert len(prefix) == 15 and prefix[8] == "_"  # 形如 20260924_201805
    assert prefix in Path(recs[0].output_dir).name
    # 返回 dict 含 stems（供 UI 播放器组填充）
    assert out["stems"] and out["stems"][0]["type"] == "vocals"


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
    # 产物落独立文件夹 outputs/covers/<时间戳>_<短id>/，时间戳前缀传给 worker
    covers = tmp_path / "yue2-webui" / "outputs" / "covers"
    assert covers.is_dir() and any(p.is_dir() for p in covers.iterdir())
    call = vc.calls[0]
    assert call[0] == "convert"
    prefix = call[2].get("prefix", "")
    assert prefix and prefix in Path(recs[0].output_dir).name
    assert out["stems"]


def test_separate_failure_raises(tmp_path):
    vc = _FakeVoiceClient(_FakeResult(ok=False, error="worker boom"))
    h = _make_handlers(tmp_path, vc)
    t = _FakeTask("t001")
    with pytest.raises(RuntimeError, match="boom"):
        h.separate_worker(t, source="a.wav", mode="2", root_task_id="r")
    assert h.history_mgr.list_all() == []  # 失败不写历史


def test_separate_worker_cancelled_by_worker_maps_to_cancelled(tmp_path):
    """worker 侧协作取消（cancelled=True，如直接 POST /api/cancel）：
    应映射为 TaskCancelledError 而非 RuntimeError，UI 才能显示"已取消"而非"任务失败"。"""
    vc = _FakeVoiceClient(_FakeResult(ok=False, error="任务已取消", cancelled=True))
    h = _make_handlers(tmp_path, vc)
    t = _FakeTask("t002")
    with pytest.raises(TaskCancelledError):
        h.separate_worker(t, source="a.wav", mode="2", root_task_id="r")
    assert h.history_mgr.list_all() == []  # 取消不写历史


def test_cover_worker_cancelled_by_worker_maps_to_cancelled(tmp_path):
    """翻唱同路：worker 侧 cancelled=True → TaskCancelledError，取消不写历史。"""
    vc = _FakeVoiceClient(_FakeResult(ok=False, error="任务已取消", cancelled=True))
    h = _make_handlers(tmp_path, vc)
    t = _FakeTask("t003")
    with pytest.raises(TaskCancelledError):
        h.cover_worker(t, source="s.wav", ref="r.wav", accompaniment="a.wav",
                       semi_tone=0, diffusion_steps=30, gain_db=0.0,
                       root_task_id="g")
    assert h.history_mgr.list_all() == []


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


# ---------------------------------------------------------------- 生成器版队列提交（run_in_queue_stream）
class _FakeStreamTask(_FakeTask):
    """带 result 与 drain_progress 的假任务（供 run_in_queue_stream 消费）。"""
    def __init__(self, tid, result=None):
        super().__init__(tid)
        self.result = result

    def drain_progress(self):
        return []


class _FakeQueueManager:
    """假队列管理器：submit 记录入参并返回预置任务；get_status 按序列吐状态，耗尽后 COMPLETED。"""
    def __init__(self, statuses, task):
        self._statuses = list(statuses)
        self.task = task
        self.submitted = None

    def submit(self, task_type, func, cancel_event=None, **kwargs):
        self.submitted = (task_type, kwargs)
        return self.task

    def get_status(self, t):
        if self._statuses:
            return self._statuses.pop(0)
        return {"status": TaskStatus.COMPLETED, "position": -1}


def _drain_stream(gen):
    """消费生成器到结束，返回 (yield 文案列表, return 值)。"""
    texts = []
    while True:
        try:
            texts.append(next(gen))
        except StopIteration as stop:
            return texts, stop.value


def test_run_in_queue_stream_progress_and_result(tmp_path, monkeypatch):
    """生成器版队列提交：yield 排队→执行文案（含秒表），return worker 结果；入参透传。"""
    vc = _FakeVoiceClient()
    h = _make_handlers(tmp_path, vc)
    task = _FakeStreamTask("q1", result={"stems": ["x"]})
    fq = _FakeQueueManager(
        [{"status": TaskStatus.QUEUED, "position": 2},   # 前面还有 1 个任务
         {"status": TaskStatus.RUNNING, "position": 0},
         {"status": TaskStatus.RUNNING, "position": 0}],  # 秒表推进（同文案去重）
        task)
    monkeypatch.setattr(voice_ui_handlers, "queue_manager", fq)
    monkeypatch.setattr(voice_ui_handlers.time, "sleep", lambda s: None)  # 提速：跳过轮询等待
    texts, result = _drain_stream(h.run_in_queue_stream(
        TaskType.SEPARATION, h.separate_worker, "zh", lambda l, s: s, "音轨分离",
        source="a.wav", mode="2"))
    # 排队与执行文案均有产出
    assert any("前面还有 1 个" in t for t in texts)
    assert any("执行中" in t for t in texts)
    # worker 入参透传（source）
    assert fq.submitted[1]["source"] == "a.wav"
    # return 携带 worker 结果
    assert result == {"stems": ["x"]}


def test_run_in_queue_stream_failure_raises(tmp_path, monkeypatch):
    """任务失败：get_status 返回 FAILED 时抛 RuntimeError（含错误信息）。"""
    vc = _FakeVoiceClient()
    h = _make_handlers(tmp_path, vc)
    task = _FakeStreamTask("q2")
    fq = _FakeQueueManager([{"status": TaskStatus.FAILED, "position": -1, "error": "boom"}], task)
    monkeypatch.setattr(voice_ui_handlers, "queue_manager", fq)
    gen = h.run_in_queue_stream(
        TaskType.SEPARATION, h.separate_worker, "zh", lambda l, s: s, "音轨分离",
        source="a.wav", mode="2")
    with pytest.raises(RuntimeError, match="boom"):
        for _ in gen:
            pass


# ---------------------------------------------------------------- 轻量人声检测（方案B）
def test_voice_score_harmonic_is_voice():
    """谐波丰富的人声样信号（150Hz 基频 + 谐波）应判定为人声。"""
    import numpy as np
    sr = 16000
    t = np.arange(sr * 2) / sr  # 2 秒
    x = (0.5 * np.sin(2 * np.pi * 150 * t)
         + 0.3 * np.sin(2 * np.pi * 300 * t)
         + 0.2 * np.sin(2 * np.pi * 450 * t))
    ok, score = _voice_score(x.astype(np.float32), sr)
    assert ok is True and score > 0.5


def test_voice_score_white_noise_not_voice():
    """白噪声（能量分散、谱平坦）应判定为非人声。"""
    import numpy as np
    rng = np.random.default_rng(42)
    x = rng.standard_normal(16000 * 2).astype(np.float32) * 0.3
    ok, score = _voice_score(x, 16000)
    assert ok is False and score < 0.5


def test_detect_voice_missing_file():
    """文件不存在返回 None（不判定、不抛异常）。"""
    assert detect_voice(str(Path("Z:/no/such/file.wav"))) is None