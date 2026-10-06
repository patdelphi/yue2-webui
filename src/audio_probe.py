"""统一的音频时长探测工具（ffprobe）。

背景（代码审计 B1）：`voice_client` / `voice_ui_handlers` / `mix_render` 三处各有一份
近似重复的 `_probe_duration` 实现，ffprobe 输出参数与超时略有差异，维护成本高。
本模块收敛为单一实现，三处改为复用。

语义约定：
- 路径为空、文件不存在、ffprobe 不可用、进程返回非零或输出无法解析为数字 → 一律返回 0.0
  （调用方把 0.0 视为"未知时长"，不抛异常）。
- 传入 `logger` 时，解析失败会记录日志（不影响返回值）；不传则静默。
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def probe_duration(path, *, logger: Optional[logging.Logger] = None) -> float:
    """用 ffprobe 探测音频时长（秒）；任何失败都返回 0.0，绝不抛异常。

    参数：
        path   —— 音频文件路径（str / Path），空值或不存在直接返回 0.0。
        logger —— 可选；传入后解析失败会记录日志（沿用 exc_info），便于排查。
    """
    if not path:
        return 0.0
    p = Path(path)
    if not p.exists():
        return 0.0

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0

    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        text = (proc.stdout or "").strip()
        return float(text) if text else 0.0
    except Exception:
        if logger is not None:
            logger.exception("ffprobe 时长解析失败(已忽略): %s", p)
        return 0.0
