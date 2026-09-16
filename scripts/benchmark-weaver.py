"""Isolated real-CLI/Beads benchmark; native task naming is a local test double.

Run with a prepared Python environment, --source CHECKOUT --samples N --output FILE.
Uses a temporary embedded Dolt database, immutable source copy, and no live tasks.
The first sample is reported separately; warm samples still use fresh CLI workers.
"""

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def instrument():
    """Same probes on historical and candidate source, independent of new spans."""
    from functools import wraps
    from fulcrum import cli, completion, configuration, coordination, ledger, roles

    def wrap(target, name, stage):
        original = getattr(target, name)

        @wraps(original)
        def measured(*args, **kwargs):
            started = time.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                label = stage
                if stage == "probe.ledger":
                    label += "." + str(args[1][0])
                with open(os.environ["FULCRUM_TIMING_FILE"], "a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "stage": label,
                                "pid": os.getpid(),
                                "duration_ms": (time.monotonic() - started) * 1000,
                            }
                        )
                        + "\n"
                    )

        setattr(target, name, measured)

    for target, name, stage in (
        (cli, "main", "probe.cli"),
        (cli, "_execute", "probe.execute"),
        (cli, "_build_request", "probe.request"),
        (ledger.Ledger, "run", "probe.ledger"),
        (configuration.ConfigurationManager, "load", "probe.config"),
        (coordination.ProcessLock, "__enter__", "probe.lock_wait"),
        (roles.RoleService, "_enter", "probe.entry"),
        (roles, "_cook_role", "probe.cook"),
        (roles, "_runtime_call", "probe.runtime"),
        (completion.CompletionService, "finish", "probe.finish"),
    ):
        wrap(target, name, stage)


def worker(source):
    sys.path.insert(0, str(Path(source) / "src"))
    from fulcrum.contracts import ParsedRequest
    from fulcrum.runtime import TaskFacts
    from fulcrum.worker import main

    instrument()
    import asyncio

    class Native:
        def __init__(self):
            self.transport = self
            self.title = None

        async def set_name(self, thread, title):
            self.title = title

        async def inspect_task(self, thread):
            return TaskFacts(
                thread,
                self.title,
                None,
                "toy",
                (),
                False,
                True,
                True,
                "idle",
                None,
                None,
                (),
                "2026-09-16T00:00:00Z",
            )

    parse = ParsedRequest.from_wire

    def from_wire(value):
        return replace(
            parse(value),
            runtime_submit=lambda action, timeout: asyncio.run(action(Native())),
        )

    sys.argv = [sys.argv[0]]
    with patch.object(ParsedRequest, "from_wire", side_effect=from_wire):
        return main()


def client(instance, args):
    # Exactly the production source-selection/lease functions, followed by fresh
    # CLI import. Only the native endpoint is substituted inside the child.
    started = time.monotonic()
    source = os.environ["BENCH_SOURCE"]
    sys.path.insert(0, str(Path(source) / "src"))
    from fulcrum.bootstrap import pinned_selection

    selected, lease = pinned_selection(Path(instance))
    os.environ["FULCRUM_SOURCE"] = selected["source"]
    os.environ["FULCRUM_COMMIT"] = selected["commit"]
    selection_ms = (time.monotonic() - started) * 1000
    instrument()
    from fulcrum.cli import main

    original = subprocess.Popen

    def spawn(argv, **kwargs):
        return original(
            [sys.executable, "-B", str(Path(__file__).resolve()), "worker", source],
            **kwargs,
        )

    try:
        with patch("fulcrum.cli.subprocess.Popen", side_effect=spawn):
            code = main(args)
    finally:
        os.close(lease)
        with open(os.environ["FULCRUM_TIMING_FILE"], "a") as stream:
            stream.write(
                json.dumps(
                    {
                        "stage": "launcher.selection",
                        "duration_ms": selection_ms,
                        "pid": os.getpid(),
                    }
                )
                + "\n"
            )
    return code


