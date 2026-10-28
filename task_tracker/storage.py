"""JSON file storage with schema validation, atomic writes, and corruption recovery.

On-disk format (schema version 1)::

    {
      "version": 1,
      "next_id": 4,
      "tasks": [ {<Task.to_dict()>}, ... ]
    }

``next_id`` is persisted separately from the task list so that IDs of deleted
tasks are never reused.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Union

from .models import Task, ValidationError

SCHEMA_VERSION = 1
DEFAULT_FILENAME = "tasks.json"
PATH_ENV_VAR = "TASK_TRACKER_FILE"

PathLike = Union[str, "os.PathLike[str]"]


class StorageError(Exception):
    """Raised when the task file cannot be read or written."""


class CorruptStorageError(StorageError):
    """Raised when the task file exists but holds malformed JSON or an invalid schema."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"Task file '{path}' is corrupted: {reason}")
        self.path = path
        self.reason = reason


@dataclass
class StoreState:
    """The full contents of the task store."""

    tasks: list[Task] = field(default_factory=list)
    next_id: int = 1


@dataclass(frozen=True)
class RecoveryReport:
    """Outcome of :meth:`JsonTaskStorage.recover`.

    ``backup_path`` is None when the file was healthy and nothing was changed.
    """

    backup_path: Optional[Path]
    recovered: int
    discarded: int


def resolve_storage_path(
    explicit: Optional[PathLike] = None, environ: Optional[Mapping[str, str]] = None
) -> Path:
    """Pick the task file: explicit argument, then $TASK_TRACKER_FILE, then ./tasks.json."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser()
    env = os.environ if environ is None else environ
    from_env = env.get(PATH_ENV_VAR, "")
    if from_env.strip():
        return Path(from_env).expanduser()
    return Path.cwd() / DEFAULT_FILENAME


def encode_state(state: StoreState) -> str:
    payload = {
        "version": SCHEMA_VERSION,
        "next_id": state.next_id,
        "tasks": [task.to_dict() for task in state.tasks],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def decode_state(data: object) -> StoreState:
    """Validate parsed JSON against the schema and build a StoreState."""
    if not isinstance(data, dict):
        raise ValidationError(f"top-level value must be a JSON object, got {_json_type(data)}")

    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValidationError("'version' must be an integer")
    if version > SCHEMA_VERSION:
        raise ValidationError(
            f"schema version {version} is newer than this program supports ({SCHEMA_VERSION})"
        )
    if version != SCHEMA_VERSION:
        raise ValidationError(f"unsupported schema version {version}")

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValidationError(f"'tasks' must be a JSON array, got {_json_type(raw_tasks)}")

    tasks: list[Task] = []
    seen_ids: set[int] = set()
    for index, record in enumerate(raw_tasks):
        try:
            task = Task.from_dict(record)
        except ValidationError as exc:
            raise ValidationError(f"tasks[{index}]: {exc}") from None
        if task.id in seen_ids:
            raise ValidationError(f"tasks[{index}]: duplicate task id {task.id}")
        seen_ids.add(task.id)
        tasks.append(task)

    next_id = data.get("next_id")
    if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
        raise ValidationError("'next_id' must be a positive integer")
    highest = max(seen_ids, default=0)
    if next_id <= highest:
        raise ValidationError(f"'next_id' ({next_id}) must be greater than the highest task id ({highest})")

    return StoreState(tasks=tasks, next_id=next_id)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so readers only ever see the old or the new content.

    The data goes to a temporary file in the same directory, is fsync'd, and is
    then moved over the target with os.replace(), which is atomic on POSIX and
    Windows. The temporary file is removed if anything fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates files as 0600; keep the existing file's mode if there is one.
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp_path, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Persist the rename itself (POSIX only; a no-op where unsupported)."""
    if os.name != "posix":
        return
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _describe_os_error(exc: OSError) -> str:
    return exc.strerror or str(exc)


