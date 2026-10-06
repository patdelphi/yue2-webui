# -*- coding: utf-8 -*-
"""voice_ui_handlers 单元测试（mock voice_client，不触发真实 worker/进程）。

验证点：
1. separate_worker：产物落独立文件夹 outputs/separations_<时间戳>/，写入 history 记录；
   <项目名>_<时间戳> 前缀传给 worker（产物命名 <前缀>_<类别>.wav），返回 dict 含 stems
2. cover_worker：cover 记录写入 outputs/cover_<时间戳>/，root_task_id/derived_from 正确
3. 分离失败时（ok=False）抛 RuntimeError，不写历史
4. 音色库 save_ref/list_refs 正常落盘与列出
5. 短 id 唯一性/长度基本约束
6. run_in_queue_stream（生成器版队列提交）：yield 排队/执行进度文案并 return 结果；失败抛错
"""

import sys
import tempfile
import time
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import voice_ui_handlers  # noqa: E402  供 monkeypatch 替换模块级 queue_manager
from voice_ui_handlers import (VoiceHandlers, new_short_id, detect_voice,  # noqa: E402
                               _voice_score, _build_stems, _make_preview, schedule_preview)
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
    hm = HistoryManager(webui / "history.db", webui / "outputs")
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
    out = h.separate_worker(t, source="a.wav", mode="2", root_task_id="root_task",
                            project="夜曲demo")
    # 产物落独立文件夹 outputs/separations_<时间戳>/（outputs 下单层目录）
    outputs = tmp_path / "yue2-webui" / "outputs"
    sep_dirs = [p for p in outputs.iterdir() if p.is_dir() and p.name.startswith("separations_")]
    assert sep_dirs and len(list(outputs.iterdir())) == 1  # outputs 下仅此一个目录
    # 历史记录写入，output_dir 指向该独立文件夹
    recs = h.history_mgr.list_all()
    assert len(recs) == 1
    assert recs[0].record_type == "separation"
    assert recs[0].root_task_id == "root_task"
    assert recs[0].derived_from == "root_task"
    assert recs[0].audio_path.endswith("vocals.wav")
    assert recs[0].project == "夜曲demo"  # 项目名写入记录（改名/列表展示用）
    # output_dir 为相对 webui_root 的路径，指向独立产物文件夹
    assert (tmp_path / "yue2-webui" / recs[0].output_dir).is_dir()
    # 产物命名前缀 = <项目名>_<时间戳>（worker 产物 = <前缀>_<类别>.wav）
    call = vc.calls[0]
    assert call[0] == "separate"
    prefix = call[2].get("prefix", "")
    assert prefix.startswith("夜曲demo_") and len(prefix) == 22  # 6字项目名 + _ + 15位时间戳
    # 目录名不含项目名，仅 separations_<时间戳>
    assert recs[0].output_dir.replace("\\", "/").endswith(
        "separations_" + prefix[len("夜曲demo_"):])
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
                         root_task_id="gen_root", project="源曲_女声")
    recs = h.history_mgr.list_all()
    assert len(recs) == 1
    assert recs[0].record_type == "cover"
    assert recs[0].root_task_id == "gen_root"
    # 配音至 cover 产物
    assert recs[0].audio_path.endswith("cover.flac")
    assert recs[0].project == "源曲_女声"  # 项目名 = 源项目名_音色名（app 层拼好传入）
    # 产物落独立文件夹 outputs/cover_<时间戳>/，命名前缀传给 worker
    outputs = tmp_path / "yue2-webui" / "outputs"
    cover_dirs = [p for p in outputs.iterdir() if p.is_dir() and p.name.startswith("cover_")]
    assert cover_dirs and len(list(outputs.iterdir())) == 1
    call = vc.calls[0]
    assert call[0] == "convert"
    prefix = call[2].get("prefix", "")
    assert prefix.startswith("源曲_女声_")
    assert recs[0].output_dir.replace("\\", "/").endswith(
        "cover_" + prefix[len("源曲_女声_"):])
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