def benchmark():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Measure the original summary-only ready contract",
    )
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("use at least two samples to separate first and warm runs")
    with tempfile.TemporaryDirectory(prefix="weaver-benchmark-") as temporary:
        root = Path(temporary)
        source = root / "source"
        shutil.copytree(
            args.source / "src",
            source / "src",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        brain = root / "brain"
        brain.mkdir()
        instance = root / "instance"
        instance.mkdir()
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("BEADS_", "BD_", "FULCRUM_", "CODEX_"))
        }
        env.update(
            BD_NON_INTERACTIVE="1",
            BENCH_SOURCE=str(source),
            FULCRUM_TIMING_FILE=str(root / "timings.jsonl"),
        )

        def run(argv):
            result = subprocess.run(
                argv, env=env, cwd=brain, text=True, capture_output=True
            )
            if result.returncode:
                raise RuntimeError(f"{argv}: {result.stdout} {result.stderr}")
            return result

        run(["git", "-C", str(brain), "init", "-q"])
        run(
            [
                "bd",
                "init",
                "--prefix",
                "fc",
                "--skip-hooks",
                "--skip-agents",
                "--non-interactive",
            ]
        )
        config = brain / "fulcrum.yaml"
        config.write_text(
            json.dumps(
                {
                    "brain": {"root": str(brain)},
                    "projects": {"toy": {"root": str(brain)}},
                }
            )
        )
        (instance / "selected.json").write_text(
            json.dumps(
                {
                    "source": str(source),
                    "python": sys.executable,
                    "commit": "isolated-benchmark",
                }
            )
        )
        rows = []
        for index in range(args.samples):
            common = [
                "--instance",
                str(instance),
                "--config",
                str(config),
                "--project",
                "toy",
                "--thread-id",
                f"fixture-{index}",
                "--json",
                "--timeout",
                "120",
            ]

            def invoke(command, payload):
                if command[0] == "enter":
                    command = [*command, "--description", payload.pop("description")]
                if command[0] == "finish" and args.baseline:
                    payload.pop("acceptance")
                input_file = root / "input.json"
                input_file.write_text(json.dumps(payload))
                started = time.monotonic()
                result = run(
                    [
                        sys.executable,
                        "-B",
                        str(Path(__file__).resolve()),
                        "client",
                        str(instance),
                        *command,
                        *common,
                        "--input",
                        str(input_file),
                    ]
                )
                elapsed = (time.monotonic() - started) * 1000
                value = json.loads(result.stdout)
                if value["state"] != "completed":
                    raise RuntimeError(value)
                data = value["result"].get("result", value["result"])
                receipt = json.loads(
                    run(
                        [
                            "bd",
                            "-C",
                            str(brain),
                            "show",
                            value["operation_id"],
                            "--json",
                        ]
                    ).stdout
                )
                if isinstance(receipt, list):
                    receipt = receipt[0]
                from datetime import datetime

                operation = receipt["metadata"]["fc"]
                operation_ms = (
                    datetime.fromisoformat(operation["completed_at"])
                    - datetime.fromisoformat(operation["created_at"])
                ).total_seconds() * 1000
                rows.append(
                    {
                        "sample": index,
                        "command": command[0],
                        "elapsed_ms": elapsed,
                        "receipt_ms": operation_ms,
                        "output_bytes": len(result.stdout.encode()),
                        "operation_id": value["operation_id"],
                    }
                )
                return data

            entry = invoke(
                ["enter", "weaver"], {"description": "Please delete docs/hooks.md"}
            )
            invoke(
                [
                    "finish",
                    "--bead",
                    entry["bead_id"],
                    "--ownership-operation",
                    entry["ownership_operation"],
                    "--outcome",
                    "ready",
                ],
                {
                    "summary": "Delete docs/hooks.md and remove its two incoming links in README.md and docs/fulcrum2/audit.md; leave unrelated text intact.",
                    "acceptance": [
                        "The file is absent and neither incoming link is broken."
                    ],
                    "evidence": ["README.md:12", "docs/fulcrum2/audit.md:78"],
                },
            )
        timings = [
            json.loads(line)
            for line in (root / "timings.jsonl").read_text().splitlines()
        ]
        summary = {}
        for command in ("enter", "finish"):
            values = [row for row in rows if row["command"] == command]
            warm = sorted(row["elapsed_ms"] for row in values[1:])
            summary[command] = {
                "first_ms": values[0]["elapsed_ms"],
                "warm_n": len(warm),
                "warm_median_ms": statistics.median(warm),
                "warm_max_ms": max(warm),
                "median_output_bytes": statistics.median(
                    row["output_bytes"] for row in values
                ),
            }
        args.output.write_text(
            json.dumps(
                {
                    "source": str(args.source),
                    "samples": args.samples,
                    "python": sys.version,
                    "summary": summary,
                    "rows": rows,
                    "timings": timings,
                },
                indent=2,
            )
            + "\n"
        )
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        raise SystemExit(worker(sys.argv[2]))
    if len(sys.argv) > 1 and sys.argv[1] == "client":
        raise SystemExit(client(sys.argv[2], sys.argv[3:]))
    benchmark()
