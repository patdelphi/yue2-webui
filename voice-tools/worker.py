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
import re
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
# 协作式取消标志：/api/cancel 置位，任务在阶段边界检查并中止
# （注意：必须在串行锁 _lock 之外处理 cancel 请求，否则会被正在执行的任务阻塞）
_cancel_flag = threading.Event()


def _log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def _write_progress(progress_file: str, stage: str) -> None:
    """把当前阶段写入进度文件（JSON），供主 app 轮询展示阶段进度。

    写失败仅记日志——进度展示是尽力而为，不能影响任务本体。
    """
    if not progress_file:
        return
    try:
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump({"stage": stage, "ts": time.time()}, f)
    except Exception as e:
        _log(f"进度写入失败(忽略): {e}")


class _TaskCancelled(Exception):
    """worker 侧协作取消异常：/api/cancel 置位后由 _check_cancelled 在阶段边界抛出。

    独立异常类型供 do_POST 异常路径识别（isinstance），在响应中标记 cancelled=True，
    让主 app 把这类失败映射为"已取消"而非"任务失败"（文案与状态均正确）。
    """


def _check_cancelled() -> None:
    """协作式取消检查：已置取消标志则抛异常中止当前任务（在阶段边界调用）。"""
    if _cancel_flag.is_set():
        raise _TaskCancelled("任务已取消")


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
    # 注意：inference.py 依赖相对 cwd 定位检查点目录，必须保证调用时进程 cwd 指向 seed_dir。
    # 我们不在进程级 os.chdir（避免全局副作用），改为在 _convert 以 subprocess cwd= 传入。
    _log("加载 Seed-VC 换嗓链路（模型在首次调用 inference 时下载/加载）")
    _seedvc_loaded = True


# ---------------------------------------------------------------- 分离
def _separate(input_path: str, mode: str, output_dir: str, denoise: bool = False,
              denoise_strength=None, prefix: str = "",
              progress_file: str = "") -> dict:
    """执行 Demucs 分离，返回产物路径字典。denoise=True 时对人声轨降噪输出。

    prefix（时间戳前缀）非空时产物按 <prefix>_<类别>.wav 命名（如 20260924_201805_vocals.wav），
    与产物文件夹名对齐，便于按文件名辨识人声/伴奏等轨；为空时回退无前缀短名。
    progress_file 非空时在分离/降噪阶段边界写入阶段进度，供 UI 轮询展示。
    """
    global _demucs_model
    _check_cancelled()
    _write_progress(progress_file, "separating")
    mode = "4" if mode == "4" else "2"
    _load_demucs(mode)
    _check_cancelled()  # 模型加载可耗时 30s+，进入推理前再查一次取消

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
    pfx = f"{prefix}_" if prefix else ""  # 时间戳前缀（可空），产物名 <ts>_<类别>.wav
    if mode == "2":
        # 双轨：人声 + 伴奏（其余轨求和叠加）
        vocals_idx = stems.index("vocals")
        vocals = sources[vocals_idx]
        accompaniment = sum(s for i, s in enumerate(sources) if i != vocals_idx)
        # 推理完成落盘前检查：取消则不写半套产物（主 app 侧会回收整个产物目录）
        _check_cancelled()
        _save_track(out_root / f"{pfx}vocals.wav", vocals, sr, result, "vocals")
        _save_track(out_root / f"{pfx}accompaniment.wav", accompaniment, sr, result, "accompaniment")
    else:
        # 四轨：独立保存每轨（逐轨边界检查，取消时少写后续轨的无效 IO）
        for i, name in enumerate(stems):
            _check_cancelled()
            _save_track(out_root / f"{pfx}{name}.wav", sources[i], sr, result, name)
    # 可选降噪：仅作用于人声轨，失败时回退原轨不阻断；降噪写独立文件保留源 vocals
    if denoise and result.get("vocals"):
        _check_cancelled()
        _write_progress(progress_file, "denoising")
        result["vocals"] = denoise_audio(result["vocals"], str(out_root / f"{pfx}vocals_denoised.wav"),
                                         strength=denoise_strength)
    return result


