"""GGUF backend using audio.cpp CLI."""
import subprocess
import threading
import time
import logging
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass

from config import GenerationParams, CotMode, OutFormat

logger = logging.getLogger(__name__)


@dataclass
class GenerationResult:
    """Result of a generation."""
    success: bool
    audio_path: Optional[str] = None
    audio_duration_seconds: Optional[float] = None
    generation_time_seconds: Optional[float] = None
    error_message: Optional[str] = None
    abc_score: Optional[str] = None
    mp3_path: Optional[str] = None


class LogParser:
    """Parse audiocpp_cli --log output for progress."""
    
    def __init__(self):
        self.current_phase = ""
        self.phase_progress = 0.0
    
    def parse_line(self, line: str) -> Optional[dict]:
        """Parse a log line, return progress update if found."""
        line = line.strip()
        if not line:
            return None
        
        progress = None
        
        if "Loading model" in line or "model loaded" in line.lower():
            progress = {"phase": "loading", "message": "加载模型..."}
        elif "Planning" in line or "plan" in line.lower():
            self.current_phase = "planning"
            progress = {"phase": "planning", "message": "规划乐谱..."}
        elif "Generating" in line or "semantic" in line.lower():
            self.current_phase = "generating"
            progress = {"phase": "generating", "message": "生成音乐..."}
        elif "Synthesizing" in line or "NAR" in line or "flow" in line.lower():
            self.current_phase = "synthesizing"
            progress = {"phase": "synthesizing", "message": "合成音频..."}
        elif "Decoding" in line or "VAE" in line or "decode" in line.lower():
            self.current_phase = "decoding"
            progress = {"phase": "decoding", "message": "解码音频..."}
        elif "Total:" in line or "completed" in line.lower():
            progress = {"phase": "done", "message": "完成"}
        
        return progress


