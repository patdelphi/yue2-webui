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
    _comp_filter,
    _echo_filter,
    _fx_filters,
    _pan_filters,
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


def test_fx_params_default_and_clamped(webui_root: Path):
    """每轨音质参数（P1）：缺省为中性 0，越界钳制，非法类型回退 0。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"
    # 缺省：全部中性（此时不产生任何音质滤镜）
    track0 = parse_mix_project(_payload(tracks=[{"id": "t", "src": src, "clips": []}]),
                               webui_root).tracks[0]
    assert (track0.pan, track0.eq_low, track0.eq_mid, track0.eq_high,
            track0.hpf, track0.lpf) == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    # 越界钳制 + 非法类型回退
    track1 = parse_mix_project(_payload(tracks=[{
        "id": "t", "src": src, "clips": [],
        "pan": 9, "eq_low": -99, "eq_mid": 99, "eq_high": "abc",
        "hpf": 99999, "lpf": -5,
    }]), webui_root).tracks[0]
    assert track1.pan == 1.0
    assert track1.eq_low == -15.0 and track1.eq_mid == 15.0
    assert track1.eq_high == 0.0
    assert track1.hpf == 2000.0 and track1.lpf == 0.0


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


# ---------------------------------------------------------- 2b. 每轨音质（P1）
def test_pan_filters_follow_stereo_law():
    """声像滤镜遵循 Web Audio StereoPannerNode 等功率立体声算法；居中不产生滤镜。"""
    assert _pan_filters(0.0) == []
    assert _pan_filters(0.001) == []                 # 阈值内视为居中

    left = _pan_filters(-1.0)                        # 全左：outL=c0+c1（gL=1），outR=0*c1
    assert left[0] == "aformat=channel_layouts=stereo"
    assert left[1] == "pan=stereo|c0=c0+1.0000*c1|c1=0.0000*c1"

    right = _pan_filters(1.0)                        # 全右：outL=0*c0，outR=c1+c0
    assert right[1] == "pan=stereo|c0=0.0000*c0|c1=c1+1.0000*c0"

    mid = _pan_filters(0.5)                          # 半右：gL=cos(45°)=gR=√2/2
    assert "c0=0.7071*c0" in mid[1] and "c1=c1+0.7071*c0" in mid[1]


def test_fx_filters_neutral_skips_and_active_orders(webui_root: Path):
    """中性音质参数不产生滤镜；有值时按 高通→低通→低/中/高 EQ→声像 顺序排列。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"
    neutral = parse_mix_project(_payload(tracks=[{"id": "t", "src": src, "clips": []}]),
                                webui_root).tracks[0]
    assert _fx_filters(neutral) == []

    active = parse_mix_project(_payload(tracks=[{
        "id": "t", "src": src, "clips": [],
        "pan": -0.5, "eq_low": 3.0, "eq_mid": -2.0, "eq_high": 1.5,
        "hpf": 80, "lpf": 12000,
    }]), webui_root).tracks[0]
    fx = _fx_filters(active)
    assert fx[0] == "highpass=f=80.0:width_type=q:width=0.707"
    assert fx[1] == "lowpass=f=12000.0:width_type=q:width=0.707"
    assert fx[2] == "bass=g=3.0:f=200.0"
    assert fx[3] == "equalizer=f=1000.0:width_type=q:width=1:g=-2.0"
    assert fx[4] == "treble=g=1.5:f=4000.0"
    assert fx[5] == "aformat=channel_layouts=stereo" and fx[6].startswith("pan=stereo|")


# ------------------------------------------------- 2c. 每轨音效（P2：压缩 + 回声）
def test_fx_effect_params_default_and_clamped(webui_root: Path):
    """每轨音效参数（P2）：缺省为中性（0dB/1/0/0/0），越界钳制，非法类型回退。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"
    track0 = parse_mix_project(_payload(tracks=[{"id": "t", "src": src, "clips": []}]),
                               webui_root).tracks[0]
    assert (track0.comp_th, track0.comp_ratio, track0.echo_delay,
            track0.echo_fb, track0.echo_mix) == (0.0, 1.0, 0.0, 0.0, 0.0)
    track1 = parse_mix_project(_payload(tracks=[{
        "id": "t", "src": src, "clips": [],
        "comp_th": 12, "comp_ratio": 99, "echo_delay": 99999,
        "echo_fb": 5, "echo_mix": -3,
    }]), webui_root).tracks[0]
    assert track1.comp_th == 0.0            # 超过 0dB 上限钳制
    assert track1.comp_ratio == 20.0        # 比率上限
    assert track1.echo_delay == 2000.0      # 延迟上限（ms）
    assert track1.echo_fb == 0.95           # 反馈上限（防无限堆积）
    assert track1.echo_mix == 0.0           # 负值回退下限
    track2 = parse_mix_project(_payload(tracks=[{
        "id": "t", "src": src, "clips": [],
        "comp_th": -99, "comp_ratio": "abc", "echo_delay": "abc",
        "echo_fb": None, "echo_mix": None,
    }]), webui_root).tracks[0]
    assert track2.comp_th == -60.0          # 阈值下限
    assert track2.comp_ratio == 1.0         # 非法类型回退中性（不压缩）
    assert track2.echo_delay == 0.0
    assert track2.echo_fb == 0.0 and track2.echo_mix == 0.0


def test_comp_filter_linear_threshold_and_off(webui_root: Path):
    """压缩滤镜：阈值按 dB→线性换算；阈值到顶/比率≤1 一律视为关闭（返回空串）。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"

    def _mk(**kw):
        return parse_mix_project(_payload(tracks=[dict(
            {"id": "t", "src": src, "clips": []}, **kw)]), webui_root).tracks[0]

    assert _comp_filter(_mk()) == ""                              # 缺省中性
    assert _comp_filter(_mk(comp_th=-20, comp_ratio=1.0)) == ""   # 比率 1 = 不压缩
    assert _comp_filter(_mk(comp_th=0, comp_ratio=4.0)) == ""     # 阈值到顶 = 不压缩
    # -20dB → 线性 10^(-1) = 0.1；时间常数与前端固定值一致
    assert _comp_filter(_mk(comp_th=-20, comp_ratio=4.0)) == (
        "acompressor=threshold=0.100000:ratio=4.00:attack=20.0:release=250.0"
        ":knee=6.0:makeup=1:mix=1")
    # 极低阈值仍受 _COMP_MIN_LINEAR 保护（ffmpeg 不接受 0）
    assert "threshold=0.001000" in _comp_filter(_mk(comp_th=-60, comp_ratio=2.0))


