"""多轨混音渲染（M1）单元测试。

覆盖内容：
- 混音工程 JSON 的解析与校验（版本、轨数、路径白名单、增益/响度钳制、非法值回退）
- ffmpeg filtergraph 生成（单轨/多轨 amix/静音排除/裁剪延迟/淡变/多 clip asplit/编码选择）
- 端到端渲染（需系统 ffmpeg，缺失时自动跳过）与队列 worker 的历史写入

说明：本文件为 M1（后端渲染接口）的验收测试，运行方式：
    pytest tests/test_mix_render.py
"""
import json
import math
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mix_render import (  # noqa: E402
    DEFAULT_MASTER,
    MAX_TRACKS,
    MIX_SCHEMA_VERSION,
    MixProjectError,
    build_ffmpeg_cmd,
    mix_worker,
    parse_mix_project,
    render_mix,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None


# ------------------------------------------------------------------ 测试夹具工具
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
    """构造一个带 outputs/ 的假 webui_root，并放入两条真实 wav 轨道。"""
    root = tmp_path / "webui"
    out = root / "outputs" / "separations_20260927_104930"
    _make_wav(out / "demo_20260927_104930_vocals.wav", 1.0, 440.0)
    _make_wav(out / "demo_20260927_104930_accompaniment.wav", 1.0, 660.0)
    return root


def _payload(tracks=None, master=None, version=MIX_SCHEMA_VERSION) -> dict:
    """构造一份最小可用的混音工程 JSON。"""
    if tracks is None:
        tracks = [{
            "id": "vocals", "name": "人声",
            "src": "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav",
            "gain_db": 0.0, "mute": False, "clips": [],
        }]
    data = {"version": version, "tracks": tracks}
    if master is not None:
        data["master"] = master
    return data


# ------------------------------------------------------------------ 1. 解析与校验
def test_parse_minimal_ok(webui_root: Path):
    """最小工程可解析：默认 master、轨字段归一化。"""
    proj = parse_mix_project(_payload(), webui_root)
    assert proj.version == MIX_SCHEMA_VERSION
    assert len(proj.tracks) == 1
    assert proj.tracks[0].gain_db == 0.0 and proj.tracks[0].mute is False
    assert proj.tracks[0].clips == []          # 空 clips = 整轨
    assert proj.master["loudness_target"] == DEFAULT_MASTER["loudness_target"]
    assert proj.master["true_peak"] == DEFAULT_MASTER["true_peak"]


def test_parse_rejects_bad_version(webui_root: Path):
    """不支持的版本号必须报错，避免旧/新契约被静默误读。"""
    with pytest.raises(MixProjectError):
        parse_mix_project(_payload(version=99), webui_root)


def test_parse_rejects_empty_or_too_many_tracks(webui_root: Path):
    """轨数为空或超上限时报错。"""
    with pytest.raises(MixProjectError):
        parse_mix_project({"version": 1, "tracks": []}, webui_root)

    one = _payload()["tracks"][0]
    with pytest.raises(MixProjectError):
        parse_mix_project({"version": 1, "tracks": [dict(one) for _ in range(MAX_TRACKS + 1)]},
                          webui_root)


def test_gain_and_master_clamped(webui_root: Path):
    """增益与响度目标越界钳制、非法类型回退默认值。"""
    tracks = [{
        "id": "t1", "src": "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav",
        "gain_db": 999, "clips": [],
    }, {
        "id": "t2", "src": "outputs/separations_20260927_104930/demo_20260927_104930_accompaniment.wav",
        "gain_db": "abc", "clips": [],
    }]
    proj = parse_mix_project(_payload(tracks=tracks, master={"loudness_target": -99, "true_peak": 3}),
                             webui_root)
    assert proj.tracks[0].gain_db == 12.0        # 上限
    assert proj.tracks[1].gain_db == 0.0         # 非法类型回退
    assert proj.master["loudness_target"] == -30.0
    assert proj.master["true_peak"] == -0.1


def test_path_whitelist_rejects_outside_outputs(webui_root: Path):
    """src 越界（不在 outputs/ 下）必须拒绝，防止任意路径被交给 ffmpeg 读取。"""
    for bad in (r"C:\Windows\win.ini",
                "../../seed-vc/inference.py",
                "outputs/../../webui/app.py"):
        tracks = [{"id": "t", "src": bad, "clips": []}]
        with pytest.raises(MixProjectError):
            parse_mix_project(_payload(tracks=tracks), webui_root)


def test_path_must_exist(webui_root: Path):
    """不存在的素材文件应报错，避免 ffmpeg 阶段才失败。"""
    tracks = [{"id": "t", "src": "outputs/separations_20260927_104930/missing.wav", "clips": []}]
    with pytest.raises(MixProjectError):
        parse_mix_project(_payload(tracks=tracks), webui_root)


def test_clip_validation_fallbacks(webui_root: Path):
    """非法 clip 被丢弃、非法淡变回退为 0，且不影响合法 clip。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"
    tracks = [{
        "id": "t", "src": src, "clips": [
            {"start": 0, "in": 0.2, "out": 0.6, "fade_in": 0.1, "fade_out": 0.2},
            {"start": 0, "in": 0.9, "out": 0.3},          # out <= in → 丢弃
            {"start": -5, "in": 0.1, "out": 0.5},         # start 负数 → 回退 0
            {"start": 0, "in": 0.1, "out": 0.2, "fade_in": 5.0, "fade_out": 5.0},  # 淡变过大 → 0
        ],
    }]
    proj = parse_mix_project(_payload(tracks=tracks), webui_root)
    clips = proj.tracks[0].clips
    assert len(clips) == 3, clips
    assert clips[0].fade_in == 0.1 and clips[0].fade_out == 0.2
    assert clips[1].start == 0.0
    assert clips[2].fade_in == 0.0 and clips[2].fade_out == 0.0


def test_all_muted_rejected(webui_root: Path):
    """全部轨静音时无内容可渲染，应报错。"""
    tracks = [{
        "id": "t",
        "src": "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav",
        "mute": True, "clips": [],
    }]
    with pytest.raises(MixProjectError):
        parse_mix_project(_payload(tracks=tracks), webui_root)


# ------------------------------------------------------------------ 2. filtergraph 生成
def _filters(cmd: list) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


def test_build_cmd_single_track(webui_root: Path):
    """单轨：1 个输入、无 amix、必带 loudnorm，编码为 flac。"""
    proj = parse_mix_project(_payload(), webui_root)
    cmd = build_ffmpeg_cmd(proj, Path("out.flac"))
    assert cmd.count("-i") == 1
    f = _filters(cmd)
    assert "amix" not in f
    assert "loudnorm" in f
    assert "volume=0.0dB" in f
    assert "flac" in cmd


def test_build_cmd_multi_track_amix(webui_root: Path):
    """多轨：输入数正确、amix inputs 数一致且禁用归一化（避免电平被压低）。"""
    srcs = "outputs/separations_20260927_104930/"
    tracks = [
        {"id": "vocals", "src": srcs + "demo_20260927_104930_vocals.wav", "gain_db": -2.5, "clips": []},
        {"id": "acc", "src": srcs + "demo_20260927_104930_accompaniment.wav", "gain_db": 1.0, "clips": []},
    ]
    proj = parse_mix_project(_payload(tracks=tracks), webui_root)
    cmd = build_ffmpeg_cmd(proj, Path("out.flac"))
    assert cmd.count("-i") == 2
    f = _filters(cmd)
    assert "amix=inputs=2" in f and "normalize=0" in f
    assert "volume=-2.5dB" in f and "volume=1.0dB" in f


def test_build_cmd_mute_excluded(webui_root: Path):
    """静音轨不进 filtergraph，也不进 -i 列表。"""
    srcs = "outputs/separations_20260927_104930/"
    tracks = [
        {"id": "vocals", "src": srcs + "demo_20260927_104930_vocals.wav", "mute": True, "clips": []},
        {"id": "acc", "src": srcs + "demo_20260927_104930_accompaniment.wav", "clips": []},
    ]
    proj = parse_mix_project(_payload(tracks=tracks), webui_root)
    cmd = build_ffmpeg_cmd(proj, Path("out.flac"))
    assert cmd.count("-i") == 1
    assert "accompaniment" in " ".join(cmd)
    assert "vocals" not in " ".join(cmd)


def test_build_cmd_clip_trim_delay_fade(webui_root: Path):
    """clip 的裁剪、时间线延迟与淡入淡出参数计算正确。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"
    tracks = [{
        "id": "t", "src": src, "gain_db": 0.0,
        "clips": [{"start": 1.5, "in": 0.2, "out": 1.2, "fade_in": 0.1, "fade_out": 0.25}],
    }]
    proj = parse_mix_project(_payload(tracks=tracks), webui_root)
    f = _filters(build_ffmpeg_cmd(proj, Path("out.flac")))
    assert "atrim=start=0.2:end=1.2" in f
    assert "adelay=1500" in f                      # 1.5s → 1500ms
    assert "afade=t=in:st=0:d=0.1" in f
    assert "afade=t=out:st=0.75:d=0.25" in f       # 时长 1.0s - 0.25s


def test_build_cmd_multi_clip_uses_asplit(webui_root: Path):
    """同一素材的多个 clip 需 asplit 展开，避免一个输入 pad 被消费多次。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"
    tracks = [{
        "id": "t", "src": src,
        "clips": [{"start": 0, "in": 0.0, "out": 0.3}, {"start": 0.5, "in": 0.6, "out": 0.9}],
    }]
    proj = parse_mix_project(_payload(tracks=tracks), webui_root)
    f = _filters(build_ffmpeg_cmd(proj, Path("out.flac")))
    assert "asplit=2" in f
    assert "atrim=start=0.0:end=0.3" in f and "atrim=start=0.6:end=0.9" in f
    assert "amix=inputs=2" in f


@pytest.mark.parametrize("suffix,codec", [(".flac", "flac"), (".wav", "pcm_s24le")])
def test_build_cmd_codec_by_suffix(webui_root: Path, suffix: str, codec: str):
    """按输出扩展名选择编码器。"""
    proj = parse_mix_project(_payload(), webui_root)
    cmd = build_ffmpeg_cmd(proj, Path("out" + suffix))
    assert codec in cmd


def test_build_cmd_loudnorm_targets(webui_root: Path):
    """master 目标响度/真峰值写入 loudnorm。"""
    proj = parse_mix_project(_payload(master={"loudness_target": -16.0, "true_peak": -2.0}),
                             webui_root)
    f = _filters(build_ffmpeg_cmd(proj, Path("out.flac")))
    assert "I=-16.0" in f and "TP=-2.0" in f


# ------------------------------------------------------------------ 3. 端到端与 worker
@pytest.mark.skipif(not HAS_FFMPEG, reason="需要系统 ffmpeg")
def test_render_mix_end_to_end(webui_root: Path, tmp_path: Path):
    """真实渲染：产物存在、时长正确、真峰值不超 -1.5dB（loudnorm 生效）。"""
    srcs = "outputs/separations_20260927_104930/"
    tracks = [
        {"id": "vocals", "src": srcs + "demo_20260927_104930_vocals.wav", "gain_db": 0.0, "clips": []},
        {"id": "acc", "src": srcs + "demo_20260927_104930_accompaniment.wav", "gain_db": -3.0, "clips": []},
    ]
    proj = parse_mix_project(_payload(tracks=tracks), webui_root)
    out = tmp_path / "mix.flac"
    info = render_mix(proj, out)
    assert out.exists() and out.stat().st_size > 0
    assert abs(info["duration"] - 1.0) < 0.15, info

    # 用 ffmpeg volumedetect 复核真峰值（应 ≤ -1.5dB 附近，允许 0.3dB 容差）
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(out), "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert "max_volume" in proc.stderr, proc.stderr[-500:]
    max_vol = float(proc.stderr.split("max_volume:")[1].split("dB")[0].strip())
    assert max_vol <= -1.2, f"真峰值越界: {max_vol} dB"


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要系统 ffmpeg")
def test_render_mix_rejects_missing_output_dir(webui_root: Path, tmp_path: Path):
    """输出目录不可创建时抛 MixProjectError（异常必须被捕获为业务错误）。"""
    proj = parse_mix_project(_payload(), webui_root)
    with pytest.raises(MixProjectError):
        render_mix(proj, tmp_path / "mix.mp3")   # 不支持的扩展名


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要系统 ffmpeg")
def test_mix_worker_writes_history(webui_root: Path, tmp_path: Path):
    """mix_worker：渲染 + 写历史（record_type=mix、stems 含成品），返回结构可展示。"""

    class _FakeHistory:
        def __init__(self):
            self.records = []

        def append(self, rec):
            self.records.append(rec)

        def auto_prune(self):
            return 0

    class _FakeTask:
        cancel_event = None
        last_progress = None

        def push_progress(self, *a):
            self.last_progress = a

    hist = _FakeHistory()
    payload = _payload(tracks=[{
        "id": "vocals", "name": "人声",
        "src": "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav",
        "clips": [],
    }])
    out_dir = tmp_path / "outputs" / "mix_20260927_120000"
    res = mix_worker(_FakeTask(), json.dumps(payload), webui_root,
                     out_dir, project="测试曲", history_mgr=hist)

    assert res["ok"] is True and Path(res["output"]).exists()
    assert len(hist.records) == 1
    rec = hist.records[0]
    assert rec.record_type == "mix" and rec.project == "测试曲"
    assert rec.stems and rec.stems[0]["path"].endswith(".flac")


def test_mix_worker_bad_json_returns_error(webui_root: Path, tmp_path: Path):
    """工程 JSON 非法时返回业务错误（不抛未捕获异常，避免 UI 显示连接失败）。"""
    class _FakeTask:
        cancel_event = None

        def push_progress(self, *a):
            pass

    res = mix_worker(_FakeTask(), "{not json", webui_root,
                     tmp_path / "outputs" / "mix_x")
    assert res["ok"] is False and "error" in res
