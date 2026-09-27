# -*- coding: utf-8 -*-
"""翻唱音质 A/B 量化报告工具（P0 脚手架，离线命令行，不接入 UI、不改动现有流程）。

目的：给"电子音/金属感"这类主观听感找一把客观的尺子。对任意多个音频批量测量同一套
指标，输出既有可读打印、又有 CSV，供 P1~P4 各变体横向对比。

测量项（仅依赖系统 ffmpeg/ffprobe，无第三方 Python 依赖）：
1. 积分响度 LUFS、真峰值 dBTP、响度范围 LRA —— ffmpeg loudnorm 空输出取 JSON
2. 峰值 dBFS、RMS dBFS、削波代理（max_level / flat_factor / peak_count）—— ffmpeg astats
3. 谱质心 / 谱平坦度 / 谱滚降 的逐帧均值 —— ffmpeg aspectralstats
   （谱平坦度是"噪声感/金属感"的强代理：越接近噪声，值越高；纯谐波音接近 0）
4. 分带能量占比：>8k / >10k / >12k 相对全带 dB —— ffmpeg highpass + astats

轨级 A/B：混音级频谱会被伴奏掩盖，判断换嗓人声本身要用 --ref 做"源人声 vs 换嗓人声"
的轨级对比（脚本对每个变体打印与参考的逐项差值，并按方案判据标注 OK/超限）。

口径说明（重要，避免误读数值）：
- 分带能量用 2 极点 highpass 近似，截止点处有约 -3dB 滚降，故得到的是"同一把尺子下的
  近似占比"，只用于变体间横向比较（同一源曲、同一测量链），不是绝对频带能量。
- 谱质心取各帧算术平均，静音/极弱帧会拉低数值，同样只做相对比较。

用法：
    python tools/audio_ab_report.py outputs/quality_test_cover/qt_cover.flac
    python tools/audio_ab_report.py "源曲.mp3" outputs/ab_test/S1_steps100/cover.flac --out report.csv
    # 轨级 A/B：--ref 指定源人声，逐个变体打印与它的差值 + 判据
    python tools/audio_ab_report.py --ref "源人声.wav" "变体A/converted_vocals.wav" ...
"""
import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 分带能量测量的 highpass 截止频率（Hz）
BAND_CUTOFFS = (8000, 10000, 12000)
# 本脚本位于 <webui>/tools/ 下，上一级即 webui 根，用于给默认输出一个稳定位置
WEBUI_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = WEBUI_ROOT / "outputs" / "ab_test" / "ab_report.csv"

# CSV 列顺序（先辨识列，再响度/电平，再频谱，再削波代理，最后文件信息与路径）
CSV_FIELDS = [
    "label", "name",
    "lufs", "true_peak_dbtp", "lra_lu",
    "rms_dbfs", "peak_dbfs", "max_level",
    "centroid_hz", "flatness_mean", "rolloff_hz",
    "band_gt8k_db", "band_gt10k_db", "band_gt12k_db",
    "flat_factor", "peak_count",
    "duration_sec", "sample_rate", "channels", "codec", "bit_depth",
    "path",
]

# --ref 轨级对比的判据（来源：Docs/optimization-plan-cover-quality.md 第 0.4 节完成标准）
# 每项为 (指标键, 显示名, 单位, 容差)：对比与参考的绝对差，超容差标"超限"
REF_THRESHOLDS = [
    ("lufs", "积分响度", "LU", 1.0),
    ("band_gt10k_db", ">10k 分带占比", "dB", 1.5),
]


# ---------------------------------------------------------------- 基础工具
def _f(v):
    """把 ffmpeg 输出的字符串数值转 float；None/空/'inf'/'-inf'/NaN 一律返回 None。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isinf(f) or math.isnan(f) else f


def _run_af(path: str, af: str) -> str:
    """跑一遍 `ffmpeg -i path -af af -f null -`，返回 stderr（滤镜统计输出都在这里）。

    处理链固定前置 `aformat=sample_fmts=flt`：整数 PCM（如 24bit flac 的 s32）下
    astats 的 "Max level" 报的是原始采样值（实测 24bit 满刻度约 2.03e9），跨格式
    不可比；统一转 float 后满刻度恒为 1.0，dB 类指标不受影响（本就按满刻度归一）。

    显式 -map 0:a:0 只取第一条音频流：源曲 mp3 常带封面图（mjpeg）流，
    若不指定会把封面当成一路输入干扰滤镜统计。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请先安装并加入 PATH")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-i", path,
         "-map", "0:a:0", "-af", "aformat=sample_fmts=flt," + af, "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 执行失败(rc={proc.returncode}): {proc.stderr[-300:]}")
    return proc.stderr


def _astats_overall(err: str) -> str:
    """截取 astats 输出里的 Overall 段（measure_perchannel=0 时通常只有这一段）。"""
    idx = err.rfind("] Overall")
    return err[idx:] if idx >= 0 else err


def _astats_key(err: str, label: str):
    """从 astats 输出取指定标签的数值（如 'RMS level dB'）。取 Overall 段内末次出现。"""
    vals = re.findall(rf"{re.escape(label)}:\s*(-?\d+(?:\.\d+)?)", _astats_overall(err))
    return _f(vals[-1]) if vals else None


