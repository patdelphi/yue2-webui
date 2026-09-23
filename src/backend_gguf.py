"""GGUF backend using audio.cpp CLI."""
import os
import subprocess
import threading
import time
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass

from config import GenerationParams, CotMode, OutFormat, TranscriptionResult
from i18n import tr

logger = logging.getLogger(__name__)

# 模型路径默认值（config.cfg 缺失/损坏/缺项时的回退，保持与历史行为一致）
DEFAULT_MODEL_CONFIG = {
    "models_dir": "models",                                   # 主模型+VAE 目录（相对项目根）
    "main_model": "yue2-3b-q8_0.gguf",                        # 主模型文件名
    "vae_model": "yue2-vae-f16.gguf",                         # VAE 模型文件名
    "sheetsage2_path": "audio-cpp/models/SheetSage2-GGUF/sheetsage2-orig.gguf",  # 转谱模型路径
}


def load_model_config(project_root: Path) -> dict:
    """读取 yue2-webui 目录下外置 config.cfg 的 [models] 段，返回模型路径配置。

    规则：
    - cfg 不存在或解析异常时回退 DEFAULT_MODEL_CONFIG（不抛异常，保证可启动）
    - 缺失的配置项回退默认值
    - cfg 文件位于 <project_root>/yue2-webui/config.cfg（随 webui 项目走）；
      相对路径（如 models、audio-cpp/...）仍基于 project_root（Yue2 系统根）解析；绝对路径直接使用
    返回键: models_dir(Path) / main_model(str) / vae_model(str) / sheetsage2_path(Path)
    """
    cfg_path = Path(project_root) / "yue2-webui" / "config.cfg"
    values = dict(DEFAULT_MODEL_CONFIG)
    if cfg_path.exists():
        try:
            import configparser
            parser = configparser.ConfigParser()
            # 显式 utf-8 读取，兼容带中文注释的 cfg
            with open(cfg_path, "r", encoding="utf-8") as f:
                parser.read_file(f)
            if parser.has_section("models"):
                for key in values:
                    if parser.has_option("models", key):
                        values[key] = parser.get("models", key).strip()
        except Exception as e:
            # cfg 损坏不阻断启动，回退默认并记录警告
            logger.warning(f"config.cfg 解析失败，使用默认模型路径: {e}")

    # 目录/路径类配置统一解析为 Path（相对路径基于项目根）
    models_dir = Path(values["models_dir"])
    if not models_dir.is_absolute():
        models_dir = Path(project_root) / models_dir
    sheetsage2_path = Path(values["sheetsage2_path"])
    if not sheetsage2_path.is_absolute():
        sheetsage2_path = Path(project_root) / sheetsage2_path
    return {
        "models_dir": models_dir,
        "main_model": values["main_model"],
        "vae_model": values["vae_model"],
        "sheetsage2_path": sheetsage2_path,
    }


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
        # 模型路径统一由外置 config.cfg 决定（缺失时回退默认值）
        _mcfg = load_model_config(self.project_root)
        self.model_dir: Path = _mcfg["models_dir"]
        self.main_model: str = _mcfg["main_model"]
        self.vae_model: str = _mcfg["vae_model"]
        self.sheetsage2_path: Path = _mcfg["sheetsage2_path"]
        self.backend = os.environ.get("YUE2_BACKEND", "cuda")
        self._current_process: Optional[subprocess.Popen] = None
    
    def build_command(self, params: GenerationParams, output_dir: Path,
                      output_name: Optional[str] = None) -> list[str]:
        """Build CLI command from parameters."""
        cmd = [
            str(self.cli_path),
            "--task", "gen",
            "--family", "yue2",
            "--model", str(self.model_dir / self.main_model),
            "--backend", self.backend,
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
        
        # 模型文件名由外置 config.cfg 决定（系统级配置，非每次生成参数）
        cmd.extend(["--session-option", f"yue2.model_gguf={self.main_model}"])
        cmd.extend(["--session-option", f"yue2.vae_gguf={self.vae_model}"])

        if params.out_format != OutFormat.PCM16:
            cmd.extend(["--out-format", params.out_format.value])

        output_path = output_dir / f"{output_name or output_dir.name}.wav"
        cmd.extend(["--out", str(output_path)])
        cmd.extend(["--out-dir", str(output_dir)])
        cmd.append("--log")

        return cmd

    def generate(self, params: GenerationParams, output_dir: Path,
                 on_progress: Optional[Callable[[dict], None]] = None,
                 cancel_event: Optional[threading.Event] = None,
                 output_name: Optional[str] = None,
                 lang: str = "zh") -> GenerationResult:
        """Execute generation."""
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{output_name or output_dir.name}.wav"
        cmd = self.build_command(params, output_dir, output_name=output_name)
        logger.info(f"Executing audiocpp_cli command: {' '.join(str(c) for c in cmd)}")
        
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
            output_lines = []

            for line in process.stdout:
                if cancel_event and cancel_event.is_set():
                    process.kill()
                    return GenerationResult(success=False, error_message=tr(lang, "已取消"))

                output_lines.append(line)
                progress = parser.parse_line(line)
                if progress and on_progress:
                    on_progress(progress)

            process.wait()
            elapsed = time.time() - start_time
            
            if process.returncode != 0:
                logger.error(f"audiocpp_cli failed with exit code {process.returncode}")
                return GenerationResult(
                    success=False,
                    error_message=f"audiocpp_cli {tr(lang, '退出码')} {process.returncode}",
                    generation_time_seconds=elapsed,
                )
            
            if not output_path.exists():
                logger.error("Output file not found after generation")
                return GenerationResult(
                    success=False,
                    error_message=tr(lang, "生成完成但输出文件不存在"),
                    generation_time_seconds=elapsed,
                )
            
            audio_duration = self._get_audio_duration(output_path)

            abc_score = None
            abc_name = f"{output_name or output_dir.name}.abc"
            abc_path = output_dir / abc_name
            legacy_abc = output_dir / "score.abc"
            logger.info(f"Checking for ABC files: {abc_path} exists={abc_path.exists()}, {legacy_abc} exists={legacy_abc.exists()}")
            if not abc_path.exists() and legacy_abc.exists():
                logger.info(f"Renaming legacy {legacy_abc} to {abc_path}")
                legacy_abc.rename(abc_path)
            if abc_path.exists():
                abc_score = abc_path.read_text(encoding="utf-8")
                logger.info(f"Loaded ABC score: {len(abc_score)} chars")
            elif params.abc:
                abc_score = params.abc
                abc_path.write_text(abc_score, encoding="utf-8")
                logger.info(f"Saved user-provided ABC score: {len(abc_score)} chars to {abc_path}")
            else:
                logger.info(f"ABC file not found, extracting from CLI output")
                abc_score = self._extract_abc_from_output(output_lines)
                if abc_score:
                    abc_path.write_text(abc_score, encoding="utf-8")
                    logger.info(f"Extracted and saved ABC score: {len(abc_score)} chars to {abc_path}")
                else:
                    logger.warning(f"No ABC content found in output")
                    logger.info(f"Files in {output_dir}: {list(output_dir.iterdir())}")
            
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

    def _extract_abc_from_output(self, output_lines: list[str]) -> Optional[str]:
        """Extract ABC notation from CLI output lines."""
        abc_lines = []
        in_abc = False
        abc_start_markers = ['X:', 'T:', 'M:', 'L:', 'K:']

        for line in output_lines:
            stripped = line.strip()
            if not stripped:
                continue

            if not in_abc:
                if any(stripped.startswith(marker) for marker in abc_start_markers):
                    in_abc = True
                    abc_lines.append(stripped)
            else:
                if stripped.startswith('--') or 'session-option' in stripped or 'request-option' in stripped:
                    break
                abc_lines.append(stripped)

        if abc_lines:
            return '\n'.join(abc_lines)
        return None
    
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
        """Check if model files exist (paths from external config.cfg)."""
        model_path = self.model_dir / self.main_model
        vae_path = self.model_dir / self.vae_model
        
        return {
            "available": model_path.exists() and vae_path.exists(),
            "model_gguf": {"path": str(model_path), "exists": model_path.exists()},
            "vae_gguf": {"path": str(vae_path), "exists": vae_path.exists()},
        }

    def check_sheetsage2(self) -> dict:
        """Check if SheetSage2 model exists (path from external config.cfg)."""
        model_path = self.sheetsage2_path
        return {
            "available": model_path.exists(),
            "model_path": str(model_path),
            "exists": model_path.exists(),
        }

    def transcribe(self, audio_path: Path, output_dir: Path,
                   on_progress: Optional[Callable[[dict], None]] = None,
                   cancel_event: Optional[threading.Event] = None,
                   lang: str = "zh") -> TranscriptionResult:
        """Transcribe audio to ABC score using SheetSage2."""
        output_dir.mkdir(parents=True, exist_ok=True)

        sheetsage2_model = self.sheetsage2_path
        if not sheetsage2_model.exists():
            return TranscriptionResult(
                success=False,
                error_message=tr(lang, "SheetSage2 模型未找到，请检查 config.cfg 中 sheetsage2_path 配置"),
            )
        
        base_name = output_dir.name
        abc_path = output_dir / f"{base_name}.abc"
        midi_path = output_dir / f"{base_name}.mid"
        
        wav_path = audio_path
        temp_wav = None
        
        if audio_path.suffix.lower() != '.wav':
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                return TranscriptionResult(
                    success=False,
                    error_message="需要 ffmpeg 来转换音频格式，但未找到 ffmpeg",
                )
            
            tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
            tmp.close()
            temp_wav = Path(tmp.name)
            logger.info(f"Converting {audio_path.name} to WAV: {temp_wav}")
            
            try:
                convert_cmd = [
                    ffmpeg, "-y", "-i", str(audio_path),
                    "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
                    str(temp_wav)
                ]
                result = subprocess.run(
                    convert_cmd,
                    capture_output=True,
                    timeout=300
                )
                
                if result.returncode != 0:
                    stderr_text = result.stderr.decode('utf-8', errors='replace') if result.stderr else ""
                    logger.error(f"ffmpeg conversion failed: {stderr_text}")
                    if temp_wav.exists():
                        temp_wav.unlink()
                    return TranscriptionResult(
                        success=False,
                        error_message=f"音频转换失败: {stderr_text[:200]}",
                    )
                
                wav_path = temp_wav
                logger.info(f"Converted to WAV: {temp_wav}")
                
            except Exception as e:
                logger.exception(f"ffmpeg conversion error: {e}")
                if temp_wav and temp_wav.exists():
                    temp_wav.unlink()
                return TranscriptionResult(
                    success=False,
                    error_message=f"音频转换异常: {str(e)}",
                )
        
        cmd = [
            str(self.cli_path),
            "--task", "midi",
            "--family", "sheetsage2",
            "--model", str(sheetsage2_model),
            "--backend", self.backend,
            "--audio", str(wav_path),
            "--text-out", str(abc_path),
            "--out", str(midi_path),
            "--out-dir", str(output_dir),
            "--log",
        ]
        
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
            output_lines = []
            
            for line in process.stdout:
                if cancel_event and cancel_event.is_set():
                    process.kill()
                    return TranscriptionResult(success=False, error_message=tr(lang, "已取消"))
                
                line_stripped = line.strip()
                output_lines.append(line_stripped)
                logger.debug(f"SheetSage2: {line_stripped}")
                if on_progress:
                    if "Loading model" in line_stripped or "model loaded" in line_stripped.lower():
                        on_progress({"phase": "loading", "message": tr(lang, "加载 SheetSage2 模型...")})
                    elif "transcrib" in line_stripped.lower() or "decod" in line_stripped.lower():
                        on_progress({"phase": "transcribing", "message": tr(lang, "转谱中...")})
                    elif "Total:" in line_stripped or "completed" in line_stripped.lower():
                        on_progress({"phase": "done", "message": tr(lang, "转谱完成")})
            
            process.wait()
            elapsed = time.time() - start_time
            
            if process.returncode != 0:
                last_lines = [l for l in output_lines if l][-10:]
                error_detail = "\n".join(last_lines) if last_lines else tr(lang, "无输出")
                logger.error(f"SheetSage2 failed with exit code {process.returncode}:\n{error_detail}")
                return TranscriptionResult(
                    success=False,
                    error_message=f"{tr(lang, '转谱失败，退出码')} {process.returncode}: {error_detail}",
                    transcription_time_seconds=elapsed,
                )
            
            abc_score = None
            if abc_path.exists():
                abc_score = abc_path.read_text(encoding="utf-8")
            
            if not abc_score:
                return TranscriptionResult(
                    success=False,
                    error_message=tr(lang, "转谱完成但未生成乐谱"),
                    transcription_time_seconds=elapsed,
                )
            
            events_src = output_dir / "events.json"
            if events_src.exists():
                events_dst = output_dir / f"{base_name}_events.json"
                try:
                    events_src.rename(events_dst)
                    logger.debug(f"Renamed events.json to {events_dst.name}")
                except Exception as e:
                    logger.warning(f"Failed to rename events.json: {e}")
            
            score_abc = output_dir / "score.abc"
            if score_abc.exists():
                try:
                    score_abc.unlink()
                    logger.debug(f"Removed duplicate score.abc")
                except Exception as e:
                    logger.warning(f"Failed to remove score.abc: {e}")
            
            return TranscriptionResult(
                success=True,
                abc_score=abc_score,
                midi_path=str(midi_path) if midi_path.exists() else None,
                transcription_time_seconds=elapsed,
            )
            
        except Exception as e:
            logger.exception(f"Transcription failed: {e}")
            return TranscriptionResult(success=False, error_message=str(e))
        finally:
            self._current_process = None
            if temp_wav and temp_wav.exists():
                try:
                    temp_wav.unlink()
                    logger.debug(f"Cleaned up temp WAV: {temp_wav}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temp WAV {temp_wav}: {e}")
