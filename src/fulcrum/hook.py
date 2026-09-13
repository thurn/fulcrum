"""Read-only compaction context hook; never enforces stops or blocks tools."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from fulcrum.config import resolve_paths
from fulcrum.prompts import compaction_reminder
from fulcrum.store import Store


def handle_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if (
        event.get("hook_event_name") != "SessionStart"
        or event.get("source") != "compact"
    ):
        return {"continue": True}
    thread_id = event.get("session_id")
    if not isinstance(thread_id, str) or not thread_id:
        return {"continue": True}
    paths = resolve_paths()
    if not paths.database.is_file():
        return {"continue": True}
    managed = None
    try:
        with Store(paths.database, readonly=True) as store:
            managed = store.row(
                "SELECT id FROM tasks WHERE native_thread_id = ? AND state NOT IN ('retired', 'archived')",
                (thread_id,),
            )
            if managed is None:
                return {"continue": True}
            action = store.row(
                "SELECT * FROM actions WHERE task_id = ? AND state IN ('pending','starting','active','terminal','uncertain')",
                (managed["id"],),
            )
            if action is None:
                return {"continue": True}
            task = store.row("SELECT * FROM tasks WHERE id = ?", (action["task_id"],))
            if task is None:
                return {"continue": True}
            prompt = compaction_reminder(task, action)
    except Exception as error:
        if managed is None:
            return {"continue": True}
        prompt = f"Fulcrum context is temporarily unavailable: {error}. Do not invent an outcome or operational identity."
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": prompt,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    response: dict[str, Any] = {"continue": True}
    try:
        value = json.load(sys.stdin)
        if isinstance(value, dict):
            response = handle_event(value)
    except Exception:
        pass
    json.dump(response, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
