# -*- coding: utf-8 -*-
"""worker 音频电平测量函数单元测试（_rms_db/_peak_db/_lufs）。

验证点（ffmpeg 生成已知电平音频做相对差断言；无 ffmpeg 环境自动跳过）：
1. _rms_db：-6dB 与 -12dB 正弦波的 RMS 差 ≈ 6dB
2. _peak_db：-6dB 正弦波峰值 ≈ -6dBFS；与 -12dB 的差 ≈ 6dB
3. _lufs：-6dB 与 -12dB 音频的积分响度差 ≈ 6 LUFS（BS.1770 口径，容差放宽）
4. 静音输入：三者均返回 None（-inf 不被正则匹配）
5. 文件不存在：三者均返回 None，不抛异常
6. _match_gain_db：整体 LUFS 差算静态增益，钳 ±18dB，任一测量缺失返回 None
7. 混音人声链不再用 loudnorm 动态归一：静音开头不得被抬噪（回归 2026-10-06 开头宽带噪声）
8. _STEREO_UP 升混：单声道升混后左右声道等电平（回归 2026-10-06「翻唱人声只有左声道」）
9. _lufs 带 pre_filter 测量：与升混后的立体声文件响度一致（等功率升混，LUFS 不变）
"""

import math
import random
import re
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

# voice-tools/worker.py 模块级仅依赖 stdlib + torch（主 venv 可直接导入）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "voice-tools"))

import worker  # noqa: E402

FFMPEG = shutil.which("ffmpeg")

# 无 ffmpeg 的环境（如 CI 精简镜像）整体跳过
pytestmark = pytest.mark.skipif(not FFMPEG, reason="环境无 ffmpeg，跳过电平测量测试")


def _gen_sine(path: str, db: float, seconds: float = 2.0, freq: int = 440) -> str:
    """用 ffmpeg 生成指定分贝的 440Hz 正弦波 wav（44.1kHz 单声道）。"""
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}",
         "-af", f"volume={db}dB", "-ar", "44100", "-ac", "1", path],
        capture_output=True, text=True, check=True,
    )
    return path


def _gen_silence(path: str, seconds: float = 1.0) -> str:
    """生成静音 wav（44.1kHz 单声道）。"""
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", f"anullsrc=d={seconds}",
         "-ar", "44100", "-ac", "1", path],
        capture_output=True, text=True, check=True,
    )
    return path


@pytest.fixture(scope="module")
def sine_pair(tmp_path_factory):
    """-6dB 与 -12dB 两个正弦波文件（module 级复用，ffmpeg 只跑两次）。"""
    d = tmp_path_factory.mktemp("sines")
    return _gen_sine(str(d / "s6.wav"), -6.0), _gen_sine(str(d / "s12.wav"), -12.0)


class TestRmsDb:
    def test_relative_difference(self, sine_pair):
        a, b = sine_pair
        va, vb = worker._rms_db(a), worker._rms_db(b)
        assert va is not None and vb is not None
        assert abs((va - vb) - 6.0) < 0.5

    def test_silence_returns_none(self, tmp_path):
        p = _gen_silence(str(tmp_path / "silence.wav"))
        assert worker._rms_db(p) is None

    def test_missing_file_returns_none(self):
        assert worker._rms_db("Z:/nonexistent/xx.wav") is None


class TestPeakDb:
    def test_sine_crest_factor(self, sine_pair):
        # 正弦波物理规律：峰值 - RMS = 3.01dB（波峰因子），
        # 不依赖 ffmpeg sine 源的默认振幅（其非满刻度，绝对电平无意义）
        a, _ = sine_pair
        vp, vr = worker._peak_db(a), worker._rms_db(a)
        assert vp is not None and vr is not None
        assert abs((vp - vr) - 3.01) < 0.3

    def test_relative_difference(self, sine_pair):
        a, b = sine_pair
        va, vb = worker._peak_db(a), worker._peak_db(b)
        assert va is not None and vb is not None
        assert abs((va - vb) - 6.0) < 0.5

    def test_silence_returns_none(self, tmp_path):
        p = _gen_silence(str(tmp_path / "silence.wav"))
        assert worker._peak_db(p) is None

    def test_missing_file_returns_none(self):
        assert worker._peak_db("Z:/nonexistent/xx.wav") is None