def _save_track(path, track, sample_rate, result_into: dict, key: str):
    """将单个分离轨写入 wav 并登记到产物字典。track 为 (ch, T) 的 CPU 张量。"""
    import torchaudio
    out_path = Path(path)
    torchaudio.save(str(out_path), track, sample_rate)
    result_into[key] = str(out_path)


def denoise_audio(src: str, dst: str, strength=None) -> str:
    """用 ffmpeg 的 anlmdn 滤镜对音频降噪，输出到 dst，返回 dst。

    降噪不覆盖源文件：输出独立写入 dst，源 src 保持不变（约束：降噪副本与源文件并存）。
    强度 strength 为 anlmdn 的 s= 参数；为 None 时用 ffmpeg 默认值。
    降噪失败（ffmpeg 缺失/命令出错）时记录日志并返回原 src 路径，不阻断流程。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        _log("未找到 ffmpeg，跳过降噪")
        return src
    # 拼 anlmdn 滤镜；给出强度则 anlmdn=s=<强度>，否则用 ffmpeg 默认
    filt = f"anlmdn=s={strength}" if strength else "anlmdn"
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", src, "-af", filt, dst],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not Path(dst).exists():
            _log(f"降噪失败: {proc.stderr[-500:] or 'ffmpeg 无输出'}")
            return src
        _log(f"降噪完成: {Path(dst).name}")
        return dst
    except Exception as e:
        _log(f"降噪异常: {e}")
        return src


# ---------------------------------------------------------------- 换嗓
def _rms_db(path: str):
    """用 ffmpeg astats 测整段 RMS 电平（dB）。失败/静音(-inf)/无法解析时返回 None。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", path,
             "-af", "astats=measure_perchannel=0:measure_overall=RMS_level",
             "-f", "null", "-"],
            capture_output=True, text=True,
        )
    except Exception:
        return None
    # astats 输出形如 "Overall RMS level dB:  -18.23"；静音为 -inf（正则不匹配 → None）
    m = re.search(r"RMS level dB:\s*(-?\d+(?:\.\d+)?)", proc.stderr)
    return float(m.group(1)) if m else None


def _peak_db(path: str):
    """用 ffmpeg astats 测整段峰值电平（dBFS）。失败/无法解析时返回 None。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", path,
             "-af", "astats=measure_perchannel=0:measure_overall=Peak_level",
             "-f", "null", "-"],
            capture_output=True, text=True,
        )
    except Exception:
        return None
    m = re.search(r"Peak level dB:\s*(-?\d+(?:\.\d+)?)", proc.stderr)
    return float(m.group(1)) if m else None


def _lufs(path: str):
    """用 ffmpeg loudnorm 测整段积分响度 LUFS（BS.1770 口径，含响度门限）。

    原理：loudnorm 以 print_format=json 跑一遍空输出，从 stderr 末尾的 JSON 块
    解析 input_i（积分响度）。失败/静音(-inf)/无法解析时返回 None。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", path,
             "-af", "loudnorm=print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True,
        )
    except Exception:
        return None
    # JSON 形如 { "input_i" : "-16.53", ... }；-inf 不被正则匹配 → None
    m = re.search(r"\"input_i\"\s*:\s*\"(-?\d+(?:\.\d+)?)\"", proc.stderr)
    return float(m.group(1)) if m else None


