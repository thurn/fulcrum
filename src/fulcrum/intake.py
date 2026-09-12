"""Fast durable intake, shared by human CLI and managed Weaver actions."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from typing import Any

from fulcrum.beads import Beads, BeadsUncertainError, IntakeTask
from fulcrum.store import Store, StoreError, utc_now


def file_task(store: Store, beads: Beads, task: IntakeTask) -> dict[str, Any]:
    """Record external intent before native publication and retain retries."""

    task.validate()
    existing = store.row("SELECT * FROM beads WHERE intake_key = ?", (task.intake_key,))
    if existing is not None:
        if (
            existing["title"] != task.title
            or existing["description"] != task.description
        ):
            raise StoreError("intake identity already exists with different content")
        return {
            "bead_id": existing["bead_id"],
            "publication_state": existing["publication_state"],
            "activation": existing["activation"],
            "reused": True,
        }
    operation = store.create_operation("beads_create", task.intake_key, task.__dict__)
    store.execute(
        "UPDATE external_operations SET state = 'sent', updated_at = ? WHERE id = ?",
        (utc_now(), operation),
    )
    try:
        bead_id = beads.create(task)
    except Exception as error:
        timestamp = utc_now()
        state = "uncertain" if isinstance(error, BeadsUncertainError) else "failed"
        store.execute(
            "UPDATE external_operations SET state = ?, condition = ?, updated_at = ? WHERE id = ?",
            (state, str(error), timestamp, operation),
        )
        store.execute(
            """INSERT OR IGNORE INTO obligations(kind, identity, target, state, detail, created_at, updated_at)
               VALUES ('beads_publication', ?, ?, ?, ?, ?, ?)""",
            (task.intake_key, task.project, state, str(error), timestamp, timestamp),
        )
        raise
    _record_published_task(store, task, bead_id, operation)
    store.event(
        "intake_filed",
        f"filed {bead_id}: {task.title}",
        entity_type="bead",
        entity_id=bead_id,
    )
    return {
        "bead_id": bead_id,
        "publication_state": "complete",
        "activation": task.activation,
        "reused": False,
    }


def _record_published_task(
    store: Store, task: IntakeTask, bead_id: str, operation: int
) -> None:
    timestamp = utc_now()
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO beads(bead_id, intake_key, project_id, title, description, activation,
               executor_model, executor_reasoning_effort, overseer_model, overseer_reasoning_effort,
               model_provenance, plan_id, plan_commit, context_json, publication_state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?)""",
            (
                bead_id,
                task.intake_key,
                task.project,
                task.title,
                task.description,
                task.activation,
                task.executor_model,
                task.executor_reasoning_effort,
                task.overseer_model,
                task.overseer_reasoning_effort,
                task.model_provenance,
                task.plan_id,
                task.plan_commit,
                json.dumps(task.context),
                timestamp,
                timestamp,
            ),
        )
        for dependency in task.dependencies:
            connection.execute(
                "INSERT INTO bead_dependencies(bead_id, dependency_id) VALUES (?, ?)",
                (bead_id, dependency),
            )
        connection.execute(
            "UPDATE external_operations SET state = 'complete', native_id = ?, result_json = ?, updated_at = ? WHERE id = ?",
            (bead_id, json.dumps({"bead_id": bead_id}), timestamp, operation),
        )


def reconcile_beads_creation(
    store: Store, beads: Beads, operation: dict[str, Any]
) -> bool:
    inputs = json.loads(operation["input_json"])
    task = IntakeTask(
        **{
            **inputs,
            "dependencies": tuple(inputs.get("dependencies", [])),
            "context": tuple(inputs.get("context", [])),
        }
    )
    matches = beads.find_intake(task.intake_key)
    if len(matches) != 1:
        return False
    bead_id = matches[0].get("id")
    if not isinstance(bead_id, str):
        return False
    _record_published_task(store, task, bead_id, int(operation["id"]))
    store.execute(
        "UPDATE obligations SET state = 'complete', detail = NULL, updated_at = ? WHERE kind = 'beads_publication' AND identity = ?",
        (utc_now(), task.intake_key),
    )
    return True


