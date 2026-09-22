"""Test script for queue manager."""
import time
import threading
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


if __name__ == "__main__":
    test_queue()
    test_cancel_running()
    test_cancel_queued()
    test_failed()
    print("=== All queue manager tests passed ===")
