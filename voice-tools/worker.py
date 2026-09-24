"""音色工坊推理 worker（独立 venv 运行）。

提供三个只做模型推理/音频处理、不含业务逻辑的 HTTP 接口：
- GET  /api/health            模型就绪状态 + 设备 + 显存
- POST /api/separate          音轨分离（Demucs）→ 哭声/伴奏（或4轨）
- POST /api/convert           参考音色翻唱（分离 + Seed-VC 换嗓 + ffmpeg 混音）

设计要点：
- 仅依赖 Python stdlib（http.server）+ demucs + 系统 ffmpeg，零额外框架
- 模型懒加载：首次请求时才加载到 CUDA；空闲 5 分钟自动卸载，释放显存
- 单线程串行处理（同主 app queue_manager 串行的显存安全要求）
- 换嗓通过 subprocess 调用 Seed-VC 的 inference.py（已验证的命令行链路），
  避免与主 venv 的 torch 版本冲突，也符合"子进程边界调用、不 vendor 源码"的许可策略
"""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import torch  # noqa: F401  模块级导入，供各函数复用（懒加载 torchaudio 等仍函数内引入）
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# 前置：把 Seed-VC 源码根加入 sys.path（如可用），供模块级 import 使用
SEEDVC_DIR = os.environ.get("SEEDVC_DIR", "").strip()
if SEEDVC_DIR:
    sys.path.insert(0, SEEDVC_DIR)

FALLBACK_MODELS = {
    "2": "htdemucs",
    "4": "htdemucs",
}
# 分离输出子目录（由命令决定，此处统一扫描名）
SEP_STEM_2 = ("vocals", "no_vocals")
SEP_STEM_4 = ("vocals", "drums", "bass", "other")

# 空闲自动卸载阈值（秒）
IDLE_UNLOAD_SECONDS = 300

# 全局状态
_lock = threading.Lock()
_demucs_model = None
_seedvc_module = None
_seedvc_loaded = False
_last_activity = time.time()


def _log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


# ---------------------------------------------------------------- 模型加载
def _load_demucs(mode: str):
    """懒加载 Demucs 分离模型。mode: 2=双轨(哭声/伴奏) 4=四轨。"""
    global _demucs_model
    if _demucs_model is not None:
        return
    import demucs
    from demucs.pretrained import get_model
    name = FALLBACK_MODELS[mode]
    _log(f"加载 Demucs 模型 {name} ...")
    _demucs_model = get_model(name)
    _demucs_model.cuda()
    _demucs_model.eval()
    _log("Demucs 模型加载完成")


def _load_seedvc(seed_dir: str):
    """懒加载 Seed-VC 换嗓链路。seed_dir 为 Seed-VC 源码根。"""
    global _seedvc_module, _seedvc_loaded
    if _seedvc_loaded:
        return
    if not seed_dir or not Path(seed_dir).is_dir():
        raise RuntimeError("seedvc_dir 未配置或无效")
    # 确保 Seed-VC 源码与 weights 可被 import
    if seed_dir not in sys.path:
        sys.path.insert(0, seed_dir)
    os.chdir(seed_dir)  # inference.py 依赖相对 cwd 定位检查点目录
    _log("加载 Seed-VC 换嗓链路（模型在首次调用 inference 时下载/加载）")
    _seedvc_loaded = True


# ---------------------------------------------------------------- 分离
def _separate(input_path: str, mode: str, output_dir: str) -> dict:
    """执行 Demucs 分离，返回产物路径字典。"""
    global _demucs_model
    mode = "4" if mode == "4" else "2"
    _load_demucs(mode)

    src = Path(input_path)
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    import torchaudio
    from demucs.apply import apply_model
    audio, sr = torchaudio.load(str(src))
    # 转成模型需要的形状：(channels, samples)
    if audio.shape[0] == 1:
        audio = audio.repeat(2, 1)
    audio = audio.unsqueeze(0)  # (1, ch, T)
    audio = audio.to("cuda")

    with torch.no_grad():
        sources = apply_model(_demucs_model, audio, device="cuda")

    # sources: (batch, stems, ch, T)，取 batch0 并按 <stem_name>.wav 写出
    stems = _demucs_model.sources  # ['vocals','drums','bass','other']
    sources = sources.squeeze(0).cpu()   # (stems, ch, T) 移入 CPU 供 torchaudio 保存

    result = {}
    if mode == "2":
        # 双轨：人声 + 伴奏（其余轨求和叠加）
        vocals_idx = stems.index("vocals")
        vocals = sources[vocals_idx]
        accompaniment = sum(s for i, s in enumerate(sources) if i != vocals_idx)
        _save_track(out_root / "vocals.wav", vocals, sr, result, "vocals")
        _save_track(out_root / "no_vocals.wav", accompaniment, sr, result, "accompaniment")
    else:
        # 四轨：独立保存每轨
        for i, name in enumerate(stems):
            _save_track(out_root / f"{name}.wav", sources[i], sr, result, name)
    return result


def _save_track(path, track, sample_rate, result_into: dict, key: str):
    """将单个分离轨写入 wav 并登记到产物字典。track 为 (ch, T) 的 CPU 张量。"""
    import torchaudio
    out_path = Path(path)
    torchaudio.save(str(out_path), track, sample_rate)
    result_into[key] = str(out_path)


