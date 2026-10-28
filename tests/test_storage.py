import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from task_tracker.models import Task, TaskStatus
from task_tracker.storage import (
    DEFAULT_FILENAME,
    PATH_ENV_VAR,
    SCHEMA_VERSION,
    CorruptStorageError,
    JsonTaskStorage,
    StorageError,
    StoreState,
    atomic_write_text,
    resolve_storage_path,
)

NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def make_task(task_id, title="Task", status=TaskStatus.TODO):
    return Task.new(task_id, f"{title} {task_id}", None, NOW).with_status(status, NOW)


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "tasks.json"
        self.storage = JsonTaskStorage(self.path)

    def write_raw(self, content):
        if isinstance(content, bytes):
            self.path.write_bytes(content)
        else:
            self.path.write_text(content, encoding="utf-8")

    def write_json(self, data):
        self.write_raw(json.dumps(data))

    def valid_document(self, tasks=None, next_id=None):
        tasks = tasks if tasks is not None else [make_task(1), make_task(2)]
        return {
            "version": SCHEMA_VERSION,
            "next_id": next_id if next_id is not None else len(tasks) + 1,
            "tasks": [task.to_dict() for task in tasks],
        }


class LoadSaveTests(StorageTestCase):
    def test_missing_file_is_initialized(self):
        state = self.storage.load()
        self.assertEqual(state.tasks, [])
        self.assertEqual(state.next_id, 1)
        self.assertTrue(self.path.exists())
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"version": SCHEMA_VERSION, "next_id": 1, "tasks": []},
        )

    def test_missing_parent_directories_are_created(self):
        storage = JsonTaskStorage(self.dir / "nested" / "deeper" / "tasks.json")
        storage.load()
        self.assertTrue(storage.path.exists())

    def test_empty_and_whitespace_files_are_treated_as_fresh(self):
        for content in ("", "   \n\t"):
            with self.subTest(content=content):
                self.write_raw(content)
                state = self.storage.load()
                self.assertEqual(state.tasks, [])
                self.assertEqual(json.loads(self.path.read_text())["tasks"], [])

    def test_round_trip(self):
        state = StoreState(tasks=[make_task(1), make_task(5, status=TaskStatus.DONE)], next_id=9)
        self.storage.save(state)
        loaded = JsonTaskStorage(self.path).load()
        self.assertEqual(loaded.tasks, state.tasks)
        self.assertEqual(loaded.next_id, 9)

    def test_utf8_bom_is_accepted(self):
        self.write_raw(b"\xef\xbb\xbf" + json.dumps(self.valid_document()).encode("utf-8"))
        self.assertEqual(len(self.storage.load().tasks), 2)

    def test_unknown_fields_are_tolerated(self):
        doc = self.valid_document()
        doc["extra"] = True
        doc["tasks"][0]["tags"] = ["x"]
        self.write_json(doc)
        self.assertEqual(len(self.storage.load().tasks), 2)

    def test_directory_path_reports_storage_error(self):
        storage = JsonTaskStorage(self.dir)
        with self.assertRaisesRegex(StorageError, "is a directory"):
            storage.load()


class CorruptionTests(StorageTestCase):
    def assert_corrupt(self, reason_pattern):
        original = self.path.read_bytes()
        with self.assertRaisesRegex(CorruptStorageError, reason_pattern):
            self.storage.load()
        # A corrupt file must never be overwritten by a load.
        self.assertEqual(self.path.read_bytes(), original)

    def test_malformed_json(self):
        self.write_raw('{"version": 1, "tasks": [')
        self.assert_corrupt("invalid JSON at line 1")

    def test_non_utf8_bytes(self):
        self.write_raw(b"\xff\xfe\x00garbage")
        self.assert_corrupt("not valid UTF-8")

    def test_wrong_top_level_type(self):
        self.write_json([1, 2, 3])
        self.assert_corrupt("must be a JSON object, got array")

    def test_schema_violations(self):
        cases = {
            "missing version": ({"next_id": 1, "tasks": []}, "'version'"),
            "newer version": ({"version": 99, "next_id": 1, "tasks": []}, "newer"),
            "tasks not a list": ({"version": 1, "next_id": 1, "tasks": {}}, "'tasks' must be a JSON array"),
            "bad next_id": ({"version": 1, "next_id": "2", "tasks": []}, "'next_id'"),
        }
        for name, (doc, pattern) in cases.items():
            with self.subTest(name):
                self.write_json(doc)
                self.assert_corrupt(pattern)

    def test_invalid_task_record_reports_index(self):
        doc = self.valid_document()
        doc["tasks"][1]["status"] = "archived"
        self.write_json(doc)
        self.assert_corrupt(r"tasks\[1\]: 'status'")

    def test_duplicate_ids(self):
        doc = self.valid_document(tasks=[make_task(1), make_task(1)], next_id=2)
        self.write_json(doc)
        self.assert_corrupt("duplicate task id 1")

    def test_next_id_not_above_existing_ids(self):
        doc = self.valid_document(next_id=2)
        self.write_json(doc)
        self.assert_corrupt("must be greater than the highest task id")