class TestLufs:
    def test_relative_difference(self, sine_pair):
        a, b = sine_pair
        va, vb = worker._lufs(a), worker._lufs(b)
        assert va is not None and vb is not None
        # loudnorm 含响度门限，单音正弦的绝对值意义有限，相对差应稳定 ≈ 6
        assert abs((va - vb) - 6.0) < 1.0

    def test_silence_returns_none(self, tmp_path):
        p = _gen_silence(str(tmp_path / "silence.wav"))
        assert worker._lufs(p) is None

    def test_missing_file_returns_none(self):
        assert worker._lufs("Z:/nonexistent/xx.wav") is None


# --------------------------------------------------- 静态增益混音链（方案 A）


class TestMatchGainDb:
    """_match_gain_db：用整体 LUFS 差算静态匹配增益（替代 loudnorm 动态归一）。"""

    def test_gain_equals_lufs_difference(self):
        # 换嗓干声比原声低 6 LUFS -> 需 +6dB；反之 -6dB
        assert worker._match_gain_db(-16.0, -22.0) == pytest.approx(6.0)
        assert worker._match_gain_db(-16.0, -10.0) == pytest.approx(-6.0)

    def test_gain_is_clamped(self):
        assert worker._match_gain_db(-16.0, -60.0) == pytest.approx(18.0)
        assert worker._match_gain_db(-16.0, 20.0) == pytest.approx(-18.0)

    def test_missing_measurement_returns_none(self):
        # 纯静音 / 测量失败（-inf 解析不到）时无静态增益，交由调用方回退 RMS 匹配
        assert worker._match_gain_db(None, -20.0) is None
        assert worker._match_gain_db(-16.0, None) is None
        assert worker._match_gain_db(None, None) is None


def _head_rms_db(path, seconds=0.5):
    """取文件开头 seconds 秒的 RMS 电平（dB）；-inf 归一到 -120。"""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-ss", "0", "-t", str(seconds),
         "-i", str(path), "-af", "astats=metadata=1:reset=0", "-f", "null", "-"],
        capture_output=True, text=True)
    vals = re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", proc.stderr)
    if not vals:
        return None
    return -120.0 if vals[-1] == "-inf" else float(vals[-1])


def _channel_rms_db(path):
    """取分声道 RMS 电平 (ch1, ch2)；-inf 归一到 -120。

    astats 的「RMS level dB」按声道顺序输出，随后还有一条 Overall，故前 2 条即左右声道。
    """
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
         "-af", "astats=metadata=1:reset=0", "-f", "null", "-"],
        capture_output=True, text=True)
    vals = [-120.0 if v == "-inf" else float(v)
            for v in re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf)", proc.stderr)]
    return (vals[0], vals[1]) if len(vals) >= 2 else (None, None)