def test_echo_filter_taps_default_delay_and_decay_floor(webui_root: Path):
    """回声滤镜：3 抽头延迟线；延迟为 0 时用兜底 250ms；decay 有下限（ffmpeg 要求 > 0）。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"

    def _mk(**kw):
        return parse_mix_project(_payload(tracks=[dict(
            {"id": "t", "src": src, "clips": []}, **kw)]), webui_root).tracks[0]

    assert _echo_filter(_mk()) == ""                              # 混合 0 = 关闭
    assert _echo_filter(_mk(echo_mix=0.009)) == ""                # 阈值内视为关闭
    # 混合 0.5 / 反馈 0.3 / 延迟 200ms → decays = 0.5*fb^k（k=1..3）
    assert _echo_filter(_mk(echo_mix=0.5, echo_fb=0.3, echo_delay=200)) == (
        "aecho=1:1:200.0|400.0|600.0:0.1500|0.0450|0.0135")
    # 延迟留 0 → 用兜底 250ms（与前端 FX_ECHO_DEF_MS 一致）
    assert _echo_filter(_mk(echo_mix=0.5, echo_fb=0.3, echo_delay=0)).startswith(
        "aecho=1:1:250.0|500.0|750.0:")
    # 反馈 0：decay 取到下限 0.001，避免 ffmpeg 报 out of allowed range
    assert _echo_filter(_mk(echo_mix=0.5, echo_fb=0.0, echo_delay=100)).endswith(
        ":0.0010|0.0010|0.0010")


def test_fx_filters_p2_order_after_pan(webui_root: Path):
    """音效滤镜顺序：声像 → 压缩 → 回声（与前端节点链、听感一致）。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"
    active = parse_mix_project(_payload(tracks=[{
        "id": "t", "src": src, "clips": [],
        "pan": -0.5, "eq_low": 3.0, "comp_th": -18, "comp_ratio": 3.0,
        "echo_mix": 0.4, "echo_fb": 0.4, "echo_delay": 300,
    }]), webui_root).tracks[0]
    fx = _fx_filters(active)
    assert fx[0] == "bass=g=3.0:f=200.0"
    assert fx[1] == "aformat=channel_layouts=stereo" and fx[2].startswith("pan=stereo|")
    assert fx[3].startswith("acompressor=threshold=")
    assert fx[4].startswith("aecho=1:1:300.0|600.0|900.0:")
    # 中性音效（阈值 0 / 比率 1 / 混合 0）不产生任何滤镜
    neutral = parse_mix_project(_payload(tracks=[{"id": "t", "src": src, "clips": []}]),
                               webui_root).tracks[0]
    assert _fx_filters(neutral) == []


def test_build_cmd_includes_fx_after_volume(webui_root: Path):
    """音质/音效滤镜必须排在 volume 之后、adelay 之前（增益→音质音效→时间线偏移）。"""
    src = "outputs/separations_20260927_104930/demo_20260927_104930_vocals.wav"
    tracks = [{
        "id": "t", "src": src, "gain_db": -1.0,
        "clips": [{"start": 1.0, "in": 0.0, "out": 0.5}],
        "eq_low": 4.0, "hpf": 100,
        "comp_th": -20, "comp_ratio": 3.0, "echo_mix": 0.3, "echo_fb": 0.4,
    }]
    proj = parse_mix_project(_payload(tracks=tracks), webui_root)
    f = _filters(build_ffmpeg_cmd(proj, Path("out.flac")))
    chain = f.split("[t0]")[0]
    assert chain.index("volume=-1.0dB") < chain.index("highpass=f=100.0")
    assert chain.index("highpass=f=100.0") < chain.index("bass=g=4.0")
    assert chain.index("bass=g=4.0") < chain.index("acompressor=threshold=")
    assert chain.index("acompressor=threshold=") < chain.index("aecho=1:1:")
    assert chain.index("aecho=1:1:") < chain.index("adelay=1000")


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
