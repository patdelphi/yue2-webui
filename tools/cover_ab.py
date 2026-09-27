# -*- coding: utf-8 -*-
"""翻唱 A/B 变体驱动脚本（P1 脚手架，离线命令行，不改 app.py、不新增 UI）。

用途：绕过 UI，把单变量参数（扩散步数 / cfg / 参考长度 / 半音）直接透传给推理 worker，
每个变体产出到 outputs/ab_test/<label>/，并落盘参数 sidecar JSON，供
tools/audio_ab_report.py 做横向对比。

设计要点：
- 复用已有 worker：先探测端口上是否已有健康 worker（主 app 常驻那个），有则直接 POST，
  避免重复拉起第二个 worker 抢显存；没有才用 VoiceClient 拉起一个。
- 不调 VoiceClient._run()：它会无条件 ensure_running()，在"主 app 的 worker 已占用端口"
  时会在同端口再 spawn 一个注定启动失败的进程，属既有实现细节，脚本侧绕开更干净。
- 复用分离：--reuse-dir 指向任一次产出的目录（取其中 *_vocals.wav / *_accompaniment.wav），
  一次分离供全部变体复用，省 GPU 分钟级时间。

用法：
    python tools/cover_ab.py --label baseline_var1 --source "outputs/song_x/var1.wav" \
        --ref "uploads/xxx_vocals.wav"
    python tools/cover_ab.py --label S1_steps100 --source "outputs/song_x/var1.wav" \
        --ref "uploads/xxx_vocals.wav" --reuse-dir "outputs/ab_test/baseline_var1" --steps 100
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WEBUI_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = WEBUI_ROOT.parent
sys.path.insert(0, str(WEBUI_ROOT / "src"))

from voice_client import VoiceClient, VoiceError  # noqa: E402


def _health(port: int) -> bool:
    """探测指定端口是否已有存活的 worker（短超时，避免卡住脚本）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def _post_convert(port: int, payload: dict, timeout: float) -> dict:
    """向 worker 发一次 /api/convert，返回解析后的 JSON（业务失败也返回 body）。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/convert", data=data,
                                method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 worker（{getattr(e, 'reason', e)}）")


def _find_reuse(reuse_dir: str):
    """在复用目录里定位人声/伴奏轨（按文件名后缀匹配），两者齐备才返回。

    必须排除 *_converted_vocals*.wav：该名同样以 "_vocals.wav" 结尾，且字母序
    （c < v）排在真实源人声轨之前，若不排除会把上一次的换嗓产物误当源人声复用，
    导致扫描结果全部错误。
    """
    d = Path(reuse_dir)

    def _pick(pattern: str):
        return [p for p in sorted(d.glob(pattern)) if "converted" not in p.name]

    voc = _pick("*_vocals.wav") or _pick("vocals.wav")
    acc = _pick("*_accompaniment.wav") or _pick("accompaniment.wav")
    if not voc or not acc:
        raise RuntimeError(f"复用目录缺少 *_vocals.wav / *_accompaniment.wav: {d}")
    # 必须返回绝对路径：worker 会把它原样交给 Seed-VC 子进程，而该子进程 cwd=seed-vc/，
    # 相对路径会解析到 seed-vc/ 下从而报 FileNotFoundError
    return str(voc[0].resolve()), str(acc[0].resolve())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="翻唱 A/B 变体驱动（P1 脚手架）")
    ap.add_argument("--label", required=True, help="变体名，产物落 outputs/ab_test/<label>/")
    ap.add_argument("--source", required=True, help="被翻唱整曲")
    ap.add_argument("--ref", required=True, help="参考干声")
    ap.add_argument("--reuse-dir", default="", help="复用该目录下已分离的人声/伴奏（跳过 Demucs）")
    ap.add_argument("--steps", type=int, default=40, help="扩散步数（默认 40）")
    ap.add_argument("--cfg", type=float, default=0.9, help="inference-cfg-rate（默认 0.9，P1 实测最优）")
    ap.add_argument("--ref-sec", type=float, default=10.0, help="参考干声裁剪长度秒（默认 10）")
    ap.add_argument("--hf-enhance", type=float, default=0.0,
                    help="换嗓人声高频补偿强度（P2，0=关；aexciter amount，建议扫 0.5/1/2）")
    ap.add_argument("--semi-tone", type=int, default=0, help="半音移调（默认 0）")
    ap.add_argument("--gain-db", type=float, default=0.0, help="人声增益 dB（默认 0）")
    ap.add_argument("--port", type=int, default=8190, help="worker 端口（默认 8190）")
    args = ap.parse_args(argv)

    # 统一绝对化：worker 内部会以 seed-vc/ 为 cwd 调子进程，相对路径会解析错位
    source = Path(args.source).resolve()
    ref = Path(args.ref).resolve()
    for p, name in ((source, "source"), (ref, "ref")):
        if not p.is_file():
            print(f"错误：{name} 文件不存在: {p}")
            return 2

    out_dir = WEBUI_ROOT / "outputs" / "ab_test" / args.label
    out_dir.mkdir(parents=True, exist_ok=True)

    src_vocals = src_acc = ""
    if args.reuse_dir:
        src_vocals, src_acc = _find_reuse(args.reuse_dir)
        print(f"复用分离: {Path(src_vocals).name} / {Path(src_acc).name}")

    # 端口上已有健康 worker（主 app 常驻）则直接复用；否则拉起一个
    port = args.port
    if _health(port):
        print(f"复用已运行 worker: 端口 {port}")
    else:
        print(f"端口 {port} 无 worker，拉起新 worker ...")
        client = VoiceClient(PROJECT_ROOT)
        try:
            client.ensure_running()
        except VoiceError as e:
            print(f"启动 worker 失败: {e}")
            return 2
        port = client.port
        print(f"worker 已就绪: 端口 {port}")

    payload = {
        "source": str(source), "ref": str(ref),
        "semi_tone": args.semi_tone, "diffusion_steps": args.steps,
        "accompaniment": "", "gain_db": args.gain_db,
        "output_dir": str(out_dir), "denoise": False,
        "prefix": args.label,
        "source_vocals": src_vocals, "source_acc": src_acc,
        # 以下两项需 worker.py 已支持；旧 worker 会静默忽略（参数仍在 sidecar 中留档）
        "cfg_rate": args.cfg, "ref_sec": args.ref_sec,
        "hf_enhance": args.hf_enhance,
    }
    # 参数 sidecar 落盘：每个变体的完整参数可追溯（与产物同目录）
    sidecar = out_dir / f"{args.label}_params.json"
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"参数: steps={args.steps} cfg={args.cfg} ref_sec={args.ref_sec} "
          f"hf_enhance={args.hf_enhance} semi_tone={args.semi_tone} gain_db={args.gain_db} "
          f"reuse={bool(args.reuse_dir)}")

    # 超时按源时长估算（与主 app 同口径：每秒 20s 预算、下限 900s），换嗓耗时随步数线性上升
    dur = 0.0
    try:
        from voice_client import VoiceClient as _VC
        dur = _VC._probe_duration(str(source))
    except Exception:
        pass
    timeout = max(900.0, dur * 20.0)
    t0 = time.time()
    print(f"开始翻唱（预计受扩散步数影响，超时上限 {timeout:.0f}s）...")
    try:
        res = _post_convert(port, payload, timeout)
    except Exception as e:
        print(f"请求失败: {e}")
        return 1
    elapsed = time.time() - t0

    if not res.get("ok"):
        print(f"翻唱失败（耗时 {elapsed:.0f}s）: {res.get('error')}")
        return 1
    print(f"完成（耗时 {elapsed:.0f}s）:")
    for k, v in (res.get("products") or {}).items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
