"""Deterministic bootstrap for the stock Codex Desktop architecture."""

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
    fulcrum2_service_definitions,
    install_fulcrum2_service_definitions,
    master_source_root,
    reconcile_fulcrum2_skills,
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
    "schedule_overlap",
}
STANDING = {
    "steward": {
        "title": "🧰 STEWARD 🧰",
        "model": "gpt-5.6-luna",
        "prompt": (
            "Register as the existing Steward and recover any outstanding instruction/result. "
            "Call wait_for_instructions. Execute only its exact authorized native action: "
            "claim it, invoke it once, and report the actual result. Then wait again. Do not "
            "choose priorities, invent prompts, retry uncertain effects, or poll tasks. On an "
            "explicit stop, end. On an unrecoverable connection/protocol failure, attempt the "
            "permitted failure alert once and end rather than spin."
        ),
    },
    "marshal": {
        "title": "🧭 MARSHAL 🧭",
        "model": "gpt-5.6-sol",
        "prompt": (
            "Register as Marshal for this Fulcrum instance. On scheduled prompts call "
            "marshal_check, settle only the returned bounded curation or recovery scope, and "
            "end quietly when there is no action."
        ),
    },
    "vizier": {
        "title": "🔮 VIZIER 🔮",
        "model": "gpt-5.6-sol",
        "prompt": (
            "Register as Vizier for this Fulcrum instance. Present exact retained human "
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
    definitions = fulcrum2_service_definitions(
        instance_root=request.instance.instance_root,
        config_path=request.instance.config_path,
        brain_root=request.instance.brain_root,
        config=config,
        fulcrum_executable=executable,
        production=not request.instance.explicit_selection,
    )
    installed, changed = install_fulcrum2_service_definitions(
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
    path: Path, *, instance: Path, executable: Path
) -> dict[str, Any]:
    """Install one owned TOML block while preserving unrelated Codex settings."""

    begin = "# BEGIN FULCRUM STOCK DESKTOP"
    end = "# END FULCRUM STOCK DESKTOP"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if (begin in existing) != (end in existing):
        raise FulcrumError(
            "MCP_CONFIG_CONFLICT",
            "Fulcrum MCP configuration markers are incomplete",
            exit_code=4,
        )
    escaped_command = json.dumps(str(executable.resolve(strict=False)))
    escaped_instance = json.dumps(str(instance.resolve(strict=False)))
    block = (
        f"{begin}\n"
        "[mcp_servers.fulcrum]\n"
        f"command = {escaped_command}\n"
        f'args = ["--instance", {escaped_instance}]\n'
        "startup_timeout_sec = 10\n"
        "tool_timeout_sec = 3900\n"
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
        saved = _saved_request(protocol, request)
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
            codex_root / "config.toml", instance=instance, executable=executable
        )
        skills = reconcile_fulcrum2_skills(
            instance,
            production=not request.instance.explicit_selection,
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
                definitions = fulcrum2_service_definitions(
                    instance_root=instance,
                    config_path=request.instance.config_path,
                    brain_root=request.instance.brain_root,
                    config=manager.effective(document),
                    fulcrum_executable=source / ".venv" / "bin" / "fulcrum",
                    production=not request.instance.explicit_selection,
                )
                installed, changed = install_fulcrum2_service_definitions(
                    definitions, instance
                )
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
        model_support = request.input.get("model_support")
        missing_models: list[str] = []
        if isinstance(model_support, Mapping):
            for role, definition in STANDING.items():
                efforts = model_support.get(definition["model"])
                if (
                    not isinstance(efforts, list)
                    or request.input.get(f"{role}_thinking", "high") not in efforts
                ):
                    missing_models.append(str(definition["model"]))
        else:
            missing_models = sorted(
                {str(value["model"]) for value in STANDING.values()}
            )
        setup = dict(protocol.get("setup") or {})
        setup.update(
            {
                "state": "preparing",
                "mcp": mcp,
                "hooks": copy_mapping(skills.get("hook")),
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
        if isinstance(replacement, Mapping):
            role = str(replacement.get("role") or "")
            reason = replacement.get("reason")
            current = standing.get(role)
            if (
                role not in STANDING
                or not isinstance(reason, str)
                or not reason.strip()
                or replacement.get("confirmed_lost") is not True
                or not isinstance(current, Mapping)
                or replacement.get("old_task_id") != current.get("task_id")
            ):
                raise FulcrumError(
                    "REPLACEMENT_NOT_AUTHORIZED",
                    "standing replacement requires role, reason, confirmed_lost, and the exact old task ID",
                    exit_code=5,
                )
            existing_recovery = next(
                (
                    action
                    for action in actions.values()
                    if isinstance(action, Mapping)
                    and action.get("purpose") == f"recover_{role}"
                    and action.get("state") not in {"rejected", "superseded"}
                ),
                None,
            )
            if existing_recovery is None:
                definition = STANDING[role]
                action_id = _opaque("action")
                actions[action_id] = {
                    "action_id": action_id,
                    "record_id": system.id,
                    "executor": "bootstrap",
                    "tool": "create_thread",
                    "arguments": {
                        "prompt": definition["prompt"],
                        "title": definition["title"],
                        "model": definition["model"],
                        "thinking": request.input.get(f"{role}_thinking", "high"),
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
            actions[action_id] = {
                "action_id": action_id,
                "record_id": system.id,
                "executor": "bootstrap",
                "tool": "create_thread",
                "arguments": {
                    "prompt": definition["prompt"],
                    "title": definition["title"],
                    "model": definition["model"],
                    "thinking": request.input.get(f"{role}_thinking", "high"),
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
        schedule = protocol.get("marshal_schedule")
        if standing_ready and not isinstance(schedule, Mapping):
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
                    "prompt": "Call marshal_check, settle the bounded brief, and end quietly when no action is required.",
                    "rrule": "FREQ=MINUTELY;INTERVAL=15",
                    "status": "PAUSED",
                    "notificationPolicy": "failed_runs_only",
                    "targetThreadId": marshal.get("task_id"),
                    "destination": "thread",
                },
                "expected_result": {"automation_id": "bound Marshal heartbeat"},
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
            }
            protocol["marshal_schedule"] = schedule
        broker_ready = (instance / "broker.sock").exists()
        schedule_action = (
            actions.get(schedule.get("action_id"))
            if isinstance(schedule, Mapping)
            else None
        )
        acceptance = request.input.get("acceptance")
        acceptance_evidence = (
            {str(key): bool(value) for key, value in acceptance.items()}
            if isinstance(acceptance, Mapping)
            else {}
        )
        missing_acceptance = sorted(
            name for name in REQUIRED_ACCEPTANCE if not acceptance_evidence.get(name)
        )
        if (
            standing_ready
            and isinstance(schedule, Mapping)
            and schedule.get("automation_id")
            and schedule.get("target_task_id") != standing["marshal"].get("task_id")
            and not schedule.get("retarget_action_id")
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
                    "prompt": "Call marshal_check, settle the bounded brief, and end quietly when no action is required.",
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
        if (
            standing_ready
            and isinstance(schedule, Mapping)
            and schedule.get("state") == "succeeded"
            and schedule.get("automation_id")
            and not missing_acceptance
            and not schedule.get("activation_action_id")
        ):
            activation_action_id = _opaque("action")
            actions[activation_action_id] = {
                "action_id": activation_action_id,
                "record_id": system.id,
                "executor": "bootstrap",
                "tool": "automation_update",
                "arguments": {
                    "mode": "update",
                    "id": schedule["automation_id"],
                    "kind": "heartbeat",
                    "name": "Fulcrum Marshal check",
                    "prompt": "Call marshal_check, settle the bounded brief, and end quietly when no action is required.",
                    "rrule": "FREQ=MINUTELY;INTERVAL=15",
                    "status": "ACTIVE",
                    "notificationPolicy": "failed_runs_only",
                    "targetThreadId": standing["marshal"].get("task_id"),
                    "destination": "thread",
                },
                "expected_result": {
                    "automation_id": schedule["automation_id"],
                    "status": "ACTIVE",
                },
                "reporting": {"purpose": "marshal_schedule_activation"},
                "state": "pending",
                "attempts": [],
                "created_at": _utc_now(),
                "purpose": "bootstrap_marshal_schedule_activation",
            }
            schedule = {
                **dict(schedule),
                "activation_action_id": activation_action_id,
                "activation_state": "pending",
            }
            protocol["marshal_schedule"] = schedule
            protocol["actions"] = actions
        activation_action = (
            actions.get(schedule.get("activation_action_id"))
            if isinstance(schedule, Mapping)
            else None
        )
        retarget_action = (
            actions.get(schedule.get("retarget_action_id"))
            if isinstance(schedule, Mapping) and schedule.get("retarget_action_id")
            else None
        )
        ready = (
            standing_ready
            and isinstance(schedule_action, Mapping)
            and schedule_action.get("state") == "succeeded"
            and isinstance(activation_action, Mapping)
            and activation_action.get("state") == "succeeded"
            and (
                retarget_action is None
                or (
                    isinstance(retarget_action, Mapping)
                    and retarget_action.get("state") == "succeeded"
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
            "standing": copy_mapping(standing),
            "schedule": copy_mapping(schedule),
            "pending_actions": pending_actions,
            "prerequisites": {
                "native_tools": missing_tools,
                "models": missing_models,
                "broker": None if broker_ready else str(instance / "broker.sock"),
                "services": service_gap,
                "acceptance": missing_acceptance or None,
            },
            "mcp": mcp,
            "hooks": skills.get("hook"),
        }
        _save_request(protocol, request, value)
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
