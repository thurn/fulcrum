"""Idempotent installation of Fulcrum's handlers in one Codex hook source."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any, cast

FULCRUM_STATUS_PREFIX = "Fulcrum:"


def _is_fulcrum_handler(handler: object) -> bool:
    return isinstance(handler, dict) and str(
        handler.get("statusMessage", "")
    ).startswith(FULCRUM_STATUS_PREFIX)


def _remove_fulcrum_handlers(groups: object) -> list[dict[str, Any]]:
    if not isinstance(groups, list):
        return []
    retained: list[dict[str, Any]] = []
    for value in groups:
        if not isinstance(value, dict):
            continue
        group = cast(dict[str, Any], dict(value))
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            retained.append(group)
            continue
        group["hooks"] = [item for item in handlers if not _is_fulcrum_handler(item)]
        if group["hooks"]:
            retained.append(group)
    return retained


def merged_hook_config(existing: object, hook_command: Path) -> dict[str, Any]:
    """Preserve unrelated configuration and replace only marked Fulcrum handlers."""

    if not isinstance(existing, dict):
        raise ValueError("Codex hook source must contain a JSON object")
    result = cast(dict[str, Any], dict(existing))
    raw_hooks = result.get("hooks", {})
    if not isinstance(raw_hooks, dict):
        raise ValueError("Codex hook source field 'hooks' must be an object")
    hooks = cast(dict[str, Any], dict(raw_hooks))
    session_groups = _remove_fulcrum_handlers(hooks.get("SessionStart", []))
    stop_groups = _remove_fulcrum_handlers(hooks.get("Stop", []))
    command = shlex.quote(str(hook_command.expanduser().absolute()))
    session_groups.append(
        {
            "matcher": "^compact$",
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 2,
                    "additionalContextLimit": 1000,
                    "statusMessage": "Fulcrum: restoring role context",
                }
            ],
        }
    )
    stop_groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 2,
                    "statusMessage": "Fulcrum: checking handoff",
                }
            ]
        }
    )
    hooks["SessionStart"] = session_groups
    hooks["Stop"] = stop_groups
    result["hooks"] = hooks
    return result


def install_hook_source(path: Path, hook_command: Path) -> dict[str, Any]:
    """Atomically merge one user-selected hook source and return its new content."""

    if path.exists():
        existing: object = json.loads(path.read_text(encoding="utf-8"))
    else:
        existing = {}
    result = merged_hook_config(existing, hook_command)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return result
