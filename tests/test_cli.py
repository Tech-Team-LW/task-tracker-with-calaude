import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from task_tracker import __version__
from task_tracker.cli import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_STORAGE,
    EXIT_USAGE,
    TITLE_COLUMN_WIDTH,
    build_parser,
    main,
)
from task_tracker.models import MAX_TITLE_LENGTH, TaskStatus
from task_tracker.storage import PATH_ENV_VAR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_SCRIPT = PROJECT_ROOT / "main.py"


class CliTestCase(unittest.TestCase):
    """Runs the CLI in-process against an isolated task file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "tasks.json"

    def run_cli(self, *args, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--file", str(self.path), *args], stdout=out, stderr=err, environ=environ or {})
        return code, out.getvalue(), err.getvalue()

    def stored(self):
        return json.loads(self.path.read_text(encoding="utf-8"))


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def parse(self, *args):
        return self.parser.parse_args(list(args))

    def test_valid_inputs(self):
        args = self.parse("add", "Buy", "milk", "-d", "2 litres")
        self.assertEqual((args.command, args.title, args.description), ("add", ["Buy", "milk"], "2 litres"))
        self.assertIs(self.parse("list", "--status", "in-progress").status, TaskStatus.IN_PROGRESS)
        self.assertIsNone(self.parse("ls").status)
        self.assertEqual(self.parse("done", "42").id, 42)
        args = self.parse("status", "3", "DONE")
        self.assertEqual((args.id, args.status), (3, TaskStatus.DONE))
        self.assertEqual(self.parse("rm", "1").command, "rm")
        self.assertEqual(self.parse("--file", "x.json", "recover").file, "x.json")


class UsageErrorTests(CliTestCase):
    def assert_usage_error(self, args, message):
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, EXIT_USAGE, err)
        self.assertIn(message, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out, "")

    def test_no_command(self):
        self.assert_usage_error([], "required")

    def test_unknown_command(self):
        self.assert_usage_error(["frobnicate"], "invalid choice")

    def test_missing_arguments(self):
        self.assert_usage_error(["add"], "the following arguments are required: title")
        self.assert_usage_error(["done"], "the following arguments are required: id")
        self.assert_usage_error(["status", "1"], "the following arguments are required: {todo,in_progress,done}")

    def test_invalid_ids(self):
        for bad in ("abc", "0", "-1", "1.5", ""):
            with self.subTest(bad=bad):
                self.assert_usage_error(["done", bad], "must be a positive integer")

    def test_invalid_status(self):
        self.assert_usage_error(["status", "1", "finished"], "Invalid status 'finished'")
        self.assert_usage_error(["list", "--status", "nope"], "Invalid status 'nope'")

    def test_empty_and_whitespace_titles(self):
        for title in ("", "   ", "\t"):
            with self.subTest(title=title):
                self.assert_usage_error(["add", title], "must not be empty or whitespace-only")
        # Input is rejected before storage is touched, so no file was created.
        self.assertFalse(self.path.exists())

    def test_title_length_boundary(self):
        code, _, _ = self.run_cli("add", "x" * MAX_TITLE_LENGTH)
        self.assertEqual(code, EXIT_OK)
        self.assert_usage_error(["add", "x" * (MAX_TITLE_LENGTH + 1)], "too long")

    def test_help_and_version_exit_cleanly(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("usage: task-tracker", out)
        code, out, _ = self.run_cli("--version")
        self.assertEqual(code, EXIT_OK)
        self.assertIn(__version__, out)


class CommandTests(CliTestCase):
    def test_add_and_list(self):
        self.assertEqual(self.run_cli("add", "Buy", "milk")[1], "Added task 1: Buy milk\n")
        self.run_cli("add", "Write report", "--description", "Q3 numbers")
        code, out, err = self.run_cli("list")
        self.assertEqual(code, EXIT_OK, err)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["ID", "STATUS", "TITLE", "CREATED", "UPDATED"])
        self.assertTrue(set(lines[1]) <= {"-", " "})
        self.assertIn("Buy milk", lines[2])
        self.assertIn("Write report", lines[3])
        self.assertEqual(lines[-1], "2 tasks")

    def test_list_empty(self):
        self.assertEqual(self.run_cli("list")[1], "No tasks found.\n")
        self.assertEqual(self.run_cli("list", "-s", "done")[1], "No tasks found with status 'done'.\n")

    def test_long_titles_are_truncated_in_table(self):
        self.run_cli("add", "y" * 120)
        out = self.run_cli("list")[1]
        self.assertIn("y" * (TITLE_COLUMN_WIDTH - 1) + "…", out)
        self.assertNotIn("y" * TITLE_COLUMN_WIDTH, out)

    def test_status_lifecycle_and_filtering(self):
        for title in ("a", "b", "c"):
            self.run_cli("add", title)
        self.assertEqual(self.run_cli("start", "1")[1], "Task 1 status: todo -> in_progress\n")
        self.assertEqual(self.run_cli("done", "2")[1], "Task 2 status: todo -> done\n")
        self.assertEqual(self.run_cli("done", "2")[1], "Task 2 is already done.\n")
        self.assertEqual(self.run_cli("status", "2", "todo")[1], "Task 2 status: done -> todo\n")

        code, out, _ = self.run_cli("list", "--status", "in_progress", "--json")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([t["id"] for t in json.loads(out)], [1])

    def test_show(self):
        self.run_cli("add", "Plan", "-d", "line one\nline two")
        code, out, _ = self.run_cli("show", "1")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Title:       Plan", out)
        self.assertIn("Status:      todo", out)
        self.assertIn("line one\nline two", out)

    def test_delete(self):
        self.run_cli("add", "temp")
        self.assertEqual(self.run_cli("rm", "1")[1], "Deleted task 1: temp\n")
        self.assertEqual(self.stored()["tasks"], [])

    def test_unknown_id_returns_not_found(self):
        for args in (("show", "7"), ("start", "7"), ("done", "7"), ("delete", "7")):
            with self.subTest(args=args):
                code, out, err = self.run_cli(*args)
                self.assertEqual(code, EXIT_NOT_FOUND)
                self.assertEqual(err, "task-tracker: error: No task with ID 7.\n")

    def test_json_output_round_trips_stored_fields(self):
        self.run_cli("add", "Café ☕")
        task = json.loads(self.run_cli("list", "--json")[1])[0]
        self.assertEqual(task, self.stored()["tasks"][0])
        self.assertEqual(task["title"], "Café ☕")

    def test_environment_variable_selects_file(self):
        env_path = self.dir / "from-env.json"
        out = io.StringIO()
        code = main(["add", "env task"], stdout=out, stderr=io.StringIO(), environ={PATH_ENV_VAR: str(env_path)})
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(env_path.read_text())["tasks"][0]["title"], "env task")


class CorruptFileCliTests(CliTestCase):
    def test_corrupt_file_reports_error_without_traceback(self):
        self.path.write_text("{ definitely not json", encoding="utf-8")
        code, out, err = self.run_cli("list")
        self.assertEqual(code, EXIT_STORAGE)
        self.assertEqual(out, "")
        self.assertIn("is corrupted: invalid JSON", err)
        self.assertIn("recover", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.path.read_text(), "{ definitely not json")

    def test_mutations_refuse_to_overwrite_corrupt_file(self):
        self.path.write_text('{"version": 1}', encoding="utf-8")
        code, _, _ = self.run_cli("add", "new")
        self.assertEqual(code, EXIT_STORAGE)
        self.assertEqual(self.path.read_text(), '{"version": 1}')

    def test_recover_then_continue(self):
        self.run_cli("add", "survivor")
        self.run_cli("add", "victim")
        doc = self.stored()
        doc["tasks"][1]["status"] = "???"
        self.path.write_text(json.dumps(doc), encoding="utf-8")

        code, out, err = self.run_cli("recover")
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("Restored 1 task(s); discarded 1 unreadable record(s).", out)
        self.assertEqual(len(list(self.dir.glob("tasks.json.corrupt-*"))), 1)

        self.assertEqual([t["title"] for t in json.loads(self.run_cli("list", "--json")[1])], ["survivor"])
        self.assertEqual(self.run_cli("add", "next")[1], "Added task 3: next\n")

    def test_recover_on_healthy_file(self):
        self.run_cli("add", "fine")
        code, out, _ = self.run_cli("recover")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("nothing to recover", out)


class SeparateProcessTests(unittest.TestCase):
    """End-to-end: each command is a separate interpreter process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.env = {k: v for k, v in os.environ.items() if k != PATH_ENV_VAR}
        self.env["PYTHONIOENCODING"] = "utf-8"

    def run_process(self, *args):
        return subprocess.run(
            [sys.executable, str(MAIN_SCRIPT), *args],
            cwd=self.dir,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

    def test_persistence_across_invocations_in_default_location(self):
        self.assertEqual(self.run_process("add", "first", "-d", "details").returncode, 0)
        self.assertEqual(self.run_process("add", "second").returncode, 0)
        self.assertEqual(self.run_process("start", "1").returncode, 0)
        self.assertEqual(self.run_process("delete", "2").returncode, 0)

        self.assertTrue((self.dir / "tasks.json").exists())
        result = self.run_process("list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        tasks = json.loads(result.stdout)
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]["id"], tasks[0]["status"], tasks[0]["description"]), (1, "in_progress", "details"))

    def test_errors_exit_nonzero_without_traceback(self):
        (self.dir / "tasks.json").write_text("[[[", encoding="utf-8")
        result = self.run_process("list")
        self.assertEqual(result.returncode, EXIT_STORAGE)
        self.assertNotIn("Traceback", result.stderr)

        result = self.run_process("add", "   ")
        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertNotIn("Traceback", result.stderr)

    def test_module_entrypoint(self):
        result = subprocess.run(
            [sys.executable, "-m", "task_tracker", "--version"],
            cwd=PROJECT_ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(__version__, result.stdout)


if __name__ == "__main__":
    unittest.main()
