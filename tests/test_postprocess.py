"""后处理容错测试（A2）。

1. _embed_metadata 写标签失败时必须记 warning（不再静默 pass）；
2. app.py 调用 postprocess_audio 处必须包 try/except，失败不阻断历史写入。
"""
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import postprocess as pp  # noqa: E402


def test_embed_metadata_logs_warning_on_failure(tmp_path, caplog, monkeypatch):
    """mutagen 写标签抛异常时应记 warning（sidecar JSON 仍落盘）。"""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")  # _embed_metadata 不读音频内容，仅写 sidecar JSON

    fake_pkg = types.ModuleType("mutagen")

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("boom")

    fake_id3 = types.ModuleType("mutagen.id3")
    fake_id3.ID3 = _Boom
    fake_id3.TIT2 = fake_id3.TPE1 = fake_id3.COMM = lambda *a, **k: None
    fake_id3.ID3NoHeaderError = Exception
    fake_pkg.id3 = fake_id3
    monkeypatch.setitem(sys.modules, "mutagen", fake_pkg)
    monkeypatch.setitem(sys.modules, "mutagen.id3", fake_id3)

    with caplog.at_level(logging.WARNING, logger="postprocess"):
        pp._embed_metadata(wav, title="t")

    assert wav.with_suffix(".json").exists()  # sidecar 仍写成功
    assert any("元数据" in r.getMessage() for r in caplog.records)


def test_postprocess_has_no_silent_except():
    """postprocess.py 末尾不得再有裸 `except Exception: pass`。"""
    src = (Path(__file__).parent.parent / "src" / "postprocess.py").read_text(
        encoding="utf-8")
    assert "logger.warning(" in src
    assert "except Exception:\n        pass" not in src


def test_app_wraps_postprocess_call_in_try():
    """app.py 的 postprocess_audio 调用必须被 try/except 包裹，失败仅记日志后继续。"""
    from _app_bundle import app_bundle  # C1 拆分后源码级断言读 app bundle
    src = app_bundle()
    idx = src.index("postprocess_audio(")
    window = src[idx - 200:idx]
    assert "try:" in window
    assert "后处理失败(已忽略，音频已落盘)" in src
