"""Test script for queue manager."""
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from queue_manager import (queue_manager, TaskType, TaskStatus, TaskCancelledError,
                           DEFAULT_MAX_RUNTIME, TIMEOUT_ERROR)


def task_worker(_task, duration, name):
    """Simulate a task that takes time.（工作回调，非测试，避免 pytest 误收集）"""
    print(f"[{name}] Starting...")
    for i in range(int(duration)):
        if _task.cancel_event.is_set():
            print(f"[{name}] Cancelled!")
            return f"{name} cancelled"
        _task.push_progress(i / duration, f"{name} step {i+1}/{int(duration)}")
        time.sleep(1)
    print(f"[{name}] Completed!")
    return f"{name} done"


def cooperative_task(_task, duration, name):
    """Simulate a task that raises TaskCancelledError on cancel."""
    print(f"[{name}] Starting...")
    for i in range(int(duration)):
        if _task.cancel_event.is_set():
            print(f"[{name}] Raising TaskCancelledError!")
            raise TaskCancelledError("cancelled")
        _task.push_progress(i / duration, f"{name} step {i+1}/{int(duration)}")
        time.sleep(1)
    print(f"[{name}] Completed!")
    return f"{name} done"


def test_queue():
    """Test queue execution order."""
    print("=== Testing Queue Manager ===\n")
    
    # Submit 3 tasks
    task1 = queue_manager.submit(TaskType.GENERATION, task_worker, duration=3, name="Task1")
    print(f"Submitted {task1.task_id}")
    
    time.sleep(0.5)
    
    task2 = queue_manager.submit(TaskType.TRANSCRIPTION, task_worker, duration=2, name="Task2")
    print(f"Submitted {task2.task_id}")
    
    time.sleep(0.5)
    
    task3 = queue_manager.submit(TaskType.GENERATION, task_worker, duration=2, name="Task3")
    print(f"Submitted {task3.task_id}")
    
    print("\n=== Queue Info ===")
    info = queue_manager.get_queue_info()
    print(f"Current: {info['current_task']}")
    print(f"Queued: {info['queued_count']}")
    
    # Monitor tasks
    tasks = [task1, task2, task3]
    for task in tasks:
        print(f"\n--- Monitoring {task.task_id} ---")
        while True:
            status = queue_manager.get_status(task)
            if status['status'] == TaskStatus.QUEUED:
                print(f"  Queued at position {status['position']}")
            elif status['status'] == TaskStatus.RUNNING:
                print(f"  Running for {status['running_time']:.1f}s")
                # Drain progress
                for prog, desc in task.drain_progress():
                    print(f"    Progress: {prog:.0%} - {desc}")
            elif status['status'] == TaskStatus.COMPLETED:
                print(f"  Completed! Result: {task.result}")
                break
            elif status['status'] == TaskStatus.FAILED:
                print(f"  Failed: {status.get('error')}")
                break
            time.sleep(1)
    
    print("\n=== All tasks completed ===")


def test_cancel_running():
    """Cancel a running task that raises TaskCancelledError."""
    print("\n=== Testing cancel of running task ===\n")
    task = queue_manager.submit(TaskType.GENERATION, cooperative_task, duration=10, name="Cancellable")
    time.sleep(1.5)
    ok = queue_manager.cancel_task_by_id(task.task_id)
    print(f"cancel_task_by_id returned: {ok}")
    deadline = time.time() + 5
    while time.time() < deadline:
        status = queue_manager.get_status(task)
        if status["status"] in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            break
        time.sleep(0.2)
    final = queue_manager.get_status(task)["status"]
    print(f"Final status: {final}")
    assert final == TaskStatus.CANCELLED, f"expected CANCELLED, got {final}"
    print("PASS: running task cancelled -> CANCELLED\n")


