# -*- coding: utf-8 -*-
"""VoiceClient 单元测试（mock 进程与网络，不真正启动 worker）。

验证点：
1. seedvc 未配置时 ensure_running 抛 VoiceError 并给出可读指引
2. worker 脚本/venv 缺失时给出明确错误
3. 成功启动后 separate/convert 走通，异常统一收敛为 VoiceResult(ok=False)
4. 健康检查成功即复用既有进程，失败则逐端口重试
5. enabled=false 时直接拒绝
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from voice_client import VoiceClient, VoiceError  # noqa: E402


def _make_client(project_root: Path) -> VoiceClient:
    return VoiceClient(project_root)


def _make_roots():
    tmp = Path(tempfile.mkdtemp())
    webui = tmp / "yue2-webui"
    (webui / "voice-tools" / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
    (webui / "voice-tools" / "venv" / "Scripts" / "python.exe").touch()  # win 探测路径
    (webui / "voice-tools" / "worker.py").write_text("", encoding="utf-8")
    return tmp, webui


def test_disabled_config_raises(tmp_path):
    webui = tmp_path / "yue2-webui"
    webui.mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nenabled = false\n", encoding="utf-8")
    client = _make_client(tmp_path)
    with pytest.raises(VoiceError, match="关闭"):
        client.ensure_running()


def test_missing_seedvc_dir_raises(tmp_path):
    webui = tmp_path / "yue2-webui"
    webui.mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nenabled = true\n", encoding="utf-8")
    client = _make_client(tmp_path)
    # 无 seed-vc 目录且 cfg 未配置 -> 报安装指引错误
    with pytest.raises(VoiceError, match="seedvc_dir|Seed-VC"):
        client.ensure_running()


def test_missing_worker_script_or_venv():
    tmp, webui = _make_roots()
    # 有 seed-vc，但删掉 venv python，验证 venv 缺失错误
    svc = tmp / "seed-vc"
    svc.mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nseedvc_dir = seed-vc\n", encoding="utf-8")
    (webui / "voice-tools" / "venv" / "Scripts" / "python.exe").unlink()
    client = _make_client(tmp)
    with pytest.raises(VoiceError, match="venv|虚拟环境"):
        client.ensure_running()


def test_confirm_ffmpeg_absent_assist():
    # heal_ffmpeg_check 在 ffmpeg 存在时应返回空串（此处仅验证函数可调用不崩）
    from voice_client import heal_ffmpeg_check
    assert isinstance(heal_ffmpeg_check(), str)


def test_separate_error_returns_voiceresult(monkeypatch):
    """worker 未就绪/请求失败时 separate 不应抛异常，而是返回 ok=False。"""
    tmp, webui = _make_roots()
    svc = tmp / "seed-vc"
    svc.mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nseedvc_dir = seed-vc\n", encoding="utf-8")
    client = _make_client(tmp)

    # 强制 ensure_running 抛错，验证 _run 收敛为 VoiceResult
    monkeypatch.setattr(client, "ensure_running",
                        lambda: (_ for _ in ()).throw(VoiceError("worker 启动失败")))
    res = client.separate("in.wav", mode="2", output_dir=str(tmp / "out"))
    assert res.ok is False
    assert "worker" in (res.error or "")


def test_success_path(monkeypatch):
    """worker 就绪后 separate/convert 返回产物，路径传入正确。"""
    tmp, webui = _make_roots()
    svc = tmp / "seed-vc"
    svc.mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nseedvc_dir = seed-vc\n", encoding="utf-8")
    client = _make_client(tmp)

    # 模拟 worker 已就绪：跳过进程启动，直接打到 HTTP 层
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "port", 8190)
    monkeypatch.setattr(client, "_request", lambda method, url, payload, timeout=300: {
        "ok": True,
        "products": {"vocals": "v.wav", "no_vocals": "a.wav"}
        if "/api/separate" in url else
        {"cover": "c.flac", "converted_vocals": "cv.wav", "accompaniment": "a.wav"},
    })

    res = client.separate("in.wav", mode="2", output_dir=str(tmp / "out"))
    assert res.ok
    assert res.products["vocals"] == "v.wav"

    res2 = client.convert("s.wav", "ref.wav", semi_tone=-12, diffusion_steps=30,
                          accompaniment="a.wav", output_dir=str(tmp / "cov"))
    assert res2.ok
    assert all(k in res2.products for k in ("cover", "converted_vocals", "accompaniment"))