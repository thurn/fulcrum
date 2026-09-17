from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fulcrum.performance import analyze, chrome_trace, load_spans
from fulcrum.timing import span, stage_name


class PerformanceTracingTests(unittest.TestCase):
    def test_nested_spans_are_correlated_and_payload_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timings.jsonl"
            environment = {
                "FULCRUM_TIMING_FILE": str(path),
                "FULCRUM_TRACE_ID": "trace-example",
                "FULCRUM_TIMING_LABEL": "test",
                "FULCRUM_TIMING_SAMPLE": "1",
            }
            with patch.dict(os.environ, environment, clear=False):
                with span("outer"):
                    with span("inner"):
                        time.sleep(0.001)

            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["stage"] for row in rows], ["inner", "outer"])
            inner, outer = rows
            self.assertEqual(inner["trace_id"], "trace-example")
            self.assertEqual(inner["parent_span_id"], outer["span_id"])
            self.assertIsNone(outer["parent_span_id"])
            self.assertEqual(inner["label"], "test")
            self.assertNotIn("arguments", inner)

    def test_analysis_ranks_exclusive_time_without_double_counting(self) -> None:
        rows = [
            _row("wall", "command.wall", 0.0, 100.0),
            _row("dispatch", "application.dispatch", 0.01, 80.0, parent="wall"),
            _row("ledger", "ledger.command.list", 0.02, 60.0, parent="dispatch"),
        ]

        report = analyze(rows)

        label = report["labels"][0]
        self.assertEqual(label["wall_ms"]["p95"], 100.0)
        opportunities = {
            item["stage"]: item["total_self_ms"] for item in label["opportunities"]
        }
        self.assertEqual(opportunities["ledger.command.list"], 60.0)
        self.assertAlmostEqual(opportunities["application.dispatch"], 20.0)
        self.assertAlmostEqual(opportunities["unattributed command.wall"], 20.0)

    def test_loader_reports_bad_rows_and_chrome_export_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timings.jsonl"
            path.write_text(
                json.dumps(_row("one", "cli.main", 2.0, 3.0)) + "\nnot-json\n"
            )
            rows, errors = load_spans([path])

        self.assertEqual(len(rows), 1)
        self.assertEqual(len(errors), 1)
        event = chrome_trace(rows)["traceEvents"][0]
        self.assertEqual(event["ph"], "X")
        self.assertEqual(event["dur"], 3000.0)

    def test_stage_names_are_bounded_and_do_not_preserve_separators(self) -> None:
        value = stage_name("application.command", "work create /private/value")
        self.assertEqual(value, "application.command.work_create_private_value")


def _row(
    span_id: str,
    stage: str,
    started: float,
    duration: float,
    *,
    parent: str | None = None,
) -> dict[str, object]:
    return {
        "schema": 1,
        "trace_id": "trace",
        "span_id": span_id,
        "parent_span_id": parent,
        "pid": 1,
        "stage": stage,
        "started_monotonic": started,
        "duration_ms": duration,
        "outcome": "ok",
        "label": "test",
        "sample": "1",
    }


if __name__ == "__main__":
    unittest.main()
