"""音色工坊 worker 客户端（主 app 侧）。

负责管理独立的推理 worker 子进程（voice-tools/worker.py）并调用其三个 HTTP 接口：
- ensure_running(): 校验 seedvc 目录，拉起 worker 子进程并做健康检查（端口被占用时自动 +1 重试）
- separate(): 音轨分离
- convert(): 参考音色翻唱

所有对外方法均内置异常处理与明确的错误信息，供 UI 直接展示，调用方不再重复 try/except。
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from voice_config import load_voice_config

logger = logging.getLogger(__name__)

# worker 源码路径（相对 webui 根）
WORKER_SCRIPT = Path(__file__).resolve().parent.parent / "voice-tools" / "worker.py"
# venv 路径（相对 webui 根）——阶段 0 已用 Python 3.11 创建
VENV_REL = Path("voice-tools") / "venv"


class VoiceError(Exception):
    """音色工坊相关错误的统一载体，message 面向 UI 展示。"""


@dataclass
class VoiceResult:
    """一次任务的结果：产物路径字典 + 可选错误。"""
    ok: bool
    products: dict = field(default_factory=dict)
    error: Optional[str] = None
    # worker 侧协作取消标志（/api/cancel 置位后任务在阶段边界中止）。
    # 与主 app 侧 cancel_event 相互独立：任一来源的取消都应映射为"已取消"
    cancelled: bool = False


class VoiceClient:
    """worker 进程与 HTTP 通信的封装（主 app 单例使用）。"""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.webui_root = self.project_root / "yue2-webui"
        self.worker_proc: Optional[subprocess.Popen] = None
        self.port: int = 8190
        self.venv_python: Optional[Path] = None
        self.seedvc_dir: Optional[Path] = None
        self._cfg = None
        self._worker_log = None  # worker 日志文件句柄（进程存活期间保持打开）

    # ---------------------------------------------------------------- 配置
    def _load_cfg(self) -> dict:
        """读取 [voice] 段配置；seedvc_dir 为空时回退探测 project_root/seed-vc。"""
        if self._cfg is None:
            cfg = load_voice_config(self.project_root)
            # 阶段 0 手动 clone 到 Yue2/seed-vc；未在 cfg 配置时自动探测，减少首次配置成本
            if cfg["seedvc_dir"] is None:
                probe = self.project_root / "seed-vc"
                if probe.is_dir():
                    cfg = {**cfg, "seedvc_dir": probe}
            self._cfg = cfg
            logger.info(f"语音配置: enabled={cfg['enabled']}, seedvc_dir={cfg['seedvc_dir']}, "
                        f"worker_port={cfg['worker_port']}, max_min={cfg['max_input_minutes']}")
        return self._cfg

    @property
    def enabled(self) -> bool:
        return bool(self._load_cfg()["enabled"])

    @property
    def max_input_minutes(self) -> int:
        return int(self._load_cfg()["max_input_minutes"])

    # ---------------------------------------------------------------- 进程管理
    def _resolve_venv_python(self) -> Optional[Path]:
        """定位 worker 使用的 venv python（Windows/Linux 分支）。"""
        venv_root = self.webui_root / VENV_REL
        for p in (venv_root / "Scripts" / "python.exe",
                  venv_root / "Scripts" / "python",
                  venv_root / "bin" / "python"):
            if p.exists():
                return p
        return None

    def ensure_running(self) -> None:
        """确保 worker 子进程已启动并通过健康检查。任何配置错误均抛 VoiceError。"""
        cfg = self._load_cfg()
        if not cfg["enabled"]:
            raise VoiceError("音色工坊已通过 config.cfg [voice] enabled=false 关闭")
        if cfg["seedvc_dir"] is None:
            raise VoiceError("未找到 Seed-VC 目录：请在 config.cfg [voice] 设置 seedvc_dir，"
                             "并按 Docs/setup.md 的「音色工坊」章节完成独立环境安装")
        self.seedvc_dir = cfg["seedvc_dir"]

        # worker 脚本必须存在
        if not WORKER_SCRIPT.exists():
            raise VoiceError(f"worker 脚本缺失: {WORKER_SCRIPT}")

        # 探测 worker venv
        self.venv_python = self._resolve_venv_python()
        if self.venv_python is None:
            raise VoiceError("未找到 worker 虚拟环境，请先运行 voice-tools/install_voice.bat 安装独立环境")

        # 若进程仍在且健康，直接使用
        if self.worker_proc is not None and self.worker_proc.poll() is None:
            if self._health_check():
                return
            # 进程挂了/不健康，终止并重启兜底
            self.close()

        # 启动: 从指定端口开始尝试，被占用则 +1
        base_port = int(cfg["worker_port"])
        env = dict(os.environ)
        env["SEEDVC_DIR"] = str(self.seedvc_dir)
        env["PYTHONIOENCODING"] = "utf-8"  # worker print 中文统一按 UTF-8 写日志文件
        # worker 日志落盘：DEVNULL 会丢弃 _log 全部输出（响度匹配数值/失败详情），
        # 排查全靠猜；改为追加写 voice-tools/worker.log，超 10MB 轮转为 .old（单份保留）
        if self._worker_log is None:
            try:
                log_path = self.webui_root / "voice-tools" / "worker.log"
                if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
                    log_path.replace(log_path.with_suffix(".log.old"))
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._worker_log = open(log_path, "ab")
            except Exception:
                logger.exception("打开 worker 日志文件失败，回退丢弃输出")
                self._worker_log = None
        last_err = None
        for off in range(5):
            port = base_port + off
            env["WORKER_PORT"] = str(port)
            _log_proc = subprocess.Popen(
                [str(self.venv_python), str(WORKER_SCRIPT)],
                cwd=str(self.webui_root),
                env=env,
                stdout=self._worker_log if self._worker_log is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if self._worker_log is not None else subprocess.DEVNULL,
            )
            # 等健康检查（最多 ~30s）
            if self._wait_health(port, timeout=30):
                self.worker_proc = _log_proc
                self.port = port
                logger.info(f"worker 已就绪，端口 {port}")
                return
            # 健康失败：kill 后尝试下一个端口
            last_err = f"worker 端口 {port} 健康检查超时"
            try:
                _log_proc.kill()
            except Exception:
                pass
        raise VoiceError(f"无法启动 worker: {last_err}")

    def _wait_health(self, port: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._health_check(port):
                return True
            time.sleep(0.5)
        return False

    def _health_check(self, port: Optional[int] = None) -> bool:
        port = port or self.port
        try:
            data = self._request("GET", f"http://127.0.0.1:{port}/api/health", {}, timeout=5)
            return bool(data.get("ok")) if data else False
        except Exception:
            return False

    def close(self) -> None:
        """正常关闭 worker 子进程与网络开销。"""
        if self.worker_proc is not None and self.worker_proc.poll() is None:
            try:
                # 尽力 POST 一个关闭信号（worker 无关闭接口即忽略），随后 terminate
                self.worker_proc.terminate()
                try:
                    self.worker_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.worker_proc.kill()
            except Exception:
                pass
            self.worker_proc = None
        # 关闭日志句柄（下次 ensure_running 重新打开，实现简单轮转点）
        if self._worker_log is not None:
            try:
                self._worker_log.close()
            except Exception:
                pass
            self._worker_log = None

    # ---------------------------------------------------------------- HTTP 请求
    def _request(self, method: str, url: str, payload: dict, timeout: float = 300) -> dict:
        """统一 HTTP 请求：构造/解析 JSON，失败抛 VoiceError（含可读信息）。"""
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise VoiceError(f"无法连接推理 worker（{getattr(e, 'reason', e)}），请确认音色工坊环境已安装")
        except Exception as e:
            raise VoiceError(f"worker 请求失败: {e}")

    def _run(self, api: str, payload: dict, timeout: Optional[float] = None,
             cancel_event: Optional[threading.Event] = None) -> VoiceResult:
        """执行一次 worker 调用，统一解析返回，异常收敛为 VoiceResult。

        timeout 为 None 时用默认 300s；长任务（分离/翻唱）应传按源时长估算的动态值。
        cancel_event 触发时通过后台线程尽力通知 worker 协作中止（worker 在阶段边界检查）。
        """
        done_event = threading.Event()
        try:
            self.ensure_running()
            # 取消监视线程：收到 cancel_event 或任务结束事件即退出
            # （原实现只等 cancel_event，任务正常结束时线程仍空等最多 30 分钟并随高频任务堆积）
            if cancel_event is not None:
                threading.Thread(target=self._cancel_notify,
                                 args=(cancel_event, done_event),
                                 daemon=True).start()
            data = self._request("POST", f"http://127.0.0.1:{self.port}{api}", payload,
                                 timeout=timeout if timeout else 300)
            if data.get("ok"):
                return VoiceResult(ok=True, products=data.get("products", {}))
            # cancelled 由 worker 在取消异常时标记，供上层把这类失败映射为"已取消"
            return VoiceResult(ok=False, error=str(data.get("error", "未知错误")),
                               cancelled=bool(data.get("cancelled", False)))
        except VoiceError as e:
            return VoiceResult(ok=False, error=str(e))
        except Exception as e:  # 兜底：任何未预期异常都不应打断主 app
            logger.exception("voice task failed unexpectedly")
            return VoiceResult(ok=False, error=f"音色工坊任务异常: {e}")
        finally:
            done_event.set()  # 唤醒监视线程立即退出，避免空等

    def _cancel_notify(self, cancel_event: threading.Event,
                       done_event: Optional[threading.Event] = None) -> None:
        """等待取消信号并尽力通知 worker 中止（协作式取消的客户端半边）。

        以 0.5s 短轮询同时监视 cancel_event 与任务结束事件 done_event：
        任务正常结束时 done_event 被置位，监视线程立即退出，不再空等 30 分钟。
        """
        while True:
            if cancel_event.wait(0.5):
                break
            if done_event is not None and done_event.is_set():
                return
        try:
            self._request("POST", f"http://127.0.0.1:{self.port}/api/cancel", {}, timeout=5)
            logger.info("已通知 worker 取消当前任务")
        except Exception:
            pass  # worker 可能已停止，忽略

    @staticmethod
    def _probe_duration(path: str) -> float:
        """用 ffprobe 测音频时长（秒）；失败/文件不存在返回 0。仅用于超时估算。"""
        if not path or not Path(path).exists():
            return 0.0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=15,
            )
            return float(out.stdout.strip()) if out.stdout.strip() else 0.0
        except Exception:
            return 0.0

    def _timeout_for(self, *paths: str) -> float:
        """按源音频时长估算 HTTP 超时：每秒音频给 20s 处理预算，下限 900s。

        翻唱 = 分离 + 换嗓 + 混音，3-4 分钟歌曲全流程可超 5 分钟，固定 300s 会
        在长曲上超时（worker 仍在跑、结果拿不到、历史不写）；动态放大消除该风险。
        """
        dur = max((self._probe_duration(p) for p in paths), default=0.0)
        return max(900.0, dur * 20.0)

    # ---------------------------------------------------------------- 业务接口
    def separate(self, input_path: str, mode: str = "2", output_dir: str = "",
                 denoise: bool = False, prefix: str = "",
                 progress_file: str = "",
                 cancel_event: Optional[threading.Event] = None) -> VoiceResult:
        """音轨分离。mode: 2=双轨 4=四轨。denoise=True 时对输出人声降噪。返回产物路径字典。

        prefix（时间戳前缀）传给 worker，产物命名 <prefix>_<类别>.wav（可空回退短名）。
        progress_file 为阶段进度文件路径（worker 在分离/降噪边界写入，供 UI 轮询展示）。
        cancel_event 触发时尽力通知 worker 协作中止。
        参数强校验：mode/denoise 入口即做类型归一，非法值回退默认并记录警告，及早报错，
        不依赖 worker 兜底（约束：JSON 字段易出现字符串/空值）。
        """
        # mode 强校验：仅接受 "2"/"4"（兼容去空白），其余回退 "2" 并告警
        try:
            m = str(mode).strip() if mode is not None else "2"
        except Exception:
            m = "2"
        if m not in ("2", "4"):
            logger.warning(f"separate: mode='{mode}' 非法，回退为 '2'")
            m = "2"
        # denoise 强校验：仅真值视为 True
        try:
            d = bool(denoise) and str(denoise).strip().lower() not in ("", "0", "false", "no", "off")
        except Exception:
            d = False
        return self._run("/api/separate", {
            "input": str(input_path), "mode": m, "output_dir": str(output_dir),
            "denoise": d, "denoise_strength": self._load_cfg().get("denoise_strength"),
            "prefix": str(prefix or ""), "progress_file": str(progress_file or ""),
        }, timeout=self._timeout_for(input_path), cancel_event=cancel_event)

    def convert(self, source: str, ref: str, semi_tone: int = 0,
                diffusion_steps: int = 30, accompaniment: str = "",
                gain_db: float = 0.0, output_dir: str = "",
                denoise: bool = False, prefix: str = "",
                source_vocals: str = "", source_acc: str = "",
                progress_file: str = "",
                cancel_event: Optional[threading.Event] = None,
                cfg_rate: float = 0.9, ref_sec: float = 10.0,
                hf_enhance: float = 0.0,
                ref_mode: str = "smart", ref_acc: str = "") -> VoiceResult:
        """参考音色翻唱。source=换嗓人声来源, ref=参考干声, accompaniment=伴奏。denoise=True 时对换嗓人声降噪。

        prefix（时间戳前缀）传给 worker，全部产物平铺 output_dir 并命名 <prefix>_<类别>（可空回退短名）。
        source_vocals/source_acc 非空时复用已有分离结果（跳过 Demucs 重复分离）：
        source_vocals=源人声干声轨, source_acc=源伴奏轨（未提供自定义伴奏时兼作混音伴奏）。
        progress_file 为阶段进度文件路径；cancel_event 触发时尽力通知 worker 协作中止。
        cfg_rate=Seed-VC 推理 CFG 强度（0~1，默认 0.9：P1 扫描实测谱质心最贴源的档位）；
        ref_sec=参考干声裁剪秒数（2~30，默认 10：实测越短输出削波越重）。
        hf_enhance=换嗓人声高频细节补偿强度（0~4，默认 0=关闭；补偿 over-smoothing 导致的高频细节丢失）。
        ref_mode=参考段策略（smart=智能/默认｜energy=能量最高段｜full=整曲不裁剪），
        非法值回退 smart；ref_acc=参考干声的配对伴奏轨（仅"分离人声"来源有，
        smart 用它算人声主导度挑段；缺失时 worker 侧自动回退能量最高段）。
        以上均为音质调优参数，越界值在此钳制，避免 worker 端异常。
        """
        # 数值入参强校验：非法（含 None/空串/非数字）回退默认并告警，避免 worker 端崩溃
        def _int_or(v, default):
            try:
                return int(str(v).strip() or default)
            except (TypeError, ValueError):
                logger.warning(f"convert: 数值入参 '{v}' 非法，回退为 {default}")
                return default

        def _float_or(v, default):
            try:
                return float(str(v).strip() or default)
            except (TypeError, ValueError):
                logger.warning(f"convert: 数值入参 '{v}' 非法，回退为 {default}")
                return default

        try:
            den = bool(denoise) and str(denoise).strip().lower() not in ("", "0", "false", "no", "off")
        except Exception:
            den = False
        # 参考段策略白名单：非法/空值一律回退默认 smart（P5C 盲听验证的最优策略）
        try:
            rm = str(ref_mode or "smart").strip().lower()
        except Exception:
            rm = "smart"
        if rm not in ("smart", "energy", "full"):
            logger.warning(f"convert: ref_mode='{ref_mode}' 非法，回退为 'smart'")
            rm = "smart"
        return self._run("/api/convert", {
            "source": str(source), "ref": str(ref),
            "semi_tone": _int_or(semi_tone, 0),
            "diffusion_steps": _int_or(diffusion_steps, 30),
            "accompaniment": str(accompaniment), "gain_db": _float_or(gain_db, 0.0),
            "output_dir": str(output_dir), "denoise": den,
            "denoise_strength": self._load_cfg().get("denoise_strength"),
            "prefix": str(prefix or ""),
            "source_vocals": str(source_vocals or ""), "source_acc": str(source_acc or ""),
            "progress_file": str(progress_file or ""),
            # 音质调优参数：越界在此钳制，worker 端还会再钳一次（双保险）
            "cfg_rate": max(0.0, min(1.0, _float_or(cfg_rate, 0.9))),
            "ref_sec": max(2.0, min(30.0, _float_or(ref_sec, 10.0))),
            "hf_enhance": max(0.0, min(4.0, _float_or(hf_enhance, 0.0))),
            # 参考段策略 + 配对伴奏（智能挑段用；无配对轨时 worker 侧回退能量最高段）
            "ref_mode": rm, "ref_acc": str(ref_acc or ""),
        }, timeout=self._timeout_for(source, source_vocals or source, ref),
           cancel_event=cancel_event)


# ffmpeg 可用性检查工具函数（供 UI 预检提示；tests/test_voice_client.py 仍引用）
def heal_ffmpeg_check() -> str:
    """返回 ffmpeg 可用性检查文案（空串=可用）。"""
    return "" if shutil.which("ffmpeg") else "未找到 ffmpeg，请加入系统 PATH"


# ---------------------------------------------------------------- 模型状态检查
# 分离模型 htdemucs_ft 经 HuggingFace hub 加载（demucs 优先走 HF），权重落 HF 缓存
# 仓库 models--adefossez--HTDemucs-ft/snapshots/<revision>/ 下的 4 个 safetensors
# （4 模型 bag 集成，合计约 320MB）；缺失时首次分离自动下载
DEMUCS_FT_REPO = "models--adefossez--HTDemucs-ft"
DEMUCS_FT_FILES = ("f7e0c4bc.safetensors", "d12395a8.safetensors",
                   "92cfc3b6.safetensors", "04573f0d.safetensors")


def _hf_hub_dir() -> Path:
    """定位 HuggingFace 本地缓存 hub 目录（HUGGINGFACE_HUB_CACHE 指向 hub 本身，
    HF_HOME 指向其父目录，均未设置时默认 ~/.cache/huggingface）。"""
    hub_cache = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if hub_cache:
        return Path(hub_cache)
    hf_home = os.environ.get("HF_HOME")
    base = Path(hf_home) if hf_home else Path.home() / ".cache" / "huggingface"
    return base / "hub"


def _demucs_snapshot_dir() -> Path:
    """定位 htdemucs_ft 的 HF 快照目录（内含 4 个权重文件）。"""
    snap = _hf_hub_dir() / DEMUCS_FT_REPO / "snapshots"
    subs = [d for d in snap.iterdir() if d.is_dir()] if snap.is_dir() else []
    return subs[0] if subs else snap


def check_voice_models(project_root) -> dict:
    """检查音色工坊三个模型文件的存在状态（仅文件级检查，不加载模型）。

    供系统设置页「模型状态」展示：
    - Demucs (htdemucs_ft)：HF 缓存快照下 4 个 safetensors 齐备才算就绪
    - Seed-VC 主模型：seedvc_dir/checkpoints/models--Plachta--Seed-VC/snapshots 下的 .pth
    - campplus 说话人编码器：models--funasr--campplus/snapshots 下的 campplus_cn_common.bin

    返回 {"enabled", "demucs"/"seedvc"/"campplus": {"exists": bool|None, "path": str}}；
    exists=None 表示该项不适用（未启用 / seedvc_dir 未配置），UI 应显示对应占位文案。
    """
    # 复用 VoiceClient 的配置读取（含 seedvc_dir 回退探测 project_root/seed-vc）
    cfg = VoiceClient(Path(project_root))._load_cfg()

    demucs_path = _demucs_snapshot_dir()
    result = {
        "enabled": bool(cfg["enabled"]),
        "demucs": {"exists": None, "path": str(demucs_path)},
        "seedvc": {"exists": None, "path": ""},
        "campplus": {"exists": None, "path": ""},
    }
    if not result["enabled"]:
        return result

    result["demucs"]["exists"] = demucs_path.is_dir() and all(
        (demucs_path / f).is_file() for f in DEMUCS_FT_FILES)

    seedvc_dir = cfg.get("seedvc_dir")
    if seedvc_dir is None:
        return result  # 目录未配置：seedvc/campplus 保持 None（UI 显示「未配置」）

    # Seed-VC 主模型：HF 缓存 snapshots 下任意 .pth（DiT 权重，文件名随版本变化故不硬编码）
    seed_snap = Path(seedvc_dir) / "checkpoints" / "models--Plachta--Seed-VC" / "snapshots"
    result["seedvc"]["path"] = str(seed_snap)
    result["seedvc"]["exists"] = seed_snap.is_dir() and any(
        p.is_file() and p.suffix == ".pth" for p in seed_snap.rglob("*"))

    # campplus 说话人编码器：固定文件名 campplus_cn_common.bin
    camp_snap = Path(seedvc_dir) / "checkpoints" / "models--funasr--campplus" / "snapshots"
    result["campplus"]["path"] = str(camp_snap)
    result["campplus"]["exists"] = camp_snap.is_dir() and any(
        p.is_file() and p.name == "campplus_cn_common.bin" for p in camp_snap.rglob("*"))

    return result