"""Installed Fulcrum command line.

The parser is declarative so the complete public command tree is visible in one
place. Workflow behavior lives behind :mod:`fulcrum.application`, never here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, Sequence

from fulcrum.application import default_application
from fulcrum.contracts import ActorContext, CommandResult, FulcrumError, ParsedRequest
from fulcrum.diagnostics import DiagnosticLog
from fulcrum.instance import WriterLock, resolve_instance
from fulcrum.ipc import (
    ControllerUnavailable,
    MAX_MESSAGE_BYTES,
    request_sync,
)
from fulcrum.supervision import ControllerSupervisor

ROLES = (
    "vizier",
    "marshal",
    "weaver",
    "executor",
    "warden",
    "sage",
    "mason",
    "justiciar",
)
READ_ONLY_COMMANDS = {
    ("config", "show"),
    ("config", "validate"),
    ("policy", "show"),
    ("project", "list"),
    ("project", "show"),
    ("service", "status"),
    ("runtime", "capabilities"),
    ("runtime", "status"),
    ("context",),
    ("hook", "context"),
    ("work", "show"),
    ("work", "list"),
    ("work", "children"),
    ("leader", "show"),
    ("marshal", "brief"),
    ("backlog", "list"),
    ("human", "list"),
    ("task", "list"),
    ("task", "show"),
    ("task", "output"),
    ("task", "wait"),
    ("task", "requests"),
    ("task", "terminals"),
    ("worktree", "inspect"),
    ("validation", "show"),
    ("promotion", "show"),
    ("operation", "show"),
    ("operation", "list"),
    ("operation", "wait"),
    ("status",),
    ("doctor",),
    ("logs",),
    ("trace",),
    ("wait",),
    ("recover", "inspect"),
    ("plan", "show"),
    ("plan", "complete"),
    ("memory", "list"),
    ("memory", "show"),
    ("ledger", "status"),
    ("usage",),
    ("cost",),
    ("rates", "list"),
    ("rates", "show"),
    ("fixture", "show"),
    ("fixture", "barrier", "show"),
}
BROKEN_CONFIG_COMMANDS = {("service", "status"), ("recover", "inspect")}


@dataclass(frozen=True)
class CommandDefinition:
    path: tuple[str, ...]
    help: str


COMMANDS = (
    CommandDefinition(("setup",), "install or repair a Fulcrum instance"),
    CommandDefinition(("config", "show"), "show authoritative configuration"),
    CommandDefinition(("config", "validate"), "validate authoritative configuration"),
    CommandDefinition(("config", "set"), "update authorized configuration fields"),
    CommandDefinition(("config", "sync"), "publish authoritative configuration"),
    CommandDefinition(("policy", "show"), "show dispatch policy"),
    CommandDefinition(("policy", "set"), "update dispatch policy"),
    CommandDefinition(("project", "add"), "enroll a project"),
    CommandDefinition(("project", "list"), "list enrolled projects"),
    CommandDefinition(("project", "show"), "show an enrolled project"),
    CommandDefinition(("project", "enable"), "enable automatic project work"),
    CommandDefinition(("project", "disable"), "disable automatic project work"),
    CommandDefinition(("project", "remove"), "remove an enrolled project"),
    CommandDefinition(("service", "start"), "start the controller service"),
    CommandDefinition(("service", "stop"), "stop the controller service"),
    CommandDefinition(("service", "restart"), "restart the controller service"),
    CommandDefinition(("service", "status"), "inspect controller service artifacts"),
    CommandDefinition(("service", "update"), "build and activate installed source"),
    CommandDefinition(("serve",), "run the foreground controller"),
    CommandDefinition(("reconcile",), "run one bounded reconciliation pass"),
    CommandDefinition(("skills", "reconcile"), "repair owned role skill links"),
    CommandDefinition(
        ("runtime", "launch-desktop"), "launch Desktop on the configured runtime"
    ),
    CommandDefinition(
        ("runtime", "capabilities"), "inspect native runtime capabilities"
    ),
    CommandDefinition(("runtime", "status"), "inspect native runtime state"),
    CommandDefinition(("enter",), "enter one of the eight Fulcrum roles"),
    CommandDefinition(("context",), "show current work or role context"),
    CommandDefinition(("hook", "context"), "provide compact managed-task context"),
    CommandDefinition(("work", "create"), "create a work root or graph"),
    CommandDefinition(("work", "show"), "show work"),
    CommandDefinition(("work", "list"), "list work"),
    CommandDefinition(("work", "adopt"), "adopt existing work"),
    CommandDefinition(("work", "update"), "update work scope"),
    CommandDefinition(("work", "transfer"), "transfer work ownership"),
    CommandDefinition(("work", "children"), "show work children"),
    CommandDefinition(("work", "dependencies"), "change work dependencies"),
    CommandDefinition(("work", "close"), "close work with a disposition"),
    CommandDefinition(("work", "reopen"), "reopen work under a new acquisition"),
    CommandDefinition(("finish",), "finish the current role responsibility"),
    CommandDefinition(("progress",), "record substantive progress"),
    CommandDefinition(("report",), "file an independently attributed follow-up"),
    CommandDefinition(("leader", "show"), "show standing leadership"),
    CommandDefinition(("leader", "replace"), "replace a standing leader"),
    CommandDefinition(("marshal", "brief"), "preview a decision-focused brief"),
    CommandDefinition(("marshal", "request"), "request a recorded Marshal decision"),
    CommandDefinition(("marshal", "decide"), "apply a retained Marshal decision"),
    CommandDefinition(("backlog", "list"), "list actionable and waiting work"),
    CommandDefinition(("dispatch",), "authorize or execute admitted work"),
    CommandDefinition(("human", "list"), "list irreducible human blockers"),
    CommandDefinition(("human", "resolve"), "resolve one human blocker"),
    CommandDefinition(("task", "list"), "list managed native tasks"),
    CommandDefinition(("task", "show"), "show a managed native task"),
    CommandDefinition(("task", "start"), "create and start a native task"),
    CommandDefinition(("task", "send"), "send a managed native turn"),
    CommandDefinition(("task", "output"), "read bounded native output"),
    CommandDefinition(("task", "wait"), "wait for an observed native condition"),
    CommandDefinition(("task", "requests"), "list pending native requests"),
    CommandDefinition(("task", "respond"), "respond to one native request"),
    CommandDefinition(("task", "interrupt"), "interrupt a native turn"),
    CommandDefinition(("task", "terminals"), "list a task's owned terminals"),
    CommandDefinition(
        ("task", "terminal", "stop"), "stop selected owned terminal resources"
    ),
    CommandDefinition(("task", "release"), "release Fulcrum's native subscription"),
    CommandDefinition(("task", "archive"), "archive a managed native task"),
    CommandDefinition(
        ("task", "unarchive"), "unarchive and suppress automatic rearchive"
    ),
    CommandDefinition(("task", "delete"), "delete an exact owned native task"),
    CommandDefinition(("worktree", "prepare"), "prepare a managed delivery workspace"),
    CommandDefinition(("worktree", "inspect"), "inspect a managed delivery workspace"),
    CommandDefinition(("worktree", "cleanup"), "clean a settled managed workspace"),
    CommandDefinition(("validation", "start"), "submit exact source for validation"),
    CommandDefinition(("validation", "show"), "show provider validation facts"),
    CommandDefinition(("review", "approve"), "approve exact Warden source"),
    CommandDefinition(("promotion", "start"), "start nonblocking promotion"),
    CommandDefinition(("promotion", "show"), "show promotion facts"),
    CommandDefinition(("source", "sync"), "synchronize recorded promoted source"),
    CommandDefinition(("operation", "show"), "show one operation receipt"),
    CommandDefinition(("operation", "list"), "list operation receipts"),
    CommandDefinition(("operation", "wait"), "wait for an operation observation"),
    CommandDefinition(("operation", "cancel"), "cancel and inspect an operation"),
    CommandDefinition(("operation", "reconcile"), "reconcile an operation"),
    CommandDefinition(("status",), "show machine-readable instance status"),
    CommandDefinition(("doctor",), "diagnose installation components and loops"),
    CommandDefinition(("logs",), "read structured diagnostic logs"),
    CommandDefinition(("logs", "prune"), "prune logs within retention policy"),
    CommandDefinition(("trace",), "trace durable work and operation evidence"),
    CommandDefinition(("wait",), "wait for a work observation"),
    CommandDefinition(("recover", "inspect"), "inspect an exact repair scope"),
    CommandDefinition(("recover", "takeover"), "fence and take over a repair scope"),
    CommandDefinition(("recover", "repair"), "perform typed scoped repair actions"),
    CommandDefinition(("recover", "release"), "release a settled repair fence"),
    CommandDefinition(("plan", "draft"), "save a complete unpublished plan draft"),
    CommandDefinition(("plan", "show"), "show retained plan facts"),
    CommandDefinition(("plan", "review", "start"), "start an independent review task"),
    CommandDefinition(
        ("plan", "review", "finish"), "submit typed independent findings"
    ),
    CommandDefinition(("plan", "approve"), "approve retained plan scope"),
    CommandDefinition(("plan", "publish"), "publish approved plan scope"),
    CommandDefinition(("plan", "refine"), "refine approved plan scope"),
    CommandDefinition(("plan", "activate"), "activate authorized future scope"),
    CommandDefinition(
        ("plan", "complete"), "mechanically inspect and close a plan root"
    ),
    CommandDefinition(("memory", "list"), "list curated memory"),
    CommandDefinition(("memory", "show"), "show curated memory"),
    CommandDefinition(("memory", "set"), "set curated memory"),
    CommandDefinition(("knowledge", "publish"), "publish selected knowledge documents"),
    CommandDefinition(("ledger", "sync"), "flush native Beads history"),
    CommandDefinition(("ledger", "status"), "show native publication status"),
    CommandDefinition(("usage",), "query unique managed native usage"),
    CommandDefinition(("usage", "reconcile"), "reconcile native usage observations"),
    CommandDefinition(("cost",), "query API-equivalent workflow cost"),
    CommandDefinition(("rates", "list"), "list retained rate cards"),
    CommandDefinition(("rates", "show"), "show a retained rate card"),
    CommandDefinition(("rates", "add"), "add an immutable documented rate card"),
    CommandDefinition(("fleet", "replace"), "replace a scoped managed task fleet"),
    CommandDefinition(("reset",), "perform an explicitly authorized hard reset"),
    CommandDefinition(("fixture", "create"), "create an isolated test fixture"),
    CommandDefinition(("fixture", "show"), "show retained fixture inventory"),
    CommandDefinition(("fixture", "cleanup"), "clean an exact disposable fixture"),
    CommandDefinition(("fixture", "barrier", "prepare"), "prepare a fixture barrier"),
    CommandDefinition(("fixture", "barrier", "arrive"), "arrive at a fixture barrier"),
    CommandDefinition(("fixture", "barrier", "show"), "show a fixture barrier"),
    CommandDefinition(("fixture", "barrier", "release"), "release a fixture barrier"),
    CommandDefinition(("scenario", "emit"), "emit a deterministic provider event"),
    CommandDefinition(("scenario", "advance"), "advance the deterministic clock"),
    CommandDefinition(("scenario", "fault"), "arm a deterministic provider fault"),
    CommandDefinition(
        ("scenario", "crash"), "arm a persisted controller crash boundary"
    ),
    CommandDefinition(
        ("smoke", "concurrency"), "run bounded native concurrency validation"
    ),
)


POSITIONAL_ID: set[tuple[str, ...]] = {
    path
    for path in (definition.path for definition in COMMANDS)
    if path
    in {
        ("project", "show"),
        ("project", "enable"),
        ("project", "disable"),
        ("project", "remove"),
        ("work", "show"),
        ("work", "adopt"),
        ("work", "update"),
        ("work", "transfer"),
        ("work", "children"),
        ("work", "dependencies"),
        ("work", "close"),
        ("work", "reopen"),
        ("task", "show"),
        ("task", "send"),
        ("task", "output"),
        ("task", "wait"),
        ("task", "requests"),
        ("task", "respond"),
        ("task", "interrupt"),
        ("task", "terminals"),
        ("task", "terminal", "stop"),
        ("task", "release"),
        ("task", "archive"),
        ("task", "unarchive"),
        ("task", "delete"),
        ("operation", "show"),
        ("operation", "wait"),
        ("operation", "cancel"),
        ("operation", "reconcile"),
        ("plan", "show"),
        ("plan", "activate"),
        ("plan", "complete"),
        ("memory", "show"),
        ("rates", "show"),
        ("fixture", "show"),
        ("fixture", "cleanup"),
    }
}


class ContractParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise FulcrumError.invalid("INVALID_COMMAND", message)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", default=argparse.SUPPRESS)
    parser.add_argument("--config", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--input", default=argparse.SUPPRESS)
    parser.add_argument("--project", default=argparse.SUPPRESS)
    parser.add_argument("--thread-id", default=argparse.SUPPRESS)
    parser.add_argument("--actor", default=argparse.SUPPRESS)
    parser.add_argument("--request-id", default=argparse.SUPPRESS)
    parser.add_argument("--wait", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--offline", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--model", default=argparse.SUPPRESS)
    parser.add_argument("--effort", default=argparse.SUPPRESS)
    parser.add_argument("--ownership-operation", default=argparse.SUPPRESS)


def _option(parser: argparse.ArgumentParser, *names: str, **kwargs: Any) -> None:
    kwargs.setdefault("default", argparse.SUPPRESS)
    parser.add_argument(*names, **kwargs)


def _add_command_options(
    parser: argparse.ArgumentParser, path: tuple[str, ...]
) -> None:
    if path in POSITIONAL_ID:
        parser.add_argument("id")
    if path == ("project", "disable"):
        _option(parser, "--reason", required=True)
    if path == ("serve",):
        _option(parser, "--once", action="store_true")
    elif path == ("reconcile",):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--bead", default=argparse.SUPPRESS)
        group.add_argument("--operation", default=argparse.SUPPRESS)
    elif path == ("setup",):
        _option(parser, "--non-interactive", action="store_true")
    elif path == ("enter",):
        parser.add_argument("role", choices=ROLES)
        _option(parser, "--description", required=True)
        _option(parser, "--bead")
        _option(parser, "--origin", choices=("human", "dispatch"), default="human")
    elif path == ("context",):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--bead", default=argparse.SUPPRESS)
        group.add_argument("--role", choices=ROLES, default=argparse.SUPPRESS)
    elif path == ("work", "list"):
        _option(parser, "--role", choices=ROLES)
        _option(parser, "--owner")
        _option(parser, "--phase")
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
    elif path == ("work", "adopt"):
        _option(parser, "--role", choices=ROLES, required=True)
    elif path == ("work", "transfer"):
        _option(parser, "--to-thread", required=True)
        _option(parser, "--role", choices=ROLES, required=True)
        _option(parser, "--reason", required=True)
    elif path == ("work", "close"):
        _option(parser, "--outcome", required=True)
        _option(parser, "--summary", required=True)
    elif path == ("work", "reopen"):
        _option(parser, "--reason", required=True)
        _option(parser, "--role", choices=ROLES)
    elif path in {("finish",), ("progress",), ("report",)}:
        _option(parser, "--bead")
        if path == ("finish",):
            _option(parser, "--outcome")
        if path == ("progress",):
            _option(parser, "--kind")
            _option(parser, "--summary")
            _option(parser, "--evidence", action="append")
    elif path[:1] == ("marshal",) and path[-1] in {"brief", "request"}:
        _option(parser, "--kind", choices=("auto", "groom", "dispatch", "recover"))
        _option(parser, "--bead")
    elif path == ("leader", "show"):
        parser.add_argument("role", choices=("vizier", "marshal"))
    elif path == ("backlog", "list"):
        _option(parser, "--ready", action="store_true")
        _option(parser, "--include-deferred", action="store_true")
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
    elif path == ("dispatch",):
        _option(parser, "--bead", required=True)
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--authorize", action="store_true", default=argparse.SUPPRESS
        )
        group.add_argument("--human", action="store_true", default=argparse.SUPPRESS)
    elif path == ("leader", "replace"):
        parser.add_argument("role", choices=("vizier", "marshal"))
        _option(parser, "--reason", required=True)
    elif path == ("task", "output"):
        _option(parser, "--turn-id")
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
        _option(parser, "--max-bytes", type=int)
    elif path == ("task", "terminals"):
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
    elif path == ("task", "wait"):
        _option(parser, "--turn-id")
        _option(parser, "--until", choices=("idle", "terminal"), required=True)
    elif path == ("task", "start"):
        _option(parser, "--role", choices=ROLES, required=True)
        _option(parser, "--bead", required=True)
    elif path == ("task", "list"):
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
    elif path == ("task", "interrupt"):
        _option(parser, "--turn-id")
    elif path == ("task", "respond"):
        _option(parser, "--request", required=True)
    elif path == ("task", "delete"):
        _option(parser, "--yes", action="store_true", required=True)
    elif path == ("task", "terminal", "stop"):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--terminal", default=argparse.SUPPRESS)
        group.add_argument(
            "--all-owned", action="store_true", default=argparse.SUPPRESS
        )
        _option(parser, "--reason", required=True)
    elif path in {
        ("worktree", "prepare"),
        ("worktree", "inspect"),
        ("worktree", "cleanup"),
    }:
        _option(parser, "--bead", required=True)
    elif path in {
        ("validation", "start"),
        ("review", "approve"),
        ("promotion", "start"),
    }:
        _option(parser, "--bead", required=True)
        _option(
            parser,
            "--source",
            required=path in {("validation", "start"), ("promotion", "start")},
        )
    elif path in {
        ("validation", "show"),
        ("promotion", "show"),
        ("source", "sync"),
        ("knowledge", "publish"),
    }:
        _option(parser, "--bead", required=True)
    elif path in {("status",), ("trace",)}:
        _option(parser, "--bead")
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
    elif path == ("logs",):
        _option(parser, "--follow", action="store_true")
        _option(parser, "--bead")
        _option(parser, "--operation")
        _option(parser, "--since")
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
        _option(parser, "--max-bytes", type=int)
    elif path == ("wait",):
        _option(parser, "--bead", required=True)
        _option(parser, "--until", required=True)
    elif path[:1] == ("recover",):
        _option(parser, "--scope", required=True)
    elif path == ("plan", "draft") or path in {
        ("plan", "approve"),
        ("plan", "publish"),
        ("plan", "refine"),
    }:
        _option(parser, "--bead", required=True)
    elif path == ("plan", "review", "start"):
        _option(parser, "--bead", required=True)
        _option(
            parser,
            "--perspective",
            choices=("cold_reader", "requirements"),
            required=True,
        )
    elif path == ("plan", "review", "finish"):
        _option(parser, "--task", required=True)
    elif path in {("memory", "list"), ("usage",), ("cost",)}:
        for name in ("scope", "workflow", "role", "task", "turn", "root"):
            _option(parser, f"--{name}")
        _option(parser, "--group-by")
    elif path == ("fleet", "replace"):
        _option(parser, "--mode", choices=("drain", "interrupt"), required=True)
        _option(parser, "--reason", required=True)
    elif path == ("reset",):
        _option(parser, "--hard", action="store_true", required=True)
        _option(parser, "--yes", action="store_true", required=True)
    elif path in {
        ("fixture", "cleanup"),
    }:
        _option(parser, "--yes", action="store_true", required=True)
    elif path[:3] == ("fixture", "barrier", "prepare"):
        parser.add_argument("id")
        _option(parser, "--name", required=True)
    elif path[:3] == ("fixture", "barrier", "arrive"):
        parser.add_argument("id")
        _option(parser, "--name", required=True)
        _option(parser, "--participant", required=True)
    elif path[:3] in {
        ("fixture", "barrier", "show"),
        ("fixture", "barrier", "release"),
    }:
        parser.add_argument("id")
        _option(parser, "--name", required=True)
    elif path == ("scenario", "crash"):
        _option(parser, "--operation", required=True)
        _option(parser, "--boundary", required=True)
    elif path == ("smoke", "concurrency"):
        _option(parser, "--workers", type=int, default=30)


def build_parser() -> argparse.ArgumentParser:
    parser = ContractParser(
        prog="fulcrum", description="Durable local agent workflow coordination"
    )
    _add_common(parser)
    parsers: dict[tuple[str, ...], argparse.ArgumentParser] = {(): parser}
    children: dict[
        tuple[str, ...], argparse._SubParsersAction[argparse.ArgumentParser]
    ] = {}
    full_paths = {definition.path for definition in COMMANDS}
    for definition in COMMANDS:
        for depth, name in enumerate(definition.path, start=1):
            path = definition.path[:depth]
            if path in parsers:
                continue
            parent_path = path[:-1]
            parent = parsers[parent_path]
            if parent_path not in children:
                required = not (parent_path in full_paths)
                children[parent_path] = parent.add_subparsers(
                    dest=f"_level_{len(parent_path)}", required=required
                )
            help_text = next(
                (item.help for item in COMMANDS if item.path == path),
                f"{name} commands",
            )
            child = children[parent_path].add_parser(
                name, help=help_text, description=help_text
            )
            _add_common(child)
            parsers[path] = child
    for definition in COMMANDS:
        command_parser = parsers[definition.path]
        command_parser.set_defaults(_command_path=definition.path)
        _add_command_options(command_parser, definition.path)
    return parser


INPUT_FIELDS: dict[tuple[str, ...], set[str]] = {
    ("hook", "context"): {
        "hook_event_name",
        "source",
        "session_id",
        "cwd",
        "model",
        "permission_mode",
        "transcript_path",
    },
    ("setup",): {
        "brain",
        "beads",
        "runtime",
        "delivery",
        "models",
        "projects",
        "knowledge",
        "source_watch_root",
        "timing",
        "diagnostics",
        "resources",
        "policy",
    },
    ("config", "set"): {
        "brain",
        "beads",
        "runtime",
        "delivery",
        "models",
        "projects",
        "knowledge",
        "source_watch_root",
        "timing",
        "diagnostics",
        "resources",
        "policy",
    },
    ("policy", "set"): {
        "automatic_capacity",
        "default_project_capacity",
        "project_capacity",
        "paused_projects",
        "suspended_rules",
        "rationale",
    },
    ("project", "add"): {
        "id",
        "root",
        "codex_project_id",
        "delivery",
        "integration_branch",
        "prepare_argv",
        "validate_argv",
        "enabled",
        "source_remote",
        "require_source_sync",
        "models",
    },
    ("work", "create"): {
        "title",
        "outcome",
        "project",
        "acceptance",
        "requested_role",
        "summary",
        "dependencies",
        "priority",
        "size",
        "overlap_tags",
        "context",
        "intake",
        "models",
        "children",
    },
    ("work", "update"): {
        "title",
        "outcome",
        "acceptance",
        "summary",
        "size",
        "overlap_tags",
        "context",
        "priority",
        "intake",
        "models",
    },
    ("work", "dependencies"): {"add", "remove"},
    ("work", "close"): {
        "waived_requirements",
        "known_defects",
        "canonical_bead",
    },
    ("finish",): {
        "summary",
        "source_oid",
        "checks",
        "evidence",
        "findings",
        "known_defects",
        "waived_requirements",
        "answer",
    },
    ("progress",): {"kind", "summary", "evidence", "bead"},
    ("report",): {
        "title",
        "problem",
        "observed_evidence",
        "required_change",
        "acceptance_checks",
        "project",
        "discovered_from",
        "context",
        "intake",
    },
    ("marshal", "decide"): {"decision_operation", "decisions"},
    ("human", "resolve"): {"reason_id", "answer", "scope_change"},
    ("task", "start"): {
        "instructions",
        "title",
        "developer_instructions",
        "purpose",
        "associated_beads",
    },
    ("task", "send"): {"text"},
    ("task", "respond"): {"response"},
    ("recover", "repair"): {"actions"},
    ("plan", "draft"): {"text", "tasks", "summary", "publication", "validation"},
    ("plan", "review", "finish"): {"review_operation", "findings", "summary"},
    ("plan", "approve"): {"reason", "resolutions", "waivers"},
    ("plan", "publish"): {
        "approval_operation",
        "approved_by",
        "approval_evidence",
        "tasks",
        "publication",
    },
    ("plan", "refine"): {
        "approval_operation",
        "tasks",
        "publication",
        "dispositions",
        "cosmetic_changes",
    },
    ("memory", "set"): {"id", "scope", "title", "text", "references"},
    ("rates", "add"): {
        "model",
        "currency",
        "effective_at",
        "retrieved_at",
        "source",
        "prices",
    },
    ("fixture", "create"): {
        "root",
        "runtime",
        "delivery",
        "model",
        "effort",
        "capacity",
        "source_sync",
    },
    ("fixture", "barrier", "prepare"): {"participants"},
    ("scenario", "emit"): {"provider", "event", "facts"},
    ("scenario", "advance"): {"seconds"},
    ("scenario", "fault"): {"provider", "method", "occurrence", "applied", "outcome"},
}


DIRECT_INPUTS: dict[tuple[str, ...], dict[str, str]] = {
    ("enter",): {"description": "description", "bead": "bead"},
    ("finish",): {"outcome": "outcome", "bead": "bead"},
    ("progress",): {
        "kind": "kind",
        "summary": "summary",
        "evidence": "evidence",
        "bead": "bead",
    },
}


def _read_input(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        else:
            with Path(path).open("rb") as handle:
                raw = handle.read(MAX_MESSAGE_BYTES + 1)
    except OSError as error:
        raise FulcrumError.invalid(
            "INPUT_UNREADABLE", f"cannot read input: {error}"
        ) from error
    if len(raw) > MAX_MESSAGE_BYTES:
        raise FulcrumError.invalid(
            "INPUT_TOO_LARGE",
            "input exceeds the 4 MiB limit",
            details={"limit": MAX_MESSAGE_BYTES},
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FulcrumError.invalid(
            "INVALID_JSON", f"input is not valid UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FulcrumError.invalid("INVALID_INPUT", "input must be a JSON object")
    return value


def _payload(namespace: argparse.Namespace, command: tuple[str, ...]) -> dict[str, Any]:
    values = vars(namespace)
    supplied = _read_input(values.get("input"))
    direct: dict[str, Any] = {}
    for destination, field in DIRECT_INPUTS.get(command, {}).items():
        if destination in values:
            direct[field] = values[destination]
    duplicates = sorted(set(supplied).intersection(direct))
    if duplicates:
        raise FulcrumError.invalid(
            "DUPLICATE_INPUT",
            "fields were supplied both directly and through --input",
            details={"fields": duplicates},
        )
    merged = {**supplied, **direct}
    allowed = INPUT_FIELDS.get(command, set()).union(
        DIRECT_INPUTS.get(command, {}).values()
    )
    unknown = sorted(set(merged).difference(allowed))
    if unknown:
        raise FulcrumError.invalid(
            "UNKNOWN_FIELDS",
            "input contains unknown fields",
            details={"fields": unknown},
        )
    return merged


def _request_id(value: str | None, mutation: bool) -> str | None:
    if value is None:
        if not mutation:
            return None
        generated = str(uuid.uuid4())
        print(f"request_id={generated}", file=sys.stderr)
        return generated
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise FulcrumError.invalid(
            "INVALID_REQUEST_ID", "request ID must be a UUID"
        ) from error
    return str(parsed)


def _build_request(namespace: argparse.Namespace) -> ParsedRequest:
    values = vars(namespace)
    command = tuple(values["_command_path"])
    mutation = command not in READ_ONLY_COMMANDS and command != ("serve",)
    timeout = float(values.get("timeout", 30.0))
    if timeout <= 0:
        raise FulcrumError.invalid("INVALID_TIMEOUT", "timeout must be positive")
    allow_broken = command in BROKEN_CONFIG_COMMANDS
    instance = resolve_instance(
        instance=values.get("instance"),
        config=values.get("config"),
        allow_broken_config=allow_broken,
    )
    environment_task = os.environ.get("CODEX_THREAD_ID")
    thread_id = values.get("thread_id") or environment_task
    actor_text = values.get("actor") or (f"task:{thread_id}" if thread_id else "human")
    actor = ActorContext.parse(actor_text)
    common = {
        "_command_path",
        "instance",
        "config",
        "json",
        "input",
        "project",
        "thread_id",
        "actor",
        "request_id",
        "wait",
        "timeout",
        "offline",
        "model",
        "effort",
        "ownership_operation",
    }
    arguments = {
        key: value
        for key, value in values.items()
        if key not in common
        and not key.startswith("_level_")
        and key not in DIRECT_INPUTS.get(command, {})
    }
    if "model" in values:
        arguments["model"] = values["model"]
    if "effort" in values:
        arguments["effort"] = values["effort"]
    return ParsedRequest(
        command=command,
        arguments=arguments,
        input=_payload(namespace, command),
        actor=actor,
        instance=instance,
        request_id=_request_id(values.get("request_id"), mutation),
        project=values.get("project"),
        thread_id=thread_id,
        ownership_operation=values.get("ownership_operation"),
        wait=bool(values.get("wait", False)),
        timeout=timeout,
        offline=bool(values.get("offline", False)),
    )


def _serve(request: ParsedRequest) -> CommandResult:
    if request.instance.lock_path is None:
        raise FulcrumError(
            "CONFIG_INVALID", "a brain root is required to serve", exit_code=4
        )
    application = default_application()
    with WriterLock(request.instance.lock_path):
        supervisor = ControllerSupervisor(request, application)
        if request.arguments.get("once"):
            summary = asyncio.run(supervisor.run_once())
            return CommandResult.query(summary.to_dict())
        asyncio.run(supervisor.serve())
    return CommandResult.query({"stopped": True})


def _execute(request: ParsedRequest) -> dict[str, Any]:
    if request.command == ("serve",):
        return _serve(request).to_dict()
    application = default_application()
    is_read = request.command in READ_ONLY_COMMANDS
    if request.offline or is_read:
        if is_read:
            try:
                return request_sync(
                    request.instance.socket_path,
                    request.to_wire(),
                    timeout=request.timeout,
                )
            except ControllerUnavailable:
                return application.dispatch(replace(request, offline=True)).to_dict()
        if request.instance.lock_path is None:
            raise FulcrumError(
                "CONFIG_INVALID",
                "offline mutation requires a valid brain root",
                exit_code=4,
            )
        with WriterLock(request.instance.lock_path):
            return application.dispatch(request).to_dict()
    try:
        return request_sync(
            request.instance.socket_path, request.to_wire(), timeout=request.timeout
        )
    except ControllerUnavailable as error:
        raise FulcrumError(
            "CONTROLLER_UNAVAILABLE",
            str(error),
            exit_code=4,
            retryable=True,
            request_id=request.request_id,
            next_command=(
                "fulcrum",
                *request.command,
                "--offline",
                "--request-id",
                str(request.request_id),
            ),
        ) from error


def _exit_code(result: dict[str, Any]) -> int:
    if result.get("state") == "degraded":
        return 6
    if result.get("ok"):
        return 0
    error = result.get("error") or {}
    code = error.get("code")
    if code == "WAIT_TIMEOUT":
        return 3
    if code in {
        "OWNERSHIP_CONFLICT",
        "STALE_DECISION",
        "REQUEST_CONFLICT",
        "FINISH_SEALED",
        "STALE_REVIEW",
        "APPROVAL_CONFLICT",
        "ACTIVATION_NOT_AUTHORIZED",
    }:
        return 5
    if result.get("state") == "uncertain" or code in {
        "CONTROLLER_UNAVAILABLE",
        "WRITER_BUSY",
        "CAPABILITY_UNAVAILABLE",
        "CONFIG_NOT_FOUND",
        "CONFIG_INVALID",
        "DEPENDENCY_UNAVAILABLE",
    }:
        return 4
    return 2


def _emit(result: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
        return
    if result.get("ok"):
        print(result.get("state", "completed"))
        if result.get("result") is not None:
            print(json.dumps(result["result"], indent=2, ensure_ascii=False))
    else:
        error = result.get("error") or {}
        print(
            f"{error.get('code', 'ERROR')}: {error.get('message', 'command failed')}",
            file=sys.stderr,
        )
        if error.get("next_command"):
            print("next: " + " ".join(error["next_command"]), file=sys.stderr)


def _emit_log_stream(request: ParsedRequest, result: dict[str, Any]) -> None:
    current = result.get("result") or {}
    emitted = 0
    cursor = current.get("cursor")
    gaps: list[Any] = list(current.get("gaps", []))

    def emit_items(items: Sequence[Mapping[str, Any]]) -> None:
        nonlocal emitted
        for item in items:
            print(
                json.dumps(
                    {"ok": True, "state": "running", "event": item},
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                flush=True,
            )
            emitted += 1

    emit_items(current.get("items", []))
    deadline = time.monotonic() + request.timeout
    log = DiagnosticLog.from_request(request)
    arguments = request.arguments
    while time.monotonic() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        page = log.read(
            bead=str(arguments["bead"]) if "bead" in arguments else None,
            operation=(
                str(arguments["operation"]) if "operation" in arguments else None
            ),
            since=str(arguments["since"]) if "since" in arguments else None,
            limit=int(arguments.get("limit", 20)),
            cursor=str(cursor) if cursor else None,
            max_bytes=int(arguments.get("max_bytes", 256 * 1024)),
        )
        emit_items(page["items"])
        if page.get("cursor"):
            cursor = page["cursor"]
        gaps.extend(page.get("gaps", []))
    print(
        json.dumps(
            {
                "ok": True,
                "state": "completed",
                "result": {
                    "items_emitted": emitted,
                    "cursor": cursor,
                    "gaps": gaps,
                    "observed_until": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    namespace: argparse.Namespace | None = None
    try:
        namespace = parser.parse_args(argv)
        request = _build_request(namespace)
        result = _execute(request)
        if request.command == ("hook", "context"):
            print(
                json.dumps(
                    result.get("result") or {"continue": True},
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            )
            return 0
        if request.command == ("logs",) and request.arguments.get("follow"):
            _emit_log_stream(request, result)
            return _exit_code(result)
        _emit(result, json_output=bool(getattr(namespace, "json", False)))
        return _exit_code(result)
    except FulcrumError as error:
        if namespace is not None and tuple(getattr(namespace, "_command_path", ())) == (
            "hook",
            "context",
        ):
            print('{"continue":true}')
            return 0
        result = error.to_result().to_dict()
        json_output = (
            bool(getattr(namespace, "json", False))
            if namespace
            else ("--json" in (argv or sys.argv[1:]))
        )
        _emit(result, json_output=json_output)
        return error.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
