"""多轨混音渲染（M1）：混音工程 JSON → 校验 → ffmpeg filtergraph → 渲染产物。

本模块是纯后端能力，与 Gradio 前端解耦，便于独立单元测试：
    parse_mix_project(payload, webui_root) -> MixProject     # 校验 + 素材路径白名单
    build_ffmpeg_cmd(project, out_path) -> list[str]         # 纯函数，不做任何 IO
    render_mix(project, out_path, cancel_event, progress_cb) -> dict
    mix_worker(_task, payload_json, webui_root, out_dir, project, history_mgr) -> dict

安全与健壮性约束：
- 素材路径必须解析到 <webui_root>/outputs 之下，避免任意路径被交给 ffmpeg 读取
- 增益/响度目标越界钳制、非法类型回退默认值、非法切片丢弃，脏数据不中断整条渲染
- 业务失败统一抛 MixProjectError；mix_worker 捕获后返回 {"ok": False, "error": ...}
- 渲染过程可按 cancel_event 协作取消（杀 ffmpeg 子进程并抛 TaskCancelledError）
"""
import json
import logging
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from history import HistoryRecord, sanitize_project
from queue_manager import TaskCancelledError

logger = logging.getLogger(__name__)

# 工程契约版本与容量上限
MIX_SCHEMA_VERSION = 1
MAX_TRACKS = 8

# 母带默认目标（响度 LUFS / 真峰值 dBTP）；LRA 固定，不做 UI 暴露
DEFAULT_MASTER = {"loudness_target": -14.0, "true_peak": -1.5}
_LRA = 11.0

# 参数钳制区间
_GAIN_MIN, _GAIN_MAX = -30.0, 12.0
_LOUD_MIN, _LOUD_MAX = -30.0, -5.0
_TP_MIN, _TP_MAX = -6.0, -0.1
# 每轨音质参数（P1）：声像 [-] / 低中高 EQ [dB] / 高通·低通 [Hz]（0 = 关闭）
_PAN_MIN, _PAN_MAX = -1.0, 1.0
_EQ_DB_MIN, _EQ_DB_MAX = -15.0, 15.0
_HPF_MIN, _HPF_MAX = 0.0, 2000.0
_LPF_MIN, _LPF_MAX = 0.0, 20000.0
# 三段 EQ 中心/转折频率（与前端 Web Audio 节点保持一致）
_EQ_LOW_F, _EQ_MID_F, _EQ_HIGH_F = 200.0, 1000.0, 4000.0

# 每轨音效参数（P2）：压缩（阈值 dB / 比率）+ 回声（延迟 ms / 反馈 / 混合）
_COMP_TH_MIN, _COMP_TH_MAX = -60.0, 0.0        # 0 dB = 阈值顶到上限，等同不压缩
_COMP_RATIO_MIN, _COMP_RATIO_MAX = 1.0, 20.0   # 1 = 不压缩
_ECHO_DELAY_MIN, _ECHO_DELAY_MAX = 0.0, 2000.0
_ECHO_FB_MAX = 0.95                            # 反馈上限，避免无限堆积
_ECHO_MIX_MIN, _ECHO_MIX_MAX = 0.0, 1.0
# 压缩器固定时间常数（与前端 Web Audio 节点保持一致）；attack/release 是毫秒
_COMP_ATTACK_MS, _COMP_RELEASE_MS, _COMP_KNEE_DB = 20.0, 250.0, 6.0
_COMP_MIN_LINEAR = 0.001                       # ffmpeg acompressor threshold 下限（约 -60dB）
_ECHO_DEFAULT_DELAY_MS = 250.0                 # 混合 > 0 但未填延迟时的兜底值
_ECHO_TAPS = 3                                  # 回声抽头数（前后端一致：fb^1 / fb^2 / fb^3）
_ECHO_DECAY_FLOOR = 0.001                       # ffmpeg 要求 decay ∈ (0, 1]
# 时长未知（clip 未给 out）时淡入的保守上限（秒）
_MAX_FADE = 60.0

# 输出扩展名 → 编码器（只支持无损，避免二次有损压缩）
_CODEC_BY_SUFFIX = {".flac": "flac", ".wav": "pcm_s24le"}


class MixProjectError(Exception):
    """混音工程非法或渲染失败（业务错误，由调用方转为可展示文案）。"""