def test_ensure_separation_passes_cancel_event(tmp_path):
    """翻唱前置的同步分离阶段：cancel_event 必须透传给 worker；UI 取消（事件已置位）
    时映射为 TaskCancelledError，且回收本次衍生目录、不写历史。"""
    import threading
    ev = threading.Event()
    vc = _FakeVoiceClient(_FakeResult(ok=False, error="worker stopped"))
    h = _make_handlers(tmp_path, vc)
    src = tmp_path / "src.wav"
    src.write_bytes(b"\x00")
    ev.set()  # 模拟 UI 已在同步阶段点了取消
    with pytest.raises(TaskCancelledError):
        h.ensure_separation(str(src), project="p", cancel_event=ev)
    call = vc.calls[0]
    assert call[0] == "separate"
    assert call[2].get("cancel_event") is ev  # 事件原样透传
    assert h.history_mgr.list_all() == []  # 取消不写历史
    outputs = tmp_path / "yue2-webui" / "outputs"
    # 衍生的 separations_* 目录已被回收（不存在残留）
    assert not [p for p in outputs.iterdir()
                if p.is_dir() and p.name.startswith("separations_")]


def test_persist_upload_dedup_reuses_same_file(tmp_path):
    """上传留存：同内容重复上传复用同一份（不产生第二份副本）。"""
    vc = _FakeVoiceClient()
    h = _make_handlers(tmp_path, vc)
    src = tmp_path / "song.wav"
    src.write_bytes(b"hello-world")
    p1 = h.persist_upload(str(src), "sep_src")
    p2 = h.persist_upload(str(src), "sep_src")
    assert p2 == p1
    assert len(h.list_uploads("sep_src")) == 1


def test_persist_upload_skips_md5_when_size_differs(tmp_path, monkeypatch):
    """A9：去重先比 size——候选文件 size 不同时不应再算 md5（避免 O(N×文件大小) 全量重算）。"""
    import hashlib
    calls = {"n": 0}
    real_md5 = hashlib.md5

    def _counting(*a, **k):
        calls["n"] += 1
        return real_md5(*a, **k)

    monkeypatch.setattr(hashlib, "md5", _counting)
    vc = _FakeVoiceClient()
    h = _make_handlers(tmp_path, vc)

    a = tmp_path / "a.wav"
    a.write_bytes(b"aaaa")          # size 4
    h.persist_upload(str(a), "sep_src")   # 1 次 md5（算 src，无候选）
    assert calls["n"] == 1

    b = tmp_path / "b.wav"
    b.write_bytes(b"bbbbbbbb")      # size 8 ≠ 4
    h.persist_upload(str(b), "sep_src")
    # 只多算一次 src 的 md5；候选项因 size 不同被短路，未算 md5
    assert calls["n"] == 2


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


# ---------------------------------------------------------------- 回放预览件（小音频）
def test_make_preview_missing_source_returns_empty():
    """源文件不存在 → 返回空串（回放退回原件），不抛异常。"""
    assert _make_preview(str(Path("Z:/no/such/file.wav"))) == ""


def test_make_preview_without_ffmpeg_returns_empty(tmp_path, monkeypatch):
    """ffmpeg 缺失 → 返回空串（静默降级，不生成预览件）。"""
    src = tmp_path / "a.wav"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(voice_ui_handlers.shutil, "which", lambda name: None)
    assert _make_preview(str(src)) == ""
    assert not (tmp_path / "a_preview.mp3").exists()


def test_make_preview_reuses_fresh_existing(tmp_path, monkeypatch):
    """已有不旧于原件的预览件 → 直接复用，不再次调用 ffmpeg。"""
    src = tmp_path / "b.wav"
    dst = tmp_path / "b_preview.mp3"
    src.write_bytes(b"\x00")
    dst.write_bytes(b"\x00")  # 后写 → mtime 不早于原件

    def _boom(name):
        raise AssertionError("不应调用 ffmpeg：已有可用预览件")

    monkeypatch.setattr(voice_ui_handlers.shutil, "which", _boom)
    assert _make_preview(str(src)) == str(dst)