# ---------------------------------------------------------------- 各项测量
def probe(path: str) -> dict:
    """用 ffprobe 读时长/采样率/声道/编码/位深。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("未找到 ffprobe，请先安装并加入 PATH")
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_entries",
         "stream=sample_rate,channels,codec_name,bits_per_raw_sample,bits_per_sample",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 执行失败(rc={proc.returncode})")
    data = json.loads(proc.stdout or "{}")
    st = (data.get("streams") or [{}])[0]
    return {
        "duration_sec": _f((data.get("format") or {}).get("duration")),
        "sample_rate": _f(st.get("sample_rate")),
        "channels": st.get("channels"),
        "codec": st.get("codec_name"),
        # flac/wav 用 bits_per_raw_sample；部分容器只有 bits_per_sample（0 表示未知）
        "bit_depth": _f(st.get("bits_per_raw_sample")) or _f(st.get("bits_per_sample")),
    }


def loudness(path: str) -> dict:
    """loudnorm 一遍取积分响度/真峰值/响度范围（BS.1770 口径，含响度门限）。"""
    err = _run_af(path, "loudnorm=print_format=json")
    # JSON 块在 stderr 末尾；用 input_i 作为锚点定位该块再解析
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        raise RuntimeError("loudnorm 未输出 JSON 统计")
    d = json.loads(m.group(0))
    return {
        "lufs": _f(d.get("input_i")),
        "true_peak_dbtp": _f(d.get("input_tp")),
        "lra_lu": _f(d.get("input_lra")),
    }


def astats(path: str) -> dict:
    """astats 取整段 Overall 统计：峰值/RMS 与削波代理字段。"""
    err = _run_af(
        path,
        "astats=measure_perchannel=0:"
        "measure_overall=Max_level+Peak_level+RMS_level+Flat_factor+Peak_count",
    )
    return {
        "max_level": _astats_key(err, "Max level"),
        "peak_dbfs": _astats_key(err, "Peak level dB"),
        "rms_dbfs": _astats_key(err, "RMS level dB"),
        "flat_factor": _astats_key(err, "Flat factor"),
        "peak_count": _astats_key(err, "Peak count"),
    }


def spectral_stats(path: str) -> dict:
    """aspectralstats 一遍取谱质心/谱平坦度/谱滚降的逐帧均值。

    谱平坦度（flatness，0~1）是噪声感的强代理：纯谐波/周期性信号接近 0，
    含大量非谐波成分（合成毛刺、金属感）时明显抬升，是判断"电音感"的主指标。
    逐帧取均值；静音/无效帧（<=0）剔除，避免拉低数值。
    """
    measures = ("centroid", "flatness", "rolloff")
    keys = ",".join(
        f"ametadata=mode=print:key=lavfi.aspectralstats.1.{m}" for m in measures
    )
    err = _run_af(path, f"aspectralstats=measure={'+'.join(measures)},{keys}")
    out = {}
    for m in measures:
        vals = [float(v) for v in re.findall(rf"\.{m}=(-?\d+(?:\.\d+)?)", err)]
        vals = [v for v in vals if v > 0]
        out[{"centroid": "centroid_hz", "flatness": "flatness_mean",
             "rolloff": "rolloff_hz"}[m]] = sum(vals) / len(vals) if vals else None
    return out


def band_ratio_db(path: str, cutoff: int, rms_full):
    """>cutoff Hz 分带能量相对全带的 dB 差（近似口径，见模块说明）。"""
    err = _run_af(
        path,
        f"highpass=f={cutoff}:poles=2,"
        "astats=measure_perchannel=0:measure_overall=RMS_level",
    )
    rms_band = _astats_key(err, "RMS level dB")
    if rms_band is None or rms_full is None:
        return None
    return rms_band - rms_full


def measure(path: str) -> dict:
    """测量单个文件的全部指标；单项失败只记日志并把该组字段置空，不中断整份报告。"""
    row = {"label": Path(path).parent.name, "name": Path(path).name, "path": str(path)}
    try:
        row.update(probe(path))
    except Exception as e:
        print(f"  [警告] ffprobe 读取失败: {e}")
    try:
        row.update(loudness(path))
    except Exception as e:
        print(f"  [警告] 响度测量失败: {e}")
    try:
        a = astats(path)
        row.update(a)
        # 分带占比依赖全带 RMS：该项失败则分带一并置空（分母缺失，比值无意义）
        for cutoff in BAND_CUTOFFS:
            row[f"band_gt{cutoff // 1000}k_db"] = band_ratio_db(path, cutoff, a["rms_dbfs"])
    except Exception as e:
        print(f"  [警告] 电平/分带测量失败: {e}")
    try:
        row.update(spectral_stats(path))
    except Exception as e:
        print(f"  [警告] 频谱统计测量失败: {e}")
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()}


# ---------------------------------------------------------------- 输出
def _num(v, nd: int = 1) -> str:
    """数值格式化：None → 'N/A'。"""
    return "N/A" if v is None else f"{v:.{nd}f}"


def print_block(r: dict) -> None:
    """按文件打印一段可读指标块（竖排，规避中英文混排的列宽对齐问题）。"""
    print(f"\n=== {r.get('name')}  [label: {r.get('label')}] ===")
    print(f"  时长/格式 : {_num(r.get('duration_sec'))} s   "
          f"{r.get('sample_rate') or '?'} Hz / {r.get('channels') or '?'} ch   "
          f"{r.get('codec') or '?'}   {r.get('bit_depth') or '?'} bit")
    print(f"  响度      : {_num(r.get('lufs'))} LUFS   "
          f"真峰值 {_num(r.get('true_peak_dbtp'))} dBTP   "
          f"LRA {_num(r.get('lra_lu'))} LU")
    print(f"  电平      : RMS {_num(r.get('rms_dbfs'))} dBFS   "
          f"峰值 {_num(r.get('peak_dbfs'))} dBFS   "
          f"最大采样 {_num(r.get('max_level'), 4)}")
    print(f"  谱质心    : {_num(r.get('centroid_hz'), 0)} Hz   "
          f"谱滚降 {_num(r.get('rolloff_hz'), 0)} Hz   "
          f"谱平坦度 {_num(r.get('flatness_mean'), 5)}")
    print(f"  分带占比  : >8k {_num(r.get('band_gt8k_db'))} dB   "
          f">10k {_num(r.get('band_gt10k_db'))} dB   "
          f">12k {_num(r.get('band_gt12k_db'))} dB")
    print(f"  削波代理  : flat_factor {_num(r.get('flat_factor'), 3)}   "
          f"peak_count {_num(r.get('peak_count'), 0)}")


def write_csv(rows: list, out_path: str) -> None:
    """写 CSV：utf-8-sig 带 BOM，csv 模块默认 \\r\\n 行尾（符合项目文本文件约定）。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, restval="")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in CSV_FIELDS})


def compare_block(ref: dict, var: dict) -> None:
    """打印变体相对参考（--ref，如源人声）的逐项差值，判据项自动标注 OK/超限。

    这是"轨级 A/B"的主要用法：源人声 vs 换嗓后人声，方向含义如下 ——
    谱平坦度升高 = 更像噪声（金属感变重）；>10k 占比升高 = 高频更亮。
    """
    print(f"\n--- 变体 {var.get('name')} [{var.get('label')}]  VS  参考 {ref.get('name')} ---")
    for key, name, unit, tol in REF_THRESHOLDS:
        rv, vv = ref.get(key), var.get(key)
        if rv is None or vv is None:
            print(f"  {name}: N/A（参考或变体缺测）")
            continue
        d = vv - rv
        print(f"  {name}: {vv} vs {rv}   Δ {d:+.2f} {unit}"
              f"   [判据 |Δ|≤{tol} {unit}] {'OK' if abs(d) <= tol else '超限'}")
    # 非判据项只报差值供人工判断方向（无阈值：方向性指标，需结合听感）
    for key, name, unit, nd in (
        ("lra_lu", "响度范围 LRA", "LU", 2),
        ("true_peak_dbtp", "真峰值", "dBTP", 2),
        ("centroid_hz", "谱质心", "Hz", 0),
        ("flatness_mean", "谱平坦度", "", 5),
        ("band_gt8k_db", ">8k 分带占比", "dB", 2),
        ("band_gt12k_db", ">12k 分带占比", "dB", 2),
    ):
        rv, vv = ref.get(key), var.get(key)
        if rv is None or vv is None:
            continue
        print(f"  {name}: {_num(vv, nd)} vs {_num(rv, nd)}   Δ {vv - rv:+.{nd}f} {unit}")


# ---------------------------------------------------------------- 入口
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="翻唱音质 A/B 量化报告（P0 脚手架）")
    ap.add_argument("inputs", nargs="+", help="待测音频文件（可多个，支持中文/空格路径）")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"CSV 输出路径（默认 {DEFAULT_OUT}）")
    ap.add_argument("--ref", default="",
                    help="轨级对比的参考文件（如源人声）；给定时逐个变体打印差值")
    args = ap.parse_args(argv)

    for exe in ("ffmpeg", "ffprobe"):
        if not shutil.which(exe):
            print(f"错误：未找到 {exe}，请先安装并加入 PATH")
            return 2

    rows = []
    ref_row = None
    if args.ref:
        if not Path(args.ref).is_file():
            print(f"错误：--ref 文件不存在: {args.ref}")
            return 2
        print(f"\n测量参考: {args.ref}")
        ref_row = measure(args.ref)
        rows.append(ref_row)
        print_block(ref_row)

    for p in args.inputs:
        if not Path(p).is_file():
            print(f"跳过（文件不存在）: {p}")
            continue
        print(f"\n测量: {p}")
        row = measure(p)
        rows.append(row)
        print_block(row)
        if ref_row is not None:
            compare_block(ref_row, row)

    if rows:
        write_csv(rows, args.out)
        print(f"\nCSV 已写入: {args.out}（{len(rows)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
