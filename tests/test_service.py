import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_tracker.models import TaskStatus, ValidationError
from task_tracker.service import TaskNotFoundError, TaskService
from task_tracker.storage import JsonTaskStorage


class FakeClock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "tasks.json"
        self.clock = FakeClock(datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc))
        self.service = self.new_service()

    def new_service(self):
        return TaskService(JsonTaskStorage(self.path), clock=self.clock)


class AddTaskTests(ServiceTestCase):
    def test_add_creates_todo_task_with_timestamps(self):
        task = self.service.add_task("  Write tests ", "  covering everything ")
        self.assertEqual(task.id, 1)
        self.assertEqual(task.title, "Write tests")
        self.assertEqual(task.description, "covering everything")
        self.assertIs(task.status, TaskStatus.TODO)
        self.assertEqual(task.created_at, self.clock.now)
        self.assertEqual(task.updated_at, task.created_at)

    def test_real_clock_timestamps_are_accurate(self):
        service = TaskService(JsonTaskStorage(self.path))
        before = datetime.now(timezone.utc)
        task = service.add_task("timed")
        after = datetime.now(timezone.utc)
        self.assertLessEqual(before, task.created_at)
        self.assertLessEqual(task.created_at, after)
        self.assertEqual(task.created_at.utcoffset(), timedelta(0))

    def test_ids_are_unique_and_incremental(self):
        ids = [self.service.add_task(f"task {n}").id for n in range(25)]
        self.assertEqual(ids, list(range(1, 26)))

    def test_ids_of_deleted_tasks_are_never_reused(self):
        self.service.add_task("a")
        second = self.service.add_task("b")
        self.service.delete_task(second.id)
        self.assertEqual(self.service.add_task("c").id, 3)
        # Even when the newest task is deleted and the service restarts.
        self.service.delete_task(3)
        self.assertEqual(self.new_service().add_task("d").id, 4)

    def test_invalid_title_is_rejected_and_nothing_is_saved(self):
        for title in ("", "   "):
            with self.subTest(title=title):
                with self.assertRaises(ValidationError):
                    self.service.add_task(title)
        self.assertEqual(self.service.list_tasks(), [])
        self.assertEqual(self.service.add_task("ok").id, 1)

    def test_rejects_naive_clock(self):
        service = TaskService(JsonTaskStorage(self.path), clock=lambda: datetime(2026, 1, 1))
        with self.assertRaises(ValueError):
            service.add_task("x")


class ListTaskTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.service.add_task("one")
        self.service.add_task("two")
        self.service.add_task("three")
        self.service.update_status(2, TaskStatus.IN_PROGRESS)
        self.service.update_status(3, TaskStatus.DONE)

    def test_list_all_sorted_by_id(self):
        self.assertEqual([t.id for t in self.service.list_tasks()], [1, 2, 3])

    def test_filter_by_status(self):
        self.assertEqual([t.id for t in self.service.list_tasks(TaskStatus.TODO)], [1])
        self.assertEqual([t.id for t in self.service.list_tasks("in_progress")], [2])
        self.assertEqual([t.id for t in self.service.list_tasks("DONE")], [3])

    def test_filter_with_no_matches(self):
        self.service.delete_task(1)
        self.assertEqual(self.service.list_tasks(TaskStatus.TODO), [])

    def test_invalid_filter(self):
        with self.assertRaises(ValidationError):
            self.service.list_tasks("archived")


class UpdateStatusTests(ServiceTestCase):
    def test_update_changes_status_and_updated_at_only(self):
        task = self.service.add_task("work")
        self.clock.advance(minutes=5)
        change = self.service.update_status(task.id, TaskStatus.IN_PROGRESS)
        self.assertTrue(change.changed)
        self.assertIs(change.previous_status, TaskStatus.TODO)
        self.assertIs(change.task.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(change.task.created_at, task.created_at)
        self.assertEqual(change.task.updated_at, task.created_at + timedelta(minutes=5))

    def test_setting_same_status_is_noop(self):
        task = self.service.add_task("work")
        self.clock.advance(minutes=5)
        change = self.service.update_status(task.id, "todo")
        self.assertFalse(change.changed)
        self.assertEqual(self.service.get_task(task.id).updated_at, task.updated_at)

    def test_task_can_be_reopened(self):
        task = self.service.add_task("work")
        self.service.update_status(task.id, TaskStatus.DONE)
        self.assertIs(self.service.update_status(task.id, TaskStatus.TODO).task.status, TaskStatus.TODO)

    def test_unknown_id(self):
        with self.assertRaises(TaskNotFoundError) as ctx:
            self.service.update_status(99, TaskStatus.DONE)
        self.assertEqual(ctx.exception.task_id, 99)

    def test_invalid_id(self):
        for bad in (0, -1, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.service.update_status(bad, TaskStatus.DONE)


class DeleteTests(ServiceTestCase):
    def test_delete_removes_task(self):
        keep = self.service.add_task("keep")
        gone = self.service.add_task("gone")
        self.assertEqual(self.service.delete_task(gone.id), gone)
        self.assertEqual(self.service.list_tasks(), [keep])
        with self.assertRaises(TaskNotFoundError):
            self.service.get_task(gone.id)

    def test_delete_unknown_id(self):
        with self.assertRaises(TaskNotFoundError):
            self.service.delete_task(1)


class PersistenceTests(ServiceTestCase):
    def test_changes_are_visible_to_a_fresh_service(self):
        self.service.add_task("persisted", "with description")
        self.service.add_task("second")
        self.service.update_status(1, TaskStatus.DONE)
        self.service.delete_task(2)

        reloaded = self.new_service().list_tasks()
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0].title, "persisted")
        self.assertEqual(reloaded[0].description, "with description")
        self.assertIs(reloaded[0].status, TaskStatus.DONE)


if __name__ == "__main__":
    unittest.main()
