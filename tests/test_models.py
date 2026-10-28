import unittest
from datetime import datetime, timedelta, timezone

from task_tracker.models import (
    MAX_DESCRIPTION_LENGTH,
    MAX_TITLE_LENGTH,
    Task,
    TaskStatus,
    ValidationError,
    format_timestamp,
    parse_timestamp,
    validate_description,
    validate_title,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)


def valid_record(**overrides):
    record = {
        "id": 1,
        "title": "Write report",
        "description": "Quarterly numbers",
        "status": "todo",
        "created_at": "2026-01-02T03:04:05.678901+00:00",
        "updated_at": "2026-01-02T03:04:05.678901+00:00",
    }
    record.update(overrides)
    return record


class TitleValidationTests(unittest.TestCase):
    def test_strips_surrounding_whitespace(self):
        self.assertEqual(validate_title("  Buy milk \t"), "Buy milk")

    def test_rejects_empty_and_whitespace_only(self):
        for title in ("", " ", "\t", "\n  \n"):
            with self.subTest(title=title):
                with self.assertRaisesRegex(ValidationError, "empty or whitespace"):
                    validate_title(title)

    def test_length_boundaries(self):
        self.assertEqual(len(validate_title("x" * MAX_TITLE_LENGTH)), MAX_TITLE_LENGTH)
        with self.assertRaisesRegex(ValidationError, "too long"):
            validate_title("x" * (MAX_TITLE_LENGTH + 1))

    def test_rejects_embedded_newlines_and_control_characters(self):
        for title in ("line one\nline two", "bell\x07"):
            with self.subTest(title=title):
                with self.assertRaisesRegex(ValidationError, "single line"):
                    validate_title(title)

    def test_rejects_non_string(self):
        with self.assertRaises(ValidationError):
            validate_title(42)

    def test_accepts_unicode(self):
        self.assertEqual(validate_title("Café ☕ 日本"), "Café ☕ 日本")


class DescriptionValidationTests(unittest.TestCase):
    def test_blank_description_becomes_none(self):
        self.assertIsNone(validate_description(None))
        self.assertIsNone(validate_description("   "))

    def test_multiline_description_allowed(self):
        self.assertEqual(validate_description(" a\nb "), "a\nb")

    def test_length_boundaries(self):
        validate_description("x" * MAX_DESCRIPTION_LENGTH)
        with self.assertRaisesRegex(ValidationError, "too long"):
            validate_description("x" * (MAX_DESCRIPTION_LENGTH + 1))


class TaskStatusTests(unittest.TestCase):
    def test_parse_is_lenient_about_case_and_dashes(self):
        self.assertIs(TaskStatus.parse("IN-PROGRESS"), TaskStatus.IN_PROGRESS)
        self.assertIs(TaskStatus.parse(" done "), TaskStatus.DONE)
        self.assertIs(TaskStatus.parse(TaskStatus.TODO), TaskStatus.TODO)

    def test_parse_rejects_unknown_status(self):
        with self.assertRaisesRegex(ValidationError, "todo, in_progress, done"):
            TaskStatus.parse("finished")


class TimestampTests(unittest.TestCase):
    def test_round_trip_preserves_microseconds(self):
        self.assertEqual(parse_timestamp(format_timestamp(NOW), "t"), NOW)

    def test_format_normalizes_to_utc(self):
        offset = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(format_timestamp(offset), "2026-01-02T03:04:05.678901+00:00")

    def test_format_rejects_naive_datetime(self):
        with self.assertRaises(ValueError):
            format_timestamp(datetime(2026, 1, 1))

    def test_parse_accepts_z_suffix(self):
        self.assertEqual(parse_timestamp("2026-01-02T03:04:05Z", "t"), NOW.replace(microsecond=0))

    def test_parse_rejects_garbage_and_naive(self):
        for value in ("yesterday", "2026-01-02T03:04:05", 123, None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_timestamp(value, "t")


class TaskTests(unittest.TestCase):
    def test_new_task_defaults(self):
        task = Task.new(7, "  Ship it ", "", NOW)
        self.assertEqual(task.id, 7)
        self.assertEqual(task.title, "Ship it")
        self.assertIsNone(task.description)
        self.assertIs(task.status, TaskStatus.TODO)
        self.assertEqual(task.created_at, NOW)
        self.assertEqual(task.updated_at, NOW)

    def test_with_status_returns_new_instance(self):
        task = Task.new(1, "a", None, NOW)
        later = NOW + timedelta(minutes=1)
        updated = task.with_status(TaskStatus.DONE, later)
        self.assertIs(task.status, TaskStatus.TODO)
        self.assertIs(updated.status, TaskStatus.DONE)
        self.assertEqual(updated.created_at, NOW)
        self.assertEqual(updated.updated_at, later)

    def test_dict_round_trip(self):
        task = Task.new(3, "Title", "Desc", NOW).with_status(TaskStatus.IN_PROGRESS, NOW + timedelta(hours=1))
        self.assertEqual(Task.from_dict(task.to_dict()), task)

    def test_from_dict_accepts_valid_record(self):
        task = Task.from_dict(valid_record())
        self.assertEqual(task.title, "Write report")
        self.assertEqual(task.created_at, NOW)

    def test_from_dict_rejects_invalid_records(self):
        cases = {
            "not an object": ["id", 1],
            "missing title": {k: v for k, v in valid_record().items() if k != "title"},
            "string id": valid_record(id="1"),
            "bool id": valid_record(id=True),
            "zero id": valid_record(id=0),
            "empty title": valid_record(title="  "),
            "bad status": valid_record(status="archived"),
            "bad timestamp": valid_record(created_at="not a date"),
            "numeric description": valid_record(description=5),
        }
        for name, record in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValidationError):
                    Task.from_dict(record)


if __name__ == "__main__":
    unittest.main()
