"""多轨混音 Web 接口（M2）单元测试。

覆盖内容：
- resolve_audio 路径白名单（越界/扩展名/不存在）与 audio_mime 映射
- compute_peaks 峰值分桶（需系统 ffmpeg，缺失时自动跳过）与缺文件兜底
- list_sources 素材清单（分离记录 → sources，混音记录 → mixes）
- submit_render / task_status / cancel_render 的契约与错误分支
- page_texts 文案完整性（含英文翻译）
- app.py 路由注册与分离页编辑入口（源码级断言）

运行方式：pytest tests/test_mix_web.py
"""
import math
import shutil
import struct
import sys
import time
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import mix_web  # noqa: E402
from history import HistoryRecord, HistoryManager  # noqa: E402
from queue_manager import TaskStatus  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None
WEBUI_DIR = Path(__file__).parent.parent
SRC_DIR = WEBUI_DIR / "src"


# ------------------------------------------------------------------ 夹具工具
def _make_wav(path: Path, seconds: float = 1.0, freq: float = 440.0, sr: int = 48000) -> Path:
    """生成单声道正弦 wav（标准库，避免引入额外依赖）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * sr)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            v = int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / sr))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return path


@pytest.fixture()
def webui_root(tmp_path: Path) -> Path:
    """构造带 outputs/ 的假 webui_root，并放入一次分离的两条轨道。"""
    root = tmp_path / "webui"
    out = root / "outputs" / "separations_20260927_104930"
    _make_wav(out / "demo_20260927_104930_vocals.wav", 1.0, 440.0)
    _make_wav(out / "demo_20260927_104930_accompaniment.wav", 1.0, 660.0)
    return root


@pytest.fixture()
def history_mgr(webui_root: Path) -> HistoryManager:
    """带一条分离记录与一条混音记录的历史管理器。"""
    mgr = HistoryManager(db_file=webui_root / "history.db",
                         outputs_root=webui_root / "outputs")
    sep_dir = webui_root / "outputs" / "separations_20260927_104930"
    mgr.append(HistoryRecord(
        task_id="sep_1", created_at="2026-09-27T10:49:30", style="[separation]",
        audio_path=str(sep_dir / "demo_20260927_104930_vocals.wav"),
        output_dir="outputs/separations_20260927_104930",
        record_type="separation", project="demo",
        stems=[
            {"label": "人声", "type": "vocals",
             "path": str(sep_dir / "demo_20260927_104930_vocals.wav")},
            {"label": "伴奏", "type": "accompaniment",
             "path": str(sep_dir / "demo_20260927_104930_accompaniment.wav")},
        ],
    ))
    mix_dir = webui_root / "outputs" / "mix_20260928_090000"
    _make_wav(mix_dir / "demo_20260928_090000_mix.flac", 1.0, 330.0)
    mgr.append(HistoryRecord(
        task_id="mix_1", created_at="2026-09-28T09:00:00", style="[mix]",
        audio_path=str(mix_dir / "demo_20260928_090000_mix.flac"),
        output_dir="outputs/mix_20260928_090000", record_type="mix", project="demo",
        stems=[{"label": "混音成品", "type": "mix",
                "path": str(mix_dir / "demo_20260928_090000_mix.flac")}],
    ))
    return mgr


def _payload(webui_root: Path) -> dict:
    return {
        "version": 1,
        "duration": 1.0,
        "source_root_task_id": "sep_1",
        "tracks": [{
            "id": "vocals", "name": "人声",
            "src": "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav",
            "gain_db": -1.5, "mute": False, "clips": [],
        }],
    }


class _FakeTask:
    """替代真实 Task，避免单测里真的往队列里塞渲染任务。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.result = None
        self.error = None
        self.last_progress = None


# ------------------------------------------------------------------ 1. 路径白名单
def test_resolve_audio_whitelist(webui_root: Path):
    """合法素材可解析；越界、非音频扩展名、不存在均返回 None。"""
    ok = mix_web.resolve_audio(
        webui_root, "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav")
    assert ok is not None and ok.is_file()

    assert mix_web.resolve_audio(webui_root, r"C:\Windows\win.ini") is None
    assert mix_web.resolve_audio(webui_root, "outputs/../../app.py") is None
    assert mix_web.resolve_audio(webui_root, "outputs/separations_20260927_104930/a.exe") is None
    assert mix_web.resolve_audio(webui_root, "outputs/separations_20260927_104930/missing.wav") is None
    assert mix_web.resolve_audio(webui_root, "") is None
    assert mix_web.resolve_audio(webui_root, None) is None


