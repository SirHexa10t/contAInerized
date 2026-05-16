"""Tests for launch.audit — the launcher's state-integrity report.

`_check_json_file` is the pure helper that classifies a JSON state file's
status; it's the easily-testable core. `main()` walks the launcher's actual
state dirs + maps and prints a report — left out of unit tests since it
exercises the same primitives plus a great deal of I/O orchestration."""

import json
import tempfile
import unittest
from pathlib import Path

from launch.audit import _check_json_file


class TestCheckJsonFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_missing_file(self):
        self.assertEqual(_check_json_file(self.path), "file is missing")

    def test_empty_file(self):
        self.path.write_text("")
        self.assertEqual(_check_json_file(self.path), "file is empty")

    def test_whitespace_only_is_empty(self):
        # _check_json_file strips before checking
        self.path.write_text("   \n\t  \n")
        self.assertEqual(_check_json_file(self.path), "file is empty")

    def test_invalid_json(self):
        self.path.write_text("not json {")
        result = _check_json_file(self.path)
        self.assertIsNotNone(result)
        self.assertIn("invalid JSON", result)

    def test_empty_object(self):
        self.path.write_text("{}")
        self.assertEqual(_check_json_file(self.path), "contents are an empty object")

    def test_empty_array(self):
        # The check is `if not data` — falsy for both {} and []
        self.path.write_text("[]")
        self.assertEqual(_check_json_file(self.path), "contents are an empty object")

    def test_populated_object_is_clean(self):
        self.path.write_text(json.dumps({"key": "value"}))
        self.assertIsNone(_check_json_file(self.path))

    def test_populated_array_is_clean(self):
        self.path.write_text(json.dumps(["entry"]))
        self.assertIsNone(_check_json_file(self.path))

    def test_nested_structure_is_clean(self):
        self.path.write_text(json.dumps({"a": {"b": [1, 2]}}))
        self.assertIsNone(_check_json_file(self.path))


if __name__ == "__main__":
    unittest.main()
