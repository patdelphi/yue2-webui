# -*- coding: utf-8 -*-
"""audio_probe.probe_duration 单元测试（B1：三处重复时长探测收敛为单一实现）。

覆盖：
1. 正常解析 ffprobe 输出 → float 秒
2. 空输出 / 非数字输出 → 0.0（不抛异常）
3. 路径为空 / 文件不存在 → 0.0，且不调用 ffprobe
4. ffprobe 不可用（shutil.which 返回 None）→ 0.0
5. ffprobe 执行异常 → 0.0；传入 logger 时记录日志
6. 三处调用点确实复用统一实现（源码断言，防回退）
"""

import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import audio_probe  # noqa: E402
from audio_probe import probe_duration  # noqa: E402


def _stub_ffprobe(monkeypatch, stdout="", exc=None):
    """把 ffprobe 定位与执行替换为桩：which 返回固定路径，run 返回/抛出给定结果。"""
    monkeypatch.setattr(audio_probe.shutil, "which", lambda name: "/fake/ffprobe")

    class _P:
        def __init__(self, out):
            self.stdout = out

    def _run(*a, **k):
        if exc is not None:
            raise exc
        return _P(stdout)

    monkeypatch.setattr(audio_probe.subprocess, "run", _run)


def _real_file():
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    f.write(b"\x00")
    f.close()
    return f.name


def test_parses_duration(monkeypatch):
    _stub_ffprobe(monkeypatch, stdout="12.5\n")
    assert probe_duration(_real_file()) == 12.5


def test_empty_output_returns_zero(monkeypatch):
    _stub_ffprobe(monkeypatch, stdout="   \n")
    assert probe_duration(_real_file()) == 0.0


def test_non_numeric_output_returns_zero(monkeypatch):
    _stub_ffprobe(monkeypatch, stdout="N/A")
    assert probe_duration(_real_file()) == 0.0


def test_empty_path_returns_zero_without_probe(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("空路径不应调用 ffprobe")

    monkeypatch.setattr(audio_probe.subprocess, "run", _boom)
    assert probe_duration("") == 0.0
    assert probe_duration(None) == 0.0


def test_missing_file_returns_zero_without_probe(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("文件不存在不应调用 ffprobe")

    monkeypatch.setattr(audio_probe.subprocess, "run", _boom)
    assert probe_duration("no/such/file.wav") == 0.0


def test_ffprobe_unavailable_returns_zero(monkeypatch):
    monkeypatch.setattr(audio_probe.shutil, "which", lambda name: None)
    assert probe_duration(_real_file()) == 0.0


def test_exec_error_returns_zero_and_logs(monkeypatch, caplog):
    _stub_ffprobe(monkeypatch, exc=RuntimeError("boom"))
    with caplog.at_level(logging.ERROR):
        assert probe_duration(_real_file(), logger=logging.getLogger("t")) == 0.0
    assert "时长解析失败" in caplog.text


def test_exec_error_silent_without_logger(monkeypatch):
    """未传 logger 时失败静默返回 0.0（与 voice_client/voice_ui_handlers 原语义一致）。"""
    _stub_ffprobe(monkeypatch, exc=RuntimeError("boom"))
    assert probe_duration(_real_file()) == 0.0


def test_three_call_sites_reuse_shared_impl():
    """源码断言：三处调用点均已指向 audio_probe.probe_duration，不再各自实现 ffprobe。"""
    root = Path(__file__).resolve().parent.parent
    for rel in ("src/voice_client.py", "src/voice_ui_handlers.py", "src/mix_render.py"):
        src = (root / rel).read_text(encoding="utf-8-sig")
        assert "from audio_probe import probe_duration" in src, rel
        assert "return probe_duration(" in src, rel
        # 原实现特征：局部直接调用 ffprobe（"ffprobe" 字面量）应已移除
        assert '["ffprobe"' not in src, rel


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
    print(f"\n结果: {passed} 通过, {len(tests) - passed} 失败")
    sys.exit(0 if passed == len(tests) else 1)
