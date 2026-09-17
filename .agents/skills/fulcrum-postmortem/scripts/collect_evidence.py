#!/usr/bin/env python3
"""Collect a bounded, read-only Fulcrum postmortem evidence bundle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value, False
    return encoded[:limit].decode("utf-8", errors="replace"), True


def _run(
    name: str,
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    capture_bytes: int,
) -> dict[str, Any]:
    started_at = _utc_now()
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout, stdout_truncated = _bounded(completed.stdout, capture_bytes)
        stderr, stderr_truncated = _bounded(completed.stderr, capture_bytes)
        try:
            parsed: Any = json.loads(stdout) if not stdout_truncated else None
        except json.JSONDecodeError:
            parsed = None
        return {
            "name": name,
            "command": list(command),
            "started_at": started_at,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "returncode": completed.returncode,
            "json": parsed,
            "stdout": None if parsed is not None else stdout,
            "stderr": stderr or None,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "name": name,
            "command": list(command),
            "started_at": started_at,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "returncode": None,
            "json": None,
            "stdout": None,
            "stderr": str(error),
            "truncated": False,
        }


def _fulcrum_binary(value: str | None, repo: Path) -> str | None:
    if value:
        return value
    candidates = [
        os.environ.get("FULCRUM_BIN"),
        str(repo / ".venv/bin/fulcrum"),
        shutil.which("fulcrum"),
    ]
    return next(
        (
            candidate
            for candidate in candidates
            if candidate and Path(candidate).expanduser().is_file()
        ),
        None,
    )


def _write_bundle(path: Path | None, bundle: dict[str, Any]) -> None:
    payload = json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"
    if path is None:
        sys.stdout.write(payload)
        return
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect bounded, read-only evidence for a Fulcrum postmortem."
    )
    parser.add_argument("--bead", help="Fulcrum work bead to reconstruct")
    parser.add_argument(
        "--operation", action="append", default=[], help="operation ID (repeatable)"
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="managed task/thread ID (repeatable)",
    )
    parser.add_argument(
        "--git-ref", action="append", default=[], help="Git revision to inspect"
    )
    parser.add_argument("--since", help="lower time bound for logs and Git history")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, help="output JSON; stdout by default")
    parser.add_argument("--fulcrum", help="path to the Fulcrum executable")
    parser.add_argument("--instance", help="Fulcrum instance override")
    parser.add_argument("--config", help="Fulcrum configuration override")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--capture-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--log-max-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--git-limit", type=int, default=200)
    return parser


def main() -> int:
    args = _parser().parse_args()
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        raise SystemExit(f"repository directory does not exist: {repo}")
    if args.capture_bytes < 4096 or args.log_max_bytes < 4096:
        raise SystemExit("byte capture limits must be at least 4096")
    if args.git_limit < 1:
        raise SystemExit("--git-limit must be positive")

    probes: list[dict[str, Any]] = []

    def probe(name: str, command: Sequence[str]) -> dict[str, Any]:
        result = _run(
            name,
            command,
            cwd=repo,
            timeout=args.timeout,
            capture_bytes=args.capture_bytes,
        )
        probes.append(result)
        return result

    fulcrum = _fulcrum_binary(args.fulcrum, repo)
    common: list[str] = []
    if args.instance:
        common.extend(("--instance", args.instance))
    if args.config:
        common.extend(("--config", args.config))

    def fc(name: str, parts: Sequence[str]) -> dict[str, Any] | None:
        if fulcrum is None:
            return None
        return probe(name, [fulcrum, *parts, *common, "--json"])

    if fulcrum is None:
        probes.append(
            {
                "name": "fulcrum.discovery",
                "command": [],
                "started_at": _utc_now(),
                "duration_ms": 0,
                "returncode": None,
                "json": None,
                "stdout": None,
                "stderr": "Fulcrum executable was not found; use --fulcrum or FULCRUM_BIN",
                "truncated": False,
            }
        )
    else:
        fc("fulcrum.status", ["status"])
        fc("fulcrum.service_status", ["service", "status"])
        if args.bead:
            fc("fulcrum.work_show", ["work", "show", args.bead])
            fc(
                "fulcrum.trace",
                ["trace", "--bead", args.bead, "--limit", "0"],
            )
            log_parts = [
                "logs",
                "--bead",
                args.bead,
                "--limit",
                "0",
                "--max-bytes",
                str(args.log_max_bytes),
            ]
            if args.since:
                log_parts.extend(("--since", args.since))
            fc("fulcrum.logs", log_parts)
            fc("fulcrum.promotion_show", ["promotion", "show", "--bead", args.bead])
        for operation in dict.fromkeys(args.operation):
            fc(
                f"fulcrum.operation_show:{operation}",
                ["operation", "show", operation],
            )
        for task in dict.fromkeys(args.task):
            fc(
                f"fulcrum.trace_task:{task}",
                ["trace", "--task", task, "--limit", "0"],
            )

    git = shutil.which("git") or "git"
    probe("git.status", [git, "status", "--short", "--branch"])
    history = [
        git,
        "log",
        "--all",
        f"--max-count={args.git_limit}",
        "--date=iso-strict",
        "--format=%H%x09%aI%x09%cI%x09%an%x09%s",
    ]
    if args.since:
        history.append(f"--since={args.since}")
    probe("git.history", history)
    probe(
        "git.reflog",
        [
            git,
            "reflog",
            "--all",
            f"--max-count={args.git_limit}",
            "--date=iso-strict",
            "--format=%H%x09%gD%x09%gI%x09%gs",
        ],
    )
    probe("git.worktrees", [git, "worktree", "list", "--porcelain"])
    probe("git.remotes", [git, "remote"])
    for revision in dict.fromkeys(args.git_ref):
        probe(
            f"git.show:{revision}",
            [git, "show", "--stat", "--summary", "--format=fuller", revision],
        )

    bundle = {
        "collected_at": _utc_now(),
        "local_timezone": str(datetime.now().astimezone().tzinfo),
        "scope": {
            "bead": args.bead,
            "operations": list(dict.fromkeys(args.operation)),
            "tasks": list(dict.fromkeys(args.task)),
            "git_refs": list(dict.fromkeys(args.git_ref)),
            "since": args.since,
            "repository": str(repo),
        },
        "notice": (
            "Read-only local evidence. Review prompts, paths, and metadata before sharing. "
            "Nonzero probe results are retained and may be expected for unavailable facts."
        ),
        "probes": probes,
    }
    _write_bundle(args.output, bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