def test_build_stems_carries_preview_key(tmp_path, monkeypatch):
    """_build_stems 每条 stem 含 preview（回放用）与 path（合成用），缺失文件不入列。"""
    v = tmp_path / "vocals.wav"
    v.write_bytes(b"\x00")
    # 产物入库走非阻塞的 schedule_preview（后台转码，不拖慢 worker）
    monkeypatch.setattr(voice_ui_handlers, "schedule_preview", lambda p: str(p) + ".mp3")
    stems = _build_stems({"vocals": str(v), "missing": str(tmp_path / "nope.wav")})
    assert len(stems) == 1  # 不存在的产物被剔除
    s = stems[0]
    assert s["type"] == "vocals" and s["label"] == "人声"
    assert s["path"] == str(v)                 # 原件：合成链路（翻唱/混音）用
    assert s["preview"] == str(v) + ".mp3"     # 小件：历史回放用


def test_schedule_preview_nonblocking_and_dedup(tmp_path, monkeypatch):
    """schedule_preview：不阻塞（转码未完成返回空串）、同一目标去重只排一次队。"""
    src = tmp_path / "vocals.wav"
    src.write_bytes(b"\x00")
    calls = []
    # 用一个"慢转码"替身：确认 schedule_preview 不会同步等待它完成
    monkeypatch.setattr(voice_ui_handlers, "_transcode_preview",
                        lambda s, d: (calls.append(str(s)), time.sleep(0.3),
                                      Path(d).write_bytes(b"\x00")))
    monkeypatch.setattr(voice_ui_handlers, "_preview_pending", set())
    monkeypatch.setattr(voice_ui_handlers, "_preview_queue", deque())
    t0 = time.time()
    assert schedule_preview(str(src)) == ""            # 尚未转码 → 空串（回放退回原件）
    assert time.time() - t0 < 0.2, "schedule_preview 不应同步等待转码"
    # 立即再登记：同一目标已 pending，不重复入队
    schedule_preview(str(src))
    assert len(voice_ui_handlers._preview_queue) <= 1, voice_ui_handlers._preview_queue
    # 等后台线程完成，预览件生成后再次调用应直接返回路径（复用）
    deadline = time.time() + 5
    while time.time() < deadline and not (tmp_path / "vocals_preview.mp3").exists():
        time.sleep(0.1)
    assert (tmp_path / "vocals_preview.mp3").exists(), "后台转码应生成预览件"
    assert schedule_preview(str(src)) == str(tmp_path / "vocals_preview.mp3")
    assert len(calls) == 1, f"同一目标只应转码一次，实际 {calls}"


def test_voice_stem_items_prefers_preview_keeps_full_for_mix():
    """app 源码断言：回放优先取 preview，合成链路仍读 path；切 Tab 自动回填播放器组。"""
    from _app_bundle import app_bundle  # C1 拆分后源码级断言读 app bundle
    src = app_bundle()
    assert 'play = preview if preview and Path(preview).exists() else full' in src
    assert 'full = s.get("path", "")' in src          # 合成链路取原件
    # 存量记录无 preview 键：按 <原名>_preview.mp3 命名约定推导（免 DB 迁移）
    assert 'Path(full).with_name(f"{Path(full).stem}_preview.mp3")' in src
    assert "def _voice_task_first_players(" in src     # 切 Tab 回填播放器组
    assert '*_voice_task_first_players("separation")' in src
    assert "sep_selected_task, *sep_hist_audios]" in src
    assert '*_voice_task_first_players("cover")' in src      # 翻唱 Tab 同样回填
    assert "cover_selected_task, *cover_hist_audios]" in src


