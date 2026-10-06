"""多轨混音 Web 接口（M2 + M3）单元测试。

覆盖内容：
- resolve_audio 路径白名单（越界/扩展名/不存在）与 audio_mime 映射
- compute_peaks 峰值分桶（需系统 ffmpeg，缺失时自动跳过）与缺文件兜底
- list_sources 素材清单（分离记录 → sources，混音记录 → mixes）
- submit_render / task_status / cancel_render 的契约与错误分支
- save_project / list_projects / load_project 工程持久化（M3）
- rename_mix / delete_mix 混音记录管理（M3，删除走桩函数不污染回收站）
- page_texts 文案完整性（含英文翻译）
- app.py 路由注册、「多轨编辑」Tab 内嵌 iframe（源码级断言）

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


def test_compute_peaks_per_key_lock_single_decode(webui_root: Path, monkeypatch):
    """A10：并发请求同一 key——per-key 锁 + 双重检查，只真正解码一次。"""
    import threading
    from array import array
    path = webui_root / "outputs" / "separations_20260927_104930" / "demo_20260927_104930_vocals.wav"
    monkeypatch.setattr(mix_web, "_peak_cache", type(mix_web._peak_cache)())
    monkeypatch.setattr(mix_web, "_peak_key_locks", {})
    calls = {"n": 0}

    def _fake_decode(_p):
        calls["n"] += 1
        time.sleep(0.15)  # 放大竞态窗口，让并发线程真正重叠
        return array("h", [1000] * 8000)

    monkeypatch.setattr(mix_web, "_decode_mono", _fake_decode)
    results = []

    def _worker():
        results.append(mix_web.compute_peaks(path, 400))

    ths = [threading.Thread(target=_worker) for _ in range(5)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert calls["n"] == 1, f"同 key 应只解码一次，实际 {calls['n']}"
    assert len(results) == 5 and all(r["peaks"] for r in results)


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


# ------------------------------------------------------------------ 5. 工程持久化（M3）
def test_save_and_load_project_roundtrip(webui_root: Path):
    """保存 → 落盘到 outputs/mix_projects/ → 原样载入（src 归一为相对路径）。"""
    saved = mix_web.save_project(_payload(webui_root), webui_root, "我的工程")
    assert saved["ok"] is True and saved["name"] == "我的工程"
    p = webui_root / saved["path"]
    assert p.is_file() and p.parent.name == mix_web.MIX_PROJECT_DIR

    loaded = mix_web.load_project(webui_root, saved["path"])
    assert loaded["ok"] is True and loaded["name"] == "我的工程"
    proj = loaded["project"]
    assert proj["version"] == 1
    assert proj["duration"] == 1.0
    assert proj["source_root_task_id"] == "sep_1"
    assert proj["tracks"][0]["gain_db"] == -1.5
    assert proj["tracks"][0]["src"].startswith("outputs/separations_")


def test_save_project_persists_fx_params(webui_root: Path):
    """P1 音质 + P2 音效参数随工程落盘并可原样载入（保存→载入→再保存 幂等）。"""
    payload = _payload(webui_root)
    payload["tracks"][0].update(
        {"pan": -0.4, "eq_low": 3.5, "eq_mid": -2.0, "eq_high": 1.0, "hpf": 90, "lpf": 14000,
         "comp_th": -18.0, "comp_ratio": 3.0,
         "echo_delay": 300.0, "echo_fb": 0.4, "echo_mix": 0.35})
    saved = mix_web.save_project(payload, webui_root, "fx")
    assert saved["ok"] is True

    track = mix_web.load_project(webui_root, saved["path"])["project"]["tracks"][0]
    assert (track["pan"], track["eq_low"], track["eq_mid"], track["eq_high"],
            track["hpf"], track["lpf"]) == (-0.4, 3.5, -2.0, 1.0, 90.0, 14000.0)
    assert (track["comp_th"], track["comp_ratio"], track["echo_delay"],
            track["echo_fb"], track["echo_mix"]) == (-18.0, 3.0, 300.0, 0.4, 0.35)
    # 往返：载入后再保存，音质/音效字段都不丢
    again = mix_web.save_project(mix_web.load_project(webui_root, saved["path"])["project"],
                                 webui_root, "fx2")
    t2 = mix_web.load_project(webui_root, again["path"])["project"]["tracks"][0]
    assert t2["pan"] == -0.4 and t2["comp_th"] == -18.0 and t2["echo_mix"] == 0.35


def test_save_project_rejects_invalid_and_sanitizes_name(webui_root: Path):
    """非法工程不落盘；工程名清洗非法字符、空名回退时间戳。"""
    bad = _payload(webui_root)
    bad["tracks"][0]["src"] = "../../app.py"
    assert mix_web.save_project(bad, webui_root, "x")["ok"] is False

    dirty = mix_web.save_project(_payload(webui_root), webui_root, "../a:b?")
    assert dirty["ok"] is True
    for ch in "/\\:*?\"<>|":
        assert ch not in dirty["name"], ch

    auto = mix_web.save_project(_payload(webui_root), webui_root, "   ")
    assert auto["ok"] is True and len(auto["name"]) == 15     # YYYYmmdd_HHMMSS


def test_list_projects_skips_broken(webui_root: Path):
    """工程清单只收录可解析 JSON，损坏文件跳过。"""
    mix_web.save_project(_payload(webui_root), webui_root, "p1")
    d = webui_root / "outputs" / mix_web.MIX_PROJECT_DIR
    (d / "broken.json").write_text("{不是合法 json", encoding="utf-8")

    data = mix_web.list_projects(webui_root)
    assert data["ok"] is True
    assert [p["name"] for p in data["projects"]] == ["p1"]
    assert data["projects"][0]["track_count"] == 1


def test_load_project_rejects_path_outside_or_missing(webui_root: Path):
    """载入工程必须落在 mix_projects/ 内且存在。"""
    assert mix_web.load_project(webui_root, "")["ok"] is False
    assert mix_web.load_project(webui_root, "outputs/mix_projects/none.json")["ok"] is False
    assert mix_web.load_project(webui_root, "../../app.py")["ok"] is False
    # 白名单目录之外的同名 JSON 也不允许
    (webui_root / "outputs" / "x.json").write_text("{}", encoding="utf-8")
    assert mix_web.load_project(webui_root, "outputs/x.json")["ok"] is False


# ------------------------------------------------------------------ 6. 混音记录管理（M3）
def test_rename_mix_forwards_to_history(history_mgr: HistoryManager, monkeypatch):
    """改名走 history.rename_project（文件级重命名，保留时间戳）。"""
    seen = []

    def _stub(out, name):
        seen.append((out, name))
        return 1

    monkeypatch.setattr(history_mgr, "rename_project", _stub)
    data = mix_web.rename_mix(history_mgr, "mix_1", "新名")
    assert data == {"ok": True, "renamed": 1}
    assert seen == [("outputs/mix_20260928_090000", "新名")]


def test_mix_record_unknown_or_empty_task(history_mgr: HistoryManager):
    """未知/空 task_id 一律返回 ok=False，不触碰历史。"""
    assert mix_web.rename_mix(history_mgr, "nope", "x")["ok"] is False
    assert mix_web.rename_mix(history_mgr, "", "x")["ok"] is False
    assert mix_web.delete_mix(history_mgr, "nope")["ok"] is False
    assert mix_web.delete_mix(history_mgr, "")["ok"] is False


def test_delete_mix_uses_recycle(history_mgr: HistoryManager, monkeypatch):
    """删除走 delete_project（整目录移入回收站），无匹配记录时返回 ok=False。"""
    seen = []

    def _stub(out):
        seen.append(out)
        return 1

    monkeypatch.setattr(history_mgr, "delete_project", _stub)
    assert mix_web.delete_mix(history_mgr, "mix_1") == {"ok": True, "removed": 1}
    assert seen == ["outputs/mix_20260928_090000"]

    monkeypatch.setattr(history_mgr, "delete_project", lambda out: 0)
    assert mix_web.delete_mix(history_mgr, "mix_1")["ok"] is False


# ------------------------------------------------------------------ 7. 文案
def test_page_texts_complete():
    """编辑页文案表覆盖全部键，英文有翻译（禁止前端硬编码文案）。"""
    zh = mix_web.page_texts("zh")
    en = mix_web.page_texts("en")
    assert set(zh) == set(mix_web.PAGE_TEXT_KEYS)
    for key, text in zh.items():
        assert text                                        # 中文原文非空
        assert en[key]                                     # 英文译文非空
    # 抽查：中文键在英文表里必须给出不同译文（漏译会回退原文而相等）
    for key in ("多轨编辑器", "静音", "下载", "混音记录", "渲染成品",
                "保存工程", "打开", "改名", "试听本轨",
                "播放", "暂停", "停止", "循环", "正在解码音频", "正在下载音频",
                "播放控制", "取消选择区", "起始", "时长",
                "音质", "声像", "低频", "中频", "高频", "高通", "低通",
                "分贝", "Hz（0 = 关闭）",
                "压缩", "阈值", "比率", "回声", "延迟", "延迟（0 = 自动 250ms）", "反馈", "混合",
                "归零", "全部复位到中性值", "FX 工具", "开关", "正常", "旁通",
                "旁通本轨全部音质与音效", "预设", "应用", "保存为预设…", "预设名称",
                "中性", "复制到", "复制", "全部其他轨"):
        assert en[key] != zh[key], key
    # 电平表 / GR 表标签为音频术语，中英一致（不参与「译文不同」抽查）
    for key in ("IN", "OUT", "GR"):
        assert key in mix_web.PAGE_TEXT_KEYS
        assert zh[key] == en[key] == key


# ------------------------------------------------------------------ 8. 源码接线
def test_app_registers_mix_routes_and_entry():
    """app.py 必须注册静态页/接口路由，并以独立「多轨编辑」Tab 内嵌编辑页。"""
    app_src = (WEBUI_DIR / "app.py").read_text(encoding="utf-8")
    for path in ("/static/multitrack/", "/api/mix/sources", "/api/mix/peaks",
                 "/api/mix/audio", "/api/mix/render", "/api/mix/status",
                 "/api/mix/cancel", "/api/mix/projects", "/api/mix/project",
                 "/api/mix/record"):
        assert f'"{path}"' in app_src, path
    # 独立 Tab「多轨编辑」：同一编辑页以 iframe 内嵌（embed=1 + 主题跟随）
    assert 'gr.Tab(_t("多轨编辑")) as tab_mix' in app_src
    assert '/static/multitrack/?embed=1' in app_src
    assert 'elem_id="mix-editor-embed"' in app_src
    # 分离页旧入口（内嵌 iframe + 新窗口按钮）已移除，入口统一到新 Tab
    assert "sep_mix_btn" not in app_src
    assert "sep-mix-embed" not in app_src


def test_editor_page_calls_expected_endpoints():
    """静态编辑页必须引用后端接口，且从后端文案表取词（无硬编码中文界面文案）。"""
    page = (WEBUI_DIR / "static" / "multitrack" / "index.html").read_text(encoding="utf-8")
    for path in ("/api/mix/sources", "/api/mix/peaks", "/api/mix/audio",
                 "/api/mix/render", "/api/mix/status", "/api/mix/cancel",
                 "/api/mix/projects", "/api/mix/project", "/api/mix/record"):
        assert path in page, path
    assert "S.T" in page and "T(" in page      # 文案取自后端下发的 strings
    assert "version: 1" in page                 # 工程契约版本与 mix_render 对齐
    # M3：工程持久化 + 混音记录改名/删除 + 内嵌主题跟随
    assert "syncParentTheme" in page          # 内嵌时跟随父页面明暗主题
    assert 'html[data-embed="1"]' in page     # 内嵌模式收紧内边距
    assert "applyTrackState" in page          # 载入工程后回填编辑状态
    # 单轨试听：共享隐藏播放器 + Web Audio 增益 + 逐轨 ▶ 按钮
    assert 'id="solo-audio"' in page          # 隐藏的共享播放器元素
    assert "previewTrack" in page             # 每轨试听按钮的处理函数
    assert "createMediaElementSource" in page  # 用 GainNode 套用该轨增益（支持 +dB）


def test_editor_download_progress_for_large_stems():
    """大轨远端下载耗时是主瓶颈：需按 Content-Length 显示下载百分比 + 解码提示。"""
    page = (WEBUI_DIR / "static" / "multitrack" / "index.html").read_text(encoding="utf-8")
    assert "async function fetchWithProgress(" in page
    assert "getReader()" in page                       # 流式读取以报进度
    assert 'resp.headers.get("Content-Length")' in page
    assert 'T("正在下载音频")' in page
    assert 'T("正在解码音频") + " " + pos' in page     # 解码提示带轨序号/名称
    assert "正在下载音频" in mix_web.PAGE_TEXT_KEYS      # 文案键由后端下发（英文可用）


def test_editor_page_has_transport_playback():
    """统一播放（transport）：单一播放/暂停、同步调度、播放头、空格键与循环。"""
    page = (WEBUI_DIR / "static" / "multitrack" / "index.html").read_text(encoding="utf-8")
    # UI：只有一套 transport 控件（每轨不再各播各的），播放按钮前有 label 说明
    for elem in ('id="tp-play"', 'id="tp-stop"', 'id="tp-loop"', 'id="tp-time"',
                 'id="t-transport"'):
        assert elem in page, elem
    # 选区：取消选择区按钮 + 起始时间码/总时长读数
    for elem in ('id="sel-clear"', 'id="sel-info"'):
        assert elem in page, elem
    assert '"sel-clear").addEventListener' in page
    assert "updateSelInfo" in page and "playRange()" in page
    # 引擎：同一 AudioContext + 同一个 start(t0) → 样本级同步；按 path 缓存解码结果
    assert "decodeAudioData" in page          # 首次播放解码并缓存 AudioBuffer
    assert "createBufferSource" in page       # 每轨切片用 AudioBufferSourceNode 调度
    assert "scheduleIteration" in page        # 循环 lookahead 复用同一调度函数
    assert "setTargetAtTime" in page          # Mute/Solo/增益 播放中实时生效
    # 片段/淡变/选区语义
    assert "trackSegments" in page            # 裁切按切片调度，无裁切按整轨
    assert "playRange" in page                # 有选区只播选区，无选区播整曲
    assert "gAt" in page                      # 淡入淡出包络折线
    # 交互：播放头、单击定位、空格键、循环
    assert "updatePlayheads" in page          # 覆盖层竖线跟随播放进度
    assert "seekTo" in page                   # 单击波形定位播放头
    assert "isAudible" in page                # Solo > Mute > 默认发声，与渲染一致
    assert "togglePlay" in page and "keydown" in page and 'ev.code !== "Space"' in page
    # 每轨音质（P1）：声像 + 三段 EQ + 高通/低通，实时预览与导出同一套参数
    for token in ("buildFxChain", "applyFxParams", "applyTrackFx", "fxPanel", "mkFxCtl",
                  "createStereoPanner", "lowshelf", "peaking", "highshelf",
                  "eq_low", "eq_mid", "eq_high", "hpf", "lpf"):
        assert token in page, token
    # 每轨音效（P2）：压缩 + 回声（3 抽头延迟线），同样实时预览与导出同一套参数
    for token in ("createDynamicsCompressor", "createDelay", "mkMod",
                  "comp_th", "comp_ratio", "echo_delay", "echo_fb", "echo_mix",
                  "FX_ECHO_TAPS", "FX_COMP_ATTACK"):
        assert token in page, token
    # 第二批：输入/输出电平表 + 压缩 GR 表 + EQ 频响曲线（可视化与旋钮双向联动）
    for token in ("mkMeterBlock", "mkMtrCol", "mkMeterTap", "disposeMeterTap", "meterLoop",
                  "getFloatTimeDomainData", "createChannelSplitter",
                  "drawEqCurve", "attachEqCurve", "setEqGain", "EQ_HANDLES",
                  "getFrequencyResponse", "OfflineAudioContext",
                  'className = "eqc"', "fxmeter", "mt-col", "fxside", "fxknobs"):
        assert token in page, token


def test_editor_defaults_hotkey_and_stepper():
    """交互细化：循环默认勾选、空格为全局热键、音效参数默认中性、旋钮/推子控件齐全。"""
    page = (WEBUI_DIR / "static" / "multitrack" / "index.html").read_text(encoding="utf-8")
    # 循环默认勾选，且状态初值与复选框一致
    assert 'id="tp-loop" type="checkbox" checked' in page
    assert "loop: true" in page
    # 空格 = 全局热键：捕获阶段监听 window（点过任何组件后仍生效），长按不重复触发
    assert 'window.addEventListener("keydown"' in page
    assert "}, true);" in page
    assert "ev.repeat" in page
    assert "isTextInput" in page                    # 仅文本类输入放行空格
    # FX 面板：三个模块分组框（音质/压缩/回声），旋钮与推子两种形态
    for token in ('data-mod="eq"', 'data-mod="dyn"', 'data-mod="echo"',
                  "fxmod-h", "knobrow", "faderrow"):
        assert token in page, token
    # 音效参数默认中性：延迟/反馈初值与轨道默认值均为 0（混合 0 即关闭）
    assert 'label: "延迟", tip: "延迟（0 = 自动 250ms）", kind: "ms", shape: "fader"' in page
    assert 'label: "反馈", tip: "反馈", kind: "fb", shape: "fader"' in page
    assert "echoDelay: 0, echoFb: 0" in page
    # 控件形态：常态为只读数值徽标，双击原地换成 number 输入精确录入
    assert 'inp.type = "number"; inp.className = "fxnum"' in page
    assert "beginEdit" in page
    # 交互：拖拽（Shift 精调）、滚轮、双击复位、键盘 Home 复位
    assert "setPointerCapture" in page
    assert "ev.shiftKey ? 900 : 180" in page        # 拖满 ≈180px；Shift 精调 1/5
    assert 'addEventListener("dblclick", () => set(neutral, true))' in page
    assert 'ev.code === "Home"' in page
    # 工程回灌：控件走 setV（不再直接写 .value）
    assert ".panEl.setV(t.pan)" in page
    assert ".echoMixEl.setV(t.echoMix)" in page
    # 第二批：曲线与旋钮双向联动，工程回灌后也要重绘曲线
    assert ".eqLowEl.setV(t.eqLow)" in page
    assert "drawEqCurve(t);" in page                 # applyTrackState 回灌后重绘
    assert "drawEqCurve(track);" in page             # 旋钮 onChange 驱动重绘


def test_editor_fx_reset_tools_presets_and_embed_fit():
    """第三批：内嵌自适应高度、音质/压缩/回声一键归零、FX 工具模块（旁通/预设/复制）。"""
    page = (WEBUI_DIR / "static" / "multitrack" / "index.html").read_text(encoding="utf-8")
    # 内嵌自适应高度：同源反向改写自身 iframe 高度 + ResizeObserver 跟随内容
    for token in ("fitParentHeight", "watchEmbedHeight", "window.frameElement",
                  "ResizeObserver", "getBoundingClientRect().height"):
        assert token in page, token
    assert "watchEmbedHeight();" in page          # 启动时挂上监听
    assert "fitParentHeight();" in page           # 载入素材后补一次高度
    # 音质/压缩/回声「一键归零」：控件工厂暴露 resetV，模块标题右侧渲染归零按钮
    assert "resetV: () => set(neutral, true)" in page
    assert 'rb.className = "fxmod-r"' in page
    assert 'rb.textContent = T("归零")' in page
    assert "eqResets.push(" in page and "dynResets.push(" in page and "echoResets.push(" in page
    assert 'mkMod("echo", "回声", echoResets)' in page
    assert "echoResets.push(echoDelayEl.resetV, echoFbEl.resetV, echoMixEl.resetV);" in page
    # 音质 = 压缩 固定同宽（压缩内容居中），回声自适应
    assert "--fxmod-w: 362px;" in page
    assert '.fxmod[data-mod="eq"], .fxmod[data-mod="dyn"] { width: var(--fxmod-w); }' in page
    assert '.fxmod[data-mod="dyn"] .fxmod-b { align-items: center; }' in page
    # FX 工具模块（独占整行、内部横向均布）：旁通开关 / 预设（内置+自定义）/ 复制到其他轨
    assert 'mkMod("tool", "FX 工具")' in page
    assert '.fxmod[data-mod="tool"] { flex: 1 1 100%; }' in page
    assert ".fxmod[data-mod=\"tool\"] .fxmod-b { flex-direction: row;" in page
    for token in ("ftrow", "ftlbl", "ftsel", "ftbtn", "ftbtn wfull", "fxToolsPaint"):
        assert token in page, token
    # 旁通：只压暗被影响的处理模块，弹回时参数不变（前端推中性值，后端不生成滤镜）
    assert ".fxpanel.fx-off .fxmod:not([data-mod=\"tool\"]), .fxpanel.fx-off .fxmeter" in page
    assert "const v = s.fxOn === false" in page
    # 工程契约 version 1 新增 fx_on：前端 buildPayload 写出、applyTrackState 回灌
    assert "fx_on: t.fxOn !== false," in page
    assert "t.fxOn = st.fx_on === undefined ? true : !!st.fx_on;" in page
    # FX 预设：内置三档 + localStorage 自定义（保存项为下拉里的特殊选项）
    assert "FX_PRESET_BUILTIN" in page and "FX_PRESET_STORE" in page
    assert '"yue2.mix.fxPresets"' in page
    assert 'opt("__save__", T("保存为预设…"))' in page
    # 复制 FX：目标列表在展开时惰性重建（此时轨道列表才完整），含「全部其他轨」
    assert 'opt("__all__", T("全部其他轨"))' in page
    assert "o.fxOn = track.fxOn;" in page          # 旁通状态一并复制


def test_editor_meters_wiring_is_bypass_only():
    """电平表/GR 表接线：取样链必须旁路（不串进音频链路），播放后启动刷新、停止后释放。"""
    page = (WEBUI_DIR / "static" / "multitrack" / "index.html").read_text(encoding="utf-8")
    # 旁路取样：分析器挂在 ChannelSplitter 上，源节点额外 connect 一路，不改动主链路
    assert "split.connect(anL, 0, 0)" in page and "split.connect(anR, 1, 0)" in page
    assert "g.connect(t.meter.out.split)" in page            # OUT 取轨增益之后
    assert "env.connect(t.meter.in.split)" in page           # IN 取音质链之前（含淡变包络）
    # 播放时建链、停止时释放（保留残值给回落动画）
    assert "t.meter = { in: mkMeterTap(ctx), out: mkMeterTap(ctx), fx: t.fx }" in page
    assert "disposeMeterTap(t.meter.in); disposeMeterTap(t.meter.out);" in page
    assert "if (!t.mtr) t.mtr = newMtrState();" in page
    # rAF 刷新循环：播放中持续、停止后归零并自终止（不空转）
    assert "meterLoop();" in page
    assert "if (S.playing || live)" in page and "_mtrRaf = requestAnimationFrame(step)" in page
    # GR 读压缩节点的增益衰减量（Chrome 为只读 number，规范为 AudioParam，两种形态都兼容）
    assert "comp.reduction" in page and 'typeof rr === "number"' in page
    # EQ 曲线：3 个手柄按 x 就近吸附后纵向改 gain（频率固定），与旋钮共用同一套区间
    assert "EQ_GAIN_MAX" in page and "Math.round(v / 0.5) * 0.5" in page
    # 主题切换（内嵌跟随父页）后波形与曲线一并重绘
    assert "S.tracks.forEach((t) => { drawTrack(t); drawEqCurve(t); });" in page


def test_history_recycles_mix_dir():
    """history 必须把 mix_ 目录纳入可整目录回收的前缀（删除记录时一并回收）。"""
    import history as H
    assert "mix_" in H._PROJECT_DIR_PREFIXES
    assert H._DERIVED_DIR_PREFIXES.get("mix") == "mix_"


# ------------------------------------------------------------------ 9. 端到端
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
