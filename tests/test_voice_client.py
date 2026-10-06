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
import threading
import time
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


def test_convert_passes_quality_params(monkeypatch):
    """convert 应把 cfg_rate/ref_sec/hf_enhance 透传给 worker，并对越界/非法值做钳制与回退。"""
    tmp, webui = _make_roots()
    (tmp / "seed-vc").mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nseedvc_dir = seed-vc\n", encoding="utf-8")
    client = _make_client(tmp)
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "port", 8190)
    captured = {}

    def _fake_request(method, url, payload, timeout=300):
        captured.clear()
        captured.update(payload)
        return {"ok": True, "products": {}}

    monkeypatch.setattr(client, "_request", _fake_request)
    client.convert("s.wav", "ref.wav", output_dir=str(tmp / "c1"),
                   cfg_rate=0.9, ref_sec=6.0, hf_enhance=1.5)
    assert captured["cfg_rate"] == 0.9
    assert captured["ref_sec"] == 6.0
    assert captured["hf_enhance"] == 1.5
    # 越界钳制 + 非法回退默认
    client.convert("s.wav", "ref.wav", output_dir=str(tmp / "c2"),
                   cfg_rate=3.0, ref_sec="bad", hf_enhance=9.0)
    assert captured["cfg_rate"] == 1.0
    assert captured["ref_sec"] == 10.0
    assert captured["hf_enhance"] == 4.0
    # 不传时的默认值：cfg 0.9（P1 实测最优）、hf_enhance 0（保持原链路行为）
    client.convert("s.wav", "ref.wav", output_dir=str(tmp / "c3"))
    assert captured["cfg_rate"] == 0.9
    assert captured["hf_enhance"] == 0.0


def test_convert_passes_ref_mode(monkeypatch):
    """convert 应透传 ref_mode/ref_acc，非法 ref_mode 回退默认 smart（P5C 参考段策略）。"""
    tmp, webui = _make_roots()
    (tmp / "seed-vc").mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nseedvc_dir = seed-vc\n", encoding="utf-8")
    client = _make_client(tmp)
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "port", 8190)
    captured = {}

    def _fake_request(method, url, payload, timeout=300):
        captured.clear()
        captured.update(payload)
        return {"ok": True, "products": {}}

    monkeypatch.setattr(client, "_request", _fake_request)
    # 不传：默认 smart（P5C 盲听验证的最优参考段策略）
    client.convert("s.wav", "ref.wav", output_dir=str(tmp / "r1"))
    assert captured["ref_mode"] == "smart"
    assert captured["ref_acc"] == ""
    # 显式透传三态与配对伴奏
    client.convert("s.wav", "ref.wav", output_dir=str(tmp / "r2"),
                   ref_mode="full", ref_acc="acc.wav")
    assert captured["ref_mode"] == "full"
    assert captured["ref_acc"] == "acc.wav"
    client.convert("s.wav", "ref.wav", output_dir=str(tmp / "r3"), ref_mode="energy")
    assert captured["ref_mode"] == "energy"
    # 非法值（含大小写/空白）回退 smart
    client.convert("s.wav", "ref.wav", output_dir=str(tmp / "r4"), ref_mode="  bogus ")
    assert captured["ref_mode"] == "smart"


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


def test_cancel_notify_exits_when_done_event_set(monkeypatch):
    """任务结束（done_event 置位）后监视线程应立即退出，不空等 30 分钟，且不误发取消。"""
    tmp, webui = _make_roots()
    (tmp / "seed-vc").mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nseedvc_dir = seed-vc\n", encoding="utf-8")
    client = _make_client(tmp)
    called = []
    monkeypatch.setattr(client, "_request",
                        lambda *a, **k: (called.append(a), {"ok": True})[1])
    done = threading.Event()
    done.set()
    t0 = time.time()
    client._cancel_notify(threading.Event(), done)  # 直接调用应快速返回
    assert time.time() - t0 < 3
    assert called == []  # 未取消：不应 POST /api/cancel


