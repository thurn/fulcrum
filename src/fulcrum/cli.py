"""Installed Fulcrum command line.

The parser is declarative so the complete public command tree is visible in one
place. Workflow behavior lives behind :mod:`fulcrum.application`, never here.
"""

from __future__ import annotations

from fulcrum.timing import timed

import argparse
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
from fulcrum.contracts import (
    ActorContext,
    FulcrumError,
    ParsedRequest,
)
from fulcrum.diagnostics import DiagnosticLog
from fulcrum.instance import resolve_instance

MAX_MESSAGE_BYTES = 4 * 1024 * 1024

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
    ("work", "show"),
    ("work", "list"),
    ("work", "children"),
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
    ("plan", "show"),
    ("ledger", "status"),
    ("usage",),
    ("cost",),
    ("rates", "list"),
    ("rates", "show"),
}
BROKEN_CONFIG_COMMANDS = {
    ("service", "status"),
}
LOCAL_COMMANDS = {
    ("service", "start"),
    ("service", "stop"),
    ("service", "restart"),
    ("service", "update"),
    ("service", "status"),
    ("reset",),
}


@dataclass(frozen=True)
class CommandDefinition:
    path: tuple[str, ...]
    help: str


COMMANDS = (
    CommandDefinition(("bootstrap",), "prepare the Fulcrum Desktop instance"),
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
    CommandDefinition(("service", "start"), "start the owned broker and Dolt services"),
    CommandDefinition(("service", "stop"), "stop the broker after pausing admission"),
    CommandDefinition(("service", "restart"), "restart the broker safely"),
    CommandDefinition(("service", "update"), "apply exceptional broker maintenance"),
    CommandDefinition(
        ("service", "status"), "inspect broker and Dolt service artifacts"
    ),
    CommandDefinition(("skills", "reconcile"), "repair owned role skill links"),
    CommandDefinition(("work", "create"), "create a work root or graph"),
    CommandDefinition(("work", "show"), "show work"),
    CommandDefinition(("work", "list"), "list work"),
    CommandDefinition(("work", "adopt"), "adopt existing work"),
    CommandDefinition(("work", "update"), "update work scope"),
    CommandDefinition(("work", "children"), "show work children"),
    CommandDefinition(("work", "dependencies"), "change work dependencies"),
    CommandDefinition(("work", "close"), "close work with a disposition"),
    CommandDefinition(("work", "reopen"), "reopen work under a new acquisition"),
    CommandDefinition(("finish",), "finish the current role responsibility"),
    CommandDefinition(("progress",), "record substantive progress"),
    CommandDefinition(("report",), "file an independently attributed follow-up"),
    CommandDefinition(("worktree", "prepare"), "prepare a managed delivery workspace"),
    CommandDefinition(("worktree", "inspect"), "inspect a managed delivery workspace"),
    CommandDefinition(("worktree", "cleanup"), "clean a settled managed workspace"),
    CommandDefinition(
        ("validation", "check"), "run exact-source configured validation"
    ),
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
    CommandDefinition(("plan", "draft"), "save a complete unpublished plan draft"),
    CommandDefinition(("plan", "show"), "show retained plan facts"),
    CommandDefinition(("plan", "approve"), "approve retained plan scope"),
    CommandDefinition(("plan", "publish"), "publish approved plan scope"),
    CommandDefinition(("plan", "refine"), "refine approved plan scope"),
    CommandDefinition(("plan", "activate"), "activate authorized future scope"),
    CommandDefinition(
        ("plan", "complete"), "mechanically inspect and close a plan root"
    ),
    CommandDefinition(("ledger", "sync"), "flush native Beads history"),
    CommandDefinition(("ledger", "status"), "show native publication status"),
    CommandDefinition(("usage",), "query unique managed native usage"),
    CommandDefinition(("usage", "reconcile"), "reconcile native usage observations"),
    CommandDefinition(("cost",), "query API-equivalent workflow cost"),
    CommandDefinition(("rates", "list"), "list retained rate cards"),
    CommandDefinition(("rates", "show"), "show a retained rate card"),
    CommandDefinition(("rates", "add"), "add an immutable documented rate card"),
    CommandDefinition(("reset",), "perform an explicitly authorized hard reset"),
    CommandDefinition(("transport", "snapshot"), "inspect durable broker inputs"),
    CommandDefinition(("register", "standing"), "register a standing native task"),
    CommandDefinition(("action", "claim"), "claim one exact native invocation"),
    CommandDefinition(("action", "result"), "record one native invocation result"),
    CommandDefinition(("instruction", "wait"), "wait for Steward instructions"),
    CommandDefinition(("worker", "register"), "register an assigned worker"),
    CommandDefinition(("candidate", "submit"), "submit an exact Warden candidate"),
    CommandDefinition(("ci", "wait"), "wait for exact candidate CI"),
    CommandDefinition(("pause",), "pause new Fulcrum effects"),
    CommandDefinition(("resume",), "resume eligible Fulcrum effects"),
    CommandDefinition(("hook", "handle"), "handle one trusted native hook event"),
    CommandDefinition(("marshal", "check"), "run one scheduled Marshal check"),
    CommandDefinition(("marshal", "apply"), "apply targeted Marshal decisions"),
    CommandDefinition(("incident", "report"), "record or update one incident"),
    CommandDefinition(("repair", "record"), "record one repair-cycle outcome"),
    CommandDefinition(("recovery", "prepare"), "prepare one Justiciar intervention"),
    CommandDefinition(("decision", "respond"), "record one exact human decision"),
)