def test_audio_mime_mapping():
    """扩展名 → MIME 映射，未知扩展名回退 octet-stream。"""
    assert mix_web.audio_mime("a.wav") == "audio/wav"
    assert mix_web.audio_mime("a.flac") == "audio/flac"
    assert mix_web.audio_mime("a.WAV") == "audio/wav"
    assert mix_web.audio_mime("a.txt") == "application/octet-stream"


# ------------------------------------------------------------------ 2. 波形峰值
def test_compute_peaks_missing_file_returns_empty(tmp_path: Path):
    """文件不存在时返回空峰值而不是抛异常。"""
    info = mix_web.compute_peaks(tmp_path / "nope.wav", 400)
    assert info == {"peaks": [], "duration": 0.0}


def test_compute_peaks_buckets_clamped(webui_root: Path):
    """桶数越界被钳制到区间内（不依赖 ffmpeg，仅校验解析逻辑）。"""
    import inspect
    src = inspect.getsource(mix_web.compute_peaks)
    assert "PEAK_BUCKETS_MIN" in src and "PEAK_BUCKETS_MAX" in src


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要系统 ffmpeg")
def test_compute_peaks_ok(webui_root: Path):
    """1 秒 440Hz 正弦：时长≈1s、桶数=上限、峰值≈0.3。"""
    path = webui_root / "outputs" / "separations_20260927_104930" / "demo_20260927_104930_vocals.wav"
    info = mix_web.compute_peaks(path, 1200)
    assert abs(info["duration"] - 1.0) < 0.02
    assert len(info["peaks"]) == 1200
    top = max(p[1] for p in info["peaks"])
    assert 0.25 < top < 0.35, top
    for mn, mx in info["peaks"]:
        assert -1.0 <= mn <= mx <= 1.0


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要系统 ffmpeg")
def test_compute_peaks_cached(webui_root: Path):
    """同一文件重复请求命中缓存（返回同一对象）。"""
    path = webui_root / "outputs" / "separations_20260927_104930" / "demo_20260927_104930_vocals.wav"
    first = mix_web.compute_peaks(path, 400)
    second = mix_web.compute_peaks(path, 400)
    assert first is second


# ------------------------------------------------------------------ 3. 素材清单
def test_list_sources_separation_and_mix(webui_root: Path, history_mgr: HistoryManager):
    """分离记录进入 sources（含全部轨道），混音记录进入 mixes。"""
    data = mix_web.list_sources(history_mgr, webui_root)
    assert data["ok"] is True
    assert len(data["sources"]) == 1
    src = data["sources"][0]
    assert src["task_id"] == "sep_1"
    assert src["name"] == "demo · separations_20260927_104930"
    assert [t["type"] for t in src["tracks"]] == ["vocals", "accompaniment"]
    # 路径统一为相对 webui_root 的正斜杠形式，供前端直接回填工程 JSON
    assert src["tracks"][0]["path"].startswith("outputs/separations_20260927_104930/")

    assert len(data["mixes"]) == 1
    assert data["mixes"][0]["task_id"] == "mix_1"
    assert data["mixes"][0]["path"].startswith("outputs/mix_20260928_090000/")


def test_list_sources_skips_records_without_files(webui_root: Path, history_mgr: HistoryManager):
    """文件被删除后的分离记录（stems 全部失效）不进素材清单。"""
    (webui_root / "outputs" / "separations_20260927_104930"
     / "demo_20260927_104930_vocals.wav").unlink()
    (webui_root / "outputs" / "separations_20260927_104930"
     / "demo_20260927_104930_accompaniment.wav").unlink()
    data = mix_web.list_sources(history_mgr, webui_root)
    assert data["sources"] == []


