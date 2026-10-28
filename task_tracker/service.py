"""Business logic for managing tasks. Every mutation is persisted immediately."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Union

from .models import Task, TaskStatus, utc_now, validate_description, validate_task_id, validate_title
from .storage import JsonTaskStorage

Clock = Callable[[], datetime]


class TaskNotFoundError(LookupError):
    """Raised when no task has the requested ID."""

    def __init__(self, task_id: int) -> None:
        super().__init__(f"No task with ID {task_id}.")
        self.task_id = task_id


@dataclass(frozen=True)
class StatusChange:
    task: Task
    previous_status: TaskStatus

    @property
    def changed(self) -> bool:
        return self.task.status is not self.previous_status


class TaskService:
    def __init__(self, storage: JsonTaskStorage, clock: Clock = utc_now) -> None:
        self._storage = storage
        self._clock = clock

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Clock must return timezone-aware datetimes.")
        return moment

    def add_task(self, title: str, description: Optional[str] = None) -> Task:
        # Validate input before touching storage so bad input is reported first.
        title = validate_title(title)
        description = validate_description(description)
        state = self._storage.load()
        task = Task.new(state.next_id, title, description, self._now())
        state.tasks.append(task)
        state.next_id += 1
        self._storage.save(state)
        return task

    def list_tasks(self, status: Optional[Union[TaskStatus, str]] = None) -> list[Task]:
        tasks = self._storage.load().tasks
        if status is not None:
            wanted = TaskStatus.parse(status)
            tasks = [task for task in tasks if task.status is wanted]
        return sorted(tasks, key=lambda task: task.id)

    def get_task(self, task_id: int) -> Task:
        validate_task_id(task_id)
        for task in self._storage.load().tasks:
            if task.id == task_id:
                return task
        raise TaskNotFoundError(task_id)

    def update_status(self, task_id: int, status: Union[TaskStatus, str]) -> StatusChange:
        """Set a task's status. Setting the current status again is a no-op."""
        validate_task_id(task_id)
        new_status = TaskStatus.parse(status)
        state = self._storage.load()
        for index, task in enumerate(state.tasks):
            if task.id != task_id:
                continue
            if task.status is new_status:
                return StatusChange(task=task, previous_status=task.status)
            updated = task.with_status(new_status, self._now())
            state.tasks[index] = updated
            self._storage.save(state)
            return StatusChange(task=updated, previous_status=task.status)
        raise TaskNotFoundError(task_id)

    def delete_task(self, task_id: int) -> Task:
        validate_task_id(task_id)
        state = self._storage.load()
        for index, task in enumerate(state.tasks):
            if task.id == task_id:
                del state.tasks[index]
                self._storage.save(state)
                return task
        raise TaskNotFoundError(task_id)
