"""Deterministic bootstrap for the stock Codex Desktop architecture."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.coordination import coordinated
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
from fulcrum.install import reconcile_fulcrum2_skills

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
        system = self._system(ledger)
        protocol = _protocol(system.fc or {})
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        instance = request.instance.instance_root.resolve(strict=False)
        source = Path(
            os.environ.get("FULCRUM_SOURCE") or Path(__file__).resolve().parents[2]
        )
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
                "updated_at": _utc_now(),
            }
        )
        protocol["setup"] = setup
        actions = dict(protocol.get("actions") or {})
        standing = protocol.get("standing") or {}
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
        standing_ready = isinstance(standing, Mapping) and all(
            role in standing for role in STANDING
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
            }
            protocol["marshal_schedule"] = schedule
        broker_ready = (instance / "broker.sock").exists()
        schedule_action = (
            actions.get(schedule.get("action_id"))
            if isinstance(schedule, Mapping)
            else None
        )
        acceptance_passed = bool(request.input.get("acceptance_passed"))
        ready = (
            standing_ready
            and isinstance(schedule_action, Mapping)
            and schedule_action.get("state") == "succeeded"
            and broker_ready
            and not missing_tools
            and not missing_models
            and acceptance_passed
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
                "acceptance": (
                    None
                    if acceptance_passed
                    else "focused acceptance has not been recorded"
                ),
            },
            "mcp": mcp,
            "hooks": skills.get("hook"),
        }
        _save_request(protocol, request, value)
        ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
        return _result(request, value)


def copy_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