class JsonTaskStorage:
    """Loads and saves the whole task store as a single JSON document."""

    def __init__(self, path: PathLike) -> None:
        self.path = Path(path)

    def load(self) -> StoreState:
        """Read and validate the store, initializing an empty file if none exists.

        Raises CorruptStorageError for malformed content and StorageError for I/O
        failures. A corrupt file is never modified by this method.
        """
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            state = StoreState()
            self.save(state)
            return state
        except IsADirectoryError:
            raise StorageError(f"Task file path '{self.path}' is a directory, not a file.") from None
        except OSError as exc:
            raise StorageError(f"Cannot read task file '{self.path}': {_describe_os_error(exc)}.") from None

        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CorruptStorageError(self.path, "file is not valid UTF-8 text") from None

        if not text.strip():
            # A zero-byte file (e.g. created with `touch`) is treated as a fresh store.
            state = StoreState()
            self.save(state)
            return state

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CorruptStorageError(
                self.path, f"invalid JSON at line {exc.lineno}, column {exc.colno} ({exc.msg})"
            ) from None

        try:
            return decode_state(data)
        except ValidationError as exc:
            raise CorruptStorageError(self.path, str(exc)) from None

    def save(self, state: StoreState) -> None:
        try:
            atomic_write_text(self.path, encode_state(state))
        except OSError as exc:
            raise StorageError(f"Cannot write task file '{self.path}': {_describe_os_error(exc)}.") from None

    def recover(self, now: Optional[datetime] = None) -> RecoveryReport:
        """Back up a corrupt task file and replace it with every task that can be salvaged.

        If the file is healthy (or missing) nothing is changed. The original bytes
        are always preserved in the backup file next to the task file.
        """
        healthy: Optional[StoreState]
        try:
            healthy = self.load()
        except CorruptStorageError:
            healthy = None
        if healthy is not None:
            return RecoveryReport(backup_path=None, recovered=len(healthy.tasks), discarded=0)

        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise StorageError(f"Cannot read task file '{self.path}': {_describe_os_error(exc)}.") from None

        salvaged, discarded = _salvage(raw)
        backup_path = self._backup_path(now or datetime.now(timezone.utc))
        try:
            os.replace(self.path, backup_path)
        except OSError as exc:
            raise StorageError(
                f"Cannot back up corrupt task file to '{backup_path}': {_describe_os_error(exc)}."
            ) from None
        self.save(salvaged)
        return RecoveryReport(backup_path=backup_path, recovered=len(salvaged.tasks), discarded=discarded)

    def _backup_path(self, now: datetime) -> Path:
        stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        counter = 1
        while candidate.exists():
            candidate = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{counter}")
            counter += 1
        return candidate


def _salvage(raw: bytes) -> tuple[StoreState, int]:
    """Extract every individually valid task from damaged content.

    Returns the rebuilt state and the number of task records that were dropped.
    Unparseable JSON yields an empty store.
    """
    try:
        data = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError:
        return StoreState(), 0

    if isinstance(data, list):
        candidates: list[object] = data
        stored_next_id: object = None
    elif isinstance(data, dict) and isinstance(data.get("tasks"), list):
        candidates = data["tasks"]
        stored_next_id = data.get("next_id")
    else:
        return StoreState(), 0

    tasks: list[Task] = []
    seen_ids: set[int] = set()
    discarded = 0
    for record in candidates:
        try:
            task = Task.from_dict(record)
        except ValidationError:
            discarded += 1
            continue
        if task.id in seen_ids:
            discarded += 1
            continue
        seen_ids.add(task.id)
        tasks.append(task)

    next_id = max(seen_ids, default=0) + 1
    if isinstance(stored_next_id, int) and not isinstance(stored_next_id, bool):
        next_id = max(next_id, stored_next_id)
    return StoreState(tasks=tasks, next_id=next_id), discarded