def test_cancel_queued():
    """Cancel a task while it is still queued behind a running one."""
    print("=== Testing cancel of queued task ===\n")
    blocker = queue_manager.submit(TaskType.GENERATION, task_worker, duration=4, name="Blocker")
    queued = queue_manager.submit(TaskType.TRANSCRIPTION, task_worker, duration=2, name="Queued")
    time.sleep(0.5)
    ok = queue_manager.cancel_task_by_id(queued.task_id)
    print(f"cancel_task_by_id returned: {ok}")
    deadline = time.time() + 10
    while time.time() < deadline:
        status = queue_manager.get_status(queued)
        if status["status"] in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            break
        time.sleep(0.2)
    final = queue_manager.get_status(queued)["status"]
    print(f"Final status: {final}")
    assert final == TaskStatus.CANCELLED, f"expected CANCELLED, got {final}"
    # Blocker should finish normally
    deadline = time.time() + 10
    while time.time() < deadline:
        if queue_manager.get_status(blocker)["status"] == TaskStatus.COMPLETED:
            break
        time.sleep(0.2)
    print(f"Blocker status: {queue_manager.get_status(blocker)['status']}")
    print("PASS: queued task cancelled -> CANCELLED\n")


def test_failed():
    """A task raising ValueError is marked FAILED, not CANCELLED."""
    print("=== Testing failed task ===\n")

    def failing_task(_task):
        raise ValueError("boom")

    task = queue_manager.submit(TaskType.GENERATION, failing_task)
    deadline = time.time() + 5
    while time.time() < deadline:
        status = queue_manager.get_status(task)
        if status["status"] in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            break
        time.sleep(0.2)
    final = queue_manager.get_status(task)
    print(f"Final status: {final['status']}, error: {final.get('error')}")
    assert final["status"] == TaskStatus.FAILED, f"expected FAILED, got {final['status']}"
    print("PASS: ValueError -> FAILED\n")


def test_queue_snapshot():
    """get_queue_snapshot：运行中/排队/最近历史三段快照（进度只读不清空 drain 流）。"""
    print("=== Testing queue snapshot ===\n")
    t1 = queue_manager.submit(TaskType.GENERATION, task_worker, duration=1, name="Snap1")
    t2 = queue_manager.submit(TaskType.TRANSCRIPTION, task_worker, duration=0.6, name="Snap2")
    time.sleep(0.4)  # t1 运行中、t2 排队

    snap = queue_manager.get_queue_snapshot()
    assert snap["running"] is not None, snap
    assert snap["running"]["task_id"] == t1.task_id, snap
    assert snap["running"]["progress"] is not None, snap  # last_progress 可读
    assert len(snap["queued"]) == 1 and snap["queued"][0]["task_id"] == t2.task_id, snap
    assert snap["worker_alive"] is True, snap
    # 只读快照不应清空进度队列：t1 的 progress 仍可 drain 到
    drained = t1.drain_progress()
    assert drained, "snapshot 不应消费 drain 流"

    # 等两个任务完成，验证历史段
    for t in (t1, t2):
        deadline = time.time() + 10
        while time.time() < deadline:
            if queue_manager.get_status(t)["status"] in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                break
            time.sleep(0.2)
    snap2 = queue_manager.get_queue_snapshot()
    assert snap2["running"] is None and snap2["queued"] == [], snap2
    ids = [h["task_id"] for h in snap2["recent"]]
    assert t1.task_id in ids and t2.task_id in ids, snap2
    assert all(h["status"] in ("completed", "failed", "cancelled") for h in snap2["recent"]), snap2
    print("PASS: queue snapshot (running/queued/recent)\n")


def test_cancel_queued_records_history():
    """排队取消的任务应写入"最近任务"历史（与运行中取消展示一致）。"""
    print("=== Testing queued cancel recorded in history ===\n")
    blocker = queue_manager.submit(TaskType.GENERATION, task_worker, duration=3, name="QBlock")
    queued = queue_manager.submit(TaskType.TRANSCRIPTION, task_worker, duration=1, name="QTarget")
    time.sleep(0.4)  # 等 blocker 进入运行、queued 落入排队
    assert queue_manager.cancel_task_by_id(queued.task_id) is True, "排队任务应取消成功"
    snap = queue_manager.get_queue_snapshot()
    rec = [h for h in snap["recent"] if h["task_id"] == queued.task_id]
    assert rec and rec[0]["status"] == "cancelled", snap["recent"]
    # 等 blocker 结束，避免影响后续测试
    deadline = time.time() + 10
    while time.time() < deadline and \
            queue_manager.get_status(blocker)["status"] != TaskStatus.COMPLETED:
        time.sleep(0.2)
    print("PASS: queued cancel appears in recent history\n")


