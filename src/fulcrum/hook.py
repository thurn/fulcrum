"""Read-only compaction context hook; never enforces stops or blocks tools."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from fulcrum.config import resolve_paths
from fulcrum.operative import journal_is_unfinished, read_journal
from fulcrum.prompts import compaction_reminder, operative_compaction_reminder
from fulcrum.store import Store, StoreError


def _context(prompt: str) -> dict[str, Any]:
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": prompt,
        },
    }


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
    try:
        journal = read_journal(paths.operative_journal)
    except StoreError:
        return _context(
            "An Operative takeover fence is present but its authoritative journal is "
            "unreadable. Ordinary Fulcrum authority is fenced. Do not finish, resume, "
            "publish, deliver, retry, or mutate workflow state; recover the journal "
            "protectively without assuming completion."
        )
    if journal_is_unfinished(journal):
        assert journal is not None
        if journal["native_thread_id"] == thread_id:
            return _context(operative_compaction_reminder(journal))
        return _context(
            "An Operative takeover is active. Ordinary Fulcrum authority is fenced: "
            "do not finish, resume, publish, deliver, retry, or mutate workflow state. "
            "Any late result is quarantined evidence for the bound Operative."
        )
    if not paths.database.is_file():
        return {"continue": True}
    managed = None
    try:
        with Store(paths.database, readonly=True) as store:
            takeover = store.unfinished_operative_takeover()
            managed = store.row(
                "SELECT id FROM tasks WHERE native_thread_id = ? AND state NOT IN ('retired', 'archived')",
                (thread_id,),
            )
            if managed is None:
                return {"continue": True}
            role = store.row("SELECT role FROM tasks WHERE id = ?", (managed["id"],))
            if (
                takeover is not None
                and role is not None
                and role["role"] != "operative"
            ):
                prompt = (
                    "An Operative takeover is active. Ordinary Fulcrum authority is "
                    "fenced: do not finish, resume, publish, deliver, retry, or mutate "
                    "workflow state. Any late result is quarantined evidence for the "
                    "bound Operative."
                )
                return _context(prompt)
            if (
                takeover is not None
                and role is not None
                and role["role"] == "operative"
            ):
                return _context(operative_compaction_reminder(takeover))
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
    return _context(prompt)


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