def test_tab_switch_refreshes_library_previews():
    """app 源码断言：切 Tab 刷新下拉时同步刷新「试听」播放器。

    Gradio 的 .change 只在用户交互时触发，切 Tab 用 gr.update 程序化设值不会触发它；
    若不同步刷新试听，就会出现「下拉显示着选中项、试听却是空的」假选中。
    """
    from _app_bundle import app_bundle  # C1 拆分后源码级断言读 app bundle
    src = app_bundle()
    assert "def _preview_first_update(" in src
    assert "lib_stem_preview, lib_ref_preview," in src       # 分离 Tab：素材库/音色库试听
    assert "cover_ref_preview, cover_acc_preview," in src    # 翻唱 Tab：音色库/自定义伴奏试听
    assert src.count("_preview_first_update(_voice_stem_choices(_CUR_LANG))") == 2
    assert src.count("_preview_first_update(_voice_ref_choices(_CUR_LANG))") == 2


def test_history_rename_and_delete_handle_preview(tmp_path):
    """history.py 源码断言：改名同步 preview 字段、删除范围纳入 preview。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "history.py").read_text(encoding="utf-8")
    assert 's["preview"] = _remap(s.get("preview", ""))' in src
    assert 'for key in ("path", "preview"):' in src


# ---------------------------------------------------------------- 库列表/主播放器小件化
def test_preview_files_excluded_from_refs_and_stems(tmp_path):
    """库列表排除自动生成的 _preview.mp3 小件，避免污染下拉选项。"""
    h = _make_handlers(tmp_path, _FakeVoiceClient())
    refs = h.refs_dir()
    (refs / "fetched_a1b2.wav").write_bytes(b"\x00")
    (refs / "fetched_a1b2_preview.mp3").write_bytes(b"\x00")
    assert [Path(p).name for p in h.list_refs()] == ["fetched_a1b2.wav"]
    stems = h.stems_dir()
    (stems / "drum__drums.wav").write_bytes(b"\x00")
    (stems / "drum__drums_preview.mp3").write_bytes(b"\x00")
    assert [(n, t) for n, t, _ in h.list_stems()] == [("drum", "drums")]


def test_rename_and_delete_move_preview_sibling(tmp_path, monkeypatch):
    """改名/删除库条目时预览小件一并搬移/回收（不留孤儿）。"""
    h = _make_handlers(tmp_path, _FakeVoiceClient())
    refs = h.refs_dir()
    src = refs / "old_a1b2.wav"
    src.write_bytes(b"\x00")
    prev = refs / "old_a1b2_preview.mp3"
    prev.write_bytes(b"\x00")
    dest = Path(h.rename_ref(str(src), "newname"))
    assert dest.name == "newname_a1b2.wav"
    assert not prev.exists()                                     # 旧预览件已随改名搬走
    assert dest.with_name("newname_a1b2_preview.mp3").exists()
    seen = []
    # delete_files_to_recycle 已上提到模块顶部 import，需 patch 本模块的绑定名
    monkeypatch.setattr(voice_ui_handlers, "delete_files_to_recycle",
                        lambda files: seen.extend(files))
    h.delete_ref(str(dest))
    assert sorted(Path(p).name for p in seen) == ["newname_a1b2.wav", "newname_a1b2_preview.mp3"]


def test_app_prefers_small_audio_for_all_players():
    """app.py 源码断言：主播放器/分离翻唱任务历史回放/库试听统一走小件；后端链路仍用原件。"""
    from _app_bundle import app_bundle  # C1 拆分后源码级断言读 app bundle
    src = app_bundle()
    assert "def _prefer_mp3(path):" in src
    assert "def _preview_for_library(path):" in src
    assert "from voice_ui_handlers import VoiceHandlers, detect_voice, _make_preview" in src
    # 主播放器：生成 / 重合成 / 批量变体 / 历史页 四处均优先取同目录同名 MP3
    # （生成组回调拆分后调用形式为 d.prefer_mp3(...)，故按不含前缀的名字断言）
    assert "prefer_mp3(str(first_result.audio_path))" in src
    assert "prefer_mp3(str(result.audio_path)), duration_info" in src
    assert src.count("prefer_mp3(str(result.audio_path))") >= 2  # 重合成返回值 + 批量变体载荷
    assert "prefer_mp3(str(audio_path))" in src
    # 分离/翻唱任务历史回放用 _voice_stem_items（preview 优先 + 命名推导）；
    # 音色组搬迁后调用注入 deps（_voice_stem_items(d, getattr(...))），故分两段等价匹配
    assert "_voice_stem_items(" in src
    assert 'getattr(entry, "stems", None)' in src
    # 歌曲历史页表格只列 generation 记录，无 stems 可回放 → 相关轨道回放 UI 已整体移除
    assert "history-stem-audio" not in src
    assert "history_stem" not in src
    # 库试听 4 处（cover 参考/伴奏 + 素材库 + 音色库）按需生成 preview 小件
    assert src.count("gr.update(value=_preview_for_library(p), visible=bool(p))") == 4


def test_cover_tab_management_moved_to_separation():
    """翻唱页仅保留选择+试听；删除/重命名管理功能移至分离页库管理区。

    断言 app 源码：
    1. 翻唱页的管理组件（cover_ref_del/cover_acc_del/重命名）已移除；
    2. 分离页存在库管理组件（lib_stem_*/lib_ref_*）并绑定现有删除/重命名回调；
    3. 翻唱页保留试听播放器（cover_ref_preview/cover_acc_preview）。
    """
    from _app_bundle import app_bundle  # C1 拆分后源码级断言读 app bundle
    src = app_bundle()
    # 翻唱页管理组件移除
    for token in ("cover_ref_del_btn", "cover_acc_del_btn",
                  "cover_ref_rename_input", "cover_acc_rename_input"):
        assert token not in src, f"翻唱页应移除管理组件: {token}"
    # 翻唱页保留选择+试听
    for token in ("cover_ref_dropdown", "cover_ref_preview",
                  "cover_acc_dd", "cover_acc_preview"):
        assert token in src, f"翻唱页应保留选择/试听组件: {token}"
    # 分离页库管理区：素材库 + 音色库的删除/重命名绑定现有回调
    for token in ("lib_stem_del_btn", "lib_stem_rename_btn",
                  "lib_ref_del_btn", "lib_ref_rename_btn",
                  "lib_stem_dd.change", "lib_ref_dd.change"):
        assert token in src, f"分离页应有库管理组件/绑定: {token}"
    assert "lib_stem_del_btn.click(fn=on_voice_stem_delete" in src
    assert "lib_ref_del_btn.click(fn=on_voice_ref_delete" in src


def test_separation_tracks_can_be_saved_to_stem_library():
    """素材库必须有应用内写入入口（此前 save_stem 无 UI 调用方 → 素材库下拉永远为空）。

    断言 app.py 源码：
    1. 分离页库管理区有「待入库轨道 / 素材名称 / 保存到素材库」组件；
    2. 保存按钮调用 on_voice_save_stem_to_lib，且该回调调用 voice_handlers.save_stem；
    3. 分离完成时回填「待入库轨道」下拉并缓存 stems（供保存时回查轨道类型）。
    """
    from _app_bundle import app_bundle  # C1 拆分后源码级断言读 app bundle
    src = app_bundle()
    for token in ("lib_stem_pick", "lib_stem_save_name", "lib_stem_save_btn", "sep_stems_state"):
        assert token in src, f"分离页应有入库组件: {token}"
    assert "def on_voice_save_stem_to_lib(" in src
    assert "lib_stem_save_btn.click(fn=on_voice_save_stem_to_lib" in src
    assert "voice_handlers.save_stem(" in src                      # 后端写入能力已被接线
    # 分离完成回填待入库下拉；音色组搬迁后调用注入 deps，故按组合调用前半段等价匹配
    assert "_dd_update(_stem_pick_choices(" in src
    assert "def _stem_pick_choices(" in src
    # 人声归音色库：待入库下拉须排除 vocals（与素材库下拉一致）
    assert 's.get("type") == "vocals"' in src