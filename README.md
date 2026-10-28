# task-tracker

A small, dependency-free command line task tracker. Tasks live in a local JSON
file that is validated on every read and written atomically on every change.

- **Standard library only.** Nothing to install beyond Python 3.10+.
- **Safe persistence.** Every change is committed immediately with an atomic
  write: temporary file, `fsync`, then `os.replace`. A crash mid-write leaves
  the previous file intact.
- **Defensive by default.** Input is validated before storage is touched. A
  corrupted task file is never overwritten silently, and a `recover` command
  backs it up and restores every readable task.

## Architecture

```
task-tracker/
├── main.py                  # Root entrypoint: python main.py <command>
├── task_tracker/
│   ├── __init__.py          # Package version
│   ├── __main__.py          # python -m task_tracker
│   ├── models.py            # Task dataclass, TaskStatus enum, validation, (de)serialization
│   ├── storage.py           # JSON storage, schema validation, atomic writes, recovery
│   ├── service.py           # Business logic: add / list / filter / update status / delete
│   └── cli.py               # argparse parser, table & detail formatting, exit codes
├── tests/                   # unittest suite (unit + integration + subprocess end-to-end)
├── pyproject.toml           # Optional install metadata (provides the `task-tracker` command)
└── requirements.txt         # Intentionally empty: no runtime dependencies
```

Layers depend only downward: `cli → service → storage → models`.

| Module       | Responsibility |
|--------------|----------------|
| `models.py`  | Immutable `Task` dataclass and `TaskStatus` enum (`todo`, `in_progress`, `done`). Validates titles (non-empty, single line, ≤ 200 chars) and descriptions (≤ 5000 chars). Strict `from_dict` / `to_dict` with ISO 8601 UTC timestamps. |
| `storage.py` | `JsonTaskStorage` loads and saves the whole store. Creates the file when it is missing, treats an empty file as a fresh store, raises `CorruptStorageError` for malformed JSON or schema violations, and implements `recover()`. |
| `service.py` | `TaskService` holds the business rules: incremental IDs that are never reused, status transitions that update `updated_at`, and not-found errors. Each mutation does load, change, then save. The clock is injectable for tests. |
| `cli.py`     | Argument parsing, output formatting (table, details, JSON), and mapping exceptions to human-readable messages and exit codes. User errors never print a traceback. |

### Storage format

```json
{
  "version": 1,
  "next_id": 3,
  "tasks": [
    {
      "id": 1,
      "title": "Write the quarterly report",
      "description": "Include Q3 revenue",
      "status": "in_progress",
      "created_at": "2026-10-28T16:02:11.482113+00:00",
      "updated_at": "2026-10-28T16:05:40.009251+00:00"
    }
  ]
}
```

`next_id` is stored explicitly, so the ID of a deleted task is never handed out
again. On load the file is checked against the schema: version, field types,
valid statuses, parseable timezone-aware timestamps, unique IDs, and
`next_id` greater than every ID. Unknown extra fields are ignored for forward
compatibility.

### Task file location

The file is chosen in this order:

1. `--file PATH` on the command line
2. The `TASK_TRACKER_FILE` environment variable
3. `./tasks.json` in the current working directory

Missing parent directories are created automatically.

## Setup

Requires Python 3.10 or newer. There are no third-party dependencies.

```bash
cd task-tracker
python3 --version          # must be 3.10+
python3 main.py --help
```