def task_from_payload(
    payload: dict[str, Any], *, intake_key: str | None = None
) -> IntakeTask:
    required = ("project", "title", "description")
    missing = [
        name
        for name in required
        if not isinstance(payload.get(name), str) or not payload[name].strip()
    ]
    if missing:
        raise StoreError("missing required intake fields: " + ", ".join(missing))
    explicit = any(
        key in payload
        for key in (
            "executor_model",
            "executor_reasoning_effort",
            "overseer_model",
            "overseer_reasoning_effort",
        )
    )
    return IntakeTask(
        intake_key=intake_key or str(payload.get("intake_key") or uuid.uuid4()),
        project=payload["project"],
        title=payload["title"],
        description=payload["description"],
        activation=payload.get("activation", "pending"),
        dependencies=tuple(payload.get("depends_on", [])),
        context=tuple(payload.get("context", [])),
        executor_model=payload.get("executor_model", "gpt-5.6-sol"),
        executor_reasoning_effort=payload.get("executor_reasoning_effort", "high"),
        overseer_model=payload.get("overseer_model", "gpt-5.6-sol"),
        overseer_reasoning_effort=payload.get("overseer_reasoning_effort", "high"),
        model_provenance="human" if explicit else "default",
        plan_id=payload.get("plan_id"),
        plan_commit=payload.get("plan_commit"),
    )


def file_graph(
    store: Store, beads: Beads, graph: dict[str, Any], *, group_id: str | None = None
) -> dict[str, Any]:
    """Publish a validated graph while preventing partial dispatch."""

    raw_tasks = graph.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise StoreError("graph intake requires a nonempty tasks list")
    identity = group_id or str(graph.get("intake_key") or uuid.uuid4())
    existing = store.row("SELECT * FROM intake_groups WHERE id = ?", (identity,))
    if existing is not None and existing["state"] == "complete":
        rows = store.rows(
            "SELECT bead_id FROM intake_group_beads WHERE group_id = ? ORDER BY bead_id",
            (identity,),
        )
        return {
            "intake_group": identity,
            "bead_ids": [row["bead_id"] for row in rows],
            "reused": True,
        }
    project_default = graph.get("project")
    drafts: list[IntakeTask] = []
    local_keys: set[str] = set()
    for position, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise StoreError("each graph task must be an object")
        item = dict(raw)
        item.setdefault("project", project_default)
        key = str(item.get("intake_key") or f"{identity}:{position}")
        if key in local_keys:
            raise StoreError(f"duplicate graph intake key {key}")
        local_keys.add(key)
        drafts.append(task_from_payload(item, intake_key=key))
    _validate_graph(drafts)
    timestamp = utc_now()
    store.execute(
        """INSERT INTO intake_groups(id, state, created_at, updated_at) VALUES (?, 'publishing', ?, ?)
           ON CONFLICT(id) DO UPDATE SET state = 'publishing', condition = NULL, updated_at = excluded.updated_at""",
        (identity, timestamp, timestamp),
    )
    created: list[str] = []
    try:
        remaining = list(drafts)
        local_ids: dict[str, str] = {}
        while remaining:
            ready = [
                draft
                for draft in remaining
                if all(
                    dependency not in local_keys or dependency in local_ids
                    for dependency in draft.dependencies
                )
            ]
            if not ready:
                raise StoreError("graph intake dependencies could not be resolved")
            draft = ready[0]
            dependencies = tuple(
                local_ids.get(item, item) for item in draft.dependencies
            )
            result = file_task(store, beads, replace(draft, dependencies=dependencies))
            bead_id = str(result["bead_id"])
            created.append(bead_id)
            local_ids[draft.intake_key] = bead_id
            store.execute(
                "INSERT OR IGNORE INTO intake_group_beads(group_id, bead_id) VALUES (?, ?)",
                (identity, bead_id),
            )
            remaining.remove(draft)
    except Exception as error:
        store.execute(
            "UPDATE intake_groups SET state = 'failed', condition = ?, updated_at = ? WHERE id = ?",
            (str(error), utc_now(), identity),
        )
        raise
    store.execute(
        "UPDATE intake_groups SET state = 'complete', updated_at = ? WHERE id = ?",
        (utc_now(), identity),
    )
    return {"intake_group": identity, "bead_ids": created, "reused": False}


def _validate_graph(tasks: list[IntakeTask]) -> None:
    keys = {task.intake_key for task in tasks}
    edges: dict[str, list[str]] = {
        task.intake_key: [item for item in task.dependencies if item in keys]
        for task in tasks
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise StoreError("graph intake contains a dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in edges[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in keys:
        visit(key)