POSITIONAL_ID: set[tuple[str, ...]] = {
    path
    for path in (definition.path for definition in COMMANDS)
    if path
    in {
        ("project", "show"),
        ("project", "enable"),
        ("project", "disable"),
        ("work", "show"),
        ("work", "adopt"),
        ("work", "update"),
        ("work", "children"),
        ("work", "dependencies"),
        ("work", "close"),
        ("work", "reopen"),
        ("operation", "show"),
        ("operation", "wait"),
        ("operation", "cancel"),
        ("operation", "reconcile"),
        ("plan", "show"),
        ("plan", "activate"),
        ("plan", "complete"),
        ("rates", "show"),
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
    if path == ("bootstrap",):
        _option(parser, "--non-interactive", action="store_true")
    elif path in {("service", "stop"), ("service", "restart")}:
        _option(parser, "--interrupt", action="store_true")
    elif path == ("service", "update"):
        _option(parser, "--maintenance", action="store_true")
        _option(parser, "--retry", action="store_true")
    elif path == ("work", "list"):
        _option(parser, "--role", choices=ROLES)
        _option(parser, "--owner")
        _option(parser, "--phase")
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
    elif path == ("work", "adopt"):
        _option(parser, "--role", choices=ROLES, required=True)
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
    elif path in {
        ("worktree", "prepare"),
        ("worktree", "inspect"),
        ("worktree", "cleanup"),
    }:
        _option(parser, "--bead", required=True)
    elif path in {
        ("validation", "check"),
        ("validation", "start"),
        ("review", "approve"),
        ("promotion", "start"),
    }:
        _option(parser, "--bead", required=True)
        _option(
            parser,
            "--source",
            required=True,
        )
        if path == ("review", "approve"):
            _option(parser, "--summary", required=True)
    elif path in {
        ("validation", "show"),
        ("promotion", "show"),
        ("source", "sync"),
    }:
        _option(parser, "--bead", required=True)
    elif path == ("status",):
        _option(parser, "--bead")
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
    elif path == ("trace",):
        for name in ("bead", "operation", "action", "task"):
            _option(parser, f"--{name}")
        _option(parser, "--wait-id", dest="wait")
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
    elif path == ("plan", "draft") or path in {
        ("plan", "approve"),
        ("plan", "publish"),
        ("plan", "refine"),
    }:
        _option(parser, "--bead", required=True)
    elif path == ("plan", "activate"):
        _option(parser, "--authorization")
    elif path in {("usage",), ("cost",)}:
        for name in (
            "bead",
            "workflow",
            "operation",
            "role",
            "since",
            "until",
        ):
            _option(parser, f"--{name}")
        _option(
            parser,
            "--group-by",
            choices=(
                "bead",
                "workflow",
                "operation",
                "task",
                "role",
                "project",
                "model",
            ),
        )
    elif path == ("usage", "reconcile"):
        _option(parser, "--bead")
    elif path == ("rates", "list"):
        _option(parser, "--limit", type=int)
        _option(parser, "--cursor")
    elif path == ("reset",):
        _option(parser, "--hard", action="store_true", required=True)
        _option(parser, "--yes", action="store_true", required=True)
    elif path in {("action", "claim"), ("action", "result")}:
        _option(parser, "--record-id", required=True)
        _option(parser, "--action-id", required=True)
    elif path in {
        ("worker", "register"),
        ("candidate", "submit"),
        ("ci", "wait"),
        ("incident", "report"),
        ("repair", "record"),
        ("recovery", "prepare"),
        ("decision", "respond"),
    }:
        _option(parser, "--bead", required=True)


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
    ("bootstrap",): {
        "codex_root",
        "configuration",
        "native_tools",
        "model_support",
        "steward_thinking",
        "marshal_thinking",
        "vizier_thinking",
        "acceptance",
        "replacement",
    },
    ("register", "standing"): {
        "role",
        "task_id",
        "host_id",
        "session_id",
        "turn_id",
        "action_id",
    },
    ("action", "claim"): {"record_id", "action_id", "attempt_id", "native_tool_use_id"},
    ("action", "result"): {
        "record_id",
        "action_id",
        "attempt_id",
        "outcome",
        "evidence",
        "native_result",
    },
    ("instruction", "wait"): {"turn_id", "loop_id"},
    ("worker", "register"): {
        "bead",
        "assignment_token",
        "workspace",
        "host_id",
        "turn_id",
        "session_id",
        "git_root",
        "branch",
        "source",
    },
    ("candidate", "submit"): {
        "bead",
        "assignment_token",
        "source",
    },
    ("ci", "wait"): {"bead", "candidate_id", "assignment_token", "turn_id"},
    ("pause",): {"reason"},
    ("resume",): {"reason"},
    ("marshal", "check"): {"turn_id", "trigger", "schedule_id"},
    ("marshal", "apply"): {"decision_id", "decisions", "turn_id"},
    ("incident", "report"): {
        "bead",
        "incident_key",
        "scope",
        "required_decision",
        "evidence",
    },
    ("repair", "record"): {"bead", "incident_key", "outcome", "evidence"},
    ("recovery", "prepare"): {"bead", "incident_key", "scope"},
    ("decision", "respond"): {
        "bead",
        "decision_id",
        "answer",
        "additional_repair_cycles",
    },
    ("reset",): {
        "expected_remote_ref",
        "legacy_state_root",
        "legacy_database",
        "schedule_disable_result",
    },
    ("config", "set"): {
        "brain",
        "beads",
        "delivery",
        "models",
        "projects",
        "knowledge",
        "source",
        "timing",
        "diagnostics",
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
        "report_key",
        "implementation_ready",
        "dependencies",
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
        "assignment_token",
        "summary",
        "acceptance",
        "source_oid",
        "checks",
        "evidence",
        "findings",
        "known_defects",
        "waived_requirements",
        "answer",
        "plan_id",
        "changes",
        "blocker",
        "attempts",
        "required_action",
        "implementation_notes",
    },
    ("progress",): {"kind", "summary", "evidence", "bead", "assignment_token"},
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
        "report_key",
        "implementation_ready",
        "dependencies",
    },
    ("plan", "draft"): {"text", "tasks", "summary", "publication", "validation"},
    ("plan", "approve"): {"reason", "resolutions", "waivers"},
    ("plan", "publish"): {
        "approval_operation",
        "approved_by",
        "approval_evidence",
        "activation",
        "text",
        "reviews",
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
    ("rates", "add"): {
        "model",
        "currency",
        "effective_at",
        "retrieved_at",
        "source_url",
        "input_per_million",
        "cached_input_per_million",
        "cache_write_input_per_million",
        "output_per_million",
        "tiers",
        "long_context",
        "tools",
    },
}


DIRECT_INPUTS: dict[tuple[str, ...], dict[str, str]] = {
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
    if command == ("hook", "handle"):
        return merged
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


@timed("cli._build_request")
def _build_request(namespace: argparse.Namespace) -> ParsedRequest:
    values = vars(namespace)
    command = tuple(values["_command_path"])
    payload = _payload(namespace, command)
    mutation = command not in READ_ONLY_COMMANDS
    timeout = float(values.get("timeout", 30.0))
    if timeout <= 0:
        raise FulcrumError.invalid("INVALID_TIMEOUT", "timeout must be positive")
    if command == ("bootstrap",):
        configuration = payload.get("configuration", {})
        if not isinstance(configuration, Mapping):
            raise FulcrumError.invalid(
                "INVALID_INPUT", "configuration must be a JSON object"
            )
        provisional = resolve_instance(
            instance=values.get("instance"),
            config=values.get("config"),
            allow_broken_config=True,
        )
        if provisional.brain_root is None:
            from fulcrum.configuration import initialize_bootstrap_configuration
            from fulcrum.install import master_source_root

            initialize_bootstrap_configuration(
                provisional.config_path,
                configuration,
                source_repository=master_source_root(),
            )
    allow_broken = command in BROKEN_CONFIG_COMMANDS
    instance = resolve_instance(
        instance=values.get("instance"),
        config=values.get("config"),
        allow_broken_config=allow_broken,
    )
    environment_task = os.environ.get("CODEX_THREAD_ID")
    explicit_actor = values.get("actor")
    bootstrap_authority = command in {("setup",), ("bootstrap",)}
    if environment_task and not bootstrap_authority:
        if values.get("thread_id") not in {
            None,
            environment_task,
        } or explicit_actor not in {
            None,
            f"task:{environment_task}",
        }:
            raise FulcrumError(
                "AUTHORITY_MISMATCH",
                "a native task cannot claim another task or human identity",
                exit_code=5,
            )
        thread_id = environment_task
        actor_text = f"task:{environment_task}"
    else:
        thread_id = values.get("thread_id") or (
            None
            if explicit_actor == "human" or bootstrap_authority
            else environment_task
        )
        actor_text = values.get("actor") or (
            "human"
            if bootstrap_authority
            else f"task:{thread_id}" if thread_id else "human"
        )
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
    if command in {("usage",), ("cost",)}:
        if values.get("project") is not None:
            arguments["project"] = values["project"]
        if values.get("thread_id") is not None:
            arguments["thread_id"] = values["thread_id"]
    elif command == ("usage", "reconcile") and values.get("thread_id") is not None:
        arguments["thread_id"] = values["thread_id"]
    return ParsedRequest(
        command=command,
        arguments=arguments,
        input=payload,
        actor=actor,
        instance=instance,
        request_id=_request_id(values.get("request_id"), mutation),
        project=values.get("project"),
        thread_id=thread_id,
        ownership_operation=values.get("ownership_operation"),
        wait=bool(values.get("wait", False)),
        timeout=timeout,
    )


@timed("cli._execute")
def _execute(request: ParsedRequest) -> dict[str, Any]:
    if request.command == ("bootstrap",):
        from fulcrum.desktop_setup import prepare_bootstrap_primitives

        prepare_bootstrap_primitives(request)
    return default_application().dispatch(request).to_dict()


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
        "ROLE_MISMATCH",
        "STALE_DECISION",
        "REQUEST_CONFLICT",
        "FINISH_SEALED",
        "STALE_REVIEW",
        "APPROVAL_CONFLICT",
        "ACTIVATION_NOT_AUTHORIZED",
        "ACTIVE_SCOPE_CHANGE",
        "PUBLICATION_NOT_READY",
        "PLAN_AUTHORING_NOT_FINISHED",
    }:
        return 5
    if result.get("state") == "uncertain" or code in {
        "WRITER_BUSY",
        "CAPABILITY_UNAVAILABLE",
        "CONFIG_NOT_FOUND",
        "CONFIG_INVALID",
        "DEPENDENCY_UNAVAILABLE",
        "HEALTH_CHECK_FAILED",
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


@timed("cli.main")
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    namespace: argparse.Namespace | None = None
    try:
        namespace = parser.parse_args(argv)
        request = _build_request(namespace)
        result = _execute(request)
        if request.command in {("hook", "context"), ("hook", "handle")}:
            hook_result = result.get("result")
            print(
                json.dumps(
                    hook_result if hook_result is not None else {},
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
        if namespace is not None and tuple(getattr(namespace, "_command_path", ())) in {
            ("hook", "context"),
            ("hook", "handle"),
        }:
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
