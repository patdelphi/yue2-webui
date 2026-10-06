"""backend_gguf CLI 看门狗测试（A1）。

验证：CLI 静默阻塞时取消能立即中断、总超时能兜底 kill、正常结束逐行回调、
进程均被 wait 回收（不泄漏句柄）。子进程用 Python 自身模拟，避免依赖真实 CLI。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from backend_gguf import GGUFBackend


def _backend() -> GGUFBackend:
    """仅构造测 _pump_cli 所需字段（跳过 __init__ 的配置读取，避免副作用）。"""
    b = GGUFBackend.__new__(GGUFBackend)
    b.project_root = Path.cwd()
    b._current_process = None
    return b


def test_pump_cli_cancel_interrupts_blocking_read():
    """子进程静默阻塞（无日志）时，cancel_event 触发应能立即 kill 并返回 cancelled。"""
    b = _backend()
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    t0 = time.time()
    _rc, cancelled, timed_out = b._pump_cli(cmd, ev, 60, lambda line: None)
    assert cancelled is True and timed_out is False
    assert time.time() - t0 < 10            # 未被子进程 30s 睡眠阻塞


def test_pump_cli_timeout_kills_process():
    """总超时命中应 kill 子进程并返回 timed_out=True。"""
    b = _backend()
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    t0 = time.time()
    _rc, cancelled, timed_out = b._pump_cli(cmd, None, 1, lambda line: None)
    assert timed_out is True and cancelled is False
    assert time.time() - t0 < 10


def test_pump_cli_normal_passthrough():
    """正常结束：逐行回调、returncode=0、无取消/超时。"""
    b = _backend()
    cmd = [sys.executable, "-c", "print('a'); print('b')"]
    lines = []
    rc, cancelled, timed_out = b._pump_cli(cmd, None, 30, lines.append)
    assert rc == 0 and cancelled is False and timed_out is False
    assert [ln.strip() for ln in lines] == ["a", "b"]


def test_cancel_kills_and_reaps():
    """cancel() 应 kill 当前子进程并回收（wait），随后 _pump_cli 正常返回。"""
    b = _backend()
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    results = {}

    def run():
        results["ret"] = b._pump_cli(cmd, None, 60, lambda line: None)

    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.5)          # 等子进程启动并进入阻塞读
    b.cancel()
    th.join(timeout=10)
    assert not th.is_alive()
    rc, cancelled, timed_out = results["ret"]
    assert rc is not None    # 进程已被 wait 回收，returncode 有值
    assert cancelled is False and timed_out is False
