"""Fast durable intake, shared by human CLI and managed Weaver actions."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from typing import Any

from fulcrum.beads import Beads, BeadsUncertainError, IntakeTask
from fulcrum.store import Store, StoreError, utc_now


def file_task(
    store: Store,
    beads: Beads,
    task: IntakeTask,
    *,
    weaver_task_id: int | None = None,
) -> dict[str, Any]:
    """Record external intent before native publication and retain retries."""

    task.validate()
    if weaver_task_id is not None:
        weaver = store.row(
            "SELECT role, lineage_number FROM tasks WHERE id = ?", (weaver_task_id,)
        )
        if (
            weaver is None
            or weaver["role"] != "weaver"
            or weaver["lineage_number"] is None
        ):
            raise StoreError("originating Weaver lineage is unavailable")
    existing = store.row("SELECT * FROM beads WHERE intake_key = ?", (task.intake_key,))
    if existing is not None:
        retained_lineage = store.row(
            "SELECT weaver_task_id FROM bead_lineages WHERE bead_id = ?",
            (existing["bead_id"],),
        )
        if weaver_task_id is not None and (
            retained_lineage is None
            or retained_lineage["weaver_task_id"] != weaver_task_id
        ):
            raise StoreError(
                "intake identity already belongs to another Weaver lineage"
            )
        if (
            existing["title"] != task.title
            or existing["description"] != task.description
        ):
            raise StoreError("intake identity already exists with different content")
        if existing["publication_state"] == "complete":
            timestamp = utc_now()
            store.execute(
                """UPDATE obligations SET state = 'complete', detail = NULL, updated_at = ?
                   WHERE kind = 'beads_publication' AND identity = ?
                   AND state NOT IN ('complete','canceled')""",
                (timestamp, task.intake_key),
            )
            store.execute(
                """UPDATE external_operations SET state = 'canceled',
                   condition = 'superseded by confirmed same-key publication', updated_at = ?
                   WHERE kind = 'beads_create' AND target = ?
                   AND state IN ('failed','uncertain')""",
                (timestamp, task.intake_key),
            )
            store.event(
                "intake_retry_reconciled",
                f"reconciled confirmed publication for {existing['bead_id']}",
                entity_type="bead",
                entity_id=existing["bead_id"],
            )
        return {
            "bead_id": existing["bead_id"],
            "publication_state": existing["publication_state"],
            "activation": existing["activation"],
            "reused": True,
        }
    operation = store.create_operation(
        "beads_create",
        task.intake_key,
        {**task.__dict__, "weaver_task_id": weaver_task_id},
    )
    attempt = store.begin_operation_attempt(operation)
    started = time.monotonic()
    try:
        bead_id = beads.create(task)
    except Exception as error:
        timestamp = utc_now()
        state = "uncertain" if isinstance(error, BeadsUncertainError) else "failed"
        store.finish_operation_attempt(
            operation,
            attempt,
            state=state,
            stdout=getattr(error, "stdout", None),
            stderr=getattr(error, "stderr", None),
            error=str(error),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        store.execute(
            """INSERT OR IGNORE INTO obligations(kind, identity, target, state, detail, created_at, updated_at)
               VALUES ('beads_publication', ?, ?, ?, ?, ?, ?)""",
            (task.intake_key, task.project, state, str(error), timestamp, timestamp),
        )
        raise
    _record_published_task(
        store,
        task,
        bead_id,
        operation,
        weaver_task_id=weaver_task_id,
        attempt=attempt,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
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
    store: Store,
    task: IntakeTask,
    bead_id: str,
    operation: int,
    *,
    weaver_task_id: int | None = None,
    attempt: int | None = None,
    duration_ms: int | None = None,
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
        if weaver_task_id is not None:
            weaver = connection.execute(
                "SELECT role, lineage_number FROM tasks WHERE id = ?",
                (weaver_task_id,),
            ).fetchone()
            if (
                weaver is None
                or weaver["role"] != "weaver"
                or weaver["lineage_number"] is None
            ):
                raise StoreError("originating Weaver lineage is unavailable")
            connection.execute(
                """INSERT INTO bead_lineages(bead_id, weaver_task_id, lineage_number)
                   VALUES (?, ?, ?)""",
                (bead_id, weaver_task_id, weaver["lineage_number"]),
            )
            connection.execute(
                """UPDATE obligations SET state = 'canceled',
                       detail = 'new lineage work arrived before archival', updated_at = ?
                   WHERE kind = 'archive' AND state IN ('pending','failed')
                     AND target IN (
                       SELECT native_thread_id FROM tasks
                       WHERE lineage_number = ?
                         AND role IN ('weaver','executor','overseer')
                     )""",
                (timestamp, weaver["lineage_number"]),
            )
            connection.execute(
                """UPDATE tasks SET archive_eligible_at = NULL,
                       archive_idle_turn_id = NULL, updated_at = ?
                   WHERE lineage_number = ?
                     AND role IN ('weaver','executor','overseer')
                     AND state NOT IN ('retired','archived')""",
                (timestamp, weaver["lineage_number"]),
            )
        for dependency in task.dependencies:
            connection.execute(
                "INSERT INTO bead_dependencies(bead_id, dependency_id) VALUES (?, ?)",
                (bead_id, dependency),
            )
        connection.execute(
            """UPDATE external_operations SET state = 'complete', native_id = ?,
               result_json = ?, condition = NULL, completed_at = ?, updated_at = ? WHERE id = ?""",
            (
                bead_id,
                json.dumps({"bead_id": bead_id}),
                timestamp,
                timestamp,
                operation,
            ),
        )
        if attempt is not None:
            connection.execute(
                """UPDATE operation_attempts SET state = 'complete', result_json = ?,
                   finished_at = ?, duration_ms = ? WHERE operation_id = ? AND attempt = ?""",
                (
                    json.dumps({"bead_id": bead_id}),
                    timestamp,
                    duration_ms,
                    operation,
                    attempt,
                ),
            )


def reconcile_beads_creation(
    store: Store, beads: Beads, operation: dict[str, Any]
) -> bool:
    inputs = json.loads(operation["input_json"])
    weaver_task_id = inputs.pop("weaver_task_id", None)
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
    _record_published_task(
        store,
        task,
        bead_id,
        int(operation["id"]),
        weaver_task_id=weaver_task_id,
    )
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


REPORT_FIELDS: frozenset[str] = frozenset(
    {
        "report_key",
        "project",
        "title",
        "problem",
        "observed_evidence",
        "required_change",
        "acceptance_checks",
        "dependencies",
        "context",
    }
)


def report_task_from_payload(
    payload: dict[str, Any],
    *,
    project: str,
    provenance: dict[str, Any],
) -> IntakeTask:
    """Validate and translate one implementation-ready follow-up report."""

    unexpected = sorted(set(payload) - REPORT_FIELDS)
    if unexpected:
        raise StoreError("unexpected report fields: " + ", ".join(unexpected))
    required_strings = (
        "report_key",
        "title",
        "problem",
        "observed_evidence",
        "required_change",
    )
    missing = [
        name
        for name in required_strings
        if not isinstance(payload.get(name), str) or not payload[name].strip()
    ]
    if missing:
        raise StoreError("missing required report fields: " + ", ".join(missing))

    def string_list(name: str, *, required: bool = False) -> tuple[str, ...]:
        raw = payload.get(name, [])
        if not isinstance(raw, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw
        ):
            raise StoreError(f"report field {name} must be a list of nonempty strings")
        if required and not raw:
            raise StoreError(f"report field {name} must not be empty")
        return tuple(item.strip() for item in raw)

    checks = string_list("acceptance_checks", required=True)
    dependencies = string_list("dependencies")
    context = string_list("context")
    description = "\n".join(
        (
            f"Problem: {payload['problem'].strip()}",
            f"Observed evidence: {payload['observed_evidence'].strip()}",
            f"Required change: {payload['required_change'].strip()}",
            "Acceptance checks:\n" + "\n".join(f"- {item}" for item in checks),
            *(
                ("Context:\n" + "\n".join(f"- {item}" for item in context),)
                if context
                else ()
            ),
        )
    )
    return IntakeTask(
        intake_key=f"report:{payload['report_key'].strip()}",
        project=project,
        title=payload["title"].strip(),
        description=description,
        activation="pending",
        dependencies=dependencies,
        context=context,
        report_provenance=provenance,
    )


def file_report(
    store: Store,
    beads: Beads,
    task: IntakeTask,
) -> dict[str, Any]:
    """Publish one report, accepting only byte-equivalent normalized retries."""

    existing = store.row("SELECT 1 FROM beads WHERE intake_key = ?", (task.intake_key,))
    if existing is not None:
        operation = store.row(
            """SELECT input_json FROM external_operations
               WHERE kind = 'beads_create' AND target = ? ORDER BY id LIMIT 1""",
            (task.intake_key,),
        )
        retained = json.loads(operation["input_json"]) if operation else None
        if isinstance(retained, dict):
            retained.pop("weaver_task_id", None)
        current = json.loads(json.dumps(task.__dict__))
        if retained != current:
            raise StoreError("report identity already exists with different content")
    return file_task(store, beads, task)


def file_graph(
    store: Store,
    beads: Beads,
    graph: dict[str, Any],
    *,
    group_id: str | None = None,
    weaver_task_id: int | None = None,
) -> dict[str, Any]:
    """Publish a validated graph while preventing partial dispatch."""

    raw_tasks = graph.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise StoreError("graph intake requires a nonempty tasks list")
    identity = group_id or str(graph.get("intake_key") or uuid.uuid4())
    existing = store.row("SELECT * FROM intake_groups WHERE id = ?", (identity,))
    if existing is not None and existing["state"] == "complete":
        rows = store.rows(
            """SELECT grouped.bead_id, lineage.weaver_task_id
               FROM intake_group_beads grouped
               LEFT JOIN bead_lineages lineage ON lineage.bead_id = grouped.bead_id
               WHERE grouped.group_id = ? ORDER BY grouped.bead_id""",
            (identity,),
        )
        if weaver_task_id is not None and any(
            row["weaver_task_id"] != weaver_task_id for row in rows
        ):
            raise StoreError("intake graph already belongs to another Weaver lineage")
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
            result = file_task(
                store,
                beads,
                replace(draft, dependencies=dependencies),
                weaver_task_id=weaver_task_id,
            )
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
