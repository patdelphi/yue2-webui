"""Test script for queue manager."""
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from queue_manager import queue_manager, TaskType, TaskStatus, TaskCancelledError


def test_task(_task, duration, name):
    """Simulate a task that takes time."""
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
    task1 = queue_manager.submit(TaskType.GENERATION, test_task, duration=3, name="Task1")
    print(f"Submitted {task1.task_id}")
    
    time.sleep(0.5)
    
    task2 = queue_manager.submit(TaskType.TRANSCRIPTION, test_task, duration=2, name="Task2")
    print(f"Submitted {task2.task_id}")
    
    time.sleep(0.5)
    
    task3 = queue_manager.submit(TaskType.GENERATION, test_task, duration=2, name="Task3")
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
    blocker = queue_manager.submit(TaskType.GENERATION, test_task, duration=4, name="Blocker")
    queued = queue_manager.submit(TaskType.TRANSCRIPTION, test_task, duration=2, name="Queued")
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
    t1 = queue_manager.submit(TaskType.GENERATION, test_task, duration=1, name="Snap1")
    t2 = queue_manager.submit(TaskType.TRANSCRIPTION, test_task, duration=0.6, name="Snap2")
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


if __name__ == "__main__":
    test_queue()
    test_cancel_running()
    test_cancel_queued()
    test_failed()
    test_queue_snapshot()
    print("=== All queue manager tests passed ===")
