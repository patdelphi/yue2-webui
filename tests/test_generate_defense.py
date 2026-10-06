# -*- coding: utf-8 -*-
"""生成 worker（_generate_worker）种子/批量数防御测试。

背景：_generate_worker 通过 seeds[i] 取每变体种子，依赖 on_generate 保证
len(seeds)==batch_count。worker 层无防御时，种子列表偏短会 IndexError。
修复：worker 入口统一归一化 seeds 并把 batch_count 收敛到 min(batch_count, len(seeds))。

验证点：
1. seeds 短于 batch_count：不抛 IndexError，实际只生成 len(seeds) 个变体；
2. seeds 为空：给默认随机种子，不崩；
3. 同时确认冗余的重复 batch_count 转换已被清理（源码断言）。
"""

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # app.py 在 webui 根目录

import app  # noqa: E402
from history import HistoryManager  # noqa: E402


class _FakeTask:
    """最小任务桩：仅提供取消事件与进度推送。"""
    def __init__(self):
        self.cancel_event = threading.Event()

    def push_progress(self, frac, label):
        pass


class _FakeResult:
    """最小生成结果桩：字段与 backend.generate 返回值一致。"""
    def __init__(self, wav_path):
        self.success = True
        self.error_message = None
        self.abc_score = "X:1\nT:stub"
        self.audio_path = str(wav_path)
        self.mp3_path = None
        self.generation_time_seconds = 0.1
        self.audio_duration_seconds = 1.0


class _FakeBackend:
    """假后端：记录 generate 调用次数，并落一个假的音频文件。"""
    main_model = "m.gguf"
    vae_model = "v.gguf"

    def __init__(self):
        self.calls = []

    def generate(self, params, output_dir, on_progress, cancel_event,
                 output_name, lang):
        self.calls.append((params.seed, output_name))
        wav = Path(output_dir) / f"{output_name}.wav"
        wav.write_bytes(b"\x00")
        return _FakeResult(wav)


def _run_worker(tmp, seeds, batch_count):
    """在临时 WEBUI_ROOT 下跑一次 _generate_worker，返回 (假后端, 历史记录数)。"""
    webui = tmp / "yue2-webui"
    (webui / "outputs").mkdir(parents=True, exist_ok=True)

    fake_backend = _FakeBackend()
    hm = HistoryManager(webui / "history.db", webui / "outputs")

    orig = (app.WEBUI_ROOT, app.LAST_INPUTS_FILE, app.backend, app.history_mgr,
            app.refresh_history)
    app.WEBUI_ROOT = webui
    app.LAST_INPUTS_FILE = webui / "last_inputs.json"
    app.backend = fake_backend
    app.history_mgr = hm
    app.refresh_history = lambda: ([], "")
    try:
        app._generate_worker(
            _FakeTask(), "项目", "pop", "la la", "off", seeds,
            None, 8, "pcm16", batch_count,
            False, False, False, False, "",
            0.7, 0.9, 30, 1.005, 100, 32, 4096,
            1.0, 0.95, 100, 1.2, 50, 200, 9000,
            lang="zh",
        )
        return fake_backend, len(hm.list_all())
    finally:
        (app.WEBUI_ROOT, app.LAST_INPUTS_FILE, app.backend, app.history_mgr,
         app.refresh_history) = orig
        hm.close()


def test_seeds_shorter_than_batch_is_clamped():
    """seeds 只有 1 个、batch_count=3：收敛为 1 个变体，不抛 IndexError。"""
    with tempfile.TemporaryDirectory() as td:
        backend, n = _run_worker(Path(td), [7], 3)
        assert len(backend.calls) == 1
        assert backend.calls[0][0] == 7
        assert n == 1  # 只登记 1 条历史记录（batch_count 已被收敛）


def test_empty_seeds_uses_random_seed():
    """seeds 为空：给随机种子兜底，不崩且只生成 1 个变体。"""
    with tempfile.TemporaryDirectory() as td:
        backend, n = _run_worker(Path(td), [], 2)
        assert len(backend.calls) == 1
        assert backend.calls[0][0] >= 0  # 随机种子非负（validate_params 要求）
        assert n == 1


def test_no_duplicate_batch_count_conversion():
    """冗余清理：_generate_worker 内不应再出现重复的整行 batch_count 转换。

    注：on_generate（UI 入口）仍保留一处 `batch_count = int(batch_count) if ... else 1`，
    属正常归一化，故按整行精确匹配而非子串匹配。
    """
    src = (Path(__file__).parent.parent / "app.py").read_text(encoding="utf-8-sig")
    assert "\n    batch_count = int(batch_count)\n" not in src