# ---------------------------------------------------------------- 换嗓
def _convert(seed_dir: str, source: str, ref: str, semi_tone: int,
             diffusion_steps: int, accompaniment: str, output_dir: str,
             gain_db: float = 0.0) -> dict:
    """执行翻唱：分离 + Seed-VC 换嗓 + ffmpeg 混音。

    source=被翻唱整曲；ref=参考干声；accompaniment=自定义伴奏（空则用分离出的伴奏）。
    """
    _load_seedvc(seed_dir)

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    # 0) 分离源整曲：人声轨供换嗓、伴奏轨供混音（Seed-VC 需要干净人声，不能用整曲）
    sep_products = _separate(source, "2", str(out_root / "sep"))
    source_vocals = sep_products["vocals"]
    if not accompaniment:
        # 未提供自定义伴奏：混音用分离出的原曲伴奏
        accompaniment = sep_products["accompaniment"]

    # 1) 换嗓（subprocess 调 inference.py）
    venv_python = sys.executable
    inference_py = Path(seed_dir) / "inference.py"
    conv_out = out_root / "converted"
    conv_out.mkdir(parents=True, exist_ok=True)
    cmd = [
        venv_python, str(inference_py),
        "--source", source_vocals,
        "--target", ref,
        "--output", str(conv_out),
        "--diffusion-steps", str(diffusion_steps),
        "--f0-condition", "True",
        "--semi-tone-shift", str(semi_tone),
        "--fp16", "True",
    ]
    _log("运行 Seed-VC 换嗓: " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Seed-VC 换嗓失败: {proc.stderr[-2000:]}")

    # 定位换嗓输出（文件名带参数前缀）
    conv_files = sorted(conv_out.glob("*.wav"))
    if not conv_files:
        raise RuntimeError("Seed-VC 未产出换嗓音频")
    converted_vocals = str(conv_files[0])

    # 2) 用伴奏混音 -> 48kHz 立体声 FLAC（+ 可选伴奏增益）
    mix_out = out_root / "cover"
    mix_out.mkdir(parents=True, exist_ok=True)
    cover_flac = mix_out / "cover.flac"
    amix_filter = (
        f"[1:a]aresample=48000,pan=stereo|c0=c0|c1=c1,volume={gain_db:.1f}dB[a1];"
        "[0:a]aresample=48000,pan=stereo|c0=c0|c1=c1[a0];"
        "[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0.05"
    )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请将其加入系统 PATH")
    proc2 = subprocess.run(
        [ffmpeg, "-y", "-i", converted_vocals, "-i", accompaniment,
         "-filter_complex", amix_filter, "-c:a", "flac", "-sample_fmt", "s16",
         str(cover_flac)],
        capture_output=True, text=True,
    )
    if proc2.returncode != 0:
        raise RuntimeError(f"ffmpeg 混音失败: {proc2.stderr[-2000:]}")

    return {
        "cover": str(cover_flac),
        "converted_vocals": converted_vocals,
        "accompaniment": accompaniment,
        "separated_vocals": source_vocals,
    }


# ---------------------------------------------------------------- HTTP 层
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 关闭默认访问日志

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global _last_activity
        _last_activity = time.time()
        url = urlparse(self.path)
        if url.path == "/api/health":
            self._send(200, _health())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        global _last_activity
        _last_activity = time.time()
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        try:
            # 串行处理：模型推理只能单线程（显存安全）
            with _lock:
                if url.path == "/api/separate":
                    result = _separate(body["input"], body.get("mode", "2"), body["output_dir"])
                    self._send(200, {"ok": True, "products": result})
                elif url.path == "/api/convert":
                    result = _convert(
                        os.environ.get("SEEDVC_DIR", ""),
                        body["source"], body["ref"],
                        int(body.get("semi_tone", 0)),
                        int(body.get("diffusion_steps", 30)),
                        body["accompaniment"], body["output_dir"],
                        float(body.get("gain_db", 0.0)),
                    )
                    self._send(200, {"ok": True, "products": result})
                else:
                    self._send(404, {"error": "not found"})
        except Exception as e:
            _log(f"处理失败: {e}")
            self._send(500, {"error": str(e)})


def _health() -> dict:
    import torch
    # 模型懒加载：服务启动时可能一个模型都没加载。ok 表示"服务存活"（恒 True），
    # 具体模型状态由 models_loaded 表达，供 UI 展示，不应被客户端当作存活判据
    enabled_models = {
        "demucs": _demucs_model is not None,
        "seedvc": _seedvc_loaded,
    }
    vram_used = None
    if torch.cuda.is_available():
        vram_used = torch.cuda.memory_allocated(0) // (1024 * 1024)
    return {
        "ok": True,
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "vram_used_mib": vram_used,
        "models_loaded": enabled_models,
        "seedvc_dir_ok": bool(SEEDVC_DIR) and Path(SEEDVC_DIR).is_dir(),
    }


def _idle_unload_loop(port: int):
    """空闲超时后台线程：卸载已加载模型，释放显存。"""
    global _demucs_model, _seedvc_loaded
    while True:
        time.sleep(30)
        if time.time() - _last_activity > IDLE_UNLOAD_SECONDS:
            if _demucs_model is not None or _seedvc_loaded:
                _log("空闲超时，卸载模型并释放显存")
                with _lock:
                    _demucs_model = None
                    _seedvc_loaded = False
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass


if __name__ == "__main__":
    port = int(os.environ.get("WORKER_PORT", "8190"))
    _log(f"启动 worker, 端口 {port}, seedvc_dir={SEEDVC_DIR or '(空)'}")
    threading.Thread(target=_idle_unload_loop, args=(port,), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.serve_forever()