def _wait_terminal(task, timeout=15):
    """轮询等待任务进入终态（completed/failed/cancelled），返回最终状态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = queue_manager.get_status(task)["status"]
        if st in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return st
        time.sleep(0.1)
    return queue_manager.get_status(task)["status"]


def test_submit_assigns_default_max_runtime():
    """任务级超时兜底：未指定 max_runtime 时按任务类型取默认值；显式值优先。"""
    print("=== Testing default max_runtime ===\n")
    t = queue_manager.submit(TaskType.GENERATION, task_worker, duration=0.1, name="DefRT")
    assert t.max_runtime == DEFAULT_MAX_RUNTIME[TaskType.GENERATION], t.max_runtime
    t2 = queue_manager.submit(TaskType.MIX, task_worker, duration=0.1, name="DefRT2",
                              max_runtime=123.0)
    assert t2.max_runtime == 123.0, t2.max_runtime
    _wait_terminal(t)
    _wait_terminal(t2)
    print("PASS: default/override max_runtime\n")


def test_task_timeout_marks_failed_and_signals_cancel():
    """超时（协作式）：监控线程置 timed_out 并下发 cancel_event；不配合的 worker 正常返回也判 FAILED。"""
    print("=== Testing task-level timeout ===\n")

    def slow_ignoring_cancel(_task, secs):
        # 故意不检查 cancel_event，模拟挂死/不配合，验证收尾仍判定为超时失败
        time.sleep(secs)
        return "done"

    t = queue_manager.submit(TaskType.MIX, slow_ignoring_cancel, secs=1.2, max_runtime=0.4)
    st = _wait_terminal(t, timeout=15)
    assert st == TaskStatus.FAILED, f"expected FAILED, got {st}"
    assert t.timed_out is True, "timed_out 标记应为 True"
    assert t.cancel_event.is_set(), "超时应下发 cancel_event"
    assert queue_manager.get_status(t)["error"] == TIMEOUT_ERROR, queue_manager.get_status(t)
    print("PASS: timeout -> FAILED + cancel_event\n")


def test_task_timeout_overrides_cancelled_status():
    """超时导致 worker 抛 TaskCancelledError 时，状态应为 FAILED（超时）而非 CANCELLED。"""
    print("=== Testing timeout vs cancelled status ===\n")
    t = queue_manager.submit(TaskType.MIX, cooperative_task, duration=3, name="TOCancel",
                             max_runtime=0.4)
    st = _wait_terminal(t, timeout=15)
    assert st == TaskStatus.FAILED, f"expected FAILED(timeout), got {st}"
    assert queue_manager.get_status(t)["error"] == TIMEOUT_ERROR
    print("PASS: timeout wins over cancelled\n")


def test_app_localizes_timeout_error():
    """app.py 三处失败分支必须把队列超时哨兵翻译为当前语言文案（i18n 约束）。"""
    from _app_bundle import app_bundle  # C1 拆分后源码级断言读 app bundle
    src = app_bundle()
    assert "def _localize_task_error(" in src
    assert src.count('localize_task_error(lang, status_info.get("error"))') == 3, \
        "生成/重新合成/转谱三处失败分支都应走 _localize_task_error"
    assert "TIMEOUT_ERROR" in src


if __name__ == "__main__":
    test_queue()
    test_cancel_running()
    test_cancel_queued()
    test_failed()
    test_queue_snapshot()
    test_cancel_queued_records_history()
    test_submit_assigns_default_max_runtime()
    test_task_timeout_marks_failed_and_signals_cancel()
    test_task_timeout_overrides_cancelled_status()
    test_app_localizes_timeout_error()
    print("=== All queue manager tests passed ===")