def _convert(seed_dir: str, source: str, ref: str, semi_tone: int,
             diffusion_steps: int, accompaniment: str, output_dir: str,
             gain_db: float = 0.0, denoise: bool = False,
             denoise_strength=None, prefix: str = "",
             source_vocals_path: str = "", source_acc_path: str = "",
             progress_file: str = "") -> dict:
    """执行翻唱：分离 + Seed-VC 换嗓 + ffmpeg 混音。

    source=被翻唱整曲；ref=参考干声；accompaniment=自定义伴奏（空则用分离出的伴奏）。
    denoise=True 时对换嗓后的人声降噪，并用降噪后人声参与混音。
    prefix（时间戳前缀）非空时全部产物平铺在 output_dir 下，统一命名
    <prefix>_<类别>（如 20260924_201805_cover.flac），便于按文件夹整组回放/下载。
    source_vocals_path/source_acc_path 非空时复用已有分离结果（跳过 Demucs 重复分离，
    "先分离听过 → 满意后翻唱"场景省 GPU 分钟级时间）；此时 source_acc 兼作默认伴奏。
    progress_file 非空时在换嗓/降噪/混音阶段边界写入阶段进度，供 UI 轮询展示。
    """
    _load_seedvc(seed_dir)

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    pfx = f"{prefix}_" if prefix else ""  # 时间戳前缀（可空）

    _check_cancelled()
    # 0) 获取源人声/伴奏：优先复用已有分离结果（跳过 Demucs），否则现场分离整曲
    #    （Seed-VC 需要干净人声，不能用整曲）
    ref_acc = ""  # 源曲伴奏参照（供自定义伴奏响度对齐）
    if source_vocals_path and Path(source_vocals_path).exists():
        source_vocals = source_vocals_path
        ref_acc = source_acc_path if source_acc_path and Path(source_acc_path).exists() else ""
        if not accompaniment:
            if not ref_acc:
                raise RuntimeError("复用分离结果作翻唱源时必须提供源伴奏轨")
            accompaniment = ref_acc
        _log(f"复用已有分离结果（跳过 Demucs）: 人声={Path(source_vocals).name} "
             f"伴奏={Path(accompaniment).name}")
    else:
        sep_products = _separate(source, "2", str(out_root),
                                 denoise_strength=denoise_strength, prefix=prefix,
                                 progress_file=progress_file)
        source_vocals = sep_products["vocals"]
        ref_acc = sep_products["accompaniment"]
        if not accompaniment:
            # 未提供自定义伴奏：混音用分离出的原曲伴奏
            accompaniment = sep_products["accompaniment"]

    # 1) 换嗓（subprocess 调 inference.py）：原始输出进临时子目录，随后拷贝为带前缀的确定性文件名
    venv_python = sys.executable
    inference_py = Path(seed_dir) / "inference.py"
    conv_tmp = out_root / "_seedvc_tmp"
    conv_tmp.mkdir(parents=True, exist_ok=True)
    cmd = [
        venv_python, str(inference_py),
        "--source", source_vocals,
        "--target", ref,
        "--output", str(conv_tmp),
        "--diffusion-steps", str(diffusion_steps),
        "--f0-condition", "True",
        "--semi-tone-shift", str(semi_tone),
        "--fp16", "True",
    ]
    _log("运行 Seed-VC 换嗓: " + " ".join(cmd))
    # cwd=seed_dir：inference.py 依赖相对 cwd 定位检查点目录，用子进程级 cwd 替代进程级 chdir
    # Popen + 1s 轮询：取消信号触发时 terminate 子进程（Seed-VC 内部无法中断，
    # 换嗓常占全流程大半时间，可中断是"运行中取消"的主要收益点）
    _write_progress(progress_file, "converting")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=seed_dir)
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            if _cancel_flag.is_set():
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    proc.kill()
                shutil.rmtree(conv_tmp, ignore_errors=True)
                raise _TaskCancelled("任务已取消")
    if proc.returncode != 0:
        # 失败也清理临时子目录，避免失败大 wav 堆积污染产物文件夹
        shutil.rmtree(conv_tmp, ignore_errors=True)
        raise RuntimeError(f"Seed-VC 换嗓失败: {stderr[-2000:] or '无输出'}")

    # 定位换嗓输出：不按 sorted(glob())[0] 猜测文件名（多个文件时取序易取错轨）。
    # 取目录内修改时间最新的 wav，整体拷贝为确定性文件名 <pfx>converted_vocals.wav
    # （拷贝不动源文件；随后清理临时目录，产物文件夹只留最终可回放轨道）。
    conv_files = sorted(conv_tmp.glob("*.wav"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not conv_files:
        shutil.rmtree(conv_tmp, ignore_errors=True)
        raise RuntimeError("Seed-VC 未产出换嗓音频")
    converted_vocals = str(conv_files[0])
    conv_deterministic = out_root / f"{pfx}converted_vocals.wav"
    if Path(converted_vocals).resolve() != conv_deterministic.resolve():
        try:
            shutil.copyfile(converted_vocals, str(conv_deterministic))
        except Exception:
            shutil.rmtree(conv_tmp, ignore_errors=True)
            raise
    converted_vocals = str(conv_deterministic)
    # 清理换嗓临时目录（内容已拷贝为确定性产物，保留会污染产物文件夹）
    shutil.rmtree(conv_tmp, ignore_errors=True)
    # 可选降噪：输出写 <pfx>converted_vocals_denoised.wav（与源并存，不覆盖），失败回退原声；
    # 后续混音与产物均用（可能降噪后的）该文件
    if denoise:
        _check_cancelled()
        _write_progress(progress_file, "denoising")
        converted_vocals = denoise_audio(converted_vocals,
                                         str(out_root / f"{pfx}converted_vocals_denoised.wav"),
                                         strength=denoise_strength)

    # 2) 用伴奏混音 -> 48kHz 立体声 FLAC（+ 可选伴奏增益），平铺为 <pfx>cover.flac
    #    换嗓人声先做响度匹配：Seed-VC 换嗓输出电平常显著低于原声干声（实测可差 14dB+），
    #    不匹配会被伴奏完全盖住。优先 loudnorm 响度归一（LUFS 对齐原声干声 + 真峰值 TP 防削波）：
    #    静态增益受峰值余量限制拉不满平均电平（换嗓人声峰值高、平均低，实测收窄后人声偏弱），
    #    动态归一可在峰值不超限的前提下把响度拉到位；LUFS 测量失败时回退 RMS+峰值钳制静态增益。
    _check_cancelled()
    _write_progress(progress_file, "mixing")
    cover_flac = out_root / f"{pfx}cover.flac"
    # 自定义伴奏响度对齐：素材库伴奏电平未知，与源曲伴奏差可超 10dB（换伴奏翻唱失衡）。
    # 把自定义伴奏 LUFS 静态增益对齐到源伴奏 LUFS（限 ±18dB）；测量失败回退 0dB 不处理
    acc_gain_db = 0.0
    if accompaniment and ref_acc and \
            Path(accompaniment).resolve() != Path(ref_acc).resolve():
        acc_lufs = _lufs(accompaniment)
        ref_lufs = _lufs(ref_acc)
        if acc_lufs is not None and ref_lufs is not None:
            acc_gain_db = max(-18.0, min(18.0, ref_lufs - acc_lufs))
            _log(f"自定义伴奏响度对齐: {acc_gain_db:+.1f}dB"
                 f"（素材 {acc_lufs:.1f} -> 源 {ref_lufs:.1f} LUFS）")
    src_lufs = _lufs(source_vocals)
    if src_lufs is not None:
        _log(f"人声响度匹配: loudnorm 归一到 {src_lufs:.1f} LUFS（原声干声，TP=-1.5dB）")
        # 注意 pan 在 loudnorm 之前：换嗓输出为单声道，先复制成立体声再归一，
        # 与原声干声（立体声）的 LUFS 声道求和口径一致；loudnorm 内部升 192k，需 aresample 回 48k
        a0_chain = (f"[0:a]aresample=48000,pan=stereo|c0=c0|c1=c1,"
                    f"loudnorm=I={src_lufs:.2f}:TP=-1.5:LRA=11,aresample=48000[a0]")
    else:
        match_db = 0.0
        src_rms = _rms_db(source_vocals)
        conv_rms = _rms_db(converted_vocals)
        if src_rms is not None and conv_rms is not None:
            match_db = max(-18.0, min(18.0, src_rms - conv_rms))
            # 峰值防削波：放大后峰值不超过 -1dBFS。换嗓电平常远低于原声（差 14dB+），
            # 若只按 RMS 匹配，放大后峰值会冲破 0dBFS，alimiter 大量触发 → 削顶失真。
            # 此处收窄增益到峰值安全范围，alimiter 仅作兜底。
            conv_peak = _peak_db(converted_vocals)
            if conv_peak is not None and match_db > 0:
                ceiling = -1.0 - conv_peak  # 保证 peak + gain <= -1dB
                if match_db > ceiling:
                    _log(f"人声响度匹配: 峰值防削波收窄增益 {match_db:+.1f}dB -> {ceiling:+.1f}dB"
                         f"（换嗓峰值 {conv_peak:.1f}dB）")
                    match_db = ceiling
            _log(f"人声响度匹配: 原声干声RMS={src_rms:.1f}dB 换嗓RMS={conv_rms:.1f}dB"
                 f" -> 增益 {match_db:+.1f}dB")
        else:
            _log("人声响度匹配: RMS 测量不可用，跳过（增益 0dB）")
        a0_chain = (f"[0:a]aresample=48000,pan=stereo|c0=c0|c1=c1,"
                    f"volume={match_db:+.1f}dB,alimiter=limit=0.98:level=false[a0]")
    amix_filter = (
        f"[1:a]aresample=48000,pan=stereo|c0=c0|c1=c1,"
        f"volume={gain_db + acc_gain_db:+.1f}dB[a1];"
        f"{a0_chain};"
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
            # 取消信号必须在串行锁 _lock 之外处理：任务持锁执行时，cancel 请求
            # 若也排队等锁就永远等到任务结束，协作取消完全失效
            if url.path == "/api/cancel":
                _cancel_flag.set()
                _log("收到取消信号")
                self._send(200, {"ok": True})
                return
            # 串行处理：模型推理只能单线程（显存安全）
            with _lock:
                _cancel_flag.clear()  # 新任务开始：重置取消标志
                if url.path == "/api/separate":
                    result = _separate(body["input"], body.get("mode", "2"),
                                       body["output_dir"], bool(body.get("denoise", False)),
                                       body.get("denoise_strength"),
                                       str(body.get("prefix", "")),
                                       str(body.get("progress_file", "")))
                    self._send(200, {"ok": True, "products": result})
                elif url.path == "/api/convert":
                    result = _convert(
                        os.environ.get("SEEDVC_DIR", ""),
                        body["source"], body["ref"],
                        int(body.get("semi_tone", 0)),
                        int(body.get("diffusion_steps", 30)),
                        body["accompaniment"], body["output_dir"],
                        float(body.get("gain_db", 0.0)),
                        bool(body.get("denoise", False)),
                        body.get("denoise_strength"),
                        str(body.get("prefix", "")),
                        str(body.get("source_vocals", "")),
                        str(body.get("source_acc", "")),
                        str(body.get("progress_file", "")),
                    )
                    self._send(200, {"ok": True, "products": result})
                else:
                    self._send(404, {"error": "not found"})
        except Exception as e:
            _log(f"处理失败: {e}")
            # 业务失败（含协作取消"任务已取消"）用 200 + ok:false 返回：
            # HTTP 500 会让客户端 urllib 抛 HTTPError 丢失 body，文案退化为
            # "无法连接推理 worker"，误导排障方向
            # cancelled=True 标记本次失败源于协作取消，主 app 据此显示"已取消"
            self._send(200, {"ok": False, "error": str(e),
                             "cancelled": isinstance(e, _TaskCancelled)})


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