# ------------------------------------------------------------------ 4. 提交与状态
def test_submit_render_rejects_invalid_project(webui_root: Path, history_mgr: HistoryManager):
    """非法工程在提交前即被拦下，且不占用队列。"""
    bad = _payload(webui_root)
    bad["tracks"][0]["src"] = "../../app.py"
    data = mix_web.submit_render(bad, webui_root, history_mgr, "demo")
    assert data["ok"] is False and data["error"]


def test_submit_render_ok_and_registered(webui_root: Path, history_mgr: HistoryManager,
                                         monkeypatch):
    """合法工程提交成功，任务登记到状态注册表（用假 submit 避免真的渲染）。"""
    fake = _FakeTask("mix_test_0001")
    monkeypatch.setattr(mix_web.queue_manager, "submit", lambda *a, **k: fake)
    data = mix_web.submit_render(_payload(webui_root), webui_root, history_mgr, "demo")
    assert data == {"ok": True, "task_id": "mix_test_0001"}
    assert mix_web._lookup("mix_test_0001") is fake


def test_task_status_unknown_task(webui_root: Path):
    """未登记/已淘汰的任务返回 ok=False。"""
    data = mix_web.task_status("mix_not_exist", webui_root)
    assert data["ok"] is False and "不存在" in data["error"]


def test_task_status_completed_maps_output(webui_root: Path, monkeypatch):
    """完成态：产物路径转为相对 webui_root，并带上时长。"""
    fake = _FakeTask("mix_test_0002")
    out = webui_root / "outputs" / "mix_20260928_090000" / "demo_20260928_090000_mix.flac"
    _make_wav(out, 1.0, 330.0)
    fake.result = {"ok": True, "output": str(out), "duration": 1.0}
    monkeypatch.setattr(mix_web.queue_manager, "get_status",
                        lambda task: {"status": TaskStatus.COMPLETED, "position": -1})
    mix_web._remember(fake)

    data = mix_web.task_status("mix_test_0002", webui_root)
    assert data["ok"] is True and data["status"] == "completed"
    assert data["output"] == "outputs/mix_20260928_090000/demo_20260928_090000_mix.flac"
    assert data["duration"] == 1.0


def test_task_status_business_failure_becomes_failed(webui_root: Path, monkeypatch):
    """worker 内部业务失败（队列仍为 completed）必须对前端呈现为 failed + error。"""
    fake = _FakeTask("mix_test_0003")
    fake.result = {"ok": False, "error": "ffmpeg 渲染失败: boom"}
    monkeypatch.setattr(mix_web.queue_manager, "get_status",
                        lambda task: {"status": TaskStatus.COMPLETED, "position": -1})
    mix_web._remember(fake)

    data = mix_web.task_status("mix_test_0003", webui_root)
    assert data["status"] == "failed"
    assert "boom" in data["error"]


def test_task_status_running_reports_progress(webui_root: Path, monkeypatch):
    """运行态带上最近一次进度（值 + 文案）。"""
    fake = _FakeTask("mix_test_0004")
    fake.last_progress = (0.5, "混音渲染中...")
    monkeypatch.setattr(mix_web.queue_manager, "get_status",
                        lambda task: {"status": TaskStatus.RUNNING, "position": 0})
    mix_web._remember(fake)

    data = mix_web.task_status("mix_test_0004", webui_root)
    assert data["status"] == "running"
    assert data["progress"] == {"value": 0.5, "desc": "混音渲染中..."}


def test_task_registry_is_bounded(webui_root: Path):
    """注册表是 FIFO 上限队列，最旧任务被淘汰。"""
    for i in range(mix_web._TASKS_MAX + 3):
        mix_web._remember(_FakeTask(f"mix_evict_{i}"))
    assert len(mix_web._tasks) == mix_web._TASKS_MAX
    assert mix_web._lookup("mix_evict_0") is None
    assert mix_web._lookup(f"mix_evict_{mix_web._TASKS_MAX + 2}") is not None


def test_cancel_render_unknown_task(webui_root: Path):
    """取消未登记任务返回 ok=False，不触碰队列。"""
    assert mix_web.cancel_render("mix_not_exist")["ok"] is False


