# CLAUDE.md

Guidance for working on **task-tracker**, a dependency-free Python CLI for
tracking tasks in a local JSON file. User-facing documentation is in
`README.md`. This file covers how the code is put together, the rules that must
not break, and the latest test report.

## Quick commands

Run everything from this directory (`task-tracker/`).

```bash
python3 -m unittest discover tests          # full suite (92 tests, ~1.5 s)
python3 -m unittest discover tests -v       # verbose
python3 -m unittest tests.test_storage      # one module
python3 main.py --help                      # run the app
python3 -m task_tracker --help              # same, as a module
TASK_TRACKER_FILE=/tmp/t.json python3 main.py list   # use a throwaway task file
```

Coverage (`coverage` is not installed; use the stdlib tracer):

```bash
python3 -m trace --count --summary --missing -C /tmp/trace \
  --ignore-dir="$(python3 -c 'import sysconfig;print(sysconfig.get_paths()["stdlib"])')" \
  --module unittest discover tests
```

## Hard constraints (from the original spec)

- **Standard library only.** Never add a third-party runtime or test dependency.
  `requirements.txt` stays empty apart from its comment.
- **Python 3.10+.** Don't use 3.11+ features such as `datetime.UTC`,
  `typing.Self`, `ExceptionGroup`, or `fromisoformat` with a trailing `Z`
  (`parse_timestamp` normalizes `Z` by hand). Keep
  `from __future__ import annotations` in each module.
- **No placeholders.** No `pass`, `...`, or `# TODO` in the code; use
  `contextlib.suppress` or restructure instead.
- **No hardcoded paths, credentials, or personal data.**
- **Don't run git or deployment commands** unless the user explicitly asks.
- **Tests must be isolated.** Every test uses `tempfile.TemporaryDirectory()`.
  CLI tests pass `--file` and an explicit `environ`. Subprocess tests strip
  `TASK_TRACKER_FILE` from the environment. Never touch a real `tasks.json`.

## Architecture

Dependencies only go downward: `cli → service → storage → models`.

| File | Role | Key items |
|------|------|-----------|
| `main.py` | Root entrypoint | `raise SystemExit(main())` |
| `task_tracker/__init__.py` | Version | `__version__ = "2.0.0"` (pyproject reads it dynamically) |
| `task_tracker/__main__.py` | `python -m task_tracker` | |
| `task_tracker/models.py` | Domain and validation | frozen `Task` dataclass, `TaskStatus(str, Enum)`, `ValidationError(ValueError)`, `validate_title` / `validate_description` / `validate_task_id`, `format_timestamp` / `parse_timestamp`, `Task.to_dict` / `from_dict` |
| `task_tracker/storage.py` | Persistence | `JsonTaskStorage.load/save/recover`, `decode_state` (schema validation), `encode_state`, `atomic_write_text`, `resolve_storage_path`, `StorageError` → `CorruptStorageError`, `StoreState`, `RecoveryReport` |
| `task_tracker/service.py` | Business logic | `TaskService.add_task/list_tasks/get_task/update_status/delete_task`, `TaskNotFoundError(LookupError)`, `StatusChange`; injectable `clock` |
| `task_tracker/cli.py` | argparse and output | `build_parser`, `main(argv, *, stdout, stderr, environ) -> int`, `render_table`, `render_task_details`, exit-code constants |

### Rules the code relies on

1. **Validate before touching storage.** `TaskService.add_task` validates the
   title and description before `load()`. Bad input therefore gets exit 2 even
   when the file is corrupt, and a missing file is not created.
2. **A corrupt file is never written by normal commands.** `load()` raises
   `CorruptStorageError` and changes nothing. Only `recover()` changes a corrupt
   file, and it first moves the original to `<name>.corrupt-<UTC stamp>[-n]`.
3. **IDs are never reused.** `next_id` is stored in the file, and
   `decode_state` requires `next_id > max(id)`.
4. **Every write is atomic:** `mkstemp` in the same directory, write, `fsync`,
   copy the existing file mode, `os.replace`, then `fsync` the directory on
   POSIX. The temp file is removed on any exception, including `BaseException`.
5. **Missing or 0-byte file:** treated as an empty store and written out
   immediately.
6. **Timestamps** are timezone-aware, normalized to UTC, and serialized with
   microseconds (`2026-10-28T16:02:11.482113+00:00`). The table shows
   `YYYY-MM-DD HH:MM UTC`.
7. **Setting a task to its current status is a no-op**, so `updated_at` does not
   change and nothing is saved.
8. **The CLI never shows a traceback for user or storage errors.** `main()`
   catches argparse's `SystemExit` and maps the domain exceptions:

| Exit | Raised by |
|------|-----------|
| 0 | success, `--help`, `--version` |
| 1 | `TaskNotFoundError` |
| 2 | argparse usage errors, `ValidationError` |
| 3 | `StorageError` / `CorruptStorageError` (corrupt message includes the `recover` hint) |
| 130 | `KeyboardInterrupt` |

