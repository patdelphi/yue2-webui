# -*- coding: utf-8 -*-
"""多轨编辑器 P2（压缩 / 回声）听感盲测生成器。

用途：把 P2 的参数梯度做成一组"盲编码"音频，交给人耳试听后再揭盲。
音频走产线同一条渲染链路（mix_render.build_ffmpeg_cmd，含每轨音量/音质/压缩/回声
+ 总线 loudnorm），但**不写历史记录**，避免污染歌曲历史。

盲测设计：
- 源：指定分离任务目录下的 人声 + 伴奏 两轨，截取"最响的 N 秒"（近似副歌）。
- P2 参数只加在**人声轨**上（伴奏保持中性），符合 DAW 常规用法。
- 8 个变体：压缩梯度 4 档（含关闭基线）、回声梯度 3 档、组合 1 档。
- 用固定随机种子把 参数组合 映射到 V1..V8，映射只写进"揭盲答案"文件，不打印到终端。

用法：
    python tools/p2_blind_test.py --src-dir separations_20260926_074016 --seconds 30
"""
import argparse
import array
import itertools
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 复用产线代码（本脚本在 <webui>/tools/ 下）
WEBUI_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEBUI_ROOT / "src"))

from mix_render import build_ffmpeg_cmd, parse_mix_project  # noqa: E402

# 盲测变体表：(内部编码, 人话说明, 加在人声轨上的 P2 参数)
# 内部编码仅用于揭盲答案，音频文件名一律用盲编码 V1..Vn。
VARIANTS = [
    ("C0", "压缩关闭（基线）", {}),
    ("C1", "轻压缩：阈值 -18dB / 2:1", {"comp_th": -18.0, "comp_ratio": 2.0}),
    ("C2", "中压缩：阈值 -24dB / 4:1", {"comp_th": -24.0, "comp_ratio": 4.0}),
    ("C3", "重压缩：阈值 -30dB / 8:1", {"comp_th": -30.0, "comp_ratio": 8.0}),
    ("E1", "轻回声：混合 0.25 / 反馈 0.30 / 延迟 250ms",
     {"echo_mix": 0.25, "echo_fb": 0.30, "echo_delay": 250.0}),
    ("E2", "中回声：混合 0.40 / 反馈 0.50 / 延迟 300ms",
     {"echo_mix": 0.40, "echo_fb": 0.50, "echo_delay": 300.0}),
    ("E3", "重回声：混合 0.60 / 反馈 0.60 / 延迟 400ms",
     {"echo_mix": 0.60, "echo_fb": 0.60, "echo_delay": 400.0}),
    ("X1", "中压缩 + 中回声",
     {"comp_th": -24.0, "comp_ratio": 4.0,
      "echo_mix": 0.40, "echo_fb": 0.50, "echo_delay": 300.0}),
]

BLIND_SEED = 20260929          # 固定种子：盲编码可复现，且不随时间变化
FADE = 0.5                     # 片段首尾淡变（秒），避免硬切爆音

# 副歌估计用的下采样参数（只做包络分析，8kHz 足够）
ENV_SR = 4000
ENV_HOP = 0.5


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("未找到 ffmpeg，请先安装并加入 PATH")
    return exe


def _probe_duration(path: Path) -> float:
    """用 ffprobe 取时长（秒）。"""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True).stdout.strip()
    return float(out)


def _decode_env(path: Path):
    """解码为单声道 4kHz PCM，用于找"最响的片段"。"""
    raw = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", str(ENV_SR), "-f", "s16le", "-"],
        capture_output=True).stdout
    pcm = array.array("h")
    pcm.frombytes(raw[: len(raw) // 2 * 2])
    return pcm


def best_window(path: Path, seconds: float) -> float:
    """返回"录音内能量最高"的 seconds 秒窗口起点（秒），按 ENV_HOP 取整。

    用滑动窗口平方和找副歌：副歌的伴奏最满、电平最高，比按时间硬切更稳。
    """
    pcm = _decode_env(path)
    n = len(pcm)
    win = int(seconds * ENV_SR)
    if n <= win:
        return 0.0
    prefix = [0.0]
    prefix.extend(itertools.accumulate((float(v) * v for v in pcm)))
    step = max(1, int(ENV_HOP * ENV_SR))
    best_sum, best_start = -1.0, 0
    for start in range(0, n - win + 1, step):
        s = prefix[start + win] - prefix[start]
        if s > best_sum:
            best_sum, best_start = s, start
    return round(best_start / ENV_SR / ENV_HOP) * ENV_HOP


def assign_blind_codes(seed: int = BLIND_SEED):
    """把变体映射到盲编码 V1..Vn（固定种子打乱，保证可复现且顺序无规律）。"""
    order = list(range(len(VARIANTS)))
    random.Random(seed).shuffle(order)
    return {f"V{i + 1}": VARIANTS[idx] for i, idx in enumerate(order)}


def build_project(src_dir: Path, win: float, seconds: float, p2: dict) -> dict:
    """构造混音工程：人声/伴奏同截 [win, win+seconds)，P2 参数只加在人声轨。"""
    tracks = []
    for kind, label in (("vocals", "人声"), ("accompaniment", "伴奏")):
        f = next((p for p in sorted(src_dir.glob("*.wav"))
                  if p.stem.endswith("_" + kind)), None)
        if f is None:
            raise RuntimeError(f"{src_dir} 下找不到 {kind} 轨")
        track = {
            "id": kind, "name": label,
            "src": f"outputs/{src_dir.name}/{f.name}",
            "gain_db": 0.0, "mute": False,
            "clips": [{"start": 0.0, "in": win, "out": win + seconds,
                       "fade_in": FADE, "fade_out": FADE}],
        }
        if kind == "vocals":
            track.update(p2)                    # 压缩/回声只作用于人声轨
        tracks.append(track)
    return {"version": 1, "sample_rate": 48000, "duration": seconds,
            "source_root_task_id": src_dir.name, "tracks": tracks}


def render_one(payload: dict, out_path: Path) -> float:
    """按工程渲染一个变体，返回耗时（秒）。"""
    project = parse_mix_project(payload, WEBUI_ROOT)
    cmd = build_ffmpeg_cmd(project, out_path)
    cmd[0] = _ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"渲染失败 {out_path.name}: {proc.stderr[-500:]}")
    return time.time() - t0


def write_text(path: Path, text: str) -> None:
    """按项目约定写文本：UTF-8 BOM + CRLF。"""
    path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))


def make_guide(path: Path, src_name: str, seconds: float, win: float, codes: list) -> None:
    """盲听指南（不含揭盲映射，可边听边看）。"""
    rows = "\n".join(
        f"| {c} | ☐ | ☐ | ☐ | ☐ | ☐ |" for c in codes)
    lines = f"""# P2（压缩 / 回声）听感盲测 — 盲听指南

> 本轮测什么：多轨编辑器 P2 新增的**每轨压缩**与**每轨回声**，在什么参数下听感可接受。
> 盲测原则：音频文件名（V1…V{len(codes)}）与参数**随机对应**，映射在《揭盲答案_听完再看.md》里，
> **请先听完并打分，再打开答案文件**。

## 一、测试素材

- 源：`{src_name}`（人声 + 伴奏两轨，产线同一条混音链路渲染）
- 片段：源曲能量最高的 {seconds:.0f}s（近似副歌），起点 {win:.1f}s，首尾各 {FADE}s 淡变
- 参数只作用在**人声轨**，伴奏保持中性；成品经总线 loudnorm 归一（-14 LUFS / -1.5 dBTP）

## 二、试听方法

1. 全程用**同一副耳机/音箱、同一音量**，音量在 V1 上定好后不再调整。
2. 严格按 V1 → V{len(codes)} 顺序听一遍（不要跳序、不要反复 AB 同一对）。
3. 每听完一个，立刻凭第一印象打 1–5 分，不要回改。
4. 全部听完后，可再回头做 2~3 次两两对比补分。
5. 打分完成后打开《揭盲答案_听完再看.md》对号入座。

## 三、评分表（1=很差，5=很好）

| 变体 | 自然度 | 人声清晰度 | 力度/厚度 | 空间感(回声) | 整体喜好 |
| --- | --- | --- | --- | --- | --- |
{rows}

补充项（可文字记录）：

- 哪个变体听起来"最闷"或"最糊"？______
- 哪个变体人声最容易听出被压扁 / 忽大忽小（抽吸感）？______
- 哪个变体回声最自然？哪个明显"拖泥带水"？______
- 有没有变体出现可听的失真 / 削波 / 金属感？______
- 最优变体编号：______（若 V1–V{len(codes)} 都不满意，请说明期望方向）

## 四、结果怎么用

- 打分与客观指标（`ab_report.csv`：LUFS / 真峰值 / LRA / 谱质心 / 谱平坦度 / 分带占比）
  对照，若主观"重压缩"最优但 LRA 掉得厉害，就把 UI 默认值定在相邻轻档。
- 结论会用来定 P2 的**默认参数**与**提示文案**（例如回声默认混合值）。
"""
    write_text(path, lines)


def make_key(path: Path, mapping: dict, meta: dict) -> None:
    """揭盲答案（含参数映射与客观指标提示），听完再看。"""
    rows = "\n".join(
        f"| {code} | {v[0]} | {v[1]} | {json.dumps(v[2], ensure_ascii=False)} |"
        for code, v in mapping.items())
    lines = f"""# P2 听感盲测 — 揭盲答案（听完再打开）

> 如果你还没开始听，请先关闭本文件，打开《盲听指南.md》。

- 源目录：`{meta['src_dir']}`
- 片段：起点 {meta['win']:.1f}s，时长 {meta['seconds']:.0f}s，首尾淡变 {FADE}s
- 盲编码种子：{BLIND_SEED}（可复现）
- 渲染：产线 `mix_render.build_ffmpeg_cmd`，不含历史记录写入

## 映射表

| 盲编码 | 内部编码 | 参数说明 | P2 参数（加在人声轨） |
| --- | --- | --- | --- |
{rows}

## 解读提示

- 压缩档位看 **LRA（响度范围）**：档位越重 LRA 越低，人声越"平"；LRA 掉太多通常就是听感变糊的原因。
- 回声档位看 **分带占比 / 谱质心**：回声带来重复能量，会让谱质心与高频占比轻微变化。
- `ab_report.csv` 里所有变体的 LUFS / 真峰值应基本一致（总线 loudnorm 已归一），
  若差异明显说明该变体触发了 loudnorm 的动态调整，需要单独留意。
"""
    write_text(path, lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P2（压缩/回声）听感盲测生成器")
    ap.add_argument("--src-dir", required=True,
                    help="outputs/ 下的分离任务目录名，如 separations_20260926_074016")
    ap.add_argument("--seconds", type=float, default=30.0, help="片段时长（秒，默认 30）")
    ap.add_argument("--out-dir", default="outputs/p2_blind_test", help="产物目录")
    ap.add_argument("--seed", type=int, default=BLIND_SEED, help="盲编码打乱种子")
    args = ap.parse_args(argv)

    src_dir = WEBUI_ROOT / "outputs" / args.src_dir
    if not src_dir.is_dir():
        print(f"错误：源目录不存在: {src_dir}")
        return 2
    out_dir = WEBUI_ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    t_all = time.time()
    ref = next((p for p in sorted(src_dir.glob("*_accompaniment.wav"))), None)
    if ref is None:
        print(f"错误：{src_dir} 下找不到伴奏轨，无法估计副歌位置")
        return 2
    dur = _probe_duration(ref)
    seconds = min(args.seconds, dur)
    win = best_window(ref, seconds)
    print(f"源: {src_dir.name} 时长 {dur:.1f}s → 片段起点 {win:.1f}s 时长 {seconds:.0f}s")

    mapping = assign_blind_codes(args.seed)
    used = {}
    # 变体参数不得重复，否则盲测会出现两个完全相同的文件
    for code, v in mapping.items():
        sig = json.dumps(v[2], sort_keys=True)
        if sig in used:
            print(f"错误：变体 {code}({v[0]}) 与 {used[sig]} 参数相同")
            return 2
        used[sig] = code

    codes = sorted(mapping)
    for code in codes:
        v = mapping[code]
        payload = build_project(src_dir, win, seconds, v[2])
        out = out_dir / f"{code}.flac"
        took = render_one(payload, out)
        print(f"  {code} 渲染完成 {took:.1f}s → {out.relative_to(WEBUI_ROOT)}")

    make_guide(out_dir / "盲听指南.md", src_dir.name, seconds, win, codes)
    make_key(out_dir / "揭盲答案_听完再看.md", mapping,
             {"src_dir": src_dir.name, "win": win, "seconds": seconds})
    print(f"\n产物目录: {out_dir}")
    print(f"  {len(codes)} 个音频 + 盲听指南.md + 揭盲答案_听完再看.md")
    print(f"总耗时 {time.time() - t_all:.1f}s（映射已写入答案文件，未打印到终端）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
