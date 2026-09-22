"""Task queue manager for sequential model execution.

Ensures only one model loads at a time. Tasks (generation, transcription)
are queued and executed sequentially by a background worker thread.
Progress updates are bridged back to the Gradio request thread via polling.
"""
import threading
import time
import logging
import uuid
from datetime import datetime
from typing import Callable, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

logger = logging.getLogger(__name__)


class TaskCancelledError(Exception):
    """Raised by task functions to signal cooperative cancellation."""


class TaskType(Enum):
    GENERATION = "generation"
    TRANSCRIPTION = "transcription"


class TaskStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    task_id: str
    task_type: TaskType
    func: Callable
    kwargs: dict
    status: TaskStatus = TaskStatus.QUEUED
    result: Any = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    position: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)

    _progress_queue: deque = field(default_factory=deque, repr=False)
    _progress_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def push_progress(self, progress_val: float, desc: str):
        with self._progress_lock:
            self._progress_queue.append((progress_val, desc))

    def drain_progress(self) -> list:
        with self._progress_lock:
            items = list(self._progress_queue)
            self._progress_queue.clear()
            return items


class QueueManager:
    """Manages task queue to ensure only one model loads at a time."""

    def __init__(self):
        self._queue: deque[Task] = deque()
        self._lock = threading.Lock()
        self._current_task: Optional[Task] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._start_worker()

    def _start_worker(self):
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="queue-worker")
        self._worker_thread.start()
        logger.info("Queue manager worker started")

    def _worker_loop(self):
        while not self._stop_event.is_set():
            task = None

            with self._lock:
                if self._queue:
                    task = self._queue.popleft()
                    task.status = TaskStatus.RUNNING
                    task.started_at = time.time()
                    self._current_task = task

            if task:
                logger.info(f"Starting task {task.task_id} ({task.task_type.value})")
                try:
                    result = task.func(_task=task, **task.kwargs)
                    task.result = result
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = time.time()
                    elapsed = task.completed_at - task.started_at
                    logger.info(f"Task {task.task_id} completed in {elapsed:.1f}s")
                except TaskCancelledError:
                    task.status = TaskStatus.CANCELLED
                    task.completed_at = time.time()
                    logger.info(f"Task {task.task_id} cancelled by worker")
                except Exception as e:
                    logger.exception(f"Task {task.task_id} failed: {e}")
                    task.error = str(e)
                    task.status = TaskStatus.FAILED
                    task.completed_at = time.time()

                with self._lock:
                    self._current_task = None
            else:
                self._wake_event.wait(timeout=1.0)
                self._wake_event.clear()

    def submit(self, task_type: TaskType, func: Callable, cancel_event: Optional[threading.Event] = None, **kwargs) -> Task:
        """Submit a task to the queue. Returns the Task object for polling."""
        task_id = f"{task_type.value}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        task = Task(
            task_id=task_id,
            task_type=task_type,
            func=func,
            kwargs=kwargs,
        )
        if cancel_event is not None:
            task.cancel_event = cancel_event

        with self._lock:
            task.position = len(self._queue) + 1
            self._queue.append(task)

        logger.info(f"Task {task_id} queued at position {task.position}")
        self._wake_event.set()

        return task

    def get_status(self, task: Task) -> dict:
        """Get task status and queue position."""
        with self._lock:
            if self._current_task and self._current_task.task_id == task.task_id:
                return {
                    "status": TaskStatus.RUNNING,
                    "position": 0,
                    "started_at": self._current_task.started_at,
                    "running_time": time.time() - self._current_task.started_at,
                }

            for i, t in enumerate(self._queue):
                if t.task_id == task.task_id:
                    return {
                        "status": TaskStatus.QUEUED,
                        "position": i + 1,
                        "wait_time": time.time() - t.created_at,
                    }

            if task.status == TaskStatus.COMPLETED:
                return {"status": TaskStatus.COMPLETED, "position": -1}
            elif task.status == TaskStatus.FAILED:
                return {"status": TaskStatus.FAILED, "position": -1, "error": task.error}
            elif task.status == TaskStatus.CANCELLED:
                return {"status": TaskStatus.CANCELLED, "position": -1}

            return {"status": task.status, "position": -1}

    def get_queue_info(self) -> dict:
        """Get overall queue information."""
        with self._lock:
            return {
                "current_task": self._current_task.task_id if self._current_task else None,
                "current_task_type": self._current_task.task_type.value if self._current_task else None,
                "queued_count": len(self._queue),
                "queue": [
                    {
                        "task_id": t.task_id,
                        "task_type": t.task_type.value,
                        "position": i + 1,
                    }
                    for i, t in enumerate(self._queue)
                ],
            }

    def cancel_task(self, task: Task) -> bool:
        """Cancel a task. Works for both queued and running tasks."""
        return self.cancel_task_by_id(task.task_id)

    def cancel_task_by_id(self, task_id: str) -> bool:
        """Cancel a task by ID. Works for both queued and running tasks."""
        with self._lock:
            for t in self._queue:
                if t.task_id == task_id:
                    t.status = TaskStatus.CANCELLED
                    t.cancel_event.set()
                    self._queue.remove(t)
                    logger.info(f"Task {task_id} cancelled (was queued)")
                    return True

            if self._current_task and self._current_task.task_id == task_id:
                self._current_task.cancel_event.set()
                logger.info(f"Task {task_id} cancel signal sent (was running)")
                return True

        return False

    def shutdown(self):
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)
        logger.info("Queue manager shutdown")


queue_manager = QueueManager()