### On-disk schema (version 1)

```json
{"version": 1, "next_id": 3, "tasks": [{"id": 1, "title": "...", "description": null,
 "status": "todo", "created_at": "...+00:00", "updated_at": "...+00:00"}]}
```

Unknown keys are ignored, both at the top level and on tasks. If the schema
changes, bump `SCHEMA_VERSION`, migrate older versions in `decode_state`, and
keep the "newer version" error for files from the future.

### Validation limits

- Title: stripped; non-empty; at most 200 characters (`MAX_TITLE_LENGTH`);
  printable characters only, so no newlines.
- Description: stripped; blank becomes `None`; at most 5000 characters; may
  span multiple lines.
- ID: `int` and not `bool`, at least 1. In the CLI, non-integers, `0`, and
  negative values give exit 2.
- Status: case-insensitive, and `-` is accepted for `_` (`in-progress`).

### CLI surface

`add TITLE... [-d DESC]`, `list|ls [-s STATUS] [--json]`, `show ID`,
`start ID`, `done ID`, `status ID STATUS`, `delete|rm ID`, `recover`.
Global options `--file PATH` (must come before the command) and `--version`.
The file is chosen in this order: `--file`, then `$TASK_TRACKER_FILE`, then
`./tasks.json`.

## Test layout

| File | Tests | Covers |
|------|-------|--------|
| `tests/test_models.py` | 21 | title and description validation and boundaries, status parsing, timestamps, `to_dict`/`from_dict` |
| `tests/test_storage.py` | 28 | init missing/empty/nested, corruption cases (file left untouched), atomic-write failure and interrupt via `mock`, permission preservation, recovery and salvage, path resolution |
| `tests/test_service.py` | 18 | IDs (sequential, never reused), timestamp accuracy (real and fake clock), status changes, filters, delete, persistence across instances |
| `tests/test_cli.py` | 25 | parser, usage errors, output formats, exit codes, corrupt-file messages, `recover`, end-to-end subprocess persistence, `python -m` |

Keep test modules independent; there is no shared helper module. They must
pass under both `python -m unittest discover tests` and `python -m unittest`.

---

## Test report — 2026-10-28

**Environment:** Python 3.12.9, Linux 6.12 (x86_64), run as root.
**Overall:** ✅ All functional requirements work. Two robustness gaps were
found (see Open issues). No crashes or tracebacks, and no data corruption was
observed.

### 1. Automated unit and integration suite

`python3 -m unittest discover tests`: **92 / 92 passed** in about 1.4 s. The
suite also passes under `python3 -m unittest` (discovery from the project root).

Line coverage, measured with stdlib `trace`:

| Module | Lines | Coverage | Uncovered |
|--------|-------|----------|-----------|
| `service.py` | 73 | **100%** | none |
| `models.py` | 132 | **98%** | `TaskStatus.__str__`, non-string status branch |
| `cli.py` | 178 | **97%** | generic `StorageError` and `KeyboardInterrupt` handlers, unreachable "unhandled command" guard |
| `storage.py` | 224 | **92%** | OS-error branches (read/backup failures, dir-fsync failure), `_json_type` branches, unsupported old schema version |

Every uncovered branch in the table was then exercised by hand in sections 3–4,
except `TaskStatus.__str__`, the non-string status branch, the unhandled-command
guard, the old-schema-version branch and the extra `_json_type` branches.

### 2. Black-box acceptance (real `main.py` subprocesses)

**97 / 97 checks passed.** Results by area:

| Area | Checks | Result | What was verified |
|------|--------|--------|-------------------|
| Global / help | 12 | ✅ | `--help` for every command, `--version`, no command → 2, unknown command → 2 |
| Persistence / init | 2 | ✅ | missing file auto-created with `{"version":1,"next_id":1,"tasks":[]}` |
| Add | 18 | ✅ | quoted and unquoted titles, `-d` / `--description`, trimming, Unicode, 200 chars OK / 201 rejected, empty, whitespace and newline titles rejected (exit 2), titles starting with `-` via `--`, default `todo`, `created_at == updated_at`, ISO 8601 UTC, sequential unique IDs |
| List | 9 | ✅ | table headers and footer, `…` truncation at 50 chars, `ls` alias, `--status` / `-s` filters, empty-filter message, invalid filter → 2, valid `--json` |
| Show | 2 | ✅ | details including description; unknown ID → 1 |
| Update status | 12 | ✅ | `start`, `done`, repeated `done` is a no-op, `status` with `In-Progress`, reopen to `todo`, invalid status → 2, IDs `abc` / `0` / `-1` / `1.5` → 2, unknown ID → 1, `updated_at` moves forward while `created_at` stays |
| Delete | 4 | ✅ | `delete`, `rm` alias, double delete → 1, deleted IDs not reused |
| Config | 6 | ✅ | `--file` creates nested dirs, `TASK_TRACKER_FILE` works, `--file` overrides env, directory used as file → exit 3 with a clear message |
| Corruption | 25 | ✅ | for truncated JSON, binary garbage, top-level array, missing version, future version, bad status, duplicate IDs and `next_id` too low: `list` → 3 with recover hint, `add` refused, **file byte-for-byte unchanged**; 0-byte file treated as fresh |
| Recovery | 6 | ✅ | salvages valid records and counts discarded ones, backup file created, app usable afterwards with no ID reuse, no-op on a healthy file, unparseable file → empty store |
| Atomic | 1 | ✅ | no stray `.tmp` files after normal operation |