@dataclass
class MixClip:
    """轨道上的一段素材引用（时间线坐标，单位秒）。"""
    start: float = 0.0            # 在时间线上的起始位置 → adelay
    in_: float = 0.0              # 素材内起点 → atrim start
    out: Optional[float] = None   # 素材内终点 → atrim end；None = 到素材末尾
    fade_in: float = 0.0
    fade_out: float = 0.0


@dataclass
class MixTrack:
    """一条轨道（src 已解析为绝对路径）。clips 为空表示整轨。

    音质参数（P1）默认全部中性，此时不产生任何额外滤镜：
        pan  声像 [-1 左, 1 右]；eq_low/eq_mid/eq_high 低中高增益 dB；
        hpf/lpf 高通/低通截止频率 Hz（0 = 关闭）。
    音效参数（P2）默认同样中性：
        comp_th/comp_ratio 压缩阈值 dB 与比率（0dB / 1 = 不压缩）；
        echo_delay/echo_fb/echo_mix 回声延迟 ms、反馈、混合（混合 0 = 关闭）。
    """
    id: str
    name: str
    src: Path
    gain_db: float = 0.0
    mute: bool = False
    clips: list = field(default_factory=list)
    pan: float = 0.0
    eq_low: float = 0.0
    eq_mid: float = 0.0
    eq_high: float = 0.0
    hpf: float = 0.0
    lpf: float = 0.0
    comp_th: float = 0.0
    comp_ratio: float = 1.0
    echo_delay: float = 0.0
    echo_fb: float = 0.0
    echo_mix: float = 0.0


@dataclass
class MixProject:
    """校验后的混音工程。"""
    version: int
    tracks: list
    master: dict
    duration: float = 0.0
    sample_rate: int = 48000
    source_root_task_id: str = ""


# ------------------------------------------------------------------ 通用工具
def _as_float(value, default: float = 0.0) -> float:
    """宽松取数：仅接受有限实数（bool 视为非法），其余回退默认值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    v = float(value)
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _num(value: float) -> str:
    """秒值格式化：保留至多 3 位小数，且至少保留 1 位（0.0 → "0.0"）。"""
    s = f"{value:.3f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _resolve_src(src, outputs_root: Path) -> Path:
    """把 src 解析为绝对路径并校验落在 outputs/ 之下，且文件必须存在。"""
    if not isinstance(src, str) or not src.strip():
        raise MixProjectError("轨道缺少素材路径 src")
    raw = Path(src)
    candidate = raw if raw.is_absolute() else (outputs_root.parent / src)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(outputs_root.resolve())
    except (OSError, ValueError):
        raise MixProjectError(f"素材路径越界（必须位于 outputs/ 下）: {src}")
    if not resolved.is_file():
        raise MixProjectError(f"素材文件不存在: {src}")
    return resolved


def _parse_clip(raw) -> Optional[MixClip]:
    """解析单个切片；非法切片返回 None（被丢弃），不做隐式扩张。"""
    if not isinstance(raw, dict):
        return None
    in_ = max(0.0, _as_float(raw.get("in"), 0.0))
    out_raw = raw.get("out")
    out = None if out_raw in (None, "") else _as_float(out_raw, None)
    if out is not None and out <= in_:
        return None
    start = max(0.0, _as_float(raw.get("start"), 0.0))
    fade_in = max(0.0, _as_float(raw.get("fade_in"), 0.0))
    fade_out = max(0.0, _as_float(raw.get("fade_out"), 0.0))
    if out is not None:
        dur = out - in_
        # 淡变超过切片时长（或首尾淡变相互重叠）时整体回退为 0，避免 ffmpeg 报错
        if fade_in > dur or fade_out > dur or (fade_in + fade_out) > dur:
            fade_in = fade_out = 0.0
    else:
        fade_out = 0.0                      # 终点未知：无法计算淡出起点
        fade_in = min(fade_in, _MAX_FADE)
    return MixClip(start=start, in_=in_, out=out, fade_in=fade_in, fade_out=fade_out)


def _parse_track(raw, idx: int, outputs_root: Path) -> MixTrack:
    """解析单条轨道：素材路径走白名单，增益钳制，非法切片丢弃。"""
    if not isinstance(raw, dict):
        raise MixProjectError(f"第 {idx + 1} 轨格式非法（应为对象）")
    tid = str(raw.get("id") or f"track{idx + 1}")
    name = str(raw.get("name") or tid)
    src = _resolve_src(raw.get("src"), outputs_root)
    gain = _clamp(_as_float(raw.get("gain_db"), 0.0), _GAIN_MIN, _GAIN_MAX)
    mute = bool(raw.get("mute"))
    clips_raw = raw.get("clips") or []
    if not isinstance(clips_raw, list):
        clips_raw = []
    clips = [c for c in (_parse_clip(x) for x in clips_raw) if c is not None]
    # 给了切片但全部非法：静音轨无害，非静音轨静默变空会让人误以为渲染成功
    if clips_raw and not clips and not mute:
        raise MixProjectError(f"轨道 {name} 的切片全部非法")
    # 音质参数（P1）：缺省/非法一律回退中性值，越界钳制，脏数据不中断渲染
    fx = {
        "pan": _clamp(_as_float(raw.get("pan"), 0.0), _PAN_MIN, _PAN_MAX),
        "eq_low": _clamp(_as_float(raw.get("eq_low"), 0.0), _EQ_DB_MIN, _EQ_DB_MAX),
        "eq_mid": _clamp(_as_float(raw.get("eq_mid"), 0.0), _EQ_DB_MIN, _EQ_DB_MAX),
        "eq_high": _clamp(_as_float(raw.get("eq_high"), 0.0), _EQ_DB_MIN, _EQ_DB_MAX),
        "hpf": _clamp(_as_float(raw.get("hpf"), 0.0), _HPF_MIN, _HPF_MAX),
        "lpf": _clamp(_as_float(raw.get("lpf"), 0.0), _LPF_MIN, _LPF_MAX),
        # 音效参数（P2）：压缩阈值/比率、回声延迟/反馈/混合
        "comp_th": _clamp(_as_float(raw.get("comp_th"), 0.0), _COMP_TH_MIN, _COMP_TH_MAX),
        "comp_ratio": _clamp(_as_float(raw.get("comp_ratio"), 1.0),
                             _COMP_RATIO_MIN, _COMP_RATIO_MAX),
        "echo_delay": _clamp(_as_float(raw.get("echo_delay"), 0.0),
                             _ECHO_DELAY_MIN, _ECHO_DELAY_MAX),
        "echo_fb": _clamp(_as_float(raw.get("echo_fb"), 0.0), 0.0, _ECHO_FB_MAX),
        "echo_mix": _clamp(_as_float(raw.get("echo_mix"), 0.0),
                           _ECHO_MIX_MIN, _ECHO_MIX_MAX),
    }
    return MixTrack(id=tid, name=name, src=src, gain_db=gain, mute=mute, clips=clips, **fx)


def parse_mix_project(payload, webui_root) -> MixProject:
    """校验并归一化混音工程 JSON（非法输入抛 MixProjectError）。"""
    if not isinstance(payload, dict):
        raise MixProjectError("混音工程必须是 JSON 对象")
    version = payload.get("version", MIX_SCHEMA_VERSION)
    if version != MIX_SCHEMA_VERSION:
        raise MixProjectError(f"不支持的工程版本: {version}")
    tracks_raw = payload.get("tracks")
    if not isinstance(tracks_raw, list) or not tracks_raw:
        raise MixProjectError("混音工程至少需要 1 条轨道")
    if len(tracks_raw) > MAX_TRACKS:
        raise MixProjectError(f"轨道数超过上限（最多 {MAX_TRACKS} 轨）")

    outputs_root = Path(webui_root) / "outputs"
    tracks = [_parse_track(t, i, outputs_root) for i, t in enumerate(tracks_raw)]
    if all(t.mute for t in tracks):
        raise MixProjectError("所有轨道均已静音，无内容可渲染")

    master_raw = payload.get("master") or {}
    if not isinstance(master_raw, dict):
        master_raw = {}
    master = {
        "loudness_target": _clamp(
            _as_float(master_raw.get("loudness_target"), DEFAULT_MASTER["loudness_target"]),
            _LOUD_MIN, _LOUD_MAX),
        "true_peak": _clamp(
            _as_float(master_raw.get("true_peak"), DEFAULT_MASTER["true_peak"]),
            _TP_MIN, _TP_MAX),
    }
    return MixProject(
        version=version,
        tracks=tracks,
        master=master,
        duration=_as_float(payload.get("duration"), 0.0),
        sample_rate=int(_as_float(payload.get("sample_rate"), 48000)),
        source_root_task_id=str(payload.get("source_root_task_id") or ""),
    )


# ------------------------------------------------------------------ filtergraph
def _pan_filters(pan: float) -> list:
    """声像滤镜：按 Web Audio StereoPannerNode 的立体声算法生成等功率 pan。

    规格算法（pan <= 0 时 x=(pan+1)*π/2，否则 x=pan*π/2；gL=cos(x)、gR=sin(x)）：
        pan <= 0:  outL = L + gL*R ; outR = gR*R
        pan >  0:  outL = gL*L     ; outR = R + gR*L
    先 aformat 归一为立体声，保证单声道素材也能与前端预览行为一致。
    """
    if abs(pan) < 0.005:
        return []
    if pan <= 0:
        x = (pan + 1) * math.pi / 2
        gl, gr = math.cos(x), math.sin(x)
        return [
            "aformat=channel_layouts=stereo",
            f"pan=stereo|c0=c0+{gl:.4f}*c1|c1={gr:.4f}*c1",
        ]
    x = pan * math.pi / 2
    gl, gr = math.cos(x), math.sin(x)
    return [
        "aformat=channel_layouts=stereo",
        f"pan=stereo|c0={gl:.4f}*c0|c1=c1+{gr:.4f}*c0",
    ]


def _comp_filter(track) -> str:
    """压缩滤镜：Web Audio DynamicsCompressorNode 的 ffmpeg 对应实现。

    ffmpeg acompressor 的 threshold 是**线性幅度**（非 dB），故换算 10^(dB/20)；
    attack/release/knee 与前端固定时间常数一致；makeup=1、mix=1（不做自动补偿与并联）。
    阈值 >= 0dB 或比率 <= 1 视为关闭（返回空串）。
    """
    if track.comp_th >= -0.05 or track.comp_ratio <= 1.05:
        return ""
    lin = max(_COMP_MIN_LINEAR, min(1.0, 10 ** (track.comp_th / 20.0)))
    return (f"acompressor=threshold={lin:.6f}:ratio={track.comp_ratio:.2f}"
            f":attack={_num(_COMP_ATTACK_MS)}:release={_num(_COMP_RELEASE_MS)}"
            f":knee={_num(_COMP_KNEE_DB)}:makeup=1:mix=1")


def _echo_filter(track) -> str:
    """回声滤镜：3 个抽头（fb¹/fb²/fb³）与前端 3 条并联延迟线一一对应。

    ffmpeg aecho 实测语义为 out = out_gain * (in_gain*in + Σ decay_j*in[n-d_j])，
    干声也要经过 out_gain，因此固定 in_gain=out_gain=1，靠 decays 表达「混合×反馈^k」，
    这样干声电平不变、回声强度由混合控制，与前端边听边导出保持一致。
    混合 < 0.01 视为关闭（返回空串）；延迟未填时用兜底 250ms。
    """
    if track.echo_mix < 0.01:
        return ""
    delay = track.echo_delay if track.echo_delay > 0 else _ECHO_DEFAULT_DELAY_MS
    delays = "|".join(_num(delay * k) for k in range(1, _ECHO_TAPS + 1))
    decays = "|".join(
        f"{max(_ECHO_DECAY_FLOOR, min(1.0, track.echo_mix * (track.echo_fb ** k))):.4f}"
        for k in range(1, _ECHO_TAPS + 1))
    return f"aecho=1:1:{delays}:{decays}"


def _fx_filters(track) -> list:
    """每轨音质/音效滤镜：高通→低通→低中高 EQ→声像→压缩→回声；中性参数不产生滤镜。"""
    out = []
    if track.hpf > 0:
        out.append(f"highpass=f={_num(track.hpf)}:width_type=q:width=0.707")
    if track.lpf > 0:
        out.append(f"lowpass=f={_num(track.lpf)}:width_type=q:width=0.707")
    if abs(track.eq_low) >= 0.05:                   # 低架滤波（lowshelf 200Hz）
        out.append(f"bass=g={track.eq_low:.1f}:f={_num(_EQ_LOW_F)}")
    if abs(track.eq_mid) >= 0.05:                   # 峰值滤波（peaking 1kHz / Q=1）
        out.append(f"equalizer=f={_num(_EQ_MID_F)}:width_type=q:width=1:g={track.eq_mid:.1f}")
    if abs(track.eq_high) >= 0.05:                  # 高架滤波（highshelf 4kHz）
        out.append(f"treble=g={track.eq_high:.1f}:f={_num(_EQ_HIGH_F)}")
    out.extend(_pan_filters(track.pan))
    comp = _comp_filter(track)
    if comp:
        out.append(comp)
    echo = _echo_filter(track)
    if echo:
        out.append(echo)
    return out


def build_ffmpeg_cmd(project: MixProject, out_path) -> list:
    """根据工程生成 ffmpeg 命令（纯函数，不做 IO）。

    每轨处理链：atrim 裁剪 → asetpts 归零 → afade 淡入/淡出 → volume 增益
    → 音质与音效（高通/低通/EQ/声像/压缩/回声）→ adelay 时间线偏移；同一素材多切片时用 asplit
    展开（一个输入 pad 只能被消费一次）。所有段落统一 amix（normalize=0，
    避免电平被平均压低）后接 loudnorm 归一化到母带目标。输出编码按扩展名选择
    无损编码器。
    """
    out_path = Path(out_path)
    codec = _CODEC_BY_SUFFIX.get(out_path.suffix.lower())
    if codec is None:
        raise MixProjectError(f"不支持的输出格式: {out_path.suffix}")

    inputs = []
    graph = []
    labels = []
    for track in project.tracks:
        if track.mute:
            continue                                  # 静音轨既不进 -i，也不进图
        idx = len(inputs)
        inputs.append(str(track.src))
        fx = _fx_filters(track)                       # 每轨音质滤镜（中性时为 []）

        if not track.clips:                           # 空 clips = 整轨
            parts = [f"volume={track.gain_db:.1f}dB"] + fx
            graph.append(f"[{idx}:a]" + ",".join(parts) + f"[t{len(labels)}]")
            labels.append(f"[t{len(labels)}]")
            continue

        if len(track.clips) == 1:
            branches = [f"[{idx}:a]"]
        else:                                         # 多切片需 asplit 展开输入
            pads = "".join(f"[sp{idx}{k}]" for k in range(len(track.clips)))
            graph.append(f"[{idx}:a]asplit={len(track.clips)}{pads}")
            branches = [f"[sp{idx}{k}]" for k in range(len(track.clips))]

        for clip, branch in zip(track.clips, branches):
            atrim = f"atrim=start={_num(clip.in_)}"
            if clip.out is not None:
                atrim += f":end={_num(clip.out)}"
            parts = [atrim, "asetpts=N/SR/TB"]
            if clip.fade_in > 0:
                parts.append(f"afade=t=in:st=0:d={_num(clip.fade_in)}")
            if clip.fade_out > 0 and clip.out is not None:
                st = max(0.0, (clip.out - clip.in_) - clip.fade_out)
                parts.append(f"afade=t=out:st={_num(st)}:d={_num(clip.fade_out)}")
            parts.append(f"volume={track.gain_db:.1f}dB")
            parts.extend(fx)
            delay_ms = int(round(clip.start * 1000))
            if delay_ms > 0:
                parts.append(f"adelay={delay_ms}:all=1")
            label = f"[t{len(labels)}]"
            graph.append(branch + ",".join(parts) + label)
            labels.append(label)

    if len(labels) > 1:
        graph.append("".join(labels) +
                     f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0[mixed]")
        mixed = "[mixed]"
    else:
        mixed = labels[0]

    master = project.master
    graph.append(f"{mixed}loudnorm=I={master['loudness_target']:.1f}"
                 f":TP={master['true_peak']:.1f}:LRA={_LRA:.1f}[out]")

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    for path in inputs:
        cmd += ["-i", path]
    cmd += ["-filter_complex", ";".join(graph), "-map", "[out]", "-c:a", codec, str(out_path)]
    return cmd


# ------------------------------------------------------------------ 渲染
def _report(progress_cb: Optional[Callable], value: float, desc: str) -> None:
    """进度上报（回调异常不影响渲染主流程）。"""
    if not progress_cb:
        return
    try:
        progress_cb(value, desc)
    except Exception:
        logger.debug("混音进度上报失败(已忽略)")


def _probe_duration(path: Path) -> float:
    """用 ffprobe 读取产物时长；不可用时返回 0.0（不视为失败）。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return float((proc.stdout or "").strip())
    except Exception:
        logger.exception("ffprobe 时长解析失败(已忽略): %s", path)
        return 0.0