class AtomicWriteTests(StorageTestCase):
    def leftover_temp_files(self):
        return [p for p in self.dir.iterdir() if p.name.endswith(".tmp")]

    def test_write_replaces_content_and_leaves_no_temp_files(self):
        atomic_write_text(self.path, "first")
        atomic_write_text(self.path, "second")
        self.assertEqual(self.path.read_text(), "second")
        self.assertEqual(self.leftover_temp_files(), [])

    def test_failed_replace_keeps_original_and_cleans_up(self):
        self.storage.save(StoreState(tasks=[make_task(1)], next_id=2))
        original = self.path.read_bytes()
        with mock.patch("task_tracker.storage.os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaisesRegex(StorageError, "No space left on device"):
                self.storage.save(StoreState(tasks=[], next_id=2))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.leftover_temp_files(), [])

    def test_interrupted_write_keeps_original_and_cleans_up(self):
        self.storage.save(StoreState(tasks=[make_task(1)], next_id=2))
        original = self.path.read_bytes()
        with mock.patch("task_tracker.storage.os.fsync", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.storage.save(StoreState(tasks=[], next_id=2))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.leftover_temp_files(), [])

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_existing_permissions_are_preserved(self):
        self.storage.save(StoreState())
        os.chmod(self.path, 0o640)
        self.storage.save(StoreState(tasks=[make_task(1)], next_id=2))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)


class RecoveryTests(StorageTestCase):
    def backups(self):
        return sorted(p for p in self.dir.iterdir() if ".corrupt-" in p.name)

    def test_healthy_file_is_left_alone(self):
        self.write_json(self.valid_document())
        before = self.path.read_bytes()
        report = self.storage.recover()
        self.assertIsNone(report.backup_path)
        self.assertEqual(report.recovered, 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_unparseable_file_is_backed_up_and_reset(self):
        self.write_raw("{{{ not json")
        report = self.storage.recover(now=NOW)
        self.assertEqual(report.backup_path, self.dir / "tasks.json.corrupt-20260304T050607Z")
        self.assertEqual(report.backup_path.read_text(), "{{{ not json")
        self.assertEqual(report.recovered, 0)
        self.assertEqual(self.storage.load().tasks, [])

    def test_valid_records_are_salvaged(self):
        doc = self.valid_document(tasks=[make_task(1), make_task(2), make_task(4)], next_id=7)
        doc["tasks"][1]["title"] = ""
        doc["tasks"].append(doc["tasks"][0])  # duplicate id 1
        self.write_json(doc)
        report = self.storage.recover(now=NOW)
        self.assertEqual(report.recovered, 2)
        self.assertEqual(report.discarded, 2)
        state = self.storage.load()
        self.assertEqual([t.id for t in state.tasks], [1, 4])
        self.assertEqual(state.next_id, 7)

    def test_next_id_is_rebuilt_when_stored_value_is_bad(self):
        doc = self.valid_document(tasks=[make_task(3)], next_id=2)
        self.write_json(doc)
        self.storage.recover(now=NOW)
        self.assertEqual(self.storage.load().next_id, 4)

    def test_bare_task_list_is_salvaged(self):
        self.write_json([make_task(1).to_dict(), {"junk": True}])
        report = self.storage.recover(now=NOW)
        self.assertEqual((report.recovered, report.discarded), (1, 1))

    def test_backup_names_do_not_collide(self):
        for _ in range(2):
            self.write_raw("broken")
            self.storage.recover(now=NOW)
        self.assertEqual(
            [p.name for p in self.backups()],
            ["tasks.json.corrupt-20260304T050607Z", "tasks.json.corrupt-20260304T050607Z-1"],
        )


class ResolvePathTests(unittest.TestCase):
    def test_explicit_path_wins(self):
        path = resolve_storage_path("/tmp/x.json", {PATH_ENV_VAR: "/tmp/env.json"})
        self.assertEqual(path, Path("/tmp/x.json"))

    def test_environment_variable_used_when_no_explicit_path(self):
        self.assertEqual(resolve_storage_path(None, {PATH_ENV_VAR: "/tmp/env.json"}), Path("/tmp/env.json"))

    def test_defaults_to_current_directory(self):
        self.assertEqual(resolve_storage_path(None, {}), Path.cwd() / DEFAULT_FILENAME)
        self.assertEqual(resolve_storage_path("  ", {PATH_ENV_VAR: " "}), Path.cwd() / DEFAULT_FILENAME)

    def test_user_home_is_expanded(self):
        self.assertEqual(resolve_storage_path("~/t.json", {}), Path.home() / "t.json")


if __name__ == "__main__":
    unittest.main()
