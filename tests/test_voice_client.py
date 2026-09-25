# -*- coding: utf-8 -*-
"""VoiceClient 单元测试（mock 进程与网络，不真正启动 worker）。

验证点：
1. seedvc 未配置时 ensure_running 抛 VoiceError 并给出可读指引
2. worker 脚本/venv 缺失时给出明确错误
3. 成功启动后 separate/convert 走通，异常统一收敛为 VoiceResult(ok=False)
4. 健康检查成功即复用既有进程，失败则逐端口重试
5. enabled=false 时直接拒绝
6. check_voice_models：音色工坊三模型（Demucs/Seed-VC/campplus）状态检查
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from voice_client import VoiceClient, VoiceError, check_voice_models  # noqa: E402


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


def test_cancelled_flag_parsed(monkeypatch):
    """worker 响应含 cancelled=True（协作取消）时，VoiceResult 应携带该标志，
    供上层把这类失败映射为"已取消"而非"任务失败"。"""
    tmp, webui = _make_roots()
    svc = tmp / "seed-vc"
    svc.mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nseedvc_dir = seed-vc\n", encoding="utf-8")
    client = _make_client(tmp)

    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "port", 8190)

    # worker 取消响应：ok=false + cancelled=true
    monkeypatch.setattr(client, "_request",
                        lambda method, url, payload, timeout=300: {
                            "ok": False, "error": "任务已取消", "cancelled": True})
    res = client.separate("in.wav", mode="2", output_dir=str(tmp / "out"))
    assert res.ok is False
    assert res.cancelled is True

    # 普通业务失败响应：不带 cancelled 字段 → cancelled 保持 False
    monkeypatch.setattr(client, "_request",
                        lambda method, url, payload, timeout=300: {
                            "ok": False, "error": "boom"})
    res2 = client.convert("s.wav", "ref.wav", output_dir=str(tmp / "cov"))
    assert res2.ok is False
    assert res2.cancelled is False


# ---------------------------------------------------------------- 模型状态检查

def _make_seedvc_fake(svc: Path):
    """在假 seed-vc 目录中构造 HF 缓存结构（Seed-VC 主模型 .pth + campplus .bin）。"""
    seed_snap = svc / "checkpoints" / "models--Plachta--Seed-VC" / "snapshots" / "abc123"
    seed_snap.mkdir(parents=True)
    (seed_snap / "DiT_seed_v2_fake.pth").write_bytes(b"x")
    camp_snap = svc / "checkpoints" / "models--funasr--campplus" / "snapshots" / "def456"
    camp_snap.mkdir(parents=True)
    (camp_snap / "campplus_cn_common.bin").write_bytes(b"x")


def test_check_voice_models_disabled(tmp_path):
    """enabled=false 时三项 exists 均为 None（不检查），UI 显示「未启用」。"""
    webui = tmp_path / "yue2-webui"
    webui.mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nenabled = false\n", encoding="utf-8")
    res = check_voice_models(tmp_path)
    assert res["enabled"] is False
    assert all(res[k]["exists"] is None for k in ("demucs", "seedvc", "campplus"))


def test_check_voice_models_no_seedvc_dir(tmp_path, monkeypatch):
    """enabled=true 但 seedvc_dir 未配置且探测不到：demucs 正常检查，
    seedvc/campplus exists=None（UI 显示「未配置」）。"""
    webui = tmp_path / "yue2-webui"
    webui.mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torchhome"))  # demucs 缓存指向空目录
    res = check_voice_models(tmp_path)
    assert res["enabled"] is True
    assert res["demucs"]["exists"] is False
    assert res["seedvc"]["exists"] is None
    assert res["campplus"]["exists"] is None


def test_check_voice_models_all_ready(tmp_path, monkeypatch):
    """三模型齐备：demucs 缓存文件 + Seed-VC snapshots .pth + campplus .bin。"""
    webui = tmp_path / "yue2-webui"
    webui.mkdir()
    _make_seedvc_fake(tmp_path / "seed-vc")  # 未配 seedvc_dir 时自动探测 project_root/seed-vc
    (webui / "config.cfg").write_text("\n[voice]\nenabled = true\n", encoding="utf-8")
    th = tmp_path / "torchhome"
    (th / "hub" / "checkpoints").mkdir(parents=True)
    (th / "hub" / "checkpoints" / "955717e8-8726e21a.th").write_bytes(b"x")
    monkeypatch.setenv("TORCH_HOME", str(th))
    res = check_voice_models(tmp_path)
    assert res["enabled"] is True
    assert res["demucs"]["exists"] is True
    assert res["seedvc"]["exists"] is True
    assert res["campplus"]["exists"] is True
    assert "models--Plachta--Seed-VC" in res["seedvc"]["path"]


def test_check_voice_models_missing(tmp_path, monkeypatch):
    """seed-vc 目录存在但缺模型：seedvc/campplus 为 False（缺失），路径仍返回。"""
    webui = tmp_path / "yue2-webui"
    webui.mkdir()
    (tmp_path / "seed-vc" / "checkpoints").mkdir(parents=True)  # 空的 checkpoints 目录
    (webui / "config.cfg").write_text(
        "\n[voice]\nenabled = true\nseedvc_dir = seed-vc\n", encoding="utf-8")
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torchhome"))
    res = check_voice_models(tmp_path)
    assert res["demucs"]["exists"] is False
    assert res["seedvc"]["exists"] is False
    assert res["campplus"]["exists"] is False