def render_mix(project: MixProject, out_path, cancel_event=None,
               progress_cb: Optional[Callable] = None) -> dict:
    """执行 ffmpeg 渲染，返回 {"output", "duration"}；失败抛 MixProjectError。

    ffmpeg 的 stderr 重定向到临时文件（而非管道），避免长音频日志写满管道缓冲
    造成死锁；轮询等待期间按 cancel_event 协作取消（直接杀子进程）。
    """
    out_path = Path(out_path)
    if out_path.suffix.lower() not in _CODEC_BY_SUFFIX:
        raise MixProjectError(f"不支持的输出格式: {out_path.suffix}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MixProjectError("未找到 ffmpeg，请先安装并加入 PATH")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise MixProjectError(f"输出目录创建失败: {e}") from e

    cmd = build_ffmpeg_cmd(project, out_path)
    cmd[0] = ffmpeg
    _report(progress_cb, 0.1, "混音渲染中...")
    try:
        with tempfile.TemporaryFile() as err_file:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err_file)
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    proc.wait()
                    raise TaskCancelledError("混音渲染已取消")
                time.sleep(0.2)
            err_file.seek(0)
            stderr = err_file.read().decode("utf-8", "replace")
    except TaskCancelledError:
        raise
    except OSError as e:
        raise MixProjectError(f"ffmpeg 启动失败: {e}") from e

    if proc.returncode != 0:
        tail = " | ".join([ln for ln in stderr.strip().splitlines() if ln][-5:])
        raise MixProjectError(f"ffmpeg 渲染失败: {tail}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise MixProjectError("ffmpeg 未产出有效文件")

    duration = _probe_duration(out_path)
    _report(progress_cb, 0.9, "混音渲染完成")
    return {"output": str(out_path), "duration": duration}


# ------------------------------------------------------------------ 队列 worker
def _write_history(history_mgr, project: MixProject, task_id: str, out_path: Path,
                   out_dir: Path, webui_root: Path, project_name: str,
                   duration: float, elapsed: float) -> None:
    """写入混音历史记录（stems 仅一条成品，供历史页回放/下载）。"""
    try:
        rel_dir = str(out_dir.relative_to(webui_root))
    except ValueError:
        rel_dir = str(out_dir)          # 目录不在 webui_root 下（测试/自定义路径）时存绝对路径
    record = HistoryRecord(
        task_id=task_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        style="[mix]",
        cot="voice:mix",
        audio_duration_seconds=duration,
        generation_time_seconds=elapsed,
        audio_path=str(out_path),
        output_dir=rel_dir,
        backend="ffmpeg",
        status="completed",
        record_type="mix",
        derived_from=project.source_root_task_id,
        root_task_id=project.source_root_task_id,
        stems=[{"label": "混音成品", "type": "mix", "path": str(out_path)}],
        project=sanitize_project(project_name),
    )
    history_mgr.append(record)
    history_mgr.auto_prune()


def mix_worker(_task, payload_json: str, webui_root, out_dir, project: str = "",
               history_mgr=None) -> dict:
    """队列 worker：解析工程 → 渲染 → 写历史，始终返回 {"ok": bool, ...}。

    业务失败（含工程非法、ffmpeg 报错）返回 {"ok": False, "error": ...} 而非抛异常，
    避免 UI 侧显示成"无法连接 worker"；取消则抛 TaskCancelledError 交给队列标记。
    """
    webui_root = Path(webui_root)
    out_dir = Path(out_dir)
    cancel_event = getattr(_task, "cancel_event", None)

    def _progress(value: float, desc: str) -> None:
        try:
            _task.push_progress(value, desc)
        except Exception:
            pass                        # 任务已结束等场景，进度推送失败无碍

    try:
        payload = json.loads(payload_json)
    except Exception as e:
        return {"ok": False, "error": f"混音工程 JSON 解析失败: {e}"}

    try:
        proj = parse_mix_project(payload, webui_root)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = sanitize_project(project)
        stem = f"{prefix}_{ts}_mix" if prefix else f"{ts}_mix"
        out_path = out_dir / f"{stem}.flac"
        started = time.time()
        info = render_mix(proj, out_path, cancel_event=cancel_event, progress_cb=_progress)
        if history_mgr is not None:
            _write_history(history_mgr, proj, getattr(_task, "task_id", "") or stem,
                           out_path, out_dir, webui_root, project,
                           info.get("duration", 0.0), time.time() - started)
        _progress(1.0, "混音完成")
        return {"ok": True, "output": str(out_path), "duration": info.get("duration", 0.0)}
    except TaskCancelledError:
        raise                               # 取消由队列统一标记为 CANCELLED
    except MixProjectError as e:
        logger.warning("混音任务失败: %s", e)
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception("混音任务异常")
        return {"ok": False, "error": str(e)}