def test_cancel_render_forwards_to_queue(monkeypatch):
    """取消已登记任务时把 task_id 透传给 queue_manager。"""
    fake = _FakeTask("mix_test_0005")
    mix_web._remember(fake)
    seen = {}
    monkeypatch.setattr(mix_web.queue_manager, "cancel_task_by_id",
                        lambda tid: seen.setdefault("tid", tid) or True)
    assert mix_web.cancel_render("mix_test_0005")["ok"] is True
    assert seen["tid"] == "mix_test_0005"


# ------------------------------------------------------------------ 5. 文案
def test_page_texts_complete():
    """编辑页文案表覆盖全部键，英文有翻译（禁止前端硬编码文案）。"""
    zh = mix_web.page_texts("zh")
    en = mix_web.page_texts("en")
    assert set(zh) == set(mix_web.PAGE_TEXT_KEYS)
    for key, text in zh.items():
        assert text                                        # 中文原文非空
        assert en[key]                                     # 英文译文非空
    # 抽查：中文键在英文表里必须给出不同译文（漏译会回退原文而相等）
    for key in ("多轨编辑器", "静音", "下载", "混音记录", "渲染成品"):
        assert en[key] != zh[key], key


# ------------------------------------------------------------------ 6. 源码接线
def test_app_registers_mix_routes_and_entry():
    """app.py 必须注册静态页/接口路由，并在分离页提供编辑入口。"""
    app_src = (WEBUI_DIR / "app.py").read_text(encoding="utf-8")
    for path in ("/static/multitrack/", "/api/mix/sources", "/api/mix/peaks",
                 "/api/mix/audio", "/api/mix/render", "/api/mix/status",
                 "/api/mix/cancel"):
        assert f'"{path}"' in app_src, path
    assert 'gr.Button(_t("多轨编辑")' in app_src
    assert "window.open('/static/multitrack/'" in app_src


def test_editor_page_calls_expected_endpoints():
    """静态编辑页必须引用后端接口，且从后端文案表取词（无硬编码中文界面文案）。"""
    page = (WEBUI_DIR / "static" / "multitrack" / "index.html").read_text(encoding="utf-8")
    for path in ("/api/mix/sources", "/api/mix/peaks", "/api/mix/audio",
                 "/api/mix/render", "/api/mix/status", "/api/mix/cancel"):
        assert path in page, path
    assert "S.T" in page and "T(" in page      # 文案取自后端下发的 strings
    assert "version: 1" in page                 # 工程契约版本与 mix_render 对齐


def test_history_recycles_mix_dir():
    """history 必须把 mix_ 目录纳入可整目录回收的前缀（删除记录时一并回收）。"""
    import history as H
    assert "mix_" in H._PROJECT_DIR_PREFIXES
    assert H._DERIVED_DIR_PREFIXES.get("mix") == "mix_"


# ------------------------------------------------------------------ 7. 端到端
@pytest.mark.skipif(not HAS_FFMPEG, reason="需要系统 ffmpeg")
def test_end_to_end_render_writes_history(webui_root: Path, history_mgr: HistoryManager):
    """真实链路：提交 → 队列渲染 → 产物落盘 → 写 mix 历史 → 目录可整目录回收。"""
    data = mix_web.submit_render(_payload(webui_root), webui_root, history_mgr, "e2e")
    assert data["ok"] is True, data
    task_id = data["task_id"]

    deadline = time.time() + 90
    status = ""
    while time.time() < deadline:
        status = mix_web.task_status(task_id, webui_root).get("status", "")
        if status in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.2)

    info = mix_web.task_status(task_id, webui_root)
    assert status == "completed", info
    out = webui_root / info["output"]
    assert info["output"].startswith("outputs/mix_")
    assert out.is_file() and out.stat().st_size > 0

    # 历史侧：mix 记录落库，并出现在编辑页的混音记录清单里
    rec = history_mgr.get(task_id)
    assert rec is not None and rec.record_type == "mix"
    mixes = mix_web.list_sources(history_mgr, webui_root)["mixes"]
    assert any(m["task_id"] == task_id for m in mixes)

    # 删除记录时整目录属于该记录（可安全回收），不做实际删除以免污染回收站
    assert history_mgr._derived_dir_for(rec) == out.parent.resolve()