def _gen_vocal_like(path, channels=2):
    """造"换嗓干声"式信号：前 3.5s 近静音噪声底(≈-64dB) + 后 2s 正弦(≈-18dB)。

    channels=1 用于模拟 Seed-VC 的真实输出（单声道）。
    对应真实场景：Seed-VC 换嗓输出开头是一段纯噪声底，动态归一（loudnorm）
    会把它抬成与音乐等响的宽带噪声。
    """
    sr, amp_noise, amp_tone = 48000, 6.0e-4, 0.126
    random.seed(0)
    pack = ((lambda v: struct.pack("<hh", v, v)) if channels == 2
            else (lambda v: struct.pack("<h", v)))
    buf = bytearray()
    for _ in range(int(3.5 * sr)):
        buf += pack(int(random.uniform(-1.0, 1.0) * amp_noise * 32767))
    for i in range(int(2.0 * sr)):
        buf += pack(int(amp_tone * math.sin(2 * math.pi * 440 * i / sr) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(buf))
    return str(path)


def _apply_chain(src, dst, af):
    """按给定 -af 链渲染一份音频（渲染失败即断言失败）。"""
    proc = subprocess.run([FFMPEG, "-y", "-hide_banner", "-i", str(src), "-af", af, str(dst)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-500:]
    return str(dst)


# 与产线一致的单声道→立体声升混（不再用会让右声道静音的 pan=stereo|c0=c0|c1=c1）
PAN = f"aresample=48000,{worker._STEREO_UP}"


def test_static_gain_keeps_silent_head_quiet(tmp_path):
    """回归：人声链的静音开头不得被抬噪（2026-10-06 翻唱成品开头宽带噪声）。

    旧链 loudnorm 动态归一：把 ≈-64dB 的开头噪声底放大数十 dB -> 成品开头与音乐等响；
    新链 整体LUFS静态增益 + 限幅器：开头只是乘常数，仍远低于音乐电平。
    """
    src = _gen_vocal_like(tmp_path / "vocal_like.wav")
    head_in = _head_rms_db(src)
    gain = worker._match_gain_db(-16.1, worker._lufs(src))
    assert gain is not None, "测试信号应能被 loudnorm 测出积分响度"

    new = _apply_chain(src, tmp_path / "new.wav",
                       f"{PAN},volume={gain:+.2f}dB,alimiter=limit=0.841:level=false")
    head_new = _head_rms_db(new)

    # 1) 静态增益：开头电平 = 原开头 + 增益（只乘常数，未被按段抬升）
    assert abs((head_new - head_in) - gain) < 1.5
    # 2) 仍处不可闻区（不得像旧链那样顶到 -13dB 量级）
    assert head_new < -40.0

    # 对照：旧链 loudnorm 动态归一会把同一段开头抬成"与音乐等响"
    old = _apply_chain(src, tmp_path / "old.wav",
                       f"{PAN},loudnorm=I=-16.1:TP=-1.5:LRA=11,aresample=48000")
    head_old = _head_rms_db(old)
    assert head_old > head_new + 20.0


def test_convert_uses_static_gain_not_dynamic_loudnorm():
    """源码断言：_convert 的人声链用静态增益 + 限幅器，不再用 loudnorm 动态归一。"""
    src = (Path(__file__).resolve().parent.parent / "voice-tools" / "worker.py").read_text(
        encoding="utf-8")
    assert "alimiter=limit=0.841:level=false[a0]" in src  # 静态增益 + 链尾限幅器
    assert "loudnorm=I=" not in src                       # 人声链不再做动态响度归一


def test_stereo_up_duplicates_mono_into_both_channels(tmp_path):
    """回归：单声道升混后左右声道都必须有声（2026-10-06「翻唱人声只有左声道」）。

    真实链路：Seed-VC 换嗓干声是单声道，混音前需升混成立体声。
    旧写法 pan=stereo|c0=c0|c1=c1 单声道输入下 c1 越界取静音 → 右声道 -inf。
    """
    mono = _gen_sine(str(tmp_path / "mono.wav"), -6.0, seconds=1.0)
    up = _apply_chain(mono, tmp_path / "up.wav", f"aresample=48000,{worker._STEREO_UP}")
    lv, rv = _channel_rms_db(up)
    assert lv is not None and rv is not None
    assert abs(lv - rv) < 0.1   # 等功率升混 → 两声道等电平
    assert rv > -60.0           # 右声道有声（旧写法此处为 -inf）

    # 对照：旧写法右声道全静音
    old = _apply_chain(mono, tmp_path / "old.wav", "aresample=48000,pan=stereo|c0=c0|c1=c1")
    _, r_old = _channel_rms_db(old)
    assert r_old <= -119.0


def test_lufs_pre_filter_matches_upmix(tmp_path):
    """_lufs 带 _STEREO_UP 测量单声道时，须与升混后的立体声文件响度一致。

    aformat 的 mono→stereo 是等功率升混（每声道 -3.01dB），整体 LUFS 不变。
    测量口径带上同一滤镜，才能保证「测什么 == 渲染什么」，不依赖升混系数的隐含假设。
    """
    mono = _gen_vocal_like(tmp_path / "mono.wav", channels=1)
    up = _apply_chain(mono, tmp_path / "up.wav", PAN)
    lufs_mono = worker._lufs(mono)
    lufs_metered = worker._lufs(mono, worker._STEREO_UP)
    lufs_up_file = worker._lufs(up)
    assert lufs_mono is not None and lufs_metered is not None and lufs_up_file is not None
    assert abs(lufs_metered - lufs_up_file) < 0.2   # 测量口径 == 渲染口径
    assert abs(lufs_metered - lufs_mono) < 0.2      # 等功率升混不改变整体 LUFS


def test_convert_upsamples_mono_without_broken_pan():
    """源码断言：混音链用 _STEREO_UP 升混，且不再出现会把右声道变静音的旧 pan 写法。"""
    src = (Path(__file__).resolve().parent.parent / "voice-tools" / "worker.py").read_text(
        encoding="utf-8")
    assert "pan=stereo|c0=c0|c1=c1" not in src or "不能写 pan=stereo|c0=c0|c1=c1" in src
    assert '_STEREO_UP = "aformat=channel_layouts=stereo"' in src
    assert "{_STEREO_UP}" in src                 # 渲染链实际引用了升混滤镜
    assert "_lufs(converted_vocals, _STEREO_UP)" in src  # 测量口径与渲染口径一致