Optionally, install it to get a `task-tracker` command on your `PATH`. You can
do this in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install .
task-tracker --help
```

All three invocation styles are equivalent:

```bash
python3 main.py list
python3 -m task_tracker list
task-tracker list                   # after pip install
```

The examples below use `python3 main.py`.

## Command reference

### Global options

```bash
python3 main.py --help                         # overall help
python3 main.py add --help                     # help for one command
python3 main.py --version                      # print version
python3 main.py --file ~/work/tasks.json list  # use a specific task file
TASK_TRACKER_FILE=~/work/tasks.json python3 main.py list
```

`--file` must come **before** the command name.

### `add`: create a task

```bash
python3 main.py add "Write the quarterly report"
python3 main.py add Buy milk                                  # unquoted words are joined
python3 main.py add "Write the quarterly report" -d "Include Q3 revenue"
python3 main.py add "Fix login bug" --description "Users on Safari get a 500"
```

New tasks get the next ID and status `todo`. Leading and trailing whitespace is
trimmed. Titles that are empty, whitespace-only, multi-line, or longer than 200
characters are rejected.

### `list` (alias `ls`): show tasks

```bash
python3 main.py list
python3 main.py ls
python3 main.py list --status todo
python3 main.py list -s in_progress
python3 main.py list -s in-progress        # '-' and '_' are interchangeable, case-insensitive
python3 main.py list --status done
python3 main.py list --json                # machine-readable output
python3 main.py list -s done --json
```

Example output:

```
ID  STATUS       TITLE                       CREATED               UPDATED
--  -----------  --------------------------  --------------------  --------------------
 1  in_progress  Write the quarterly report  2026-10-28 16:02 UTC  2026-10-28 16:05 UTC
 2  done         Buy milk                    2026-10-28 16:02 UTC  2026-10-28 16:06 UTC

2 tasks
```

Titles longer than 50 characters are shortened with `…` in the table. Use
`show` or `--json` to see them in full.

### `show`: full details of one task

```bash
python3 main.py show 1
```

### `start` / `done` / `status`: change status

```bash
python3 main.py start 1                 # todo -> in_progress
python3 main.py done 1                  # -> done
python3 main.py status 1 todo           # set any status explicitly (e.g. reopen)
python3 main.py status 1 in_progress
```

Setting a task to the status it already has is a no-op and leaves `updated_at`
unchanged.

### `delete` (alias `rm`): remove a task

```bash
python3 main.py delete 2
python3 main.py rm 2
```

### `recover`: repair a corrupted task file

```bash
python3 main.py recover
python3 main.py --file ~/work/tasks.json recover
```

When the task file contains malformed JSON or fails schema validation, every
command stops with a clear message and exit code 3, and the file is left
untouched:

```
task-tracker: error: Task file '/home/me/tasks.json' is corrupted: invalid JSON at line 2, column 1 (...)
Your data has not been modified. Fix the file by hand, or run 'task-tracker recover' to back it up and restore every readable task.
```

You can then fix the file by hand, or run `recover`, which:

1. Moves the damaged file to `tasks.json.corrupt-<UTC timestamp>`, so nothing is lost.
2. Writes a fresh, valid `tasks.json` that contains every task record that still
   validates. Duplicates and invalid records are dropped and counted. If the file
   is not parseable JSON at all, the new store starts empty.

On a healthy file `recover` does nothing.

### Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Task not found |
| 2    | Invalid command, arguments, or input (e.g. empty title, bad ID, unknown status) |
| 3    | Task file is corrupted, unreadable, or unwritable |
| 130  | Interrupted (Ctrl+C) |

## Running the tests

From the `task-tracker/` directory:

```bash
python -m unittest discover tests
```

For verbose output:

```bash
python -m unittest discover tests -v
```

The suite covers:

- **`test_models.py`**: title and description validation and length boundaries,
  status parsing, ISO 8601 timestamp round-trips, strict deserialization.
- **`test_storage.py`**: initialization of missing and empty files; rejection of
  malformed JSON, non-UTF-8 bytes, and schema violations without modifying the
  file; atomic-write failure and interruption (via `unittest.mock`) with no
  partial files left behind; permission preservation; recovery and backup naming;
  path resolution.
- **`test_service.py`**: unique incremental IDs (never reused after delete),
  timestamp accuracy, status transitions, filtering, not-found handling,
  persistence across service instances.
- **`test_cli.py`**: parser behavior for valid input, boundaries, and invalid
  arguments; output formatting; exit codes; corrupt-file messaging; and
  end-to-end persistence across separate `python main.py` processes.

Every test uses its own `tempfile.TemporaryDirectory()`, and the environment
variable is controlled, so running the suite never touches a real `tasks.json`.

## Design notes and limitations

- **Concurrency:** writes are atomic, so readers never see a half-written file.
  Two processes changing the store at the same moment can still race, and the
  last writer wins. This is acceptable for a single-user CLI. Add a file lock if
  you need multi-writer safety.
- **Timestamps** are stored in UTC with microsecond precision and shown in UTC.
- **Durability:** the temporary file is `fsync`'d before the rename. On POSIX the
  containing directory is also `fsync`'d after it, so the rename itself survives
  a power loss.