### 3. Stress and robustness

| Test | Result | Notes |
|------|--------|-------|
| **SIGKILL during writes**: 150 `add` processes, 130 killed at random points | ✅ **0 corrupt reads**; final file valid | ⚠️ **6 orphaned `.kill.json.*.tmp` files** were left behind. SIGKILL skips cleanup. Harmless to correctness, but they accumulate (see issue 2). |
| **Concurrent writers**: 20 parallel `add` | ⚠️ File stayed valid and no duplicate IDs, but **only 11 of 20 tasks were saved**, and all 20 processes exited 0 | Lost updates from load → modify → save without a lock (see issue 1) |
| **Scale**: 1,000 tasks (218 KiB file) | ✅ | `list`, `list -s`, `add`, `done`, `delete` each about 55–67 ms per invocation including interpreter start; in-process `add` about 11 ms, mostly `fsync` |
| **Read permission denied** (simulated with mock; root ignores chmod) | ✅ | exit 3, `Cannot read task file '…': Permission denied.` |
| **Disk full on write** (simulated) | ✅ | exit 3, `Cannot write task file '…': No space left on device.`; original file intact |
| **Ctrl+C** (simulated `KeyboardInterrupt`) | ✅ | exit 130, `task-tracker: error: interrupted.` |
| **Python 3.10 syntax** (`ast.parse(feature_version=(3,10))` on all files) | ✅ | Every file parses as 3.10 syntax. A real 3.10 interpreter was not available, so runtime behavior on 3.10 is untested. |
| **Packaging**: `pip install .` in a fresh venv | ✅ | `task-tracker` console script works (`add`, `list`, `--version`) |

### 4. Requirements checklist

| Requirement | Status |
|-------------|--------|
| Add task (title, optional description, unique ID, `todo`, ISO 8601 created/updated) | ✅ |
| List all tasks and filter by status, as a table | ✅ |
| Mark `in_progress` / `done` by ID | ✅ (plus `status` for any value, including reopening) |
| Delete by ID | ✅ |
| JSON persistence, committed immediately, survives exit | ✅ verified across separate processes |
| Reject empty or whitespace titles with clear messages | ✅ |
| Auto-initialize a missing file | ✅ |
| Handle corrupt JSON or schema without a traceback, with a recovery path | ✅ (`recover` command) |
| Atomic writes (temp file then replace) | ✅ verified under SIGKILL |
| Standard library only, modules split as specified | ✅ |
| No placeholders (`pass` / `...` / TODO) | ✅ (checked with grep) |
| `unittest` suite in `tests/` with isolated temp dirs | ✅ |
| README with architecture, setup, command reference, test command | ✅ |

### 5. Open issues and recommendations

1. **Concurrent writes lose updates (medium).** Two processes that write at the
   same time both succeed, but the last writer overwrites the other's change.
   The file never corrupts. Fix: take an exclusive lock around load → save in
   `TaskService`, using `fcntl.flock` on POSIX and `msvcrt.locking` on Windows.
   Use a sidecar `tasks.json.lock`, because the data file is replaced on every
   write. Add a test with N parallel subprocess `add` calls expecting N tasks.
2. **Orphaned temp files after hard kills (low).** `.tasks.json.*.tmp` files
   stay behind if the process is SIGKILLed or loses power mid-write. Fix: on
   `save()`, delete `.<name>.*.tmp` siblings older than a few minutes. Also
   consider having `recover` report them.
3. **Python 3.10 runtime untested (low).** Syntax is verified, but CI should
   run the suite on 3.10, 3.11, 3.12 and 3.13.
4. **Untested by any automated test (low).** Generic `StorageError` and
   `KeyboardInterrupt` paths in `cli.main`, and the backup-rename failure in
   `recover`. The first two were confirmed working in the robustness tests in
   section 3; the backup-rename failure was not exercised at all. Turning them
   into unit tests with `mock` would make `cli.py` and `storage.py` close to
   fully covered.
5. **Possible additions:** `edit ID --title/--description`, sorting options for
   `list`, and a confirmation or `--yes` flag for `delete`.
