# -*- coding: utf-8 -*-
"""翻唱音质量化脚本 tools/audio_ab_report.py 单元测试。

验证点：
1. 解析工具函数：_f 对 '-inf'/None/空串返回 None；_astats_key 从 Overall 段取末次数值
2. CSV 输出：UTF-8 BOM + CRLF 行尾，列顺序与 CSV_FIELDS 一致
3. 集成（需 ffmpeg，缺失自动跳过）：
   - 12kHz 正弦：>8k 分带占比≈0dB、>12k≈-3dB（截止点 -3dB 滚降）、谱质心≈12000Hz
   - 500Hz 正弦：>8k 分带占比显著为负
   - 白噪声的谱平坦度显著高于纯音（平坦度=噪声/金属感代理，方向不能反）
   - -6dB 与 -12dB 正弦的积分响度差 ≈ 6 LU
4. 轨级对比 compare_block：容差内标 OK、超容差标"超限"
5. 不存在的文件：measure 不抛异常，指标字段为 None
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import audio_ab_report as abr  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="环境无 ffmpeg，跳过量化集成测试")


# ---------------------------------------------------------------- 解析工具
class TestParsers:
    def test_f_handles_inf_and_empty(self):
        assert abr._f("-inf") is None
        assert abr._f("inf") is None
        assert abr._f("") is None
        assert abr._f(None) is None
        assert abr._f("abc") is None
        assert abr._f("-18.25") == -18.25

    def test_astats_key_takes_last_from_overall(self):
        # 造一段含 Overall 段的 astats 输出：应取 Overall 段内的数值
        err = (
            "[Parsed_astats_0] Channel: 1\n"
            "[Parsed_astats_0] RMS level dB: -99.0\n"
            "[Parsed_astats_0] Overall\n"
            "[Parsed_astats_0] RMS level dB: -12.297921\n"
        )
        assert abr._astats_key(err, "RMS level dB") == -12.297921

    def test_astats_key_missing_returns_none(self):
        assert abr._astats_key("[Parsed_astats_0] Overall\n", "RMS level dB") is None


# ---------------------------------------------------------------- CSV 输出
class TestCsvOutput:
    def test_bom_and_crlf(self, tmp_path):
        out = tmp_path / "r.csv"
        abr.write_csv([{"label": "L", "name": "a.flac", "lufs": -14.1}], str(out))
        raw = out.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")          # UTF-8 BOM
        assert b"\r\n" in raw                            # CRLF 行尾
        assert b"\n" not in raw.replace(b"\r\n", b"")    # 无裸 LF

    def test_header_order(self, tmp_path):
        out = tmp_path / "r.csv"
        abr.write_csv([{"label": "L", "name": "a.flac"}], str(out))
        header = out.read_text(encoding="utf-8-sig").splitlines()[0]
        assert header.split(",") == abr.CSV_FIELDS

    def test_missing_key_written_empty(self, tmp_path):
        out = tmp_path / "r.csv"
        abr.write_csv([{"name": "a.flac"}], str(out))
        lines = out.read_text(encoding="utf-8-sig").splitlines()
        # 缺字段写空串：分带列在缺测时应为空而非报错
        assert len(lines) == 2


# ---------------------------------------------------------------- 集成测量
def _gen_sine(path: str, freq: int, db: float = -6.0, seconds: float = 3.0) -> str:
    """用 ffmpeg 生成指定频率/电平的正弦波（44.1kHz 单声道）。"""
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
         "-af", f"volume={db}dB", "-ar", "44100", "-ac", "1", path],
        capture_output=True, text=True, check=True,
    )
    return path


@needs_ffmpeg
class TestMeasureIntegration:
    @pytest.fixture(scope="class")
    def sine_12k(self, tmp_path_factory):
        return _gen_sine(str(tmp_path_factory.mktemp("sines") / "s12k.wav"), 12000)

    @pytest.fixture(scope="class")
    def sine_500(self, tmp_path_factory):
        return _gen_sine(str(tmp_path_factory.mktemp("sines") / "s500.wav"), 500)

    def test_band_ratio_of_high_tone(self, sine_12k):
        r = abr.measure(sine_12k)
        # 12kHz 音全在 >8k 带内：占比应接近 0dB（略低，受高通滚降影响）
        assert r["band_gt8k_db"] == pytest.approx(0.0, abs=2.0)
        # 12kHz 恰在高通截止点：2 极点 Butterworth 约 -3dB
        assert r["band_gt12k_db"] == pytest.approx(-3.0, abs=2.0)
        assert r["centroid_hz"] == pytest.approx(12000, abs=1500)

    def test_flatness_noise_higher_than_tone(self, sine_12k, tmp_path):
        """谱平坦度必须能区分噪声与纯音（方向是"噪声感"判据的前提）。"""
        noise = str(tmp_path / "noise.wav")
        subprocess.run(
            [FFMPEG, "-y", "-f", "lavfi", "-i", "anoisesrc=color=white:duration=3",
             "-ar", "44100", "-ac", "1", noise],
            capture_output=True, text=True, check=True,
        )
        f_tone = abr.measure(sine_12k)["flatness_mean"]
        f_noise = abr.measure(noise)["flatness_mean"]
        assert f_tone is not None and f_noise is not None
        assert f_noise > f_tone * 10  # 白噪声平坦度应远高于纯音

    def test_rolloff_reported(self, sine_12k):
        # aspectralstats 的三项要一次测量全部拿到（同一次 ffmpeg 调用）
        r = abr.measure(sine_12k)
        assert r["flatness_mean"] is not None
        assert r["rolloff_hz"] is not None

    def test_band_ratio_of_low_tone(self, sine_500):
        r = abr.measure(sine_500)
        # 500Hz 音距 8k 有 4 个倍频程，2 极点滚降下应显著衰减
        assert r["band_gt8k_db"] < -30.0
        assert r["centroid_hz"] == pytest.approx(500, abs=300)

    def test_loudness_relative_difference(self, tmp_path):
        a = _gen_sine(str(tmp_path / "d6.wav"), 440, db=-6.0)
        b = _gen_sine(str(tmp_path / "d12.wav"), 440, db=-12.0)
        la, lb = abr.measure(a)["lufs"], abr.measure(b)["lufs"]
        assert la is not None and lb is not None
        # loudnorm 含响度门限，正弦绝对值意义有限，相对差应稳定 ≈ 6
        assert abs((la - lb) - 6.0) < 1.0

    def test_probe_reports_format(self, sine_12k):
        r = abr.measure(sine_12k)
        assert r["sample_rate"] == 44100
        assert r["channels"] == 1
        assert r["codec"] == "pcm_s16le"
        assert r["duration_sec"] == pytest.approx(3.0, abs=0.2)


@needs_ffmpeg
class TestMissingFile:
    def test_measure_does_not_raise(self):
        r = abr.measure("Z:/nonexistent/none.wav")
        assert r["name"] == "none.wav"
        for key in ("lufs", "true_peak_dbtp", "rms_dbfs", "centroid_hz",
                    "flatness_mean", "band_gt8k_db", "duration_sec"):
            assert r.get(key) is None


class TestCompareBlock:
    """轨级对比的判据判定（纯逻辑，不需要 ffmpeg）。"""

    def _run(self, capsys, ref_lufs, var_lufs, ref_10k=-25.0, var_10k=-25.0):
        ref = {"name": "ref.wav", "label": "R", "lufs": ref_lufs,
               "band_gt10k_db": ref_10k}
        var = {"name": "var.wav", "label": "V", "lufs": var_lufs,
               "band_gt10k_db": var_10k}
        abr.compare_block(ref, var)
        return capsys.readouterr().out

    def test_within_tolerance_marked_ok(self, capsys):
        out = self._run(capsys, -14.0, -14.5, -25.0, -25.4)
        assert out.count("OK") == 2
        assert "超限" not in out

    def test_overshoot_marked(self, capsys):
        # 响度差 4.6 LU（超 1.0）、>10k 差 2.0 dB（超 1.5）→ 两项都判超限
        out = self._run(capsys, -9.5, -14.1, -23.0, -25.0)
        assert out.count("超限") == 2

    def test_missing_metric_skipped(self, capsys):
        ref = {"name": "ref.wav", "label": "R", "lufs": None, "band_gt10k_db": None}
        var = {"name": "var.wav", "label": "V", "lufs": -14.0, "band_gt10k_db": -25.0}
        abr.compare_block(ref, var)
        assert "N/A" in capsys.readouterr().out
