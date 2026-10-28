"""Command line interface: argument parsing, output formatting, and exit codes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence, TextIO

from . import __version__
from .models import Task, TaskStatus, ValidationError
from .service import TaskNotFoundError, TaskService
from .storage import (
    PATH_ENV_VAR,
    CorruptStorageError,
    JsonTaskStorage,
    StorageError,
    resolve_storage_path,
)

PROG = "task-tracker"

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2
EXIT_STORAGE = 3
EXIT_INTERRUPTED = 130

TITLE_COLUMN_WIDTH = 50

STATUS_LABELS = {
    TaskStatus.TODO: "todo",
    TaskStatus.IN_PROGRESS: "in_progress",
    TaskStatus.DONE: "done",
}


def _task_id(value: str) -> int:
    try:
        task_id = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid task ID {value!r}: must be a positive integer") from None
    if task_id < 1:
        raise argparse.ArgumentTypeError(f"invalid task ID {value!r}: must be a positive integer")
    return task_id


def _status(value: str) -> TaskStatus:
    try:
        return TaskStatus.parse(value)
    except ValidationError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    status_metavar = "{" + ",".join(TaskStatus.choices()) + "}"

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Track tasks from the command line. Tasks are stored in a local JSON file.",
        epilog=f"Task file: --file, else ${PATH_ENV_VAR}, else ./tasks.json in the current directory.",
    )
    parser.add_argument("--file", metavar="PATH", help="path to the task file (default: ./tasks.json)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    commands = parser.add_subparsers(dest="command", metavar="<command>", title="commands")
    commands.required = True

    add = commands.add_parser("add", help="add a new task", description="Add a new task with status 'todo'.")
    add.add_argument("title", nargs="+", help="task title (multiple words are joined with spaces)")
    add.add_argument("-d", "--description", help="optional longer description")

    list_cmd = commands.add_parser(
        "list", aliases=["ls"], help="list tasks", description="List tasks, optionally filtered by status."
    )
    list_cmd.add_argument("-s", "--status", type=_status, metavar=status_metavar, help="only show tasks with this status")
    list_cmd.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a table")

    show = commands.add_parser("show", help="show full details of a task")
    show.add_argument("id", type=_task_id, help="task ID")

    start = commands.add_parser("start", help="mark a task as in_progress")
    start.add_argument("id", type=_task_id, help="task ID")

    done = commands.add_parser("done", help="mark a task as done")
    done.add_argument("id", type=_task_id, help="task ID")

    status = commands.add_parser("status", help="set a task's status explicitly")
    status.add_argument("id", type=_task_id, help="task ID")
    status.add_argument("status", type=_status, metavar=status_metavar, help="new status")

    delete = commands.add_parser("delete", aliases=["rm"], help="delete a task")
    delete.add_argument("id", type=_task_id, help="task ID")

    commands.add_parser(
        "recover",
        help="back up a corrupted task file and restore readable tasks",
        description=(
            "If the task file is corrupted, move it aside to '<file>.corrupt-<timestamp>' "
            "and write a new file containing every task that could be read. "
            "Does nothing if the file is healthy."
        ),
    )
    return parser


def _format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(tasks: Sequence[Task]) -> str:
    headers = ("ID", "STATUS", "TITLE", "CREATED", "UPDATED")
    rows = [
        (
            str(task.id),
            STATUS_LABELS[task.status],
            _truncate(task.title, TITLE_COLUMN_WIDTH),
            _format_time(task.created_at),
            _format_time(task.updated_at),
        )
        for task in tasks
    ]
    widths = [max(len(header), *(len(row[i]) for row in rows)) for i, header in enumerate(headers)]

    def line(cells: Sequence[str]) -> str:
        # Right-align the ID column, left-align the rest.
        parts = [cells[0].rjust(widths[0])] + [cell.ljust(widths[i]) for i, cell in enumerate(cells) if i]
        return "  ".join(parts).rstrip()

    separator = "  ".join("-" * width for width in widths)
    return "\n".join([line(headers), separator, *(line(row) for row in rows)])


def render_task_details(task: Task) -> str:
    return "\n".join(
        [
            f"ID:          {task.id}",
            f"Title:       {task.title}",
            f"Status:      {STATUS_LABELS[task.status]}",
            f"Created:     {task.created_at.isoformat()}",
            f"Updated:     {task.updated_at.isoformat()}",
            f"Description: {task.description if task.description else '(none)'}",
        ]
    )


def _recover_hint(file_arg: Optional[str]) -> str:
    command = PROG if file_arg is None else f"{PROG} --file {file_arg}"
    return (
        "Your data has not been modified. Fix the file by hand, or run "
        f"'{command} recover' to back it up and restore every readable task."
    )


def _run(args: argparse.Namespace, service: TaskService, storage: JsonTaskStorage, out: TextIO) -> int:
    command = args.command

    if command == "add":
        task = service.add_task(" ".join(args.title), args.description)
        print(f"Added task {task.id}: {task.title}", file=out)
        return EXIT_OK

    if command in ("list", "ls"):
        tasks = service.list_tasks(args.status)
        if args.json:
            print(json.dumps([task.to_dict() for task in tasks], indent=2, ensure_ascii=False), file=out)
        elif not tasks:
            suffix = f" with status '{args.status.value}'" if args.status else ""
            print(f"No tasks found{suffix}.", file=out)
        else:
            print(render_table(tasks), file=out)
            print(f"\n{len(tasks)} task{'s' if len(tasks) != 1 else ''}", file=out)
        return EXIT_OK

    if command == "show":
        print(render_task_details(service.get_task(args.id)), file=out)
        return EXIT_OK

    if command in ("start", "done", "status"):
        target = {"start": TaskStatus.IN_PROGRESS, "done": TaskStatus.DONE}.get(command, getattr(args, "status", None))
        change = service.update_status(args.id, target)
        if change.changed:
            print(
                f"Task {change.task.id} status: {change.previous_status.value} -> {change.task.status.value}",
                file=out,
            )
        else:
            print(f"Task {change.task.id} is already {change.task.status.value}.", file=out)
        return EXIT_OK

    if command in ("delete", "rm"):
        task = service.delete_task(args.id)
        print(f"Deleted task {task.id}: {task.title}", file=out)
        return EXIT_OK

    if command == "recover":
        report = storage.recover()
        if report.backup_path is None:
            print(f"Task file '{storage.path}' is healthy ({report.recovered} tasks); nothing to recover.", file=out)
        else:
            print(f"Backed up corrupted file to '{report.backup_path}'.", file=out)
            print(
                f"Restored {report.recovered} task(s); discarded {report.discarded} unreadable record(s).",
                file=out,
            )
        return EXIT_OK

    raise ValueError(f"Unhandled command: {command}")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """Run the CLI and return a process exit code (never raises for user errors)."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    def fail(message: str, code: int) -> int:
        print(f"{PROG}: error: {message}", file=err)
        return code

    parser = build_parser()
    try:
        # argparse writes its own usage/error text and exits; capture the code instead.
        original_streams = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            args = parser.parse_args(argv)
        finally:
            sys.stdout, sys.stderr = original_streams
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    storage = JsonTaskStorage(resolve_storage_path(args.file, environ))
    service = TaskService(storage)
    try:
        return _run(args, service, storage, out)
    except ValidationError as exc:
        return fail(str(exc), EXIT_USAGE)
    except TaskNotFoundError as exc:
        return fail(str(exc), EXIT_NOT_FOUND)
    except CorruptStorageError as exc:
        return fail(f"{exc}\n{_recover_hint(args.file)}", EXIT_STORAGE)
    except StorageError as exc:
        return fail(str(exc), EXIT_STORAGE)
    except KeyboardInterrupt:
        return fail("interrupted.", EXIT_INTERRUPTED)
