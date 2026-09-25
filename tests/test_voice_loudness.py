# -*- coding: utf-8 -*-
"""worker 音频电平测量函数单元测试（_rms_db/_peak_db/_lufs）。

验证点（ffmpeg 生成已知电平音频做相对差断言；无 ffmpeg 环境自动跳过）：
1. _rms_db：-6dB 与 -12dB 正弦波的 RMS 差 ≈ 6dB
2. _peak_db：-6dB 正弦波峰值 ≈ -6dBFS；与 -12dB 的差 ≈ 6dB
3. _lufs：-6dB 与 -12dB 音频的积分响度差 ≈ 6 LUFS（BS.1770 口径，容差放宽）
4. 静音输入：三者均返回 None（-inf 不被正则匹配）
5. 文件不存在：三者均返回 None，不抛异常
"""

import shutil
import subprocess
import sys
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
