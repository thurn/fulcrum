"""Contract tests for local coordination records."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from fulcrum.records import RecordValidationError, load_record, validate_record

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "records"


class RecordContractTest(unittest.TestCase):
    def test_all_examples_validate(self) -> None:
        fixtures = sorted(FIXTURE_ROOT.glob("*.json"))
        self.assertEqual(len(fixtures), 8)
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                record = load_record(fixture)
                self.assertEqual(record["schema_version"], 1)

    def test_unsupported_schema_version_is_actionable(self) -> None:
        value = json.loads((FIXTURE_ROOT / "installation.json").read_text())
        value["schema_version"] = 99
        with self.assertRaisesRegex(
            RecordValidationError,
            r"\$\.schema_version: unsupported version 99; supported versions: \[1\]",
        ):
            validate_record(value)

    def test_malformed_field_reports_json_path(self) -> None:
        value = json.loads((FIXTURE_ROOT / "source-push-pending.json").read_text())
        value["queue_revision"] = "seven"
        with self.assertRaisesRegex(
            RecordValidationError,
            r"\$\.queue_revision: 'seven' is not of type 'integer'",
        ):
            validate_record(value)

    def test_unknown_record_kind_lists_supported_kinds(self) -> None:
        with self.assertRaisesRegex(
            RecordValidationError, r"unknown kind 'mystery'.*project_registry"
        ):
            validate_record(
                {
                    "record_kind": "mystery",
                    "schema_version": 1,
                    "writer_id": "test",
                    "updated_at": "2026-09-11T22:10:00Z",
                }
            )

    def test_timestamp_must_be_utc(self) -> None:
        value = json.loads((FIXTURE_ROOT / "installation.json").read_text())
        value["updated_at"] = "2026-09-11T15:10:00-07:00"
        with self.assertRaisesRegex(RecordValidationError, r"\$\.updated_at"):
            validate_record(value)

    def test_unknown_fields_are_rejected(self) -> None:
        value = json.loads((FIXTURE_ROOT / "installation.json").read_text())
        value["secret"] = "must not be accepted"
        with self.assertRaisesRegex(
            RecordValidationError, r"Additional properties are not allowed"
        ):
            validate_record(value)


if __name__ == "__main__":
    unittest.main()
