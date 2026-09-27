# -*- coding: utf-8 -*-
"""程序说明：P3 微调素材切分 —— 把长人声切成 5~12s 的短语片段（跳过静音）。

背景：Seed-VC few-shot 微调的数据集要求单说话人、单文件 1~30s 音频
（见 seed-vc/data/ft_dataset.py 的 duration_setting）。直接喂整曲（224s）会被
数据集直接跳过，因此需要预切分。

策略：先用 ffmpeg silencedetect 探测静音区间，得到语音区间；再把语音区间按
"累积到 min 秒后、遇到静音边界就切一刀"的方式分组，避免把字切在中间。单个
语音区间本身超过 max 秒时（连续唱腔无停顿）按 max 秒硬切。

用法：
  python "ft_prep_segments.py" <输入音频> <输出目录> [--min 5] [--max 12]

输出：<输出目录>/seg_001.wav ...（单声道 44.1kHz 16bit），并在标准输出打印汇总。
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 静音探测默认参数：-35dB 以下、持续 0.35s 以上算静音
DEF_NOISE_DB = "-35dB"
DEF_SIL_DUR = 0.35


def _run(cmd):
    """执行外部命令；失败时抛出带 stderr 的异常。"""
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc


def probe_duration(path: str) -> float:
    """读取音频总时长（秒）。"""
    proc = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path])
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 读取时长失败: {proc.stderr.strip()}")
    return float(proc.stdout.strip())


def detect_silence(path: str, noise_db: str, sil_dur: float):
    """返回静音区间列表 [(start, end), ...]（秒）。

    ffmpeg silencedetect 的输出在 stderr，形如：
      [silencedetect @ ...] silence_start: 12.34
      [silencedetect @ ...] silence_end: 13.02 | silence_duration: 0.68
    若文件以静音结尾，可能只有 silence_start 而没有对应 silence_end，此处补到总时长。
    """
    proc = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                 "-af", f"silencedetect=noise={noise_db}:d={sil_dur}",
                 "-f", "null", "-"])
    txt = proc.stderr or ""
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", txt)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*(-?[\d.]+)", txt)]

    spans = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else float("inf")
        spans.append((max(0.0, s), e))
    return spans


def speech_intervals(total: float, silences):
    """由静音区间求补集，得到语音区间列表（已剔除过短的碎片）。"""
    spans = [(s, min(e, total)) for s, e in silences if s < total]
    spans.sort()
    out = []
    cur = 0.0
    for s, e in spans:
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < total:
        out.append((cur, total))
    # 丢弃 <0.3s 的碎片（多为换气点误检）
    return [(s, e) for s, e in out if e - s >= 0.3]


def group_intervals(speech, min_sec: float, max_sec: float):
    """把语音区间累积成 [min_sec, max_sec] 的片段（切点永远落在静音边界）。"""
    groups = []
    cur_s, cur_e = None, None
    for s, e in speech:
        # 单个区间就超过 max：先硬切
        while e - s > max_sec:
            groups.append((s, s + max_sec))
            s += max_sec
        if cur_s is None:
            cur_s, cur_e = s, e
            continue
        # 加入后是否仍在 max 内：能并就并（片段自然趋近 max），超了就在静音处收口
        if e - cur_s <= max_sec:
            cur_e = e
        else:
            groups.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    if cur_s is not None:
        groups.append((cur_s, cur_e))

    # 合并过短片段到前一片段（不足 min 的一半且合并后不超 max）
    merged = []
    for g in groups:
        if (merged and (g[1] - g[0]) < min_sec / 2
                and (g[1] - merged[-1][0]) <= max_sec):
            merged[-1] = (merged[-1][0], g[1])
        else:
            merged.append(g)
    return merged


def cut(src: str, out_dir: Path, groups):
    """按片段区间导出单声道 44.1kHz 16bit wav，返回产物路径列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for i, (s, e) in enumerate(groups, 1):
        dst = out_dir / f"seg_{i:03d}.wav"
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y",
               "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", src,
               "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(dst)]
        proc = _run(cmd)
        if proc.returncode != 0 or not dst.exists():
            raise RuntimeError(f"切分失败 [{s:.2f}~{e:.2f}]: {proc.stderr[-500:]}")
        made.append(dst)
    return made


def main(argv=None):
    ap = argparse.ArgumentParser(description="长人声切分为 5~12s 短语片段（跳过静音）")
    ap.add_argument("source", help="输入音频路径")
    ap.add_argument("out_dir", help="输出目录")
    ap.add_argument("--min", type=float, default=5.0, help="片段最短秒数（默认 5）")
    ap.add_argument("--max", type=float, default=12.0, help="片段最长秒数（默认 12）")
    ap.add_argument("--noise", default=DEF_NOISE_DB, help="静音阈值（默认 -35dB）")
    ap.add_argument("--sil-dur", type=float, default=DEF_SIL_DUR,
                    help="静音最短持续秒数（默认 0.35）")
    args = ap.parse_args(argv)

    src = args.source
    if not Path(src).exists():
        print(f"输入文件不存在: {src}", file=sys.stderr)
        return 2
    if args.min >= args.max:
        print("--min 必须小于 --max", file=sys.stderr)
        return 2

    total = probe_duration(src)
    silences = detect_silence(src, args.noise, args.sil_dur)
    speech = speech_intervals(total, silences)
    speech_sec = sum(e - s for s, e in speech)
    groups = group_intervals(speech, args.min, args.max)

    out_dir = Path(args.out_dir)
    if out_dir.exists():
        # 幂等：清掉旧的 seg_*.wav，避免上次残留混入训练集
        for old in out_dir.glob("seg_*.wav"):
            old.unlink()
    made = cut(src, out_dir, groups)

    print(f"源文件      : {src}")
    print(f"总时长      : {total:.2f}s")
    print(f"静音区间    : {len(silences)} 段")
    print(f"纯语音时长  : {speech_sec:.2f}s")
    print(f"片段数      : {len(made)} -> {out_dir}")
    if made:
        durs = [e - s for s, e in groups]
        print(f"片段时长    : min {min(durs):.2f}s / max {max(durs):.2f}s / "
              f"合计 {sum(durs):.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
