"""SQLite state owned exclusively by the Fulcrum controller."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StoreError(RuntimeError):
    """A requested state transition violates durable workflow authority."""


ROLE_CODES = {
    "weaver": ("WVR", "🧵"),
    "executor": ("EXE", "⚒️"),
    "overseer": ("OVR", "🔎"),
    "sage": ("SAGE", "📖"),
    "inquisitor": ("INQ", "🛡️"),
}

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY, repo_path TEXT NOT NULL UNIQUE,
  codex_project_id TEXT, tollgate_repo_id TEXT,
  validation_command TEXT NOT NULL DEFAULT '[]', source_remote TEXT,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)), condition TEXT
);
CREATE TABLE IF NOT EXISTS role_counters (
  role TEXT PRIMARY KEY CHECK (role IN ('weaver','executor','overseer','sage','inquisitor')),
  next_number INTEGER NOT NULL CHECK (next_number >= 1)
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, native_thread_id TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK (role IN ('archon','weaver','executor','overseer','sage','inquisitor')),
  role_number INTEGER, title TEXT NOT NULL, description TEXT NOT NULL,
  project_id TEXT REFERENCES projects(project_id), model TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL, pair_id INTEGER,
  state TEXT NOT NULL DEFAULT 'idle' CHECK (state IN ('provisioning','idle','active','uncertain','retired','archived')),
  runtime_status TEXT, last_turn_terminal INTEGER NOT NULL DEFAULT 1 CHECK (last_turn_terminal IN (0, 1)),
  helpers_terminal INTEGER NOT NULL DEFAULT 1 CHECK (helpers_terminal IN (0, 1)),
  archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(role, role_number),
  CHECK ((role = 'archon' AND role_number IS NULL) OR (role != 'archon' AND role_number IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_archon ON tasks(role) WHERE role = 'archon' AND state NOT IN ('retired','archived');
CREATE TABLE IF NOT EXISTS beads (
  bead_id TEXT PRIMARY KEY, intake_key TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL REFERENCES projects(project_id), title TEXT NOT NULL,
  description TEXT NOT NULL, activation TEXT NOT NULL CHECK (activation IN ('pending','future')),
  executor_model TEXT NOT NULL, executor_reasoning_effort TEXT NOT NULL,
  overseer_model TEXT NOT NULL, overseer_reasoning_effort TEXT NOT NULL,
  model_provenance TEXT NOT NULL, plan_id TEXT, plan_commit TEXT,
  context_json TEXT NOT NULL DEFAULT '[]', publication_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (publication_state IN ('pending','complete','failed')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_groups (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('publishing','complete','failed')),
  condition TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_group_beads (
  group_id TEXT NOT NULL REFERENCES intake_groups(id) ON DELETE CASCADE,
  bead_id TEXT NOT NULL UNIQUE REFERENCES beads(bead_id),
  PRIMARY KEY (group_id, bead_id)
);
CREATE TABLE IF NOT EXISTS bead_dependencies (
  bead_id TEXT NOT NULL REFERENCES beads(bead_id) ON DELETE CASCADE,
  dependency_id TEXT NOT NULL, PRIMARY KEY (bead_id, dependency_id), CHECK (bead_id != dependency_id)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
  authority TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'approved'
    CHECK (state IN ('approved','active','held','completed','canceled')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_beads (
  run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  bead_id TEXT NOT NULL REFERENCES beads(bead_id), position INTEGER NOT NULL CHECK (position >= 0),
  scope_snapshot TEXT NOT NULL, PRIMARY KEY (run_id, bead_id), UNIQUE (run_id, position)
);
CREATE TABLE IF NOT EXISTS assignments (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  bead_id TEXT NOT NULL REFERENCES beads(bead_id), executor_task_id INTEGER REFERENCES tasks(id),
  overseer_task_id INTEGER REFERENCES tasks(id), stage TEXT NOT NULL
    CHECK (stage IN ('queued','preparing','implementing','review_pending','reviewing','correcting','delivering','recovering','completed','canceled')),
  prior_stage TEXT, scope_snapshot TEXT NOT NULL, worktree_path TEXT,
  candidate_id TEXT, source_oid TEXT, tested_oid TEXT, review_failures INTEGER NOT NULL DEFAULT 0,
  repair_permissions TEXT NOT NULL DEFAULT '[]', mandate_candidate_id TEXT,
  mandate_scope TEXT, predecessor_candidate_id TEXT, repair_category TEXT,
  repair_rationale TEXT, repair_evidence TEXT, condition TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_unfinished_assignment_per_bead ON assignments(bead_id) WHERE stage NOT IN ('completed','canceled');
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
  assignment_id INTEGER REFERENCES assignments(id), occurrence_id INTEGER,
  kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending','starting','active','terminal','processed','failed','uncertain','canceled')),
  native_turn_id TEXT, outcome_kind TEXT, outcome_payload TEXT,
  reminder_sent INTEGER NOT NULL DEFAULT 0 CHECK (reminder_sent IN (0, 1)),
  check_after TEXT, condition TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_action_per_thread ON actions(task_id) WHERE state NOT IN ('processed','canceled');
CREATE TABLE IF NOT EXISTS reservations (
  id INTEGER PRIMARY KEY, action_id INTEGER NOT NULL UNIQUE REFERENCES actions(id),
  pair_id INTEGER, global_slots INTEGER NOT NULL DEFAULT 1 CHECK (global_slots >= 0),
  project_ids TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL CHECK (state IN ('reserved','active','uncertain')),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pair_reservation ON reservations(pair_id) WHERE pair_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS holds (
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL, target TEXT, reason TEXT NOT NULL,
  urgent INTEGER NOT NULL DEFAULT 0 CHECK (urgent IN (0, 1)), release_condition TEXT NOT NULL,
  released_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_operations (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, input_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('intent','sent','complete','failed','uncertain','canceled')),
  result_json TEXT, native_id TEXT, reconciliation_used INTEGER NOT NULL DEFAULT 0 CHECK (reconciliation_used IN (0, 1)),
  condition TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS updates (
  id INTEGER PRIMARY KEY, recipient_task_id INTEGER NOT NULL REFERENCES tasks(id),
  identity TEXT NOT NULL, content TEXT NOT NULL, actionable INTEGER NOT NULL DEFAULT 1 CHECK (actionable IN (0, 1)),
  state TEXT NOT NULL DEFAULT 'retained' CHECK (state IN ('retained','batched','processed','canceled')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(recipient_task_id, identity)
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY, recipient_task_id INTEGER NOT NULL REFERENCES tasks(id),
  state TEXT NOT NULL CHECK (state IN ('frozen','sending','accepted','processed','uncertain','failed')),
  action_id INTEGER REFERENCES actions(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_updates (
  batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  update_id INTEGER NOT NULL UNIQUE REFERENCES updates(id), PRIMARY KEY (batch_id, update_id)
);
CREATE TABLE IF NOT EXISTS policies (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, scope TEXT, cadence_seconds INTEGER,
  anchor_at TEXT, next_due_at TEXT, config_json TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)), UNIQUE(kind, scope)
);
CREATE UNIQUE INDEX IF NOT EXISTS policy_identity ON policies(kind, COALESCE(scope, ''));
CREATE TABLE IF NOT EXISTS occurrences (
  id INTEGER PRIMARY KEY, policy_id INTEGER REFERENCES policies(id), kind TEXT NOT NULL,
  scope TEXT, authority TEXT NOT NULL, prompt TEXT,
  state TEXT NOT NULL CHECK (state IN ('queued','active','collecting','publishing','complete','failed','skipped')),
  deadline_at TEXT, report_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_unfinished_occurrence ON occurrences(policy_id) WHERE policy_id IS NOT NULL AND state NOT IN ('complete','skipped');
CREATE TABLE IF NOT EXISTS interviews (
  id INTEGER PRIMARY KEY, occurrence_id INTEGER NOT NULL REFERENCES occurrences(id),
  subject_task_id INTEGER NOT NULL REFERENCES tasks(id), request TEXT NOT NULL,
  prior_archived INTEGER NOT NULL CHECK (prior_archived IN (0, 1)),
  state TEXT NOT NULL CHECK (state IN ('queued','starting','active','answered','expired','failed')),
  answer_json TEXT, deadline_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(occurrence_id, subject_task_id)
);
CREATE TABLE IF NOT EXISTS obligations (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL, target TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','active','complete','failed','uncertain','canceled')),
  detail TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(kind, identity, target)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, entity_type TEXT, entity_id TEXT,
  message TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Store:
    """Transactional access to the controller's only operational store."""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        self.connection: sqlite3.Connection
        if readonly:
            self.connection = sqlite3.connect(
                f"file:{path}?mode=ro",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.connection = sqlite3.connect(
                path, isolation_level=None, check_same_thread=False
            )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if not readonly:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = FULL")
            self.connection.executescript(SCHEMA)
            for role in ROLE_CODES:
                self.connection.execute(
                    "INSERT OR IGNORE INTO role_counters(role, next_number) VALUES (?, 1)",
                    (role,),
                )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, parameters)

    def rows(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(sql, parameters)]

    def row(self, sql: str, parameters: Sequence[Any] = ()) -> dict[str, Any] | None:
        found = self.connection.execute(sql, parameters).fetchone()
        return dict(found) if found is not None else None

    def event(
        self,
        kind: str,
        message: str,
        *,
        entity_type: str | None = None,
        entity_id: str | int | None = None,
        detail: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> int:
        cursor = self.execute(
            "INSERT INTO events(kind, entity_type, entity_id, message, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                kind,
                entity_type,
                None if entity_id is None else str(entity_id),
                message,
                json.dumps(detail or {}, sort_keys=True),
                now or utc_now(),
            ),
        )
        return int(cursor.lastrowid)

    def allocate_name(self, role: str, description: str) -> tuple[int | None, str]:
        if role == "archon":
            return None, "👑 ARCHON 👑"
        if role not in ROLE_CODES:
            raise StoreError(f"unsupported managed role: {role}")
        summary = " ".join(description.split()).strip()
        if not summary:
            raise StoreError("task description is required")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT next_number FROM role_counters WHERE role = ?", (role,)
            ).fetchone()
            if row is None:
                raise StoreError(f"missing role counter for {role}")
            number = int(row[0])
            connection.execute(
                "UPDATE role_counters SET next_number = ? WHERE role = ?",
                (number + 1, role),
            )
        code, emoji = ROLE_CODES[role]
        return number, f"{emoji} [{code}{number:04d}] {summary}"

    def register_task(
        self,
        *,
        native_thread_id: str,
        role: str,
        description: str,
        model: str,
        reasoning_effort: str,
        project_id: str | None = None,
        state: str = "idle",
        pair_id: int | None = None,
        now: str | None = None,
        role_number: int | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        existing = self.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (native_thread_id,)
        )
        if existing is not None:
            expected = (role, project_id, model, reasoning_effort)
            actual = tuple(
                existing[key]
                for key in ("role", "project_id", "model", "reasoning_effort")
            )
            if actual != expected:
                raise StoreError("thread is already bound with different authority")
            return existing
        timestamp = now or utc_now()
        if title is None:
            number, canonical_title = self.allocate_name(role, description)
        else:
            number, canonical_title = role_number, title
        try:
            cursor = self.execute(
                """INSERT INTO tasks(native_thread_id, role, role_number, title, description, project_id, model, reasoning_effort, pair_id, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    native_thread_id,
                    role,
                    number,
                    canonical_title,
                    description,
                    project_id,
                    model,
                    reasoning_effort,
                    pair_id,
                    state,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise StoreError(f"task registration rejected: {error}") from error
        self.event(
            "task_registered",
            f"registered {canonical_title}",
            entity_type="task",
            entity_id=cursor.lastrowid,
            now=timestamp,
        )
        return self.row("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)) or {}

    def current_action(self, native_thread_id: str) -> dict[str, Any]:
        row = self.row(
            """SELECT a.*, t.native_thread_id, t.role, t.title FROM actions a JOIN tasks t ON t.id = a.task_id
               WHERE t.native_thread_id = ? AND a.state NOT IN ('processed','canceled')""",
            (native_thread_id,),
        )
        if row is None:
            raise StoreError("thread has no current Fulcrum action")
        return row

    def create_operation(self, kind: str, target: str, inputs: dict[str, Any]) -> int:
        timestamp = utc_now()
        cursor = self.execute(
            "INSERT INTO external_operations(kind, target, input_json, state, created_at, updated_at) VALUES (?, ?, ?, 'intent', ?, ?)",
            (kind, target, json.dumps(inputs, sort_keys=True), timestamp, timestamp),
        )
        return int(cursor.lastrowid)

    def status(self, *, event_limit: int = 20) -> dict[str, Any]:
        reservations = self.rows("SELECT * FROM reservations")
        tasks = self.rows("SELECT * FROM tasks WHERE state != 'retired' ORDER BY id")
        for task in tasks:
            task["desktop_context"] = f"codex://thread/{task['native_thread_id']}"
            current = self.row(
                """SELECT a.bead_id, a.stage FROM assignments a
                   WHERE (a.executor_task_id = ? OR a.overseer_task_id = ?)
                   AND a.stage NOT IN ('completed','canceled') ORDER BY a.id DESC LIMIT 1""",
                (task["id"], task["id"]),
            )
            task["current_bead"] = current["bead_id"] if current else None
            task["assignment_stage"] = current["stage"] if current else None
        assignments = self.rows(
            """SELECT a.*, b.title AS bead_title, r.project_id FROM assignments a
               JOIN beads b ON b.bead_id = a.bead_id JOIN runs r ON r.id = a.run_id
               WHERE a.stage NOT IN ('completed','canceled') ORDER BY a.id"""
        )
        return {
            "controller_state": self.row(
                "SELECT value FROM meta WHERE key = 'controller_state'"
            ),
            "dispatch_enabled": self.row(
                "SELECT value FROM meta WHERE key = 'dispatch_enabled'"
            ),
            "projects": self.rows("SELECT * FROM projects ORDER BY project_id"),
            "last_reconciliation": self.row(
                "SELECT value FROM meta WHERE key = 'last_reconciliation'"
            ),
            "tasks": tasks,
            "runs": self.rows("SELECT * FROM runs ORDER BY id"),
            "assignments": assignments,
            "unfinished_work_count": len(assignments),
            "reservations": reservations,
            "slot_usage": {
                "global": sum(int(item["global_slots"]) for item in reservations),
                "projects": _project_slot_usage(reservations),
            },
            "holds": self.rows(
                "SELECT * FROM holds WHERE released_at IS NULL ORDER BY id"
            ),
            "pending_updates": self.rows(
                "SELECT * FROM updates WHERE state IN ('retained','batched') ORDER BY id"
            ),
            "operations": self.rows(
                "SELECT * FROM external_operations WHERE state NOT IN ('complete','canceled') ORDER BY id"
            ),
            "obligations": self.rows(
                "SELECT * FROM obligations WHERE state NOT IN ('complete','canceled') ORDER BY id"
            ),
            "occurrences": self.rows(
                "SELECT * FROM occurrences WHERE state NOT IN ('complete','skipped') ORDER BY id"
            ),
            "interviews": self.rows(
                "SELECT * FROM interviews WHERE state NOT IN ('answered','expired') ORDER BY id"
            ),
            "policies": self.rows(
                "SELECT * FROM policies WHERE active = 1 ORDER BY id"
            ),
            "events": self.rows(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (event_limit,)
            ),
        }


def _project_slot_usage(reservations: list[dict[str, Any]]) -> dict[str, int]:
    usage: dict[str, int] = {}
    for reservation in reservations:
        for project in json.loads(reservation["project_ids"]):
            usage[project] = usage.get(project, 0) + 1
    return usage
