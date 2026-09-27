# -*- coding: utf-8 -*-
"""worker 参考干声活跃段裁剪函数单元测试（_pick_active_ref_segment / _duration）。

验证点（ffmpeg 合成音频做断言；无 ffmpeg 环境自动跳过）：
1. 长音频取能量最高段：30 秒"前 8 秒静音 + 后 22 秒有声"应裁到有声区
   （断言裁出段整体 RMS 接近有声电平；若误取前段静音会明显偏低）
2. 裁剪产物时长 ≈ 目标时长（10 秒）
3. 音频本身不长于目标时长：直接返回原路径（不裁剪、不报错）
4. 文件不存在：返回原路径，不抛异常（不阻断翻唱流程）
5. 回归 astats 粒度换算：声头 + 静音尾必须裁在声头（旧实现会钳到静音尾）
6. 整段近乎静音：不裁剪，直接返回原参考
7. 参考段策略 mode（P5C 产线化）：
   - smart + 配对伴奏：取"人声主导度最高段"（伴奏串音最少），而非能量最高段
   - smart 无配对伴奏 / 配对伴奏时长不匹配：回退能量最高段（旧行为）
   - energy：忽略配对伴奏，固定取能量最高段
   - full：整曲不裁剪，直接返回原参考
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
pytestmark = pytest.mark.skipif(not FFMPEG, reason="环境无 ffmpeg，跳过参考裁剪测试")

SILENT_SEC = 8.0   # 前段静音时长（模拟前奏/间奏无人声）
VOICED_SEC = 22.0  # 后段有声时长
TARGET_SEC = 10.0  # 目标裁剪时长


def _cut_same_way(src: str, start: float, dur: float, dst: str) -> str:
    """测试内独立裁剪（与实现同样的 ffmpeg 参数），用于生成期望基准。"""
    subprocess.run(
        [FFMPEG, "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
         "-i", src, "-c:a", "pcm_s24le", dst],
        capture_output=True, text=True, check=True,
    )
    return dst


def _gen_silence_then_tone(path: str) -> str:
    """生成"前 8 秒静音 + 后 22 秒同幅度正弦"的 44.1kHz 单声道 wav。"""
    subprocess.run(
        [FFMPEG, "-y",
         "-f", "lavfi", "-i", f"anullsrc=d={SILENT_SEC}:r=44100",
         "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={VOICED_SEC}:r=44100",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
         "-ac", "1", path],
        capture_output=True, text=True, check=True,
    )
    return path


def test_pick_active_segment_avoids_silence(tmp_path):
    """能量最高段应落在有声区：裁出段电平应与"已知有声区"手裁基准一致。

    基准用同一 ffmpeg 流程从 SILENT_SEC 处截取同长片段（该处起至结尾全为有声），
    从而消除编码/声道转换带来的固定电平差；若函数误取开头静音段
    （8 秒静音 + 2 秒声），裁出段 RMS 会比基准低约 7dB，断言必然失败。
    """
    ref = _gen_silence_then_tone(str(tmp_path / "ref.wav"))
    dst = str(tmp_path / "seg.wav")
    out = worker._pick_active_ref_segment(ref, dst, target_sec=TARGET_SEC)

    assert out == dst, "长音频应产出裁剪文件"
    assert Path(dst).exists()
    expect = _cut_same_way(ref, SILENT_SEC, TARGET_SEC, str(tmp_path / "expect.wav"))
    diff = _rms_of(dst) - _rms_of(expect)
    assert abs(diff) < 1.5, f"裁剪段电平与已知有声区基准相差 {diff:+.1f}dB，疑似含静音"


def _rms_of(path: str) -> float:
    """测整段 RMS 并断言可测（None 直接失败，避免比较 None）。"""
    val = worker._rms_db(path)
    assert val is not None, f"RMS 测量失败: {path}"
    return val


def test_pick_active_segment_duration(tmp_path):
    """裁剪产物时长应约等于目标时长。"""
    ref = _gen_silence_then_tone(str(tmp_path / "ref2.wav"))
    dst = str(tmp_path / "seg2.wav")
    worker._pick_active_ref_segment(ref, dst, target_sec=TARGET_SEC)

    dur = worker._duration(dst)
    assert dur is not None and abs(dur - TARGET_SEC) < 0.5, f"裁剪时长异常: {dur}"


def test_pick_active_segment_custom_target_sec(tmp_path):
    """target_sec 可配置：传 6 秒应裁出约 6 秒（P1 参考长度扫描依赖该参数）。"""
    ref = _gen_silence_then_tone(str(tmp_path / "ref6.wav"))
    dst = str(tmp_path / "seg6.wav")
    worker._pick_active_ref_segment(ref, dst, target_sec=6.0)
    dur = worker._duration(dst)
    assert dur is not None and abs(dur - 6.0) < 0.5, f"裁剪时长异常: {dur}"


def test_convert_passes_cfg_rate_to_seedvc(tmp_path):
    """回归：_convert 必须把 cfg_rate 透传为 --inference-cfg-rate（P1 扫描的入口）。

    源码级断言：构建换嗓命令的代码路径在 _convert 内部，无 ffmpeg/模型环境下
    无法端到端触发，故直接校验命令片段存在，防止后续重构悄悄丢掉该参数。
    """
    import inspect
    src = inspect.getsource(worker._convert)
    assert '"--inference-cfg-rate"' in src
    assert "cfg_rate" in src


def test_short_ref_returned_as_is(tmp_path):
    """音频不长于目标时长时直接返回原路径，不做裁剪。"""
    ref = str(tmp_path / "short.wav")
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=5:r=44100",
         "-ac", "1", ref],
        capture_output=True, text=True, check=True,
    )
    out = worker._pick_active_ref_segment(ref, str(tmp_path / "never.wav"),
                                          target_sec=TARGET_SEC)
    assert out == ref, "短音频不应被裁剪"


def test_missing_file_falls_back(tmp_path):
    """文件不存在时返回原路径且不抛异常（不阻断翻唱流程）。"""
    missing = str(tmp_path / "nope.wav")
    out = worker._pick_active_ref_segment(missing, str(tmp_path / "seg3.wav"))
    assert out == missing


def _gen_quiet_loud_silence(path: str, quiet: float = 10.0, loud: float = 20.0,
                            tail: float = 40.0) -> str:
    """生成"前 10 秒弱声(-20dB) + 中 20 秒强声(-6dB) + 后 40 秒静音"的 44.1kHz 单声道 wav。

    强声段起点约 10s（对应 astats 输出索引约 107）、片尾为静音，可精准回归粒度换算
    bug：旧实现把每个输出点当"2 秒"，索引 107 被放大成 214s 越界，钳到 total-10=60s
    的静音尾 → 参考整段静音；修正后应裁在中段强声区。
    """
    subprocess.run(
        [FFMPEG, "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={quiet}:r=44100",
         "-f", "lavfi", "-i", f"sine=frequency=880:duration={loud}:r=44100",
         "-f", "lavfi", "-i", f"anullsrc=d={tail}:r=44100",
         "-filter_complex",
         "[0:a]volume=-20dB[q];[1:a]volume=-6dB[l];[q][l][2:a]concat=n=3:v=0:a=1",
         "-ac", "1", path],
        capture_output=True, text=True, check=True,
    )
    return path


def test_pick_active_segment_granularity_regression(tmp_path):
    """回归：弱声头 + 强声中段 + 静音尾，必须裁到强声中段。

    旧实现 win 按"每 2 秒 1 点"换算（实际每点约 0.093s），最佳短窗索引约 107 被
    放大 20 倍后越界，起点被钳成 total-10（静音尾）→ 参考整段静音。
    """
    ref = _gen_quiet_loud_silence(str(tmp_path / "head.wav"))
    dst = str(tmp_path / "seg_head.wav")
    out = worker._pick_active_ref_segment(ref, dst, target_sec=TARGET_SEC)

    assert out == dst, "长音频应产出裁剪文件"
    # 基准：从中段强声区起点（10s）手裁同长片段，电平应与裁出段一致
    expect = _cut_same_way(ref, 10.0, TARGET_SEC, str(tmp_path / "expect_head.wav"))
    diff = _rms_of(dst) - _rms_of(expect)
    assert abs(diff) < 1.5, f"疑似裁到静音/弱声区（与强声中段基准相差 {diff:+.1f}dB）"


def test_all_silent_ref_returned_as_is(tmp_path):
    """整段近乎静音（无人声）时不裁剪，直接用原参考，避免把静音喂给模型。"""
    ref = str(tmp_path / "silent.wav")
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=d=30:r=44100", "-ac", "1", ref],
        capture_output=True, text=True, check=True,
    )
    out = worker._pick_active_ref_segment(ref, str(tmp_path / "never2.wav"),
                                          target_sec=TARGET_SEC)
    assert out == ref


# ---------------------------------------------------------------- 参考段策略 mode

def _gen_ref_pair(tmp_path):
    """构造（人声干声, 配对伴奏）用于区分"能量最高"与"人声主导"两种挑段口径。

    前 20 秒：人声 -6dB 响、伴奏 -6dB（同响度 → 人声主导度 ≈ 0dB）
    后 20 秒：人声 -20dB 响度低 14dB、伴奏静音（人声主导度 ≈ +100dB）
    故「能量最高」选前段（响），「人声主导度最高」选后段（安静但无伴奏串音），
    两者电平相差约 14dB，必然不同，可判定 mode 是否真的生效。

    断言一律与"已知区域手工裁剪基准"比对（不做绝对电平假设）：lambda 波源的实际
    输出电平随 ffmpeg 版本而异，写死绝对阈值会随环境漂移。
    """
    voc = str(tmp_path / "pair_voc.wav")
    subprocess.run(
        [FFMPEG, "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20:r=44100",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20:r=44100",
         "-filter_complex", "[0:a]volume=-6dB[a];[1:a]volume=-20dB[b];[a][b]concat=n=2:v=0:a=1",
         "-ac", "1", voc],
        capture_output=True, text=True, check=True,
    )
    acc = str(tmp_path / "pair_acc.wav")
    subprocess.run(
        [FFMPEG, "-y",
         "-f", "lavfi", "-i", "sine=frequency=880:duration=20:r=44100",
         "-f", "lavfi", "-i", "anullsrc=d=20:r=44100",
         "-filter_complex", "[0:a]volume=-6dB[a];[a][1:a]concat=n=2:v=0:a=1",
         "-ac", "1", acc],
        capture_output=True, text=True, check=True,
    )
    return voc, acc


def _assert_level_matches(actual: str, ref: str, start: float, tmp_path,
                          label: str) -> None:
    """断言 actual 的电平与「在 ref 的 start 秒处手工裁剪同长片段」一致（±1.5dB）。

    ref 的前 20 秒与后 20 秒电平相差约 14dB，故用对应区域的手工基准即可判定
    actual 落在哪一段，且不依赖合成源的绝对电平。
    """
    expect = _cut_same_way(ref, start, TARGET_SEC, str(tmp_path / "expect_manual.wav"))
    diff = _rms_of(actual) - _rms_of(expect)
    assert abs(diff) < 1.5, f"{label} 与基准区段相差 {diff:+.1f}dB"


def test_smart_mode_prefers_vocal_dominant_segment(tmp_path):
    """smart + 配对伴奏：应选"人声主导度最高段"（安静但无伴奏串音的后 20 秒）。

    若误按能量最高挑会选到前段（比后段响约 14dB），与后段基准比对必然失败。
    """
    voc, acc = _gen_ref_pair(tmp_path)
    dst = str(tmp_path / "smart_seg.wav")
    out = worker._pick_active_ref_segment(voc, dst, target_sec=TARGET_SEC,
                                          ref_acc=acc, mode="smart")
    assert out == dst, "长音频应产出裁剪文件"
    _assert_level_matches(dst, voc, 25.0, tmp_path, "smart 未选中安静的人声主导段")


def test_smart_without_pair_acc_falls_back_to_energy(tmp_path):
    """smart 但无配对伴奏（上传干声/音色库）：回退能量最高段（旧行为）。"""
    voc, _ = _gen_ref_pair(tmp_path)
    dst = str(tmp_path / "smart_noacc.wav")
    out = worker._pick_active_ref_segment(voc, dst, target_sec=TARGET_SEC,
                                          ref_acc="", mode="smart")
    assert out == dst
    _assert_level_matches(dst, voc, 5.0, tmp_path, "无配对伴奏时应回退能量最高段")


def test_smart_with_mismatched_acc_falls_back_to_energy(tmp_path):
    """配对伴奏时长与参考明显不匹配（非同一次分离）：回退能量最高段。"""
    voc, _ = _gen_ref_pair(tmp_path)
    acc_short = str(tmp_path / "acc_short.wav")
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=5:r=44100",
         "-ac", "1", acc_short],
        capture_output=True, text=True, check=True,
    )
    dst = str(tmp_path / "smart_mismatch.wav")
    out = worker._pick_active_ref_segment(voc, dst, target_sec=TARGET_SEC,
                                          ref_acc=acc_short, mode="smart")
    assert out == dst
    _assert_level_matches(dst, voc, 5.0, tmp_path, "伴奏时长不匹配时应回退能量最高段")


def test_energy_mode_ignores_pair_acc(tmp_path):
    """energy：显式取能量最高段，即使提供了配对伴奏也不走主导度口径。"""
    voc, acc = _gen_ref_pair(tmp_path)
    dst = str(tmp_path / "energy_seg.wav")
    out = worker._pick_active_ref_segment(voc, dst, target_sec=TARGET_SEC,
                                          ref_acc=acc, mode="energy")
    assert out == dst
    _assert_level_matches(dst, voc, 5.0, tmp_path, "energy 模式应取能量最高段")


def test_full_mode_returns_ref_untouched(tmp_path):
    """full（整曲不裁剪）：长音频也直接返回原参考，不产出裁剪文件。"""
    voc, acc = _gen_ref_pair(tmp_path)
    dst = str(tmp_path / "full_seg.wav")
    out = worker._pick_active_ref_segment(voc, dst, target_sec=TARGET_SEC,
                                          ref_acc=acc, mode="full")
    assert out == voc, "full 模式不应裁剪"
    assert not Path(dst).exists(), "full 模式不应产出裁剪文件"


def test_convert_passes_ref_mode_to_picker(tmp_path):
    """源码级断言：_convert 必须把 ref_mode/ref_acc 透传给参考段挑选函数。

    裁剪发生在 _convert 内部（无模型/无 ffmpeg 环境无法端到端触发），故校验调用
    片段存在，防止后续重构悄悄丢掉该参数（与 cfg_rate 的回归断言同思路）。
    """
    import inspect
    import re
    src = inspect.getsource(worker._convert)
    assert "ref_mode" in src and "ref_acc" in src
    assert "_pick_active_ref_segment" in src
    # 挑段必须拿到入参本身（配对伴奏），而不是被内部局部变量顶掉
    assert "ref_acc=ref_acc" in src
    # 回归：_convert 内曾把"源曲伴奏"也命名成 ref_acc 并覆盖入参，导致 smart 拿到
    # 错误伴奏（源曲）而挑段失效/选错；入参名在函数体内不得被重新赋值
    # （右侧限定 ASCII 起始，避开文档串里 "ref_acc=参考干声的配对伴奏轨" 的说明行）
    assert not re.search(r"^\s*ref_acc\s*=\s*[A-Za-z0-9_\"'\[]", src, re.M), \
        "ref_acc 入参被局部变量覆盖"
    # 非法 mode 必须回退 smart（默认值），避免外部传入乱值时无策略可用
    assert '"smart", "energy", "full"' in src
