"""Configuration and parameter schema for YuE2 WebUI."""
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from i18n import tr


class CotMode(str, Enum):
    FULL = "full"
    MELODY = "melody"
    OFF = "off"


class OutFormat(str, Enum):
    PCM16 = "pcm16"
    PCM24 = "pcm24"
    FLOAT32 = "float32"


@dataclass
class SamplingParams:
    """Sampling parameters for ABC or semantic generation."""
    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 100
    repetition_penalty: float = 1.2
    penalty_window: int = 50
    min_tokens: int = 200
    max_tokens: int = 9000


@dataclass
class GenerationParams:
    """Complete generation parameters."""
    style: str = ""
    lyrics: str = ""
    cot: CotMode = CotMode.FULL
    seed: int = 831001
    id: str = "song"
    
    abc: Optional[str] = None
    cfg_scale: Optional[float] = None
    
    num_inference_steps: int = 8
    
    abc_sampling: SamplingParams = field(default_factory=lambda: SamplingParams(
        temperature=0.7, top_p=0.9, top_k=30,
        repetition_penalty=1.005, penalty_window=100,
        min_tokens=32, max_tokens=4096
    ))
    semantic_sampling: SamplingParams = field(default_factory=SamplingParams)

    # 注：模型路径/文件名由项目根外置 config.cfg 配置（backend_gguf.load_model_config），
    # 不再作为每次生成的参数。
    out_format: OutFormat = OutFormat.PCM16


@dataclass
class TranscriptionResult:
    """Result of audio-to-score transcription."""
    success: bool
    abc_score: Optional[str] = None
    midi_path: Optional[str] = None
    error_message: Optional[str] = None
    transcription_time_seconds: Optional[float] = None


def validate_params(params: GenerationParams, lang: str = "zh") -> Optional[str]:
    """Validate parameters, return error message if invalid."""
    if not params.style or not params.style.strip():
        return tr(lang, "风格描述不能为空")
    if not params.lyrics or not params.lyrics.strip():
        return tr(lang, "歌词不能为空")
    if params.seed is None or params.seed < 0:
        return tr(lang, "种子必须为非负整数")
    if params.cfg_scale is not None and (params.cfg_scale < 0 or params.cfg_scale > 20):
        return tr(lang, "CFG强度必须在0-20之间")
    if params.num_inference_steps is None or params.num_inference_steps < 1 or params.num_inference_steps > 64:
        return tr(lang, "ODE步数必须在1-64之间")
    return None