class GGUFBackend:
    """audio.cpp GGUF backend."""
    
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.cli_path = self.project_root / "audio-cpp" / "audiocpp_cli.exe"
        self.model_dir = self.project_root / "models"
        self._current_process: Optional[subprocess.Popen] = None
    
    def build_command(self, params: GenerationParams, output_dir: Path) -> list[str]:
        """Build CLI command from parameters."""
        cmd = [
            str(self.cli_path),
            "--task", "gen",
            "--family", "yue2",
            "--model", str(self.model_dir / params.model_gguf),
            "--backend", "cuda",
            "--threads", "8",
        ]
        
        cmd.extend(["--text", params.lyrics])
        
        cmd.extend(["--request-option", f"style={params.style}"])
        cmd.extend(["--request-option", f"cot={params.cot.value}"])
        cmd.extend(["--request-option", f"seed={params.seed}"])
        cmd.extend(["--request-option", f"num_inference_steps={params.num_inference_steps}"])
        
        if params.cfg_scale is not None:
            cmd.extend(["--request-option", f"guidance_scale={params.cfg_scale}"])
        
        if params.abc is not None:
            cmd.extend(["--request-option", f"abc={params.abc}"])

        from config import GenerationParams as GP
        abc_defaults = GP.__dataclass_fields__["abc_sampling"].default_factory()
        for key in ["temperature", "top_p", "top_k", "repetition_penalty", "penalty_window", "min_tokens", "max_tokens"]:
            val = getattr(params.abc_sampling, key)
            if val != getattr(abc_defaults, key):
                cmd.extend(["--request-option", f"abc_{key}={val}"])

        sem_defaults = GP.__dataclass_fields__["semantic_sampling"].default_factory()
        for key in ["temperature", "top_p", "top_k", "repetition_penalty", "penalty_window", "min_tokens", "max_tokens"]:
            val = getattr(params.semantic_sampling, key)
            if val != getattr(sem_defaults, key):
                cmd.extend(["--request-option", f"semantic_{key}={val}"])
        
        cmd.extend(["--session-option", f"yue2.model_gguf={params.model_gguf}"])
        cmd.extend(["--session-option", f"yue2.vae_gguf={params.vae_gguf}"])

        if params.out_format != OutFormat.PCM16:
            cmd.extend(["--out-format", params.out_format.value])

        output_path = output_dir / f"{output_dir.name}.wav"
        cmd.extend(["--out", str(output_path)])
        cmd.extend(["--out-dir", str(output_dir)])
        cmd.append("--log")
        
        return cmd
    
    def generate(self, params: GenerationParams, output_dir: Path,
                 on_progress: Optional[Callable[[dict], None]] = None,
                 cancel_event: Optional[threading.Event] = None) -> GenerationResult:
        """Execute generation."""
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{output_dir.name}.wav"
        cmd = self.build_command(params, output_dir)
        
        start_time = time.time()
        
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.project_root),
            )
            
            self._current_process = process
            parser = LogParser()
            
            for line in process.stdout:
                if cancel_event and cancel_event.is_set():
                    process.kill()
                    return GenerationResult(success=False, error_message="已取消")
                
                progress = parser.parse_line(line)
                if progress and on_progress:
                    on_progress(progress)
            
            process.wait()
            elapsed = time.time() - start_time
            
            if process.returncode != 0:
                logger.error(f"audiocpp_cli failed with exit code {process.returncode}")
                return GenerationResult(
                    success=False,
                    error_message=f"audiocpp_cli 退出码 {process.returncode}",
                    generation_time_seconds=elapsed,
                )
            
            if not output_path.exists():
                logger.error("Output file not found after generation")
                return GenerationResult(
                    success=False,
                    error_message="生成完成但输出文件不存在",
                    generation_time_seconds=elapsed,
                )
            
            audio_duration = self._get_audio_duration(output_path)
            
            abc_score = None
            abc_name = f"{output_dir.name}.abc"
            abc_path = output_dir / abc_name
            legacy_abc = output_dir / "score.abc"
            if not abc_path.exists() and legacy_abc.exists():
                legacy_abc.rename(abc_path)
            if abc_path.exists():
                abc_score = abc_path.read_text(encoding="utf-8")
            
            mp3_path = self.export_mp3(output_path)
            
            return GenerationResult(
                success=True,
                audio_path=str(output_path),
                audio_duration_seconds=audio_duration,
                generation_time_seconds=elapsed,
                abc_score=abc_score,
                mp3_path=str(mp3_path) if mp3_path else None,
            )
            
        except Exception as e:
            logger.exception(f"Generation failed: {e}")
            return GenerationResult(success=False, error_message=str(e))
        finally:
            self._current_process = None
    
    def cancel(self):
        """Cancel current generation."""
        if self._current_process:
            self._current_process.kill()
    
    def _get_audio_duration(self, path: Path) -> float:
        """Get WAV file duration in seconds."""
        import soundfile
        info = soundfile.info(str(path))
        return info.frames / info.samplerate

    def export_mp3(self, wav_path: Path) -> Optional[Path]:
        """Convert WAV to MP3 using ffmpeg. Returns MP3 path or None on failure."""
        import shutil
        mp3_path = wav_path.with_suffix(".mp3")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.debug("ffmpeg not found, skipping MP3 export")
            return None
        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", str(wav_path), "-codec:a", "libmp3lame", "-qscale:a", "2", str(mp3_path)],
                capture_output=True, timeout=120,
            )
            if mp3_path.exists():
                return mp3_path
            logger.warning("MP3 export completed but file not found")
            return None
        except Exception as e:
            logger.warning(f"MP3 export failed: {e}")
            return None
    
    def re_export_mp3(self, wav_path: Path) -> Optional[Path]:
        """Re-export WAV to MP3 (e.g. after post-processing). Returns MP3 path or None."""
        mp3_path = wav_path.with_suffix(".mp3")
        if mp3_path.exists():
            mp3_path.unlink()
        return self.export_mp3(wav_path)

    def check_models(self) -> dict:
        """Check if model files exist."""
        model_path = self.model_dir / "yue2-3b-q8_0.gguf"
        vae_path = self.model_dir / "yue2-vae-f16.gguf"
        
        return {
            "available": model_path.exists() and vae_path.exists(),
            "model_gguf": {"path": str(model_path), "exists": model_path.exists()},
            "vae_gguf": {"path": str(vae_path), "exists": vae_path.exists()},
        }
