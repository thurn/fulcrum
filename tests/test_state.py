"""Tests for owned atomic record I/O."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.config import ConfigurationError, RuntimePaths
from fulcrum.state import (
    OwnershipError,
    atomic_write_record,
    read_record,
    record_path,
    selected_record_path,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "records"


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text())


class StateIoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = RuntimePaths(
            brain_root=root / "brain",
            state_root=root / "state",
            config_file=root / "state" / "config.json",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_assignment_writer_must_be_its_overseer(self) -> None:
        value = fixture("healthy-review-wait-assignment.json")
        value["writer_id"] = "different-task"
        with self.assertRaisesRegex(OwnershipError, "writer mismatch"):
            atomic_write_record(self.paths, value)

    def test_path_traversal_in_record_identifier_is_rejected(self) -> None:
        value = fixture("healthy-review-wait-progress.json")
        value["role_task_id"] = "../escape"
        value["writer_id"] = "../escape"
        with self.assertRaises(ConfigurationError):
            record_path(self.paths, value)  # type: ignore[arg-type]

    def test_unknown_record_kind_is_rejected_for_reads(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown record kind"):
            selected_record_path(self.paths, "mystery", None)

    def test_failed_replace_preserves_previous_record(self) -> None:
        old_value = fixture("healthy-review-wait-progress.json")
        target = atomic_write_record(self.paths, old_value)
        previous = target.read_bytes()
        new_value = dict(old_value)
        new_value["expected_next_action"] = "New action"
        with patch("fulcrum.state.os.replace", side_effect=OSError("probe failure")):
            with self.assertRaisesRegex(OSError, "probe failure"):
                atomic_write_record(self.paths, new_value)
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_concurrent_reads_observe_only_complete_old_or_new_json(self) -> None:
        first = fixture("healthy-review-wait-progress.json")
        second = dict(first)
        second["expected_next_action"] = "Second complete value"
        atomic_write_record(self.paths, first)
        observed: list[str] = []
        failures: list[Exception] = []
        stopped = threading.Event()

        def reader() -> None:
            while not stopped.is_set():
                try:
                    value = read_record(self.paths, "progress", "task-executor-3")
                    if value["record_kind"] == "progress":
                        observed.append(value["expected_next_action"])
                except Exception as error:
                    failures.append(error)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for index in range(50):
                atomic_write_record(self.paths, first if index % 2 else second)
        finally:
            stopped.set()
            thread.join(timeout=2)

        self.assertFalse(failures)
        self.assertTrue(observed)
        self.assertLessEqual(
            set(observed), {"Review candidate candidate-91", "Second complete value"}
        )


if __name__ == "__main__":
    unittest.main()
