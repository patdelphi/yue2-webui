"""Test script for queue manager."""
import time
import threading
from queue_manager import queue_manager, TaskType, TaskStatus


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


if __name__ == "__main__":
    test_queue()
