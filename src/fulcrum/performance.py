"""Analyze opt-in Fulcrum timing spans without touching workflow state."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence
import uuid

from fulcrum.timing import record_completed_span


def load_spans(paths: Sequence[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    spans: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            errors.append(f"{path}: {error}")
            continue
        for line_number, line in enumerate(lines, start=1):
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("row is not an object")
                _validate_span(value)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                errors.append(f"{path}:{line_number}: {error}")
                continue
            spans.append(value)
    return spans, errors


def analyze(spans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = [dict(item) for item in spans]
    by_id = {str(item["span_id"]): item for item in normalized if item.get("span_id")}
    children: dict[str, list[dict[str, Any]]] = {}
    for item in normalized:
        parent = item.get("parent_span_id")
        if isinstance(parent, str) and parent in by_id:
            children.setdefault(parent, []).append(item)
    self_times = {
        str(item["span_id"]): _self_time(item, children.get(str(item["span_id"]), []))
        for item in normalized
        if item.get("span_id")
    }
    labels = sorted({_label(item) for item in normalized})
    reports = [
        _analyze_label(
            label,
            [item for item in normalized if _label(item) == label],
            self_times,
        )
        for label in labels
    ]
    return {
        "span_count": len(normalized),
        "trace_count": len({_trace_key(item) for item in normalized}),
        "labels": reports,
    }


def chrome_trace(spans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "traceEvents": [
            {
                "name": str(item["stage"]),
                "cat": "fulcrum",
                "ph": "X",
                "ts": float(item["started_monotonic"]) * 1_000_000,
                "dur": float(item["duration_ms"]) * 1_000,
                "pid": int(item.get("pid", 0)),
                "tid": int(item.get("pid", 0)),
                "args": {
                    "trace_id": item.get("trace_id"),
                    "outcome": item.get("outcome"),
                    "label": item.get("label"),
                    "sample": item.get("sample"),
                },
            }
            for item in spans
        ],
        "displayTimeUnit": "ms",
    }


def render(report: Mapping[str, Any], errors: Sequence[str] = ()) -> str:
    lines = [f"{report['trace_count']} trace(s), {report['span_count']} span(s)"]
    for label_report in report["labels"]:
        wall = label_report["wall_ms"]
        lines.extend(
            [
                "",
                f"[{label_report['label']}] {label_report['samples']} sample(s): "
                f"wall p50 {wall['p50']:.1f} ms, p95 {wall['p95']:.1f} ms, "
                f"max {wall['max']:.1f} ms",
                "",
                "Optimization opportunities (exclusive time)",
            ]
        )
        opportunities = label_report["opportunities"]
        if not opportunities:
            lines.append("  No measured spans.")
        for index, item in enumerate(opportunities, start=1):
            lines.append(
                f"  {index:>2}. {item['stage']:<46} "
                f"{item['total_self_ms']:>10.1f} ms  "
                f"{item['wall_percent']:>5.1f}% wall  "
                f"({item['count']} call(s), p95 {item['p95_ms']:.1f} ms)"
            )
        lines.extend(["", "Slowest inclusive spans"])
        for item in label_report["slowest_spans"]:
            lines.append(
                f"  {item['duration_ms']:>10.1f} ms  {item['stage']} "
                f"(sample {item.get('sample') or '?'}, pid {item.get('pid')})"
            )
    if errors:
        lines.extend(["", f"Ignored {len(errors)} invalid row(s):", *errors[:10]])
    return "\n".join(lines) + "\n"


def _analyze_label(
    label: str,
    spans: list[dict[str, Any]],
    self_times: Mapping[str, float],
) -> dict[str, Any]:
    traces: dict[str, list[dict[str, Any]]] = {}
    for item in spans:
        traces.setdefault(_trace_key(item), []).append(item)
    walls = [_trace_wall(items) for items in traces.values()]
    wall_total: float = sum(walls, 0.0)
    stages: dict[str, list[dict[str, Any]]] = {}
    for item in spans:
        stages.setdefault(str(item["stage"]), []).append(item)
    stage_rows: list[dict[str, Any]] = []
    for stage, items in stages.items():
        durations = [float(item["duration_ms"]) for item in items]
        total_self: float = sum(
            [
                self_times.get(str(item.get("span_id")), float(item["duration_ms"]))
                for item in items
            ],
            0.0,
        )
        stage_rows.append(
            {
                "stage": stage,
                "count": len(items),
                "total_ms": round(sum(durations), 3),
                "total_self_ms": round(total_self, 3),
                "p50_ms": round(_percentile(durations, 50), 3),
                "p95_ms": round(_percentile(durations, 95), 3),
                "max_ms": round(max(durations), 3),
                "wall_percent": round(
                    100 * total_self / wall_total if wall_total else 0.0, 2
                ),
            }
        )
    stage_rows.sort(key=lambda item: (-item["total_self_ms"], item["stage"]))
    opportunities = []
    for item in stage_rows[:12]:
        opportunity = dict(item)
        if item["stage"] == "command.wall":
            opportunity["stage"] = "unattributed command.wall"
        opportunities.append(opportunity)
    slowest = sorted(
        (item for item in spans if item.get("stage") != "command.wall"),
        key=lambda item: -float(item["duration_ms"]),
    )[:12]
    return {
        "label": label,
        "samples": len(traces),
        "wall_ms": {
            "p50": round(_percentile(walls, 50), 3),
            "p95": round(_percentile(walls, 95), 3),
            "max": round(max(walls, default=0.0), 3),
        },
        "stages": stage_rows,
        "opportunities": opportunities,
        "slowest_spans": [
            {
                "stage": item["stage"],
                "duration_ms": round(float(item["duration_ms"]), 3),
                "sample": item.get("sample"),
                "pid": item.get("pid"),
                "outcome": item.get("outcome"),
            }
            for item in slowest
        ],
    }


def _self_time(item: Mapping[str, Any], children: Sequence[Mapping[str, Any]]) -> float:
    started = float(item["started_monotonic"]) * 1000
    finished = started + float(item["duration_ms"])
    intervals = sorted(
        (
            max(started, float(child["started_monotonic"]) * 1000),
            min(
                finished,
                float(child["started_monotonic"]) * 1000 + float(child["duration_ms"]),
            ),
        )
        for child in children
    )
    covered = 0.0
    cursor = started
    for left, right in intervals:
        if right <= left or right <= cursor:
            continue
        covered += right - max(left, cursor)
        cursor = max(cursor, right)
    return max(0.0, float(item["duration_ms"]) - covered)


def _trace_wall(items: Sequence[Mapping[str, Any]]) -> float:
    roots = [item for item in items if item.get("stage") == "command.wall"]
    if roots:
        return max(float(item["duration_ms"]) for item in roots)
    start = min(float(item["started_monotonic"]) for item in items)
    finish = max(
        float(item["started_monotonic"]) + float(item["duration_ms"]) / 1000
        for item in items
    )
    return (finish - start) * 1000


def _percentile(values: Sequence[float], percent: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percent / 100 * len(ordered)) - 1)
    return ordered[index]


def _label(item: Mapping[str, Any]) -> str:
    value = item.get("label")
    return str(value) if value else "default"


def _trace_key(item: Mapping[str, Any]) -> str:
    value = item.get("trace_id")
    return str(value) if value else f"legacy-process-{item.get('pid', 'unknown')}"


def _validate_span(value: Mapping[str, Any]) -> None:
    for field in ("stage", "started_monotonic", "duration_ms"):
        if field not in value:
            raise ValueError(f"missing {field}")
    if not isinstance(value["stage"], str):
        raise TypeError("stage must be a string")
    started = float(value["started_monotonic"])
    duration = float(value["duration_ms"])
    if not math.isfinite(started) or not math.isfinite(duration) or duration < 0:
        raise ValueError("invalid timing values")


def _record(arguments: argparse.Namespace) -> int:
    output = Path(arguments.output).expanduser().resolve()
    if output.exists() and not arguments.append:
        print(f"timing output already exists: {output}", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    return_code = 0
    for sample in range(1, arguments.samples + 1):
        trace_id = uuid.uuid4().hex
        root_span_id = uuid.uuid4().hex
        environment = dict(os.environ)
        environment.update(
            {
                "FULCRUM_TIMING_FILE": str(output),
                "FULCRUM_TRACE_ID": trace_id,
                "FULCRUM_TIMING_PARENT_SPAN": root_span_id,
                "FULCRUM_TIMING_LABEL": arguments.label,
                "FULCRUM_TIMING_SAMPLE": str(sample),
            }
        )
        started = time.monotonic()
        try:
            exit_code = subprocess.run(
                arguments.command, env=environment, check=False
            ).returncode
        except OSError as error:
            print(f"could not execute {arguments.command[0]}: {error}", file=sys.stderr)
            exit_code = 127
        previous = {
            name: os.environ.get(name)
            for name in (
                "FULCRUM_TIMING_FILE",
                "FULCRUM_TRACE_ID",
                "FULCRUM_TIMING_LABEL",
                "FULCRUM_TIMING_SAMPLE",
            )
        }
        os.environ.update(
            {
                "FULCRUM_TIMING_FILE": str(output),
                "FULCRUM_TRACE_ID": trace_id,
                "FULCRUM_TIMING_LABEL": arguments.label,
                "FULCRUM_TIMING_SAMPLE": str(sample),
            }
        )
        try:
            record_completed_span(
                "command.wall",
                started,
                span_id=root_span_id,
                outcome="ok" if exit_code == 0 else "error",
                exit_code=exit_code,
            )
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        return_code = return_code or exit_code
    spans, errors = load_spans([output])
    print(render(analyze(spans), errors), end="")
    return return_code


def _analyze(arguments: argparse.Namespace) -> int:
    paths = [Path(value).expanduser().resolve() for value in arguments.inputs]
    spans, errors = load_spans(paths)
    report = analyze(spans)
    if arguments.chrome_trace:
        destination = Path(arguments.chrome_trace).expanduser().resolve()
        destination.write_text(
            json.dumps(chrome_trace(spans), separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    if arguments.json:
        print(json.dumps({**report, "errors": errors}, separators=(",", ":")))
    else:
        print(render(report, errors), end="")
    return 1 if not spans else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/profile-operation",
        description="Record and analyze correlated Fulcrum performance spans.",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    record = commands.add_parser("record", help="run and profile a command")
    record.add_argument("--output", required=True)
    record.add_argument("--label", default="current")
    record.add_argument("--samples", type=int, default=1)
    record.add_argument("--append", action="store_true")
    record.add_argument("command", nargs=argparse.REMAINDER)
    analyze_parser = commands.add_parser("analyze", help="analyze timing JSONL")
    analyze_parser.add_argument("inputs", nargs="+")
    analyze_parser.add_argument("--json", action="store_true")
    analyze_parser.add_argument("--chrome-trace")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.action == "record":
        if arguments.samples < 1:
            parser.error("--samples must be positive")
        if arguments.command[:1] == ["--"]:
            arguments.command = arguments.command[1:]
        if not arguments.command:
            parser.error("record requires a command after --")
        return _record(arguments)
    return _analyze(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