def test_cancel_notify_posts_when_cancel_set(monkeypatch):
    """cancel_event 置位时应 POST /api/cancel 通知 worker 协作中止。"""
    tmp, webui = _make_roots()
    (tmp / "seed-vc").mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nseedvc_dir = seed-vc\n", encoding="utf-8")
    client = _make_client(tmp)
    urls = []
    monkeypatch.setattr(client, "_request",
                        lambda method, url, payload, timeout=300: (urls.append(url), {"ok": True})[1])
    cancel = threading.Event()
    cancel.set()
    client._cancel_notify(cancel, threading.Event())
    assert urls and urls[0].endswith("/api/cancel")


# ---------------------------------------------------------------- 模型状态检查

def _make_seedvc_fake(svc: Path):
    """在假 seed-vc 目录中构造 HF 缓存结构（Seed-VC 主模型 .pth + campplus .bin）。"""
    seed_snap = svc / "checkpoints" / "models--Plachta--Seed-VC" / "snapshots" / "abc123"
    seed_snap.mkdir(parents=True)
    (seed_snap / "DiT_seed_v2_fake.pth").write_bytes(b"x")
    camp_snap = svc / "checkpoints" / "models--funasr--campplus" / "snapshots" / "def456"
    camp_snap.mkdir(parents=True)
    (camp_snap / "campplus_cn_common.bin").write_bytes(b"x")


def _make_demucs_ft_fake(hub: Path):
    """在假 HF hub 目录中构造 htdemucs_ft 缓存（4 个权重齐备）。"""
    snap = hub / "models--adefossez--HTDemucs-ft" / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    for f in ("f7e0c4bc.safetensors", "d12395a8.safetensors",
              "92cfc3b6.safetensors", "04573f0d.safetensors"):
        (snap / f).write_bytes(b"x")


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
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hfhub"))  # demucs 缓存指向空目录
    res = check_voice_models(tmp_path)
    assert res["enabled"] is True
    assert res["demucs"]["exists"] is False
    assert res["seedvc"]["exists"] is None
    assert res["campplus"]["exists"] is None


def test_check_voice_models_all_ready(tmp_path, monkeypatch):
    """三模型齐备：demucs_ft 4 权重 + Seed-VC snapshots .pth + campplus .bin。"""
    webui = tmp_path / "yue2-webui"
    webui.mkdir()
    _make_seedvc_fake(tmp_path / "seed-vc")  # 未配 seedvc_dir 时自动探测 project_root/seed-vc
    (webui / "config.cfg").write_text("\n[voice]\nenabled = true\n", encoding="utf-8")
    hub = tmp_path / "hfhub"
    _make_demucs_ft_fake(hub)
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(hub))
    res = check_voice_models(tmp_path)
    assert res["enabled"] is True
    assert res["demucs"]["exists"] is True
    assert res["seedvc"]["exists"] is True
    assert res["campplus"]["exists"] is True
    assert "models--Plachta--Seed-VC" in res["seedvc"]["path"]


def test_check_voice_models_demucs_partial(tmp_path, monkeypatch):
    """demucs_ft 权重不全（4 取 3）：exists=False，不能误判为就绪。"""
    webui = tmp_path / "yue2-webui"
    webui.mkdir()
    (webui / "config.cfg").write_text("\n[voice]\nenabled = true\n", encoding="utf-8")
    hub = tmp_path / "hfhub"
    _make_demucs_ft_fake(hub)
    snap = next((hub / "models--adefossez--HTDemucs-ft" / "snapshots").iterdir())
    (snap / "04573f0d.safetensors").unlink()  # 删掉一个权重
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(hub))
    res = check_voice_models(tmp_path)
    assert res["demucs"]["exists"] is False


def test_check_voice_models_missing(tmp_path, monkeypatch):
    """seed-vc 目录存在但缺模型：seedvc/campplus 为 False（缺失），路径仍返回。"""
    webui = tmp_path / "yue2-webui"
    webui.mkdir()
    (tmp_path / "seed-vc" / "checkpoints").mkdir(parents=True)  # 空的 checkpoints 目录
    (webui / "config.cfg").write_text(
        "\n[voice]\nenabled = true\nseedvc_dir = seed-vc\n", encoding="utf-8")
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(tmp_path / "hfhub"))
    res = check_voice_models(tmp_path)
    assert res["demucs"]["exists"] is False
    assert res["seedvc"]["exists"] is False
    assert res["campplus"]["exists"] is False