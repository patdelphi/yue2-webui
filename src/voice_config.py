"""音色工坊（音轨分离 + 参考音色翻唱）的配置解析。

读取 yue2-webui 目录下外置 config.cfg 的 [voice] 段，返回语音工具的运行时配置。
规则与 backend_gguf.load_model_config 保持一致：
- cfg 不存在或解析异常 / 段落缺失时回退默认值（不抛异常，保证可启动）
- 路径类配置支持相对（基于 Yue2 系统根）与绝对路径
"""
import configparser
import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# 默认语音工具配置（与 config.cfg [voice] 段对应）
DEFAULT_VOICE_CONFIG = {
    "enabled": "true",          # 功能总开关；false 时主 app 隐藏音色工坊 Tab 并拒收相关任务
    "worker_port": "8190",      # worker 起始端口，被占用时自动 +1 重试
    "seedvc_dir": "",           # Seed-VC 源码目录（空 = 未安装，UI 显示安装指引）
    "max_input_minutes": "8",   # 输入音频时长上限（分钟），超限拒绝并提示
    # --- 音色工坊算法参数（空/缺省时回退默认） ---
    "denoise_strength": "",     # ffmpeg anlmdn 降噪强度（s= 参数）；空 = 用 ffmpeg 默认
    "voice_detect_threshold": "0.5",    # 人声检测判定阈值
    "detect_sample_rate": "16000",      # 人声检测采样率（Hz）
    "detect_frame_len": "2048",         # 人声检测帧长（点）
    "voice_band": "80-1000",            # 人声频带（Hz），格式 "低-高"
    "detect_seconds": "30",             # 人声检测最长时长（秒）
}


def load_voice_config(project_root: Path) -> dict:
    """读取 config.cfg 的 [voice] 段，返回语音工具配置。

    返回键: enabled(bool) / worker_port(int) / seedvc_dir(Path|None) / max_input_minutes(int)
            denoise_strength(float|None) / voice_detect_threshold(float)
            detect_sample_rate(int) / detect_frame_len(int) / voice_band((int,int))
            detect_seconds(float)
    """
    cfg_path = Path(project_root) / "yue2-webui" / "config.cfg"
    values = dict(DEFAULT_VOICE_CONFIG)
    if cfg_path.exists():
        try:
            parser = configparser.ConfigParser()
            # 显式 utf-8 读取，兼容带中文注释的 cfg
            with open(cfg_path, "r", encoding="utf-8") as f:
                parser.read_file(f)
            if parser.has_section("voice"):
                for key in values:
                    if parser.has_option("voice", key):
                        values[key] = parser.get("voice", key).strip()
        except Exception as e:
            # cfg 损坏不阻断启动，回退默认并记录警告
            logger.warning(f"config.cfg [voice] 段解析失败，使用默认语音配置: {e}")
    return _normalize_values(values, project_root)


def _normalize_values(values: dict, project_root: Path) -> dict:
    """将字符串配置规范化为强类型返回。"""
    def _bool(v: str) -> bool:
        return str(v).strip().lower() not in ("0", "false", "no", "off", "")

    def _int(v: str, default: int) -> int:
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return default

    def _float(v: str, default: float) -> float:
        try:
            return float(str(v).strip())
        except (TypeError, ValueError):
            return default

    def _band(v: str) -> tuple:
        # 解析 "低-高" 频带；lo>hi 时交换（避免 band_mask 为空致 voice_ratio 恒 0），
        # 缺失/非法时回退默认 (80, 1000)
        try:
            lo, hi = str(v).strip().split("-")
            lo, hi = int(float(lo)), int(float(hi))
            if lo > hi:
                lo, hi = hi, lo
            return (lo, hi)
        except (ValueError, TypeError):
            return (80, 1000)

    seedvc_raw = str(values["seedvc_dir"] or "").strip()
    seedvc_dir: Union[Path, None] = None
    if seedvc_raw:
        p = Path(seedvc_raw)
        if not p.is_absolute():
            p = Path(project_root) / p
        # 目录不存在 / 非目录时置 None，交由 UI 做安装指引判断
        if p.is_dir():
            seedvc_dir = p
    else:
        # 留空时自动探测：优先 ${系统根}/seed-vc，回退 ${系统根}/yue2-webui/voice-tools/seed-vc
        auto_candidates = [
            Path(project_root) / "seed-vc",
            Path(project_root) / "yue2-webui" / "voice-tools" / "seed-vc",
        ]
        for p in auto_candidates:
            if p.is_dir():
                seedvc_dir = p
                logger.info(f"seedvc_dir 自动探测到: {p}")
                break

    # 降噪强度：空串（默认）表示为 None，由 worker 使用 ffmpeg anlmdn 默认值
    dn = str(values["denoise_strength"] or "").strip() or ""
    denoise_strength = _float(dn, None) if dn else None

    return {
        "enabled": _bool(values["enabled"]),
        "worker_port": _int(values["worker_port"], 8190),
        "seedvc_dir": seedvc_dir,
        "max_input_minutes": _int(values["max_input_minutes"], 8),
        "denoise_strength": denoise_strength,
        "voice_detect_threshold": _float(values["voice_detect_threshold"], 0.5),
        "detect_sample_rate": _int(values["detect_sample_rate"], 16000),
        "detect_frame_len": _int(values["detect_frame_len"], 2048),
        "voice_band": _band(values["voice_band"]),
        "detect_seconds": _float(values["detect_seconds"], 30.0),
    }