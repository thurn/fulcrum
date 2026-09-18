"""Deterministic bootstrap for the Codex Desktop architecture."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.coordination import coordinated
from fulcrum.configuration import ConfigurationManager
from fulcrum.desktop_protocol import (
    DesktopProtocolService,
    _opaque,
    _protocol,
    _replay,
    _result,
    _saved_request,
    _save_request,
    _utc_now,
    _with_protocol,
)
from fulcrum.install import (
    InstallationError,
    install_service_definitions,
    master_source_root,
    reconcile_skills,
    service_definitions,
)

REQUIRED_NATIVE_TOOLS = {
    "create_thread",
    "send_message_to_thread",
    "list_projects",
    "list_threads",
    "read_thread",
    "set_thread_title",
    "set_thread_archived",
    "automation_update",
}
REQUIRED_ACCEPTANCE = {
    "workspace_access",
    "hook_identity",
    "transcript_lifecycle",
    "usage_accounting",
    "task_targeting",
}
PRE_ACTIVATION_ACCEPTANCE: set[str] = set(REQUIRED_ACCEPTANCE)
MARSHAL_HEARTBEAT_PROMPT = (
    "This is the scheduled Fulcrum heartbeat. Read CODEX_THREAD_ID and call "
    "marshal_check with that exact task_id and input "
    '`{"trigger":"heartbeat"}`. Settle only its bounded brief, then call '
    "marshal_decide with the same task_id, the returned turn_id, and input containing "
    "the returned decision_id plus targeted decisions; use an empty decisions array for a no-op. "
    "Do not inspect implementation source or invent identifiers. End quietly when "
    "no action is required."
)
STEWARD_HEARTBEAT_PROMPT = (
    "This is the scheduled Fulcrum Steward heartbeat. Read CODEX_THREAD_ID and "
    "call wait_for_instructions with that exact task_id. Process exactly one "
    "returned action: claim it with the exact record_id and action_id, invoke its "
    "native tool and arguments once, and report the actual result with the returned "
    "attempt_id. End the turn after the result is reported. If the wait returns an "
    "idle deadline or protocol stop, end quietly. Do not choose priorities, invent "
    "arguments, retry uncertain effects, poll tasks, or involve another standing role."
)


def _acceptance_evidence(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FulcrumError.invalid(
            "ACCEPTANCE_INVALID",
            "bootstrap acceptance must be an evidence map",
        )
    unknown = sorted(set(str(key) for key in value).difference(REQUIRED_ACCEPTANCE))
    if unknown:
        raise FulcrumError.invalid(
            "ACCEPTANCE_INVALID",
            "bootstrap acceptance contains unknown checks",
            details={"unknown": unknown},
        )
    normalized: dict[str, dict[str, Any]] = {}
    for name, supplied in value.items():
        if not isinstance(supplied, Mapping):
            raise FulcrumError.invalid(
                "ACCEPTANCE_EVIDENCE_REQUIRED",
                f"acceptance.{name} must include passed and evidence",
            )
        evidence = supplied.get("evidence")
        if (
            not isinstance(supplied.get("passed"), bool)
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item.strip() for item in evidence)
        ):
            raise FulcrumError.invalid(
                "ACCEPTANCE_EVIDENCE_REQUIRED",
                f"acceptance.{name} requires passed=true|false and nonempty evidence strings",
            )
        normalized[str(name)] = {
            "passed": supplied["passed"],
            "evidence": [str(item).strip() for item in evidence],
        }
    return normalized


STANDING = {
    "steward": {
        "title": "🧰 STEWARD 🧰",
        "model": "gpt-5.6-luna",
        "prompt": (
            "First call register_standing with role `steward` and the action_id from "
            "the Fulcrum-Action marker; supply CODEX_THREAD_ID as task_id and "
            "CODEX_SESSION_ID as session_id. Omit request_id for new Fulcrum calls; "
            "the MCP server generates valid UUIDs. Then call wait_for_instructions "
            "without inventing loop or turn IDs. Run each blocking wait in one "
            'functions.exec cell beginning `// @exec: {"yield_time_ms": 3900000, '
            '"max_output_tokens": 10000}`, await the MCP result in that cell, and '
            "never poll it with functions.wait. "
            "For each returned action, call claim_action with exactly record_id, "
            "action_id, and task_id set to CODEX_THREAD_ID; do not pass an input object. "
            "Invoke its exact native tool and arguments once. Then call "
            "report_action_result with record_id, action_id, attempt_id, task_id set to "
            "CODEX_THREAD_ID, outcome, native_result, and optional evidence, and "
            "immediately wait again. Do not choose priorities, invent prompts, retry "
            "uncertain effects, or poll tasks. Treat an idle_deadline as a heartbeat and "
            "wait again. End only on an explicit pause, shutdown, protocol stop, or "
            "unrecoverable transport failure."
        ),
    },
    "marshal": {
        "title": "🧭 MARSHAL 🧭",
        "model": "gpt-5.6-sol",
        "prompt": (
            "$fulcrum-marshal\nRead and follow `~/fulcrum/skills/fulcrum-marshal/SKILL.md` "
            "from local master. First call register_standing with role `marshal` and the action_id from "
            "the Fulcrum-Action marker; supply CODEX_THREAD_ID as task_id and "
            "CODEX_SESSION_ID as session_id, and omit request_id for a new registration. On scheduled prompts call "
            "marshal_check, settle only the returned bounded curation or recovery scope, and "
            "end quietly when there is no action."
        ),
    },
    "vizier": {
        "title": "🔮 VIZIER 🔮",
        "model": "gpt-5.6-sol",
        "prompt": (
            "$fulcrum-vizier\nRead and follow `~/fulcrum/skills/fulcrum-vizier/SKILL.md` "
            "from local master. First call register_standing with role `vizier` and the action_id from "
            "the Fulcrum-Action marker; supply CODEX_THREAD_ID as task_id and "
            "CODEX_SESSION_ID as session_id, and omit request_id for a new registration. Present exact retained human "
            "decisions and record only the authority the human explicitly grants."
        ),
    },
}


def prepare_bootstrap_primitives(request: ParsedRequest) -> dict[str, Any]:
    """Create only the bounded filesystem/service prerequisites needed for Beads."""

    if request.instance.brain_root is None:
        raise FulcrumError(
            "CONFIG_INVALID",
            "bootstrap requires an authoritative brain root",
            exit_code=4,
        )
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    try:
        source = master_source_root()
    except InstallationError:
        source = Path(__file__).resolve().parents[2]
    executable = source / ".venv" / "bin" / "fulcrum"
    definitions = service_definitions(
        instance_root=request.instance.instance_root,
        config_path=request.instance.config_path,
        brain_root=request.instance.brain_root,
        config=config,
        fulcrum_executable=executable,
        production=not request.instance.explicit_selection,
    )
    installed, changed = install_service_definitions(
        definitions, request.instance.instance_root
    )
    from fulcrum.desktop_services import _start

    (request.instance.brain_root / ".beads" / "dolt").mkdir(
        parents=True, exist_ok=True, mode=0o700
    )
    dolt = _start(installed["dolt"])
    beads = config["beads"]
    beads_executable = beads.get("executable")
    if not isinstance(beads_executable, str) or not beads_executable:
        raise FulcrumError(
            "BEADS_UNAVAILABLE", "configured bd executable is unavailable", exit_code=4
        )
    beads_config = request.instance.brain_root / ".beads" / "config.yaml"
    initialized = False
    if not beads_config.is_file():
        completed = subprocess.run(
            [
                beads_executable,
                "-C",
                str(request.instance.brain_root),
                "init",
                "--server",
                "--external",
                "--server-host",
                str(beads["host"]),
                "--server-port",
                str(beads["port"]),
                "--database",
                str(beads["database"]),
                "--prefix",
                "fc",
                "--init-if-missing",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
            ],
            capture_output=True,
            text=True,
            timeout=max(30.0, request.timeout),
        )
        if completed.returncode:
            raise FulcrumError(
                "BEADS_INIT_FAILED",
                completed.stderr.strip()
                or completed.stdout.strip()
                or "Beads init failed",
                exit_code=4,
                retryable=True,
            )
        initialized = True
    broker = _start(installed["broker"])
    return {
        "changed": changed,
        "dolt": dolt,
        "broker": broker,
        "beads_initialized": initialized,
    }


def install_codex_mcp_config(
    path: Path,
    *,
    instance: Path,
    config: Path,
    executable: Path,
    tool_timeout_seconds: int,
) -> dict[str, Any]:
    """Install one owned TOML block while preserving unrelated Codex settings."""

    begin = "# BEGIN FULCRUM MCP"
    end = "# END FULCRUM MCP"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if (begin in existing) != (end in existing):
        raise FulcrumError(
            "MCP_CONFIG_CONFLICT",
            "Fulcrum MCP configuration markers are incomplete",
            exit_code=4,
        )
    escaped_command = json.dumps(str(executable.resolve(strict=False)))
    escaped_instance = json.dumps(str(instance.resolve(strict=False)))
    escaped_config = json.dumps(str(config.resolve(strict=False)))
    block = (
        f"{begin}\n"
        "[mcp_servers.fulcrum]\n"
        f"command = {escaped_command}\n"
        f'args = ["--instance", {escaped_instance}, "--config", {escaped_config}]\n'
        "startup_timeout_sec = 10\n"
        f"tool_timeout_sec = {tool_timeout_seconds}\n"
        "required = true\n"
        'default_tools_approval_mode = "auto"\n'
        f"{end}"
    )
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    updated = (
        pattern.sub(block, existing)
        if begin in existing
        else existing.rstrip() + "\n\n" + block + "\n"
    )
    changed = updated != existing
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}")
        temporary.write_text(updated, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    return {"path": str(path), "changed": changed, "server": "fulcrum"}


class DesktopSetupService(DesktopProtocolService):
    @coordinated
    def bootstrap(self, request: ParsedRequest) -> CommandResult:
        if request.actor.kind != "human":
            raise FulcrumError(
                "BOOTSTRAP_AUTHORITY",
                "bootstrap requires the authorized caller",
                exit_code=5,
            )
        ledger = self._ledger(request)
        if self._ledger_override is None:
            from fulcrum.analytics import seed_bundled_rates

            seed_bundled_rates(ledger)
        system = self._system(ledger)
        protocol = _protocol(system.fc or {})
        saved = _saved_request(protocol, request, ledger=ledger)
        if saved:
            return _replay(saved, request)
        instance = request.instance.instance_root.resolve(strict=False)
        try:
            source = master_source_root()
        except InstallationError:
            source = Path(__file__).resolve().parents[2]
        executable = source / ".venv" / "bin" / "fulcrum-mcp"
        if not executable.is_file():
            executable = Path(sys.executable).with_name("fulcrum-mcp")
        codex_root_value = request.input.get("codex_root")
        codex_root = (
            Path(str(codex_root_value)).resolve(strict=False)
            if codex_root_value
            else Path.home() / ".codex"
        )
        mcp = install_codex_mcp_config(
            codex_root / "config.toml",
            instance=instance,
            config=request.instance.config_path,
            executable=executable,
            tool_timeout_seconds=self._timing_seconds(
                request, "mcp_tool_timeout_seconds", 3900
            ),
        )
        skills = reconcile_skills(
            instance,
            production=not request.instance.explicit_selection,
            config_path=request.instance.config_path,
            skills_root=(codex_root / "skills"),
            source_root=source,
        )
        service_setup: dict[str, Any]
        service_gap: str | None = None
        if self._ledger_override is not None:
            service_setup = {"state": "injected"}
        elif request.instance.config_path.is_file() and request.instance.brain_root:
            try:
                manager = ConfigurationManager(request.instance.config_path)
                document, _ = manager.load()
                definitions = service_definitions(
                    instance_root=instance,
                    config_path=request.instance.config_path,
                    brain_root=request.instance.brain_root,
                    config=manager.effective(document),
                    fulcrum_executable=source / ".venv" / "bin" / "fulcrum",
                    production=not request.instance.explicit_selection,
                )
                installed, changed = install_service_definitions(definitions, instance)
                service_setup = {
                    "installed": sorted(installed),
                    "changed": changed,
                    "definitions": {
                        name: str(service.definition)
                        for name, service in installed.items()
                    },
                }
            except (InstallationError, OSError) as error:
                service_gap = str(error)
                service_setup = {"error": service_gap}
        else:
            service_gap = "authoritative configuration and brain are required"
            service_setup = {"error": service_gap}
        supplied_tools = request.input.get("native_tools")
        available_tools = (
            {str(value) for value in supplied_tools}
            if isinstance(supplied_tools, list)
            else set()
        )
        missing_tools = sorted(REQUIRED_NATIVE_TOOLS.difference(available_tools))
        configured_models: Mapping[str, Any] = {}
        try:
            manager = ConfigurationManager(request.instance.config_path)
            document, _ = manager.load()
            configured_models = manager.effective(document)["models"]
        except FulcrumError:
            pass

        def role_model(role: str, definition: Mapping[str, Any]) -> tuple[str, str]:
            selected = configured_models.get(role)
            model = (
                selected.get("model")
                if isinstance(selected, Mapping)
                else definition["model"]
            )
            effort = (
                selected.get("effort")
                if isinstance(selected, Mapping)
                else request.input.get(f"{role}_thinking", "high")
            )
            return str(model), str(effort)

        model_support = request.input.get("model_support")
        missing_models: list[str] = []
        if isinstance(model_support, Mapping):
            for role, definition in STANDING.items():
                model, effort = role_model(role, definition)
                efforts = model_support.get(model)
                if not isinstance(efforts, list) or effort not in efforts:
                    missing_models.append(model)
        else:
            missing_models = sorted(
                {
                    role_model(role, definition)[0]
                    for role, definition in STANDING.items()
                }
            )
        setup = dict(protocol.get("setup") or {})
        hook_status = copy_mapping(skills.get("hook"))
        setup.update(
            {
                "state": "preparing",
                "mcp": mcp,
                "hooks": hook_status,
                "missing_native_tools": missing_tools,
                "missing_models": missing_models,
                "broker_socket": str(instance / "broker.sock"),
                "services": service_setup,
                "updated_at": _utc_now(),
            }
        )
        protocol["setup"] = setup
        actions = dict(protocol.get("actions") or {})
        standing = dict(protocol.get("standing") or {})
        replacement = request.input.get("replacement")
        replacement_role = (
            str(replacement.get("role") or "")
            if isinstance(replacement, Mapping)
            else ""
        )
        for role in STANDING:
            binding = standing.get(role)
            if (
                role != replacement_role
                and isinstance(binding, Mapping)
                and binding.get("state")
                in {"stopped", "stop_observed", "interrupt_observed"}
            ):
                recovered = dict(binding)
                recovered["state"] = "registered"
                recovered["lifecycle_recovered_at"] = _utc_now()
                recovered.pop("positively_completed_at", None)
                standing[role] = recovered
        protocol["standing"] = standing
        if isinstance(replacement, Mapping):
            role = str(replacement.get("role") or "")
            reason = replacement.get("reason")
            current = standing.get(role)
            termination = replacement.get("termination_evidence")
            old_task_id = (
                current.get("task_id") if isinstance(current, Mapping) else None
            )
            positively_terminated = (
                isinstance(current, Mapping) and current.get("state") == "stopped"
            ) or (
                isinstance(termination, Mapping)
                and (
                    termination.get("threadId")
                    or termination.get("thread_id")
                    or termination.get("id")
                )
                == old_task_id
                and str(termination.get("status") or termination.get("state"))
                in {"archived", "completed", "failed", "not_found"}
            )
            if (
                role not in STANDING
                or not isinstance(reason, str)
                or not reason.strip()
                or replacement.get("confirmed_lost") is not True
                or not isinstance(current, Mapping)
                or replacement.get("old_task_id") != current.get("task_id")
                or not positively_terminated
            ):
                raise FulcrumError(
                    "REPLACEMENT_NOT_AUTHORIZED",
                    "standing replacement requires the exact old task and positive terminal or loss evidence",
                    exit_code=5,
                )
            for candidate in ledger.list_records(limit=0):
                candidate_protocol = (
                    protocol
                    if candidate.id == system.id
                    else _protocol(candidate.fc or {})
                )
                waits = dict(candidate_protocol.get("instruction_waits") or {})
                changed = False
                for wait_id, retained in tuple(waits.items()):
                    if (
                        isinstance(retained, Mapping)
                        and retained.get("task_id") == old_task_id
                        and retained.get("state") == "waiting"
                    ):
                        waits[wait_id] = {
                            **dict(retained),
                            "state": "cancelled",
                            "resolved_at": _utc_now(),
                            "response": {
                                "kind": "stop",
                                "reason": "standing_replaced",
                                "wait_id": wait_id,
                                "retained_obligation": False,
                            },
                        }
                        changed = True
                if not changed:
                    continue
                candidate_protocol["instruction_waits"] = waits
                if candidate.id == system.id:
                    protocol = candidate_protocol
                else:
                    ledger.update_fc(
                        candidate.id,
                        _with_protocol(candidate.fc or {}, candidate_protocol),
                    )
            unresolved = []
            for candidate in ledger.list_records(limit=0):
                candidate_protocol = (
                    protocol
                    if candidate.id == system.id
                    else _protocol(candidate.fc or {})
                )
                for action in (candidate_protocol.get("actions") or {}).values():
                    if (
                        isinstance(action, Mapping)
                        and action.get("claimed_by") == old_task_id
                        and action.get("state") in {"issuing", "uncertain"}
                    ):
                        unresolved.append(action.get("action_id"))
                for wait in (
                    candidate_protocol.get("instruction_waits") or {}
                ).values():
                    if (
                        isinstance(wait, Mapping)
                        and wait.get("task_id") == old_task_id
                        and wait.get("state") == "waiting"
                    ):
                        unresolved.append(wait.get("wait_id"))
            if unresolved:
                raise FulcrumError(
                    "REPLACEMENT_EFFECTS_UNSETTLED",
                    "standing replacement is fenced until the old task's waits and native effects settle",
                    exit_code=5,
                    details={"unresolved": unresolved},
                )
            existing_recovery = next(
                (
                    action
                    for action in actions.values()
                    if isinstance(action, Mapping)
                    and action.get("purpose") == f"recover_{role}"
                    and action.get("state") in {"pending", "issuing", "uncertain"}
                ),
                None,
            )
            if existing_recovery is None:
                definition = STANDING[role]
                model, effort = role_model(role, definition)
                action_id = _opaque("action")
                actions[action_id] = {
                    "action_id": action_id,
                    "record_id": system.id,
                    "executor": "bootstrap",
                    "tool": "create_thread",
                    "arguments": {
                        "prompt": definition["prompt"],
                        "title": definition["title"],
                        "model": model,
                        "thinking": effort,
                        "target": {"type": "projectless"},
                    },
                    "expected_result": {"threadId": f"replacement {role}"},
                    "reporting": {"register": "register_standing", "role": role},
                    "state": "pending",
                    "attempts": [],
                    "created_at": _utc_now(),
                    "purpose": f"recover_{role}",
                }
                standing[role] = {
                    **dict(current),
                    "state": "replacement_pending",
                    "replacement_action_id": action_id,
                    "replacement_reason": reason.strip(),
                }
                protocol["run_control"] = "paused"
                protocol["standing"] = standing
        for role, definition in STANDING.items():
            if isinstance(standing, Mapping) and role in standing:
                continue
            if any(
                isinstance(action, Mapping)
                and action.get("purpose") == f"bootstrap_{role}"
                and action.get("state") not in {"rejected", "superseded"}
                for action in actions.values()
            ):
                continue
            action_id = _opaque("action")
            model, effort = role_model(role, definition)
            actions[action_id] = {
                "action_id": action_id,
                "record_id": system.id,
                "executor": "bootstrap",
                "tool": "create_thread",
                "arguments": {
                    "prompt": definition["prompt"],
                    "title": definition["title"],
                    "model": model,
                    "thinking": effort,
                    "target": {"type": "projectless"},
                },
                "expected_result": {"threadId": f"registered {role}"},
                "reporting": {"register": "register_standing", "role": role},
                "state": "pending",
                "attempts": [],
                "created_at": _utc_now(),
                "purpose": f"bootstrap_{role}",
            }
        protocol["actions"] = actions
        standing_ready = all(
            isinstance(standing.get(role), Mapping)
            and standing[role].get("state") == "registered"
            for role in STANDING
        )
        diagnostic_action = next(
            (
                action
                for action in actions.values()
                if isinstance(action, Mapping)
                and action.get("purpose") == "bootstrap_diagnostic_target"
                and action.get("state") not in {"rejected", "superseded"}
            ),
            None,
        )
        if standing_ready and not isinstance(diagnostic_action, Mapping):
            steward = standing["steward"]
            marshal = standing["marshal"]
            action_id = _opaque("action")
            diagnostic_action = {
                "action_id": action_id,
                "record_id": system.id,
                "executor": "bootstrap",
                "tool": "send_message_to_thread",
                "arguments": {
                    "threadId": steward.get("task_id"),
                    "prompt": (
                        "Retain this exact diagnostic escape hatch: if Fulcrum MCP/CLI "
                        "or Beads is unavailable, you may send exactly one best-effort "
                        f"diagnostic message directly to Marshal task {marshal.get('task_id')} "
                        "and then stop. This grants no work creation, retry, policy, or "
                        "ownership authority. Do not loop if delivery fails."
                    ),
                },
                "expected_result": {"thread_id": steward.get("task_id")},
                "reporting": {"purpose": "diagnostic_escape_hatch"},
                "state": "pending",
                "attempts": [],
                "created_at": _utc_now(),
                "purpose": "bootstrap_diagnostic_target",
            }
            actions[action_id] = diagnostic_action
            protocol["actions"] = actions
        acceptance_evidence = _acceptance_evidence(request.input.get("acceptance"))
        hook_acceptance = acceptance_evidence.get("hook_identity", {})
        if hook_acceptance.get("passed"):
            hook_status = {
                **hook_status,
                "operational_state": "evidence_confirmed",
                "operator_confirmation_required": False,
                "evidence": list(hook_acceptance.get("evidence") or []),
            }
            setup["hooks"] = hook_status
        setup["acceptance"] = acceptance_evidence
        missing_acceptance = sorted(
            name
            for name in REQUIRED_ACCEPTANCE
            if not acceptance_evidence.get(name, {}).get("passed")
        )
        missing_pre_activation = sorted(
            name
            for name in PRE_ACTIVATION_ACCEPTANCE
            if not acceptance_evidence.get(name, {}).get("passed")
        )
        schedule = protocol.get("marshal_schedule")
        if (
            standing_ready
            and not missing_pre_activation
            and not isinstance(schedule, Mapping)
        ):
            marshal = standing["marshal"]
            action_id = _opaque("action")
            actions[action_id] = {
                "action_id": action_id,
                "record_id": system.id,
                "executor": "bootstrap",
                "tool": "automation_update",
                "arguments": {
                    "mode": "create",
                    "kind": "heartbeat",
                    "name": "Fulcrum Marshal check",
                    "prompt": MARSHAL_HEARTBEAT_PROMPT,
                    "rrule": "FREQ=MINUTELY;INTERVAL=15",
                    "status": "ACTIVE",
                    "notificationPolicy": "failed_runs_only",
                    "targetThreadId": marshal.get("task_id"),
                    "destination": "thread",
                },
                "expected_result": {
                    "automation_id": "bound Marshal heartbeat",
                    "status": "ACTIVE",
                },
                "reporting": {"purpose": "marshal_schedule"},
                "state": "pending",
                "attempts": [],
                "created_at": _utc_now(),
                "purpose": "bootstrap_marshal_schedule",
            }
            schedule = {
                "action_id": action_id,
                "state": "pending",
                "interval_minutes": 15,
                "target_task_id": marshal.get("task_id"),
                "prompt": MARSHAL_HEARTBEAT_PROMPT,
                "rrule": "FREQ=MINUTELY;INTERVAL=15",
                "status": "ACTIVE",
            }
            protocol["marshal_schedule"] = schedule
        steward_schedule = protocol.get("steward_schedule")
        if (
            standing_ready
            and not missing_pre_activation
            and (
                not isinstance(steward_schedule, Mapping)
                or not steward_schedule.get("action_id")
            )
        ):
            steward = standing["steward"]
            action_id = _opaque("action")
            actions[action_id] = {
                "action_id": action_id,
                "record_id": system.id,
                "executor": "bootstrap",
                "tool": "automation_update",
                "arguments": {
                    "mode": "create",
                    "kind": "heartbeat",
                    "name": "Fulcrum Steward loop",
                    "prompt": STEWARD_HEARTBEAT_PROMPT,
                    "rrule": "FREQ=MINUTELY;INTERVAL=1",
                    "status": "ACTIVE",
                    "notificationPolicy": "failed_runs_only",
                    "targetThreadId": steward.get("task_id"),
                    "destination": "thread",
                },
                "expected_result": {
                    "automation_id": "bound Steward heartbeat",
                    "status": "ACTIVE",
                },
                "reporting": {"purpose": "steward_schedule"},
                "state": "pending",
                "attempts": [],
                "created_at": _utc_now(),
                "purpose": "bootstrap_steward_schedule",
            }
            steward_schedule = {
                "action_id": action_id,
                "state": "pending",
                "interval_minutes": 1,
                "target_task_id": steward.get("task_id"),
                "prompt": STEWARD_HEARTBEAT_PROMPT,
                "rrule": "FREQ=MINUTELY;INTERVAL=1",
                "status": "ACTIVE",
            }
            protocol["steward_schedule"] = steward_schedule
        broker_ready = (instance / "broker.sock").exists()
        schedule_action = (
            actions.get(schedule.get("action_id"))
            if isinstance(schedule, Mapping)
            else None
        )
        steward_schedule_action = (
            actions.get(steward_schedule.get("action_id"))
            if isinstance(steward_schedule, Mapping)
            else None
        )
        retained_retarget_action = (
            actions.get(schedule.get("retarget_action_id"))
            if isinstance(schedule, Mapping) and schedule.get("retarget_action_id")
            else None
        )
        if (
            standing_ready
            and isinstance(schedule, Mapping)
            and schedule.get("automation_id")
            and (
                schedule.get("target_task_id") != standing["marshal"].get("task_id")
                or schedule.get("prompt") != MARSHAL_HEARTBEAT_PROMPT
                or schedule.get("rrule") != "FREQ=MINUTELY;INTERVAL=15"
                or schedule.get("status") != "ACTIVE"
            )
            and (
                not isinstance(retained_retarget_action, Mapping)
                or retained_retarget_action.get("state") in {"succeeded", "superseded"}
            )
        ):
            retarget_action_id = _opaque("action")
            actions[retarget_action_id] = {
                "action_id": retarget_action_id,
                "record_id": system.id,
                "executor": "bootstrap",
                "tool": "automation_update",
                "arguments": {
                    "mode": "update",
                    "id": schedule["automation_id"],
                    "kind": "heartbeat",
                    "name": "Fulcrum Marshal check",
                    "prompt": MARSHAL_HEARTBEAT_PROMPT,
                    "rrule": "FREQ=MINUTELY;INTERVAL=15",
                    "status": "ACTIVE",
                    "notificationPolicy": "failed_runs_only",
                    "targetThreadId": standing["marshal"].get("task_id"),
                    "destination": "thread",
                },
                "expected_result": {"automation_id": schedule["automation_id"]},
                "reporting": {"purpose": "marshal_schedule_retarget"},
                "state": "pending",
                "attempts": [],
                "created_at": _utc_now(),
                "purpose": "recover_marshal_schedule",
            }
            schedule = {
                **dict(schedule),
                "retarget_action_id": retarget_action_id,
                "retarget_state": "pending",
            }
            protocol["marshal_schedule"] = schedule
            protocol["actions"] = actions
        retarget_action = (
            actions.get(schedule.get("retarget_action_id"))
            if isinstance(schedule, Mapping) and schedule.get("retarget_action_id")
            else None
        )
        retained_steward_retarget_action = (
            actions.get(steward_schedule.get("retarget_action_id"))
            if isinstance(steward_schedule, Mapping)
            and steward_schedule.get("retarget_action_id")
            else None
        )
        if (
            standing_ready
            and isinstance(steward_schedule, Mapping)
            and steward_schedule.get("automation_id")
            and (
                steward_schedule.get("target_task_id")
                != standing["steward"].get("task_id")
                or steward_schedule.get("prompt") != STEWARD_HEARTBEAT_PROMPT
                or steward_schedule.get("rrule") != "FREQ=MINUTELY;INTERVAL=1"
                or steward_schedule.get("status") != "ACTIVE"
            )
            and (
                not isinstance(retained_steward_retarget_action, Mapping)
                or retained_steward_retarget_action.get("state")
                in {"succeeded", "superseded"}
            )
        ):
            retarget_action_id = _opaque("action")
            actions[retarget_action_id] = {
                "action_id": retarget_action_id,
                "record_id": system.id,
                "executor": "bootstrap",
                "tool": "automation_update",
                "arguments": {
                    "mode": "update",
                    "id": steward_schedule["automation_id"],
                    "kind": "heartbeat",
                    "name": "Fulcrum Steward loop",
                    "prompt": STEWARD_HEARTBEAT_PROMPT,
                    "rrule": "FREQ=MINUTELY;INTERVAL=1",
                    "status": "ACTIVE",
                    "notificationPolicy": "failed_runs_only",
                    "targetThreadId": standing["steward"].get("task_id"),
                    "destination": "thread",
                },
                "expected_result": {"automation_id": steward_schedule["automation_id"]},
                "reporting": {"purpose": "steward_schedule_retarget"},
                "state": "pending",
                "attempts": [],
                "created_at": _utc_now(),
                "purpose": "recover_steward_schedule",
            }
            steward_schedule = {
                **dict(steward_schedule),
                "retarget_action_id": retarget_action_id,
                "retarget_state": "pending",
            }
            protocol["steward_schedule"] = steward_schedule
            protocol["actions"] = actions
        steward_retarget_action = (
            actions.get(steward_schedule.get("retarget_action_id"))
            if isinstance(steward_schedule, Mapping)
            and steward_schedule.get("retarget_action_id")
            else None
        )
        if request.thread_id:
            for action_id, action in list(actions.items()):
                if (
                    isinstance(action, Mapping)
                    and action.get("executor") == "bootstrap"
                    and action.get("state") == "pending"
                ):
                    actions[action_id] = {
                        **dict(action),
                        "authorized_task_id": request.thread_id,
                    }
            protocol["actions"] = actions
        ready = (
            standing_ready
            and isinstance(schedule_action, Mapping)
            and schedule_action.get("state") == "succeeded"
            and schedule.get("status") == "ACTIVE"
            and isinstance(steward_schedule_action, Mapping)
            and steward_schedule_action.get("state") == "succeeded"
            and steward_schedule.get("status") == "ACTIVE"
            and isinstance(diagnostic_action, Mapping)
            and diagnostic_action.get("state") == "succeeded"
            and (
                retarget_action is None
                or (
                    isinstance(retarget_action, Mapping)
                    and retarget_action.get("state") == "succeeded"
                )
            )
            and (
                steward_retarget_action is None
                or (
                    isinstance(steward_retarget_action, Mapping)
                    and steward_retarget_action.get("state") == "succeeded"
                )
            )
            and broker_ready
            and service_gap is None
            and not missing_tools
            and not missing_models
            and not missing_acceptance
        )
        if ready:
            protocol["run_control"] = "running"
            setup["state"] = "ready"
            setup["ready_at"] = _utc_now()
        pending_actions = [
            self._action_response(request, action)
            for action in actions.values()
            if isinstance(action, Mapping) and action.get("state") == "pending"
        ]
        value = {
            "state": setup["state"],
            "admission": protocol.get("run_control", "paused"),
            "configuration": {
                "path": str(request.instance.config_path),
                "brain_root": str(request.instance.brain_root),
            },
            "standing": copy_mapping(standing),
            "schedule": copy_mapping(schedule),
            "steward_schedule": copy_mapping(steward_schedule),
            "diagnostic_escape_hatch": (
                {
                    "state": diagnostic_action.get("state"),
                    "action_id": diagnostic_action.get("action_id"),
                    "target_task_id": standing.get("marshal", {}).get("task_id"),
                }
                if isinstance(diagnostic_action, Mapping)
                else None
            ),
            "pending_actions": pending_actions,
            "prerequisites": {
                "native_tools": missing_tools,
                "models": missing_models,
                "broker": None if broker_ready else str(instance / "broker.sock"),
                "services": service_gap,
                "acceptance": missing_acceptance or None,
            },
            "mcp": mcp,
            "hooks": hook_status,
            "acceptance": acceptance_evidence,
        }
        _save_request(protocol, request, value, ledger=ledger)
        ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
        if ready:
            fence = instance / "maintenance-fence.json"
            try:
                retained = json.loads(fence.read_text(encoding="utf-8"))
            except FileNotFoundError:
                retained = None
            except (OSError, json.JSONDecodeError) as error:
                raise FulcrumError(
                    "RESET_FENCE_INVALID",
                    f"ready state was retained but the maintenance fence is unreadable: {error}",
                    exit_code=4,
                ) from error
            if (
                isinstance(retained, Mapping)
                and retained.get("state") == "bootstrap_required"
            ):
                fence.unlink()
        return _result(request, value)


def copy_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
