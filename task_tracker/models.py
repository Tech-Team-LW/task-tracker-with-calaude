"""Task domain model, input validation, and JSON (de)serialization."""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 5000


class ValidationError(ValueError):
    """Raised when user-supplied or stored task data is invalid."""


class TaskStatus(str, enum.Enum):
    """Lifecycle states a task can be in."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def choices(cls) -> list[str]:
        return [status.value for status in cls]

    @classmethod
    def parse(cls, value: object) -> "TaskStatus":
        """Parse a status leniently: case-insensitive, '-' accepted for '_'."""
        if isinstance(value, TaskStatus):
            return value
        if not isinstance(value, str):
            raise ValidationError(f"Status must be a string, got {type(value).__name__}.")
        normalized = value.strip().lower().replace("-", "_")
        try:
            return cls(normalized)
        except ValueError:
            raise ValidationError(
                f"Invalid status {value!r}. Expected one of: {', '.join(cls.choices())}."
            ) from None


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def format_timestamp(moment: datetime) -> str:
    """Serialize a timezone-aware datetime as an ISO 8601 UTC string."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Timestamps must be timezone-aware.")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_timestamp(value: object, field_name: str) -> datetime:
    """Parse an ISO 8601 timestamp string into a timezone-aware UTC datetime."""
    if not isinstance(value, str):
        raise ValidationError(f"'{field_name}' must be an ISO 8601 string.")
    text = value.strip()
    # datetime.fromisoformat() only accepts a trailing 'Z' from Python 3.11 on.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"'{field_name}' is not a valid ISO 8601 timestamp: {value!r}.") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValidationError(f"'{field_name}' must include a timezone offset: {value!r}.")
    return moment.astimezone(timezone.utc)


def validate_title(title: object) -> str:
    """Return a normalized title, or raise ValidationError if it is unusable."""
    if not isinstance(title, str):
        raise ValidationError("Task title must be a string.")
    normalized = title.strip()
    if not normalized:
        raise ValidationError("Task title must not be empty or whitespace-only.")
    if len(normalized) > MAX_TITLE_LENGTH:
        raise ValidationError(
            f"Task title is too long ({len(normalized)} characters; maximum is {MAX_TITLE_LENGTH})."
        )
    if any(not ch.isprintable() for ch in normalized):
        raise ValidationError("Task title must be a single line without control characters.")
    return normalized


def validate_description(description: object) -> Optional[str]:
    """Return a normalized description (None when blank), or raise ValidationError."""
    if description is None:
        return None
    if not isinstance(description, str):
        raise ValidationError("Task description must be a string.")
    normalized = description.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_DESCRIPTION_LENGTH:
        raise ValidationError(
            f"Task description is too long ({len(normalized)} characters; "
            f"maximum is {MAX_DESCRIPTION_LENGTH})."
        )
    return normalized


def validate_task_id(task_id: object, field_name: str = "id") -> int:
    # bool is a subclass of int; reject it explicitly so `true` is never an ID.
    if isinstance(task_id, bool) or not isinstance(task_id, int):
        raise ValidationError(f"'{field_name}' must be an integer.")
    if task_id < 1:
        raise ValidationError(f"'{field_name}' must be a positive integer, got {task_id}.")
    return task_id


@dataclass(frozen=True)
class Task:
    """An immutable task record. Mutations return new instances."""

    id: int
    title: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    description: Optional[str] = None

    @classmethod
    def new(
        cls, task_id: int, title: str, description: Optional[str], now: datetime
    ) -> "Task":
        return cls(
            id=validate_task_id(task_id),
            title=validate_title(title),
            description=validate_description(description),
            status=TaskStatus.TODO,
            created_at=now,
            updated_at=now,
        )

    def with_status(self, status: TaskStatus, now: datetime) -> "Task":
        return replace(self, status=status, updated_at=now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "created_at": format_timestamp(self.created_at),
            "updated_at": format_timestamp(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: object) -> "Task":
        """Build a Task from its stored form, validating every field strictly."""
        if not isinstance(data, Mapping):
            raise ValidationError(f"Task record must be a JSON object, got {type(data).__name__}.")
        required = ("id", "title", "status", "created_at", "updated_at")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValidationError(f"Task record is missing required field(s): {', '.join(missing)}.")

        status_value = data["status"]
        if not isinstance(status_value, str) or status_value not in TaskStatus.choices():
            raise ValidationError(
                f"'status' must be one of {', '.join(TaskStatus.choices())}, got {status_value!r}."
            )

        return cls(
            id=validate_task_id(data["id"]),
            title=validate_title(data["title"]),
            description=validate_description(data.get("description")),
            status=TaskStatus(status_value),
            created_at=parse_timestamp(data["created_at"], "created_at"),
            updated_at=parse_timestamp(data["updated_at"], "updated_at"),
        )
