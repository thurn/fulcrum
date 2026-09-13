"""SQLite state owned exclusively by the Fulcrum controller."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final


class StoreError(RuntimeError):
    """A requested state transition violates durable workflow authority."""


ROLE_CODES = {
    "weaver": ("WVR", "🧵"),
    "executor": ("EXE", "⚒️"),
    "overseer": ("OVR", "🔎"),
    "sage": ("SAGE", "📖"),
    "inquisitor": ("INQ", "🛡️"),
}

_HELPER_OWNERSHIP_GAP = "helper turn has no unambiguous collaboration ownership"

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
  archive_eligible_at TEXT,
  archive_idle_turn_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(role, role_number),
  CHECK ((role = 'archon' AND role_number IS NULL) OR (role != 'archon' AND role_number IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_archon ON tasks(role) WHERE role = 'archon' AND state NOT IN ('retired','archived');
CREATE UNIQUE INDEX IF NOT EXISTS one_current_role_per_pair ON tasks(pair_id, role)
  WHERE pair_id IS NOT NULL AND role IN ('executor','overseer') AND state NOT IN ('retired','archived');
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
CREATE TABLE IF NOT EXISTS model_decisions (
  bead_id TEXT PRIMARY KEY REFERENCES beads(bead_id) ON DELETE CASCADE,
  rationale TEXT NOT NULL, created_at TEXT NOT NULL
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
  executor_task_id INTEGER REFERENCES tasks(id),
  overseer_task_id INTEGER REFERENCES tasks(id),
  priority INTEGER NOT NULL DEFAULT 0,
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
  repair_rationale TEXT, repair_evidence TEXT,
  completion_kind TEXT CHECK (completion_kind IN ('non_code')),
  completion_evidence TEXT, condition TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  next_attempt_at TEXT, operator_hold_id INTEGER REFERENCES holds(id),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_unfinished_assignment_per_bead ON assignments(bead_id) WHERE stage NOT IN ('completed','canceled');
CREATE TABLE IF NOT EXISTS handoffs (
  id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  source_action_id INTEGER NOT NULL UNIQUE REFERENCES actions(id),
  kind TEXT NOT NULL CHECK (kind IN ('implementation_evidence','review_findings','missing_evidence')),
  content_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
  assignment_id INTEGER REFERENCES assignments(id), occurrence_id INTEGER,
  kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending','starting','active','terminal','processed','failed','uncertain','canceled')),
  native_turn_id TEXT, outcome_kind TEXT, outcome_payload TEXT,
  reminder_sent INTEGER NOT NULL DEFAULT 0 CHECK (reminder_sent IN (0, 1)),
  check_after TEXT, condition TEXT, attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TEXT, operator_hold_id INTEGER REFERENCES holds(id),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_action_per_thread ON actions(task_id) WHERE state NOT IN ('processed','canceled');
CREATE UNIQUE INDEX IF NOT EXISTS one_current_action_per_assignment ON actions(assignment_id)
  WHERE assignment_id IS NOT NULL AND state IN ('pending','starting','active','terminal','uncertain');
CREATE TABLE IF NOT EXISTS action_turn_usage (
  native_thread_id TEXT NOT NULL, native_turn_id TEXT NOT NULL,
  action_id INTEGER REFERENCES actions(id),
  attributed_action_id INTEGER REFERENCES actions(id),
  is_helper INTEGER NOT NULL DEFAULT 0 CHECK (is_helper IN (0, 1)),
  model TEXT, reasoning_effort TEXT,
  last_input_tokens INTEGER, last_cached_input_tokens INTEGER,
  last_cache_write_input_tokens INTEGER, last_output_tokens INTEGER,
  last_reasoning_output_tokens INTEGER, last_total_tokens INTEGER,
  total_input_tokens INTEGER, total_cached_input_tokens INTEGER,
  total_cache_write_input_tokens INTEGER, total_output_tokens INTEGER,
  total_reasoning_output_tokens INTEGER, total_tokens INTEGER,
  model_context_window INTEGER,
  coverage TEXT NOT NULL DEFAULT 'unknown'
    CHECK (coverage IN ('unknown','observed','complete','partial','unavailable')),
  gap_reason TEXT, first_observed_at TEXT, last_observed_at TEXT,
  terminal_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (native_thread_id, native_turn_id)
);
CREATE INDEX IF NOT EXISTS usage_by_action ON action_turn_usage(action_id);
CREATE INDEX IF NOT EXISTS usage_by_attributed_action ON action_turn_usage(attributed_action_id);
CREATE TABLE IF NOT EXISTS telemetry_helper_threads (
  id INTEGER PRIMARY KEY, native_thread_id TEXT NOT NULL,
  parent_thread_id TEXT NOT NULL, parent_turn_id TEXT NOT NULL DEFAULT '',
  collaboration_item_id TEXT NOT NULL DEFAULT '', native_turn_id TEXT,
  attributed_action_id INTEGER REFERENCES actions(id),
  first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL,
  UNIQUE(native_thread_id, parent_thread_id, parent_turn_id, collaboration_item_id)
);
CREATE TABLE IF NOT EXISTS reservations (
  id INTEGER PRIMARY KEY, action_id INTEGER NOT NULL UNIQUE REFERENCES actions(id),
  pair_id INTEGER, global_slots INTEGER NOT NULL DEFAULT 1 CHECK (global_slots >= 0),
  project_ids TEXT NOT NULL DEFAULT '[]', conflict_keys TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL CHECK (state IN ('reserved','active','uncertain')),
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
  condition TEXT, correlation_id TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TEXT, last_attempt_at TEXT, completed_at TEXT,
  operator_hold_id INTEGER REFERENCES holds(id),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operation_attempts (
  id INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL REFERENCES external_operations(id) ON DELETE CASCADE,
  attempt INTEGER NOT NULL CHECK (attempt >= 1), state TEXT NOT NULL
    CHECK (state IN ('started','complete','failed','uncertain')),
  request_json TEXT NOT NULL, result_json TEXT, stdout TEXT, stderr TEXT, error TEXT,
  started_at TEXT NOT NULL, finished_at TEXT, duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  UNIQUE(operation_id, attempt)
);
CREATE TABLE IF NOT EXISTS state_transitions (
  id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  field_name TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, reason TEXT NOT NULL,
  correlation_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
  worker_name TEXT PRIMARY KEY, state TEXT NOT NULL CHECK (state IN ('starting','running','degraded','stopped')),
  started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, completed_at TEXT,
  failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
  last_error TEXT, traceback TEXT
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
CREATE TABLE IF NOT EXISTS deferred_batches (
  batch_id INTEGER PRIMARY KEY REFERENCES batches(id) ON DELETE CASCADE,
  reactivation_json TEXT NOT NULL, next_check_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finding_publications (
  semantic_key TEXT PRIMARY KEY, occurrence_id INTEGER NOT NULL REFERENCES occurrences(id),
  bead_id TEXT NOT NULL, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('complete','uncertain')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_updates (
  batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
  update_id INTEGER NOT NULL REFERENCES updates(id), PRIMARY KEY (batch_id, update_id)
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
  deadline_at TEXT, report_json TEXT, evidence_json TEXT NOT NULL DEFAULT '{}',
  publication_revision TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
  detail TEXT, retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  next_attempt_at TEXT, operator_hold_id INTEGER REFERENCES holds(id),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(kind, identity, target)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, entity_type TEXT, entity_id TEXT,
  message TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
"""

INVARIANT_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS run_executor_must_match_pair
BEFORE UPDATE OF executor_task_id ON runs
WHEN NEW.executor_task_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM tasks WHERE id = NEW.executor_task_id
  AND role = 'executor' AND pair_id = NEW.id
)
BEGIN
  SELECT RAISE(ABORT, 'run executor must be its paired Executor');
END;
CREATE TRIGGER IF NOT EXISTS inserted_run_executor_must_match_pair
BEFORE INSERT ON runs
WHEN NEW.executor_task_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM tasks WHERE id = NEW.executor_task_id
  AND role = 'executor' AND pair_id = NEW.id
)
BEGIN
  SELECT RAISE(ABORT, 'run executor must be its paired Executor');
END;
CREATE TRIGGER IF NOT EXISTS run_overseer_must_match_pair
BEFORE UPDATE OF overseer_task_id ON runs
WHEN NEW.overseer_task_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM tasks WHERE id = NEW.overseer_task_id
  AND role = 'overseer' AND pair_id = NEW.id
)
BEGIN
  SELECT RAISE(ABORT, 'run overseer must be its paired Overseer');
END;
CREATE TRIGGER IF NOT EXISTS inserted_run_overseer_must_match_pair
BEFORE INSERT ON runs
WHEN NEW.overseer_task_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM tasks WHERE id = NEW.overseer_task_id
  AND role = 'overseer' AND pair_id = NEW.id
)
BEGIN
  SELECT RAISE(ABORT, 'run overseer must be its paired Overseer');
END;
CREATE TRIGGER IF NOT EXISTS assignment_tasks_must_match_run
BEFORE UPDATE OF executor_task_id, overseer_task_id ON assignments
WHEN (NEW.executor_task_id IS NOT NULL AND NEW.executor_task_id IS NOT
      (SELECT executor_task_id FROM runs WHERE id = NEW.run_id))
  OR (NEW.overseer_task_id IS NOT NULL AND NEW.overseer_task_id IS NOT
      (SELECT overseer_task_id FROM runs WHERE id = NEW.run_id))
BEGIN
  SELECT RAISE(ABORT, 'assignment tasks must match the retained run pair');
END;
CREATE TRIGGER IF NOT EXISTS inserted_assignment_tasks_must_match_run
BEFORE INSERT ON assignments
WHEN (NEW.executor_task_id IS NOT NULL AND NEW.executor_task_id IS NOT
      (SELECT executor_task_id FROM runs WHERE id = NEW.run_id))
  OR (NEW.overseer_task_id IS NOT NULL AND NEW.overseer_task_id IS NOT
      (SELECT overseer_task_id FROM runs WHERE id = NEW.run_id))
BEGIN
  SELECT RAISE(ABORT, 'assignment tasks must match the retained run pair');
END;
CREATE TRIGGER IF NOT EXISTS retained_task_cannot_leave_run_pair
BEFORE UPDATE OF pair_id, role ON tasks
WHEN EXISTS (
  SELECT 1 FROM runs WHERE
  (executor_task_id = NEW.id AND (NEW.role != 'executor' OR NEW.pair_id != id))
  OR (overseer_task_id = NEW.id AND (NEW.role != 'overseer' OR NEW.pair_id != id))
)
BEGIN
  SELECT RAISE(ABORT, 'retained task cannot leave its run pair');
END;
CREATE TRIGGER IF NOT EXISTS handoff_must_match_terminal_source
BEFORE INSERT ON handoffs
WHEN NOT EXISTS (
  SELECT 1 FROM actions WHERE id = NEW.source_action_id
  AND assignment_id = NEW.assignment_id
  AND ((NEW.kind = 'implementation_evidence' AND kind IN ('implement','correct') AND outcome_kind = 'ready_for_review')
    OR (NEW.kind = 'review_findings' AND kind = 'review' AND outcome_kind = 'changes_requested')
    OR (NEW.kind = 'missing_evidence' AND kind = 'review' AND outcome_kind = 'incomplete'))
)
BEGIN
  SELECT RAISE(ABORT, 'handoff must match its accepted source outcome');
END;
CREATE TRIGGER IF NOT EXISTS review_action_requires_candidate_handoff
BEFORE INSERT ON actions
WHEN NEW.kind = 'review' AND NEW.assignment_id IS NOT NULL AND (
  NOT EXISTS (
    SELECT 1 FROM assignments WHERE id = NEW.assignment_id
    AND candidate_id IS NOT NULL AND source_oid IS NOT NULL
  ) OR NOT EXISTS (
    SELECT 1 FROM handoffs WHERE assignment_id = NEW.assignment_id
    AND kind = 'implementation_evidence'
  )
)
BEGIN
  SELECT RAISE(ABORT, 'review action requires an immutable candidate and implementation handoff');
END;
CREATE TRIGGER IF NOT EXISTS reservation_requires_runnable_action
BEFORE INSERT ON reservations
WHEN NOT EXISTS (
  SELECT 1 FROM actions WHERE id = NEW.action_id
  AND state IN ('pending','starting','active','terminal','uncertain')
)
BEGIN
  SELECT RAISE(ABORT, 'reservation requires a runnable action');
END;
CREATE TRIGGER IF NOT EXISTS reservation_respects_global_capacity
BEFORE INSERT ON reservations
WHEN (
  SELECT COALESCE(SUM(global_slots), 0) + NEW.global_slots FROM reservations
) > COALESCE((SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'global_limit'), 0)
BEGIN
  SELECT RAISE(ABORT, 'reservation exceeds global capacity');
END;
CREATE TRIGGER IF NOT EXISTS reservation_respects_project_capacity
BEFORE INSERT ON reservations
WHEN EXISTS (
  SELECT 1 FROM json_each(NEW.project_ids) requested
  LEFT JOIN json_each(COALESCE((SELECT value FROM meta WHERE key = 'project_limits'), '{}')) limits
    ON limits.key = requested.value
  WHERE limits.value IS NULL OR CAST(limits.value AS INTEGER) <= (
    SELECT COUNT(*) FROM reservations existing
    WHERE EXISTS (
      SELECT 1 FROM json_each(existing.project_ids) used
      WHERE used.value = requested.value
    )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'reservation exceeds project capacity');
END;
CREATE TRIGGER IF NOT EXISTS reservation_respects_conflict_keys
BEFORE INSERT ON reservations
WHEN EXISTS (
  SELECT 1 FROM json_each(NEW.conflict_keys) requested
  JOIN reservations existing
  JOIN json_each(existing.conflict_keys) active ON active.value = requested.value
)
BEGIN
  SELECT RAISE(ABORT, 'reservation conflicts with an active resource lease');
END;
CREATE TRIGGER IF NOT EXISTS terminal_action_releases_reservation
AFTER UPDATE OF state ON actions
WHEN NEW.state IN ('processed','failed','canceled')
BEGIN
  DELETE FROM reservations WHERE action_id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS recovering_assignment_requires_progress
BEFORE UPDATE ON assignments
WHEN NEW.stage = 'recovering'
 AND NEW.next_attempt_at IS NULL
 AND NEW.operator_hold_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'recovering assignment requires retry deadline or operator hold');
END;
CREATE TRIGGER IF NOT EXISTS inserted_recovery_requires_progress
BEFORE INSERT ON assignments
WHEN NEW.stage = 'recovering'
 AND NEW.next_attempt_at IS NULL
 AND NEW.operator_hold_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'recovering assignment requires retry deadline or operator hold');
END;
CREATE TRIGGER IF NOT EXISTS audit_task_state
AFTER UPDATE OF state ON tasks WHEN OLD.state IS NOT NEW.state
BEGIN
  INSERT INTO state_transitions(entity_type, entity_id, field_name, from_state, to_state, reason, created_at)
  VALUES ('task', NEW.id, 'state', OLD.state, NEW.state, 'controller transition', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS audit_action_state
AFTER UPDATE OF state ON actions WHEN OLD.state IS NOT NEW.state
BEGIN
  INSERT INTO state_transitions(entity_type, entity_id, field_name, from_state, to_state, reason, created_at)
  VALUES ('action', NEW.id, 'state', OLD.state, NEW.state, COALESCE(NEW.condition, 'controller transition'), NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS audit_assignment_stage
AFTER UPDATE OF stage ON assignments WHEN OLD.stage IS NOT NEW.stage
BEGIN
  INSERT INTO state_transitions(entity_type, entity_id, field_name, from_state, to_state, reason, created_at)
  VALUES ('assignment', NEW.id, 'stage', OLD.stage, NEW.stage, COALESCE(NEW.condition, 'controller transition'), NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS audit_operation_state
AFTER UPDATE OF state ON external_operations WHEN OLD.state IS NOT NEW.state
BEGIN
  INSERT INTO state_transitions(entity_type, entity_id, field_name, from_state, to_state, reason, correlation_id, created_at)
  VALUES ('operation', NEW.id, 'state', OLD.state, NEW.state, COALESCE(NEW.condition, 'controller transition'), NEW.correlation_id, NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS audit_occurrence_state
AFTER UPDATE OF state ON occurrences WHEN OLD.state IS NOT NEW.state
BEGIN
  INSERT INTO state_transitions(entity_type, entity_id, field_name, from_state, to_state, reason, created_at)
  VALUES ('occurrence', NEW.id, 'state', OLD.state, NEW.state, 'controller transition', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS audit_interview_state
AFTER UPDATE OF state ON interviews WHEN OLD.state IS NOT NEW.state
BEGIN
  INSERT INTO state_transitions(entity_type, entity_id, field_name, from_state, to_state, reason, created_at)
  VALUES ('interview', NEW.id, 'state', OLD.state, NEW.state, 'controller transition', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS audit_obligation_state
AFTER UPDATE OF state ON obligations WHEN OLD.state IS NOT NEW.state
BEGIN
  INSERT INTO state_transitions(entity_type, entity_id, field_name, from_state, to_state, reason, created_at)
  VALUES ('obligation', NEW.id, 'state', OLD.state, NEW.state, COALESCE(NEW.detail, 'controller transition'), NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS audit_batch_state
AFTER UPDATE OF state ON batches WHEN OLD.state IS NOT NEW.state
BEGIN
  INSERT INTO state_transitions(entity_type, entity_id, field_name, from_state, to_state, reason, created_at)
  VALUES ('batch', NEW.id, 'state', OLD.state, NEW.state, 'controller transition', NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS audit_run_state
AFTER UPDATE OF state ON runs WHEN OLD.state IS NOT NEW.state
BEGIN
  INSERT INTO state_transitions(entity_type, entity_id, field_name, from_state, to_state, reason, created_at)
  VALUES ('run', NEW.id, 'state', OLD.state, NEW.state, 'controller transition', NEW.updated_at);
END;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Store:
    """Transactional access to the controller's only operational store."""

    def __init__(
        self,
        path: Path,
        *,
        readonly: bool = False,
        event_log: Path | None = None,
    ) -> None:
        self.path = path
        self.event_log = event_log
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
            self._migrate_existing_database()
            self._replace_invariant_triggers()
            self.connection.executescript(INVARIANT_TRIGGERS)
            for role in ROLE_CODES:
                self.connection.execute(
                    "INSERT OR IGNORE INTO role_counters(role, next_number) VALUES (?, 1)",
                    (role,),
                )

    def _replace_invariant_triggers(self) -> None:
        """Replace retained trigger definitions when controller invariants change."""

        triggers = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        for trigger in triggers:
            name = str(trigger[0]).replace('"', '""')
            self.connection.execute(f'DROP TRIGGER "{name}"')

    def _migrate_existing_database(self) -> None:
        """Add correctness metadata to an existing database without a version gate."""

        helper_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(telemetry_helper_threads)"
            )
        }
        required_helper_columns = {"id", "native_turn_id", "collaboration_item_id"}
        if helper_columns and not required_helper_columns.issubset(helper_columns):
            native_turn = (
                "native_turn_id" if "native_turn_id" in helper_columns else "NULL"
            )
            collaboration_item = (
                "collaboration_item_id"
                if "collaboration_item_id" in helper_columns
                else "''"
            )
            self.connection.executescript(f"""ALTER TABLE telemetry_helper_threads
                     RENAME TO obsolete_telemetry_helper_threads;
                CREATE TABLE telemetry_helper_threads (
                  id INTEGER PRIMARY KEY, native_thread_id TEXT NOT NULL,
                  parent_thread_id TEXT NOT NULL,
                  parent_turn_id TEXT NOT NULL DEFAULT '',
                  collaboration_item_id TEXT NOT NULL DEFAULT '',
                  native_turn_id TEXT,
                  attributed_action_id INTEGER REFERENCES actions(id),
                  first_observed_at TEXT NOT NULL,
                  last_observed_at TEXT NOT NULL,
                  UNIQUE(native_thread_id, parent_thread_id, parent_turn_id,
                         collaboration_item_id)
                );
                INSERT INTO telemetry_helper_threads(
                  native_thread_id, parent_thread_id, parent_turn_id,
                  collaboration_item_id, native_turn_id,
                  attributed_action_id, first_observed_at, last_observed_at
                ) SELECT native_thread_id, parent_thread_id,
                         COALESCE(parent_turn_id, ''), {collaboration_item},
                         {native_turn}, attributed_action_id,
                         first_observed_at, last_observed_at
                    FROM obsolete_telemetry_helper_threads;
                DROP TABLE obsolete_telemetry_helper_threads;""")
        self.connection.execute("""CREATE INDEX IF NOT EXISTS helper_ownership_by_thread
               ON telemetry_helper_threads(native_thread_id, id)""")
        self.connection.execute("""CREATE INDEX IF NOT EXISTS helper_ownership_by_action
               ON telemetry_helper_threads(attributed_action_id)""")
        self.connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS helper_ownership_by_native_turn
               ON telemetry_helper_threads(native_thread_id, native_turn_id)
               WHERE native_turn_id IS NOT NULL"""
        )

        batch_table = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'batch_updates'"
        ).fetchone()
        if batch_table is not None and "NOT NULL UNIQUE" in str(batch_table[0]).upper():
            self.connection.executescript(
                """ALTER TABLE batch_updates RENAME TO obsolete_batch_updates;
                CREATE TABLE batch_updates (
                  batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                  update_id INTEGER NOT NULL REFERENCES updates(id),
                  PRIMARY KEY (batch_id, update_id)
                );
                INSERT INTO batch_updates(batch_id, update_id)
                  SELECT batch_id, update_id FROM obsolete_batch_updates;
                DROP TABLE obsolete_batch_updates;"""
            )

        additions = {
            "tasks": {
                "archive_eligible_at": "TEXT",
                "archive_idle_turn_id": "TEXT",
            },
            "runs": {
                "executor_task_id": "INTEGER REFERENCES tasks(id)",
                "overseer_task_id": "INTEGER REFERENCES tasks(id)",
                "priority": "INTEGER NOT NULL DEFAULT 0",
            },
            "assignments": {
                "retry_count": "INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0)",
                "next_attempt_at": "TEXT",
                "operator_hold_id": "INTEGER REFERENCES holds(id)",
                "completion_kind": "TEXT CHECK (completion_kind IN ('non_code'))",
                "completion_evidence": "TEXT",
            },
            "actions": {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)",
                "next_attempt_at": "TEXT",
                "operator_hold_id": "INTEGER REFERENCES holds(id)",
            },
            "reservations": {
                "conflict_keys": "TEXT NOT NULL DEFAULT '[]'",
            },
            "external_operations": {
                "correlation_id": "TEXT",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)",
                "next_attempt_at": "TEXT",
                "last_attempt_at": "TEXT",
                "completed_at": "TEXT",
                "operator_hold_id": "INTEGER REFERENCES holds(id)",
            },
            "obligations": {
                "retry_count": "INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0)",
                "next_attempt_at": "TEXT",
                "operator_hold_id": "INTEGER REFERENCES holds(id)",
            },
            "occurrences": {
                "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
                "publication_revision": "TEXT",
            },
        }
        for table, columns in additions.items():
            present = {
                str(row[1])
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            for name, definition in columns.items():
                if name not in present:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )
        self.connection.execute(
            "UPDATE external_operations SET correlation_id = 'operation-' || id WHERE correlation_id IS NULL"
        )
        timestamp = utc_now()
        self.connection.execute(
            """UPDATE external_operations
               SET state = 'failed',
                   condition = 'retained Tollgate response proves the operation failed',
                   updated_at = ?
               WHERE kind = 'tollgate_candidate_create' AND state = 'uncertain'
                 AND EXISTS (
                   SELECT 1 FROM operation_attempts attempt
                   WHERE attempt.operation_id = external_operations.id
                     AND (attempt.stderr LIKE '%\"retryable\":false%'
                          OR attempt.error LIKE '%\"retryable\":false%')
                 )""",
            (timestamp,),
        )
        self.connection.execute(
            """UPDATE external_operations
               SET state = 'failed',
                   condition = 'superseded duplicate candidate operation',
                   updated_at = ?
               WHERE kind = 'tollgate_candidate_create'
                 AND state IN ('intent','sent','uncertain')
                 AND id NOT IN (
                   SELECT MAX(id) FROM external_operations
                   WHERE kind = 'tollgate_candidate_create'
                     AND state IN ('intent','sent','uncertain')
                   GROUP BY kind, target
                 )""",
            (timestamp,),
        )
        self.connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS one_current_tollgate_candidate_operation
               ON external_operations(kind, target)
               WHERE kind = 'tollgate_candidate_create'
                 AND state IN ('intent','sent','uncertain')"""
        )
        assignment_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(assignments)")
        }
        pair_columns = {"run_id", "executor_task_id", "overseer_task_id"}
        if pair_columns.issubset(assignment_columns):
            self.connection.execute("""UPDATE tasks SET pair_id = (
                     SELECT a.run_id FROM assignments a
                     WHERE a.executor_task_id = tasks.id OR a.overseer_task_id = tasks.id
                     ORDER BY a.id LIMIT 1
                   ) WHERE role IN ('executor','overseer') AND pair_id IS NULL
                   AND EXISTS (
                     SELECT 1 FROM assignments a
                     WHERE a.executor_task_id = tasks.id OR a.overseer_task_id = tasks.id
                   )""")
            self.connection.execute("""UPDATE runs SET
                     executor_task_id = COALESCE(executor_task_id, (
                       SELECT executor_task_id FROM assignments a
                       WHERE a.run_id = runs.id AND executor_task_id IS NOT NULL
                       ORDER BY a.id LIMIT 1)),
                     overseer_task_id = COALESCE(overseer_task_id, (
                       SELECT overseer_task_id FROM assignments a
                       WHERE a.run_id = runs.id AND overseer_task_id IS NOT NULL
                       ORDER BY a.id LIMIT 1))""")
        for assignment in self.rows(
            """SELECT id FROM assignments WHERE stage = 'recovering'
               AND next_attempt_at IS NULL AND operator_hold_id IS NULL"""
        ):
            timestamp = utc_now()
            hold = self.connection.execute(
                """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                   VALUES ('assignment', ?, 'legacy recovery had no runnable next step', 1,
                   'operator chooses an exact recovery transition', ?)""",
                (str(assignment["id"]), timestamp),
            )
            self.connection.execute(
                "UPDATE assignments SET operator_hold_id = ? WHERE id = ?",
                (hold.lastrowid, assignment["id"]),
            )
        self.connection.execute("""DELETE FROM reservations WHERE action_id IN (
                 SELECT id FROM actions WHERE state IN ('processed','failed','canceled')
               )""")
        self.connection.execute("DROP INDEX IF EXISTS one_current_action_per_thread")
        self.connection.execute(
            """CREATE UNIQUE INDEX one_current_action_per_thread ON actions(task_id)
               WHERE state IN ('pending','starting','active','terminal','uncertain')"""
        )
        if "run_id" in assignment_columns:
            self.connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS one_started_assignment_per_run
                   ON assignments(run_id)
                   WHERE stage NOT IN ('queued','completed','canceled')"""
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
        timestamp = now or utc_now()
        safe_detail = _redact(detail or {})
        encoded_detail = json.dumps(safe_detail, sort_keys=True)
        cursor = self.execute(
            "INSERT INTO events(kind, entity_type, entity_id, message, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                kind,
                entity_type,
                None if entity_id is None else str(entity_id),
                message,
                encoded_detail,
                timestamp,
            ),
        )
        if self.event_log is not None:
            self._append_event_log(
                {
                    "id": int(cursor.lastrowid),
                    "kind": kind,
                    "entity_type": entity_type,
                    "entity_id": None if entity_id is None else str(entity_id),
                    "message": message,
                    "detail": safe_detail,
                    "created_at": timestamp,
                }
            )
        return int(cursor.lastrowid)

    def _append_event_log(self, record: dict[str, Any]) -> None:
        path = self.event_log
        assert path is not None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, line.encode("utf-8"))
            finally:
                os.close(descriptor)
        except OSError:
            # SQLite remains the authoritative event log. A file sink failure must
            # never roll back the workflow mutation it is describing.
            pass

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
               WHERE t.native_thread_id = ?
               AND a.state IN ('pending','starting','active','terminal','uncertain')""",
            (native_thread_id,),
        )
        if row is None:
            raise StoreError("thread has no current Fulcrum action")
        return row

    def bind_action_turn(
        self, action_id: int, native_thread_id: str, native_turn_id: str
    ) -> None:
        """Attach retained usage to a managed action after turn reconciliation."""

        timestamp = utc_now()
        action = self.row(
            """SELECT a.id, t.model, t.reasoning_effort FROM actions a
               JOIN tasks t ON t.id = a.task_id WHERE a.id = ?""",
            (action_id,),
        )
        if action is None:
            raise StoreError(f"action {action_id} does not exist")
        retained = self.row(
            """SELECT action_id FROM action_turn_usage
               WHERE native_thread_id = ? AND native_turn_id = ?""",
            (native_thread_id, native_turn_id),
        )
        if (
            retained is not None
            and retained["action_id"] is not None
            and int(retained["action_id"]) != action_id
        ):
            raise StoreError(
                f"native turn {native_thread_id}/{native_turn_id} is already bound"
            )
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO action_turn_usage(
                       native_thread_id, native_turn_id, action_id,
                       attributed_action_id, model, reasoning_effort,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(native_thread_id, native_turn_id) DO UPDATE SET
                     action_id = excluded.action_id,
                     attributed_action_id = excluded.attributed_action_id,
                     is_helper = 0,
                     model = excluded.model,
                     reasoning_effort = excluded.reasoning_effort,
                     updated_at = excluded.updated_at""",
                (
                    native_thread_id,
                    native_turn_id,
                    action_id,
                    action_id,
                    action["model"],
                    action["reasoning_effort"],
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """UPDATE telemetry_helper_threads SET attributed_action_id = ?,
                       last_observed_at = ?
                   WHERE parent_thread_id = ? AND parent_turn_id = ?
                     AND attributed_action_id IS NULL""",
                (action_id, timestamp, native_thread_id, native_turn_id),
            )
            self._propagate_helper_ownership(connection, timestamp)

    def _propagate_helper_ownership(
        self, connection: sqlite3.Connection, timestamp: str
    ) -> None:
        """Resolve parent actions and propagate exact helper-turn ownership."""

        while True:
            relations = connection.execute(
                """UPDATE telemetry_helper_threads AS child
                   SET attributed_action_id = (
                         SELECT COALESCE(parent.attributed_action_id,
                                         parent.action_id)
                         FROM action_turn_usage parent
                         WHERE parent.native_thread_id = child.parent_thread_id
                           AND parent.native_turn_id = child.parent_turn_id
                       ),
                       last_observed_at = ?
                   WHERE child.attributed_action_id IS NULL
                     AND child.parent_turn_id != ''
                     AND EXISTS (
                       SELECT 1 FROM action_turn_usage parent
                       WHERE parent.native_thread_id = child.parent_thread_id
                         AND parent.native_turn_id = child.parent_turn_id
                         AND COALESCE(parent.attributed_action_id,
                                      parent.action_id) IS NOT NULL
                     )""",
                (timestamp,),
            ).rowcount
            turns = connection.execute(
                """UPDATE action_turn_usage AS usage
                   SET attributed_action_id = (
                         SELECT helper.attributed_action_id
                         FROM telemetry_helper_threads helper
                         WHERE helper.native_thread_id = usage.native_thread_id
                           AND helper.native_turn_id = usage.native_turn_id
                           AND helper.attributed_action_id IS NOT NULL
                       ),
                       is_helper = 1,
                       model = COALESCE(model, (
                         SELECT task.model FROM telemetry_helper_threads helper
                         JOIN actions action
                           ON action.id = helper.attributed_action_id
                         JOIN tasks task ON task.id = action.task_id
                         WHERE helper.native_thread_id = usage.native_thread_id
                           AND helper.native_turn_id = usage.native_turn_id
                           AND helper.attributed_action_id IS NOT NULL
                       )),
                       reasoning_effort = COALESCE(reasoning_effort, (
                         SELECT task.reasoning_effort
                         FROM telemetry_helper_threads helper
                         JOIN actions action
                           ON action.id = helper.attributed_action_id
                         JOIN tasks task ON task.id = action.task_id
                         WHERE helper.native_thread_id = usage.native_thread_id
                           AND helper.native_turn_id = usage.native_turn_id
                           AND helper.attributed_action_id IS NOT NULL
                       )),
                       coverage = CASE
                         WHEN usage.gap_reason = ? AND usage.terminal_at IS NOT NULL
                              AND usage.total_tokens IS NULL THEN 'unavailable'
                         WHEN usage.gap_reason = ? AND usage.terminal_at IS NOT NULL
                              THEN 'complete'
                         WHEN usage.gap_reason = ? AND usage.total_tokens IS NOT NULL
                              THEN 'observed'
                         WHEN usage.gap_reason = ? THEN 'unknown'
                         ELSE usage.coverage END,
                       gap_reason = CASE
                         WHEN usage.gap_reason = ? AND usage.terminal_at IS NOT NULL
                              AND usage.total_tokens IS NULL
                           THEN 'no token-usage notification was observed'
                         WHEN usage.gap_reason = ? THEN NULL
                         ELSE usage.gap_reason END,
                       updated_at = ?
                   WHERE usage.action_id IS NULL
                     AND usage.attributed_action_id IS NULL
                     AND EXISTS (
                       SELECT 1 FROM telemetry_helper_threads helper
                       WHERE helper.native_thread_id = usage.native_thread_id
                         AND helper.native_turn_id = usage.native_turn_id
                         AND helper.attributed_action_id IS NOT NULL
                     )""",
                (
                    _HELPER_OWNERSHIP_GAP,
                    _HELPER_OWNERSHIP_GAP,
                    _HELPER_OWNERSHIP_GAP,
                    _HELPER_OWNERSHIP_GAP,
                    _HELPER_OWNERSHIP_GAP,
                    _HELPER_OWNERSHIP_GAP,
                    timestamp,
                ),
            ).rowcount
            if relations == 0 and turns == 0:
                return

    def observe_helper_turn_started(
        self, native_thread_id: str, native_turn_id: str
    ) -> bool:
        """Bind a helper lifecycle turn to one unambiguous collaboration item."""

        if self.row(
            "SELECT 1 FROM tasks WHERE native_thread_id = ?", (native_thread_id,)
        ):
            return False
        relationships = self.rows(
            """SELECT id, native_turn_id FROM telemetry_helper_threads
               WHERE native_thread_id = ? ORDER BY id""",
            (native_thread_id,),
        )
        if not relationships:
            return False
        timestamp = utc_now()
        exact = next(
            (
                relationship
                for relationship in relationships
                if relationship["native_turn_id"] == native_turn_id
            ),
            None,
        )
        unresolved = [
            relationship
            for relationship in relationships
            if relationship["native_turn_id"] is None
        ]
        with self.transaction() as connection:
            if exact is None and len(unresolved) == 1:
                connection.execute(
                    """UPDATE telemetry_helper_threads
                       SET native_turn_id = ?, last_observed_at = ? WHERE id = ?""",
                    (native_turn_id, timestamp, unresolved[0]["id"]),
                )
                exact = unresolved[0]
            helper = connection.execute(
                """SELECT attributed_action_id FROM telemetry_helper_threads
                   WHERE native_thread_id = ? AND native_turn_id = ?""",
                (native_thread_id, native_turn_id),
            ).fetchone()
            action_id = helper[0] if helper is not None else None
            attribution = None
            if action_id is not None:
                attribution = connection.execute(
                    """SELECT t.model, t.reasoning_effort FROM actions a
                       JOIN tasks t ON t.id = a.task_id WHERE a.id = ?""",
                    (action_id,),
                ).fetchone()
            gap_reason = None if helper is not None else _HELPER_OWNERSHIP_GAP
            connection.execute(
                """INSERT INTO action_turn_usage(
                       native_thread_id, native_turn_id, attributed_action_id,
                       is_helper, model, reasoning_effort, coverage, gap_reason,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(native_thread_id, native_turn_id) DO UPDATE SET
                     attributed_action_id = action_turn_usage.attributed_action_id,
                     is_helper = 1,
                     model = COALESCE(action_turn_usage.model, excluded.model),
                     reasoning_effort = COALESCE(
                       action_turn_usage.reasoning_effort,
                       excluded.reasoning_effort),
                     coverage = CASE
                       WHEN excluded.attributed_action_id IS NULL
                        AND action_turn_usage.attributed_action_id IS NULL
                       THEN 'partial' ELSE action_turn_usage.coverage END,
                     gap_reason = CASE
                       WHEN excluded.attributed_action_id IS NULL
                        AND action_turn_usage.attributed_action_id IS NULL
                       THEN COALESCE(action_turn_usage.gap_reason,
                                     excluded.gap_reason)
                       ELSE action_turn_usage.gap_reason END,
                     updated_at = excluded.updated_at""",
                (
                    native_thread_id,
                    native_turn_id,
                    action_id,
                    attribution[0] if attribution is not None else None,
                    attribution[1] if attribution is not None else None,
                    "unknown" if helper is not None else "partial",
                    gap_reason,
                    timestamp,
                    timestamp,
                ),
            )
            self._propagate_helper_ownership(connection, timestamp)
        return True

    def observe_helper_thread(
        self,
        *,
        parent_thread_id: str,
        parent_turn_id: str | None,
        native_thread_id: str,
        collaboration_item_id: str | None = None,
    ) -> None:
        """Retain a supported collaboration parent/child identity."""

        if native_thread_id == parent_thread_id:
            return
        # A separately managed Fulcrum thread is never reclassified as a helper.
        if self.row(
            "SELECT 1 FROM tasks WHERE native_thread_id = ?", (native_thread_id,)
        ):
            return
        owner = None
        if parent_turn_id is not None:
            owner = self.row(
                """SELECT COALESCE(attributed_action_id, action_id) AS action_id
                   FROM action_turn_usage
                   WHERE native_thread_id = ? AND native_turn_id = ?""",
                (parent_thread_id, parent_turn_id),
            )
            if owner is None:
                owner = self.row(
                    """SELECT a.id AS action_id FROM actions a JOIN tasks t
                       ON t.id = a.task_id
                       WHERE t.native_thread_id = ? AND a.native_turn_id = ?
                       ORDER BY a.id DESC LIMIT 1""",
                    (parent_thread_id, parent_turn_id),
                )
        action_id = owner["action_id"] if owner else None
        timestamp = utc_now()
        normalized_parent_turn_id = parent_turn_id or ""
        normalized_item_id = collaboration_item_id or ""
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO telemetry_helper_threads(
                       native_thread_id, parent_thread_id, parent_turn_id,
                       collaboration_item_id, attributed_action_id,
                       first_observed_at, last_observed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(native_thread_id, parent_thread_id, parent_turn_id,
                               collaboration_item_id)
                   DO UPDATE SET attributed_action_id = COALESCE(
                       telemetry_helper_threads.attributed_action_id,
                       excluded.attributed_action_id),
                     last_observed_at = CASE
                       WHEN telemetry_helper_threads.attributed_action_id IS NULL
                        AND excluded.attributed_action_id IS NOT NULL
                       THEN excluded.last_observed_at
                       ELSE telemetry_helper_threads.last_observed_at END""",
                (
                    native_thread_id,
                    parent_thread_id,
                    normalized_parent_turn_id,
                    normalized_item_id,
                    action_id,
                    timestamp,
                    timestamp,
                ),
            )
            relationship = connection.execute(
                """SELECT id, native_turn_id FROM telemetry_helper_threads
                   WHERE native_thread_id = ? AND parent_thread_id = ?
                     AND parent_turn_id = ? AND collaboration_item_id = ?""",
                (
                    native_thread_id,
                    parent_thread_id,
                    normalized_parent_turn_id,
                    normalized_item_id,
                ),
            ).fetchone()
            relationship_id = int(relationship[0]) if relationship is not None else None
            unresolved_relationships = connection.execute(
                """SELECT id FROM telemetry_helper_threads
                   WHERE native_thread_id = ? AND native_turn_id IS NULL""",
                (native_thread_id,),
            ).fetchall()
            pending_turns = connection.execute(
                """SELECT native_turn_id FROM action_turn_usage
                   WHERE native_thread_id = ? AND action_id IS NULL
                     AND attributed_action_id IS NULL
                     AND NOT EXISTS (
                       SELECT 1 FROM telemetry_helper_threads helper
                       WHERE helper.native_thread_id = action_turn_usage.native_thread_id
                         AND helper.native_turn_id = action_turn_usage.native_turn_id
                     )""",
                (native_thread_id,),
            ).fetchall()
            if (
                relationship is not None
                and relationship[1] is None
                and len(unresolved_relationships) == 1
                and len(pending_turns) == 1
                and int(unresolved_relationships[0][0]) == relationship_id
            ):
                connection.execute(
                    """UPDATE telemetry_helper_threads
                       SET native_turn_id = ?, last_observed_at = ? WHERE id = ?""",
                    (pending_turns[0][0], timestamp, relationship_id),
                )
            self._propagate_helper_ownership(connection, timestamp)

    def observe_turn_usage(self, params: dict[str, Any]) -> bool:
        """Upsert one cumulative app-server snapshot without sampling inflation."""

        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        token_usage = params.get("tokenUsage")
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            return False
        if not isinstance(token_usage, dict):
            return False
        cumulative = token_usage.get("total")
        latest = token_usage.get("last")
        if not isinstance(cumulative, dict):
            return False
        total = _usage_breakdown(cumulative)
        last = _usage_breakdown(latest) if isinstance(latest, dict) else {}
        if not total:
            return False
        timestamp = utc_now()
        action = self.row(
            """SELECT a.id, t.model, t.reasoning_effort FROM actions a
               JOIN tasks t ON t.id = a.task_id
               WHERE t.native_thread_id = ? AND a.native_turn_id = ?
               ORDER BY a.id DESC LIMIT 1""",
            (thread_id, turn_id),
        )
        if action is None:
            self.observe_helper_turn_started(thread_id, turn_id)
        helper = self.row(
            """SELECT attributed_action_id FROM telemetry_helper_threads
               WHERE native_thread_id = ? AND native_turn_id = ?
                 AND attributed_action_id IS NOT NULL""",
            (thread_id, turn_id),
        )
        helper_history = self.row(
            "SELECT 1 FROM telemetry_helper_threads WHERE native_thread_id = ?",
            (thread_id,),
        )
        action_id = action["id"] if action else None
        attributed_action_id = (
            action_id
            if action_id is not None
            else helper["attributed_action_id"] if helper else None
        )
        attribution = action
        if attribution is None and attributed_action_id is not None:
            attribution = self.row(
                """SELECT t.model, t.reasoning_effort FROM actions a
                   JOIN tasks t ON t.id = a.task_id WHERE a.id = ?""",
                (attributed_action_id,),
            )
        model = attribution["model"] if attribution else None
        effort = attribution["reasoning_effort"] if attribution else None
        existing = self.row(
            """SELECT * FROM action_turn_usage
               WHERE native_thread_id = ? AND native_turn_id = ?""",
            (thread_id, turn_id),
        )
        columns = _usage_columns("total")
        last_columns = _usage_columns("last")
        if existing is None:
            values = [total.get(key) for key in _USAGE_KEYS]
            last_values = [last.get(key) for key in _USAGE_KEYS]
            self.execute(
                f"""INSERT INTO action_turn_usage(
                       native_thread_id, native_turn_id, action_id,
                       attributed_action_id, is_helper, model, reasoning_effort,
                       {', '.join(last_columns)}, {', '.join(columns)},
                       model_context_window, coverage, first_observed_at,
                       last_observed_at, created_at, updated_at
                   ) VALUES ({', '.join('?' for _ in range(7 + 6 + 6 + 6))})""",
                [
                    thread_id,
                    turn_id,
                    action_id,
                    attributed_action_id,
                    int(action_id is None and helper_history is not None),
                    model,
                    effort,
                    *last_values,
                    *values,
                    _nonnegative_int(params.get("modelContextWindow")),
                    (
                        "partial"
                        if action_id is None and helper is None and helper_history
                        else "observed"
                    ),
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                ],
            )
            if action_id is None and helper is None and helper_history:
                self.execute(
                    """UPDATE action_turn_usage SET gap_reason = ?
                       WHERE native_thread_id = ? AND native_turn_id = ?""",
                    (_HELPER_OWNERSHIP_GAP, thread_id, turn_id),
                )
            with self.transaction() as connection:
                self._propagate_helper_ownership(connection, timestamp)
            return True
        old_total = existing.get("total_tokens")
        new_total = total.get("total_tokens")
        newest = new_total is not None and (
            old_total is None
            or new_total > int(old_total)
            or (
                new_total == int(old_total)
                and existing.get("last_total_tokens") is None
            )
        )
        updates: dict[str, Any] = {}
        for key, column in zip(_USAGE_KEYS, columns, strict=True):
            value = total.get(key)
            retained = existing.get(column)
            if value is not None and (retained is None or value > int(retained)):
                updates[column] = value
        if newest:
            for key, column in zip(_USAGE_KEYS, last_columns, strict=True):
                if key in last:
                    updates[column] = last[key]
        context = _nonnegative_int(params.get("modelContextWindow"))
        if context is not None:
            updates["model_context_window"] = max(
                context, int(existing.get("model_context_window") or 0)
            )
        updates.update(
            {
                "action_id": existing.get("action_id") or action_id,
                "attributed_action_id": existing.get("attributed_action_id")
                or attributed_action_id,
                "is_helper": int(
                    existing.get("is_helper")
                    or (action_id is None and helper_history is not None)
                ),
                "model": existing.get("model") or model,
                "reasoning_effort": existing.get("reasoning_effort") or effort,
                "first_observed_at": existing.get("first_observed_at") or timestamp,
                "last_observed_at": timestamp,
                "updated_at": timestamp,
            }
        )
        if existing.get("terminal_at") and existing.get("gap_reason") == (
            "no token-usage notification was observed"
        ):
            updates["gap_reason"] = None
            updates["coverage"] = "complete"
        elif existing.get("terminal_at") and not existing.get("gap_reason"):
            updates["coverage"] = "complete"
        elif existing.get("coverage") in {"unknown", "unavailable"}:
            updates["coverage"] = "observed"
        assignments = ", ".join(f"{name} = ?" for name in updates)
        self.execute(
            f"""UPDATE action_turn_usage SET {assignments}
                WHERE native_thread_id = ? AND native_turn_id = ?""",
            (*updates.values(), thread_id, turn_id),
        )
        with self.transaction() as connection:
            self._propagate_helper_ownership(connection, timestamp)
        return True

    def mark_open_usage_gap(self, reason: str) -> None:
        timestamp = utc_now()
        self.execute(
            """UPDATE action_turn_usage SET coverage = 'partial',
                   gap_reason = COALESCE(gap_reason, ?), updated_at = ?
               WHERE terminal_at IS NULL AND coverage != 'unavailable'""",
            (reason, timestamp),
        )

    def finalize_native_turn_usage(
        self, native_thread_id: str, native_turn_id: str
    ) -> bool:
        """Finalize a known helper turn even though it owns no Fulcrum action."""

        row = self.row(
            """SELECT * FROM action_turn_usage
               WHERE native_thread_id = ? AND native_turn_id = ?""",
            (native_thread_id, native_turn_id),
        )
        helper = self.row(
            """SELECT attributed_action_id FROM telemetry_helper_threads
               WHERE native_thread_id = ? AND native_turn_id = ?
                 AND attributed_action_id IS NOT NULL""",
            (native_thread_id, native_turn_id),
        )
        if row is None and (helper is None or helper["attributed_action_id"] is None):
            return False
        timestamp = utc_now()
        if row is None:
            assert helper is not None
            self.execute(
                """INSERT INTO action_turn_usage(
                       native_thread_id, native_turn_id, attributed_action_id,
                       is_helper, coverage, gap_reason, terminal_at,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, 1, 'unavailable', ?, ?, ?, ?)""",
                (
                    native_thread_id,
                    native_turn_id,
                    helper["attributed_action_id"],
                    "no token-usage notification was observed",
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            return True
        if row["terminal_at"] is not None:
            return False
        coverage = (
            "partial"
            if row["gap_reason"]
            else "unavailable" if row["total_tokens"] is None else "complete"
        )
        gap = row["gap_reason"]
        if coverage == "unavailable" and gap is None:
            gap = "no token-usage notification was observed"
        self.execute(
            """UPDATE action_turn_usage SET terminal_at = ?, coverage = ?,
                   gap_reason = ?, updated_at = ?
               WHERE native_thread_id = ? AND native_turn_id = ?""",
            (timestamp, coverage, gap, timestamp, native_thread_id, native_turn_id),
        )
        return True

    def finalize_action_usage(self, action_id: int) -> dict[str, Any]:
        """Freeze the latest direct snapshot and return a bounded summary."""

        timestamp = utc_now()
        action = self.row(
            """SELECT a.*, t.native_thread_id FROM actions a JOIN tasks t
               ON t.id = a.task_id WHERE a.id = ?""",
            (action_id,),
        )
        if action is None or not isinstance(action.get("native_turn_id"), str):
            return self.action_usage_summary(action_id)
        self.bind_action_turn(
            action_id, str(action["native_thread_id"]), str(action["native_turn_id"])
        )
        row = self.row(
            """SELECT total_tokens, gap_reason, terminal_at FROM action_turn_usage
               WHERE native_thread_id = ? AND native_turn_id = ?""",
            (action["native_thread_id"], action["native_turn_id"]),
        )
        assert row is not None
        if row["terminal_at"] is not None:
            summary = self.action_usage_summary(action_id)
            summary["_newly_finalized"] = False
            return summary
        coverage = (
            "unavailable"
            if row["total_tokens"] is None
            else "partial" if row["gap_reason"] else "complete"
        )
        gap = row["gap_reason"]
        if coverage == "unavailable":
            gap = "no token-usage notification was observed"
        self.execute(
            """UPDATE action_turn_usage SET terminal_at = ?, coverage = ?,
                   gap_reason = ?, updated_at = ?
               WHERE native_thread_id = ? AND native_turn_id = ?""",
            (
                timestamp,
                coverage,
                gap,
                timestamp,
                action["native_thread_id"],
                action["native_turn_id"],
            ),
        )
        summary = self.action_usage_summary(action_id)
        summary["_newly_finalized"] = True
        return summary

    def action_usage_summary(self, action_id: int) -> dict[str, Any]:
        report = self.usage_report(action_id=action_id, group_by="action")
        groups = report["groups"]
        if not groups:
            return {
                "direct_total_tokens": None,
                "attributed_total_tokens": None,
                "coverage": "unknown",
            }
        item = groups[0]
        return {
            "direct_total_tokens": item["direct"]["total_tokens"],
            "attributed_total_tokens": item["attributed"]["total_tokens"],
            "coverage": item["coverage"],
            "turn_count": item["contributing_turn_count"],
        }

    def usage_report(
        self,
        *,
        action_id: int | None = None,
        task_id: int | None = None,
        assignment_id: int | None = None,
        run_id: int | None = None,
        role: str | None = None,
        project_id: str | None = None,
        group_by: str = "action",
    ) -> dict[str, Any]:
        """Return historical and active usage without replaying stream samples."""

        dimensions = {
            "action": "a.id",
            "task": "t.id",
            "assignment": "a.assignment_id",
            "run": "assn.run_id",
            "role": "t.role",
            "project": "t.project_id",
        }
        result_keys = {
            "action": "id",
            "task": "task_id",
            "assignment": "assignment_id",
            "run": "run_id",
            "role": "role",
            "project": "project_id",
        }
        if group_by not in dimensions:
            raise StoreError(f"unsupported usage grouping: {group_by}")
        filters: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("a.id", action_id),
            ("t.id", task_id),
            ("a.assignment_id", assignment_id),
            ("assn.run_id", run_id),
            ("t.role", role),
            ("t.project_id", project_id),
        ):
            if value is not None:
                filters.append(f"{column} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        actions = self.rows(
            f"""SELECT a.id, a.task_id, a.assignment_id, a.kind, a.state,
                       a.native_turn_id, t.native_thread_id, t.role, t.title,
                       t.project_id, t.model, t.reasoning_effort, assn.run_id
                FROM actions a JOIN tasks t ON t.id = a.task_id
                LEFT JOIN assignments assn ON assn.id = a.assignment_id
                {where} ORDER BY a.id""",
            values,
        )
        groups: dict[Any, list[dict[str, Any]]] = {}
        key_name = result_keys[group_by]
        for action in actions:
            key = action.get(key_name)
            if key is not None:
                groups.setdefault(key, []).append(action)
        rendered: list[dict[str, Any]] = []
        all_turns: list[dict[str, Any]] = []
        for key, members in groups.items():
            ids = [int(member["id"]) for member in members]
            marks = ",".join("?" for _ in ids)
            turns = self.rows(
                f"""SELECT * FROM action_turn_usage
                    WHERE action_id IN ({marks}) OR attributed_action_id IN ({marks})
                    ORDER BY first_observed_at, native_thread_id, native_turn_id""",
                ids + ids,
            )
            all_turns.extend(turns)
            direct = [turn for turn in turns if turn["action_id"] in ids]
            attributed = [turn for turn in turns if turn["attributed_action_id"] in ids]
            coverage = _coverage_state(attributed or direct, expected=bool(members))
            missing_helpers = self.row(
                f"""SELECT COUNT(*) AS count FROM telemetry_helper_threads helper
                    WHERE helper.attributed_action_id IN ({marks})
                      AND NOT EXISTS (
                        SELECT 1 FROM action_turn_usage usage
                        WHERE usage.native_thread_id = helper.native_thread_id
                          AND usage.native_turn_id = helper.native_turn_id
                          AND usage.attributed_action_id =
                              helper.attributed_action_id
                      )""",
                ids,
            )
            missing_helper_count = int(
                missing_helpers["count"] if missing_helpers else 0
            )
            unassociated_helpers = self.row(
                f"""SELECT COUNT(*) AS count FROM action_turn_usage usage
                    WHERE usage.action_id IS NULL
                      AND usage.attributed_action_id IS NULL
                      AND usage.is_helper = 1
                      AND EXISTS (
                        SELECT 1 FROM telemetry_helper_threads helper
                        WHERE helper.native_thread_id = usage.native_thread_id
                          AND helper.attributed_action_id IN ({marks})
                      )""",
                ids,
            )
            missing_helper_count = max(
                missing_helper_count,
                int(unassociated_helpers["count"] if unassociated_helpers else 0),
            )
            if missing_helper_count and coverage in {"complete", "observed"}:
                coverage = "partial"
            rendered.append(
                {
                    "group_by": group_by,
                    "group": key,
                    "action_ids": ids,
                    "direct": _sum_usage(direct),
                    "attributed": _sum_usage(attributed),
                    "coverage": coverage,
                    "contributing_action_count": len(
                        {
                            turn["attributed_action_id"]
                            for turn in attributed
                            if turn["attributed_action_id"] is not None
                        }
                    ),
                    "contributing_turn_count": len(attributed),
                    "direct_action_count": len(
                        {
                            turn["action_id"]
                            for turn in direct
                            if turn["action_id"] is not None
                        }
                    ),
                    "direct_turn_count": len(direct),
                    "attributed_action_count": len(
                        {
                            turn["attributed_action_id"]
                            for turn in attributed
                            if turn["attributed_action_id"] is not None
                        }
                    ),
                    "attributed_turn_count": len(attributed),
                    "helper_turn_count": sum(
                        int(turn["is_helper"]) for turn in attributed
                    ),
                    "unobserved_helper_count": missing_helper_count,
                }
            )
        unique_turns = {
            (turn["native_thread_id"], turn["native_turn_id"]): turn
            for turn in all_turns
        }
        return {
            "filters": {
                "action_id": action_id,
                "task_id": task_id,
                "assignment_id": assignment_id,
                "run_id": run_id,
                "role": role,
                "project_id": project_id,
            },
            "group_by": group_by,
            "groups": rendered,
            "turns": list(unique_turns.values()),
        }

    def create_operation(
        self,
        kind: str,
        target: str,
        inputs: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> int:
        timestamp = utc_now()
        encoded_inputs = json.dumps(_redact(inputs), sort_keys=True)
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO external_operations(
                       kind, target, input_json, state, correlation_id, created_at, updated_at
                   ) VALUES (?, ?, ?, 'intent', '', ?, ?)""",
                (
                    kind,
                    target,
                    encoded_inputs,
                    timestamp,
                    timestamp,
                ),
            )
            identifier = int(cursor.lastrowid)
            correlation = correlation_id or f"operation-{identifier}"
            connection.execute(
                "UPDATE external_operations SET correlation_id = ? WHERE id = ?",
                (correlation, identifier),
            )
        self.event(
            "operation_intended",
            f"recorded {kind} intent",
            entity_type="operation",
            entity_id=identifier,
            detail={
                "correlation_id": correlation,
                "kind": kind,
                "target": target,
            },
            now=timestamp,
        )
        return identifier

    def begin_operation_attempt(self, operation_id: int) -> int:
        """Atomically lease one durable external-operation attempt."""

        timestamp = utc_now()
        with self.transaction() as connection:
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if operation is None:
                raise StoreError(f"unknown external operation {operation_id}")
            if operation["state"] in {"complete", "canceled"}:
                raise StoreError(f"operation {operation_id} is already terminal")
            attempt = int(operation["attempt_count"]) + 1
            connection.execute(
                """UPDATE external_operations SET state = 'sent', attempt_count = ?,
                   last_attempt_at = ?, next_attempt_at = NULL, updated_at = ? WHERE id = ?""",
                (attempt, timestamp, timestamp, operation_id),
            )
            connection.execute(
                """INSERT INTO operation_attempts(
                       operation_id, attempt, state, request_json, started_at
                   ) VALUES (?, ?, 'started', ?, ?)""",
                (operation_id, attempt, operation["input_json"], timestamp),
            )
        self.event(
            "operation_started",
            f"started attempt {attempt}",
            entity_type="operation",
            entity_id=operation_id,
            detail={"attempt": attempt, "correlation_id": operation["correlation_id"]},
        )
        return attempt

    def finish_operation_attempt(
        self,
        operation_id: int,
        attempt: int,
        *,
        state: str,
        result: Any = None,
        stdout: str | None = None,
        stderr: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        native_id: str | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        if state not in {"complete", "failed", "uncertain"}:
            raise StoreError(f"invalid operation result state {state!r}")
        timestamp = utc_now()
        encoded_result = (
            None if result is None else json.dumps(_bounded(result), sort_keys=True)
        )
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT correlation_id FROM external_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
            if current is None:
                raise StoreError(f"unknown external operation {operation_id}")
            connection.execute(
                """UPDATE operation_attempts SET state = ?, result_json = ?, stdout = ?, stderr = ?,
                   error = ?, finished_at = ?, duration_ms = ?
                   WHERE operation_id = ? AND attempt = ?""",
                (
                    state,
                    encoded_result,
                    _bounded_text(stdout),
                    _bounded_text(stderr),
                    _bounded_text(error),
                    timestamp,
                    duration_ms,
                    operation_id,
                    attempt,
                ),
            )
            connection.execute(
                """UPDATE external_operations SET state = ?, result_json = ?, native_id = COALESCE(?, native_id),
                   condition = ?, next_attempt_at = ?, completed_at = CASE WHEN ? = 'complete' THEN ? ELSE NULL END,
                   updated_at = ? WHERE id = ?""",
                (
                    state,
                    encoded_result,
                    native_id,
                    _bounded_text(error),
                    next_attempt_at,
                    state,
                    timestamp,
                    timestamp,
                    operation_id,
                ),
            )
        self.event(
            f"operation_{state}",
            f"operation attempt {attempt} {state}",
            entity_type="operation",
            entity_id=operation_id,
            detail={
                "attempt": attempt,
                "correlation_id": current["correlation_id"],
                "duration_ms": duration_ms,
                "error": _bounded_text(error),
            },
        )

    def transition(
        self,
        entity_type: str,
        entity_id: int | str,
        *,
        table: str,
        field: str,
        to_state: str,
        reason: str,
        correlation_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Persist a state change and its audit record in the same transaction."""

        allowed: Final = {
            ("tasks", "state"),
            ("actions", "state"),
            ("assignments", "stage"),
            ("occurrences", "state"),
            ("interviews", "state"),
            ("obligations", "state"),
            ("batches", "state"),
        }
        if (table, field) not in allowed:
            raise StoreError(f"unsupported audited transition {table}.{field}")
        timestamp = utc_now()
        updates = {field: to_state, "updated_at": timestamp, **(extra or {})}
        assignments = ", ".join(f"{name} = ?" for name in updates)
        with self.transaction() as connection:
            row = connection.execute(
                f"SELECT {field} FROM {table} WHERE id = ?", (entity_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown {entity_type} {entity_id}")
            connection.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?",
                (*updates.values(), entity_id),
            )
            connection.execute(
                """UPDATE state_transitions SET reason = ?, correlation_id = ?
                   WHERE id = (
                     SELECT MAX(id) FROM state_transitions
                     WHERE entity_type = ? AND entity_id = ? AND field_name = ?
                       AND from_state IS ? AND to_state = ?
                   )""",
                (
                    reason,
                    correlation_id,
                    entity_type,
                    str(entity_id),
                    field,
                    row[field],
                    to_state,
                ),
            )

    def heartbeat(
        self,
        worker_name: str,
        *,
        state: str = "running",
        error: str | None = None,
        traceback_text: str | None = None,
    ) -> None:
        timestamp = utc_now()
        self.execute(
            """INSERT INTO worker_heartbeats(
                   worker_name, state, started_at, heartbeat_at, failure_count, last_error, traceback
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(worker_name) DO UPDATE SET state = excluded.state,
               heartbeat_at = excluded.heartbeat_at,
               completed_at = CASE WHEN excluded.state = 'stopped' THEN excluded.heartbeat_at ELSE NULL END,
               failure_count = worker_heartbeats.failure_count + CASE WHEN excluded.state = 'degraded' THEN 1 ELSE 0 END,
               last_error = excluded.last_error, traceback = excluded.traceback""",
            (
                worker_name,
                state,
                timestamp,
                timestamp,
                int(state == "degraded"),
                _bounded_text(error),
                _bounded_text(traceback_text, limit=32768),
            ),
        )

    def status(self, *, event_limit: int = 20) -> dict[str, Any]:
        reservations = self.rows("SELECT * FROM reservations")
        tasks = self.rows("SELECT * FROM tasks WHERE state != 'retired' ORDER BY id")
        for task in tasks:
            task["desktop_context"] = f"codex://thread/{task['native_thread_id']}"
            current = self.row(
                """SELECT a.bead_id, a.stage FROM assignments a
                   JOIN run_beads rb ON rb.run_id = a.run_id AND rb.bead_id = a.bead_id
                   WHERE (a.executor_task_id = ? OR a.overseer_task_id = ?)
                   AND a.stage NOT IN ('completed','canceled')
                   AND NOT EXISTS (
                     SELECT 1 FROM run_beads earlier
                     JOIN assignments prior ON prior.run_id = earlier.run_id
                       AND prior.bead_id = earlier.bead_id
                     WHERE earlier.run_id = rb.run_id AND earlier.position < rb.position
                       AND prior.stage NOT IN ('completed','canceled')
                   )
                   ORDER BY a.id LIMIT 1""",
                (task["id"], task["id"]),
            )
            task["current_bead"] = current["bead_id"] if current else None
            task["assignment_stage"] = current["stage"] if current else None
        assignments = self.rows(
            """SELECT a.*, b.title AS bead_title, r.project_id FROM assignments a
               JOIN beads b ON b.bead_id = a.bead_id JOIN runs r ON r.id = a.run_id
               WHERE a.stage NOT IN ('completed','canceled') ORDER BY a.id"""
        )
        for assignment in assignments:
            if assignment["operator_hold_id"] is not None:
                next_step = "operator resolves hold"
                next_at = None
            elif assignment["stage"] == "recovering":
                next_step = "controller retries retained prior stage"
                next_at = assignment["next_attempt_at"]
            else:
                next_step = {
                    "queued": "scheduler acquires capacity",
                    "preparing": "controller ensures worktree and role pair",
                    "implementing": "executor turn completes",
                    "review_pending": "controller starts independent review",
                    "reviewing": "overseer turn completes",
                    "correcting": "executor correction completes",
                    "delivering": "controller reconciles delivery",
                }.get(str(assignment["stage"]), "controller reconciles state")
                next_at = None
            assignment["progress"] = {
                "waiting_for": assignment["condition"] or next_step,
                "next_step": next_step,
                "next_attempt_at": next_at,
                "operator_hold_id": assignment["operator_hold_id"],
            }
        actions = self.rows(
            """SELECT * FROM actions WHERE state NOT IN ('processed','canceled')
               ORDER BY id"""
        )
        for action in actions:
            action["progress"] = {
                "waiting_for": action["condition"] or action["state"],
                "next_step": (
                    "operator resolves hold"
                    if action["operator_hold_id"] is not None
                    else (
                        "controller retries start"
                        if action["state"] == "pending"
                        else "controller observes runtime terminal state"
                    )
                ),
                "next_attempt_at": action["next_attempt_at"],
            }
            action["usage"] = self.action_usage_summary(int(action["id"]))
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
            "actions": actions,
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
            "workers": self.rows(
                "SELECT * FROM worker_heartbeats ORDER BY worker_name"
            ),
            "transitions": self.rows(
                "SELECT * FROM state_transitions ORDER BY id DESC LIMIT ?",
                (event_limit,),
            ),
            "readiness_reasons": self.row(
                "SELECT value FROM meta WHERE key = 'readiness_reasons'"
            ),
        }


def _project_slot_usage(reservations: list[dict[str, Any]]) -> dict[str, int]:
    usage: dict[str, int] = {}
    for reservation in reservations:
        for project in json.loads(reservation["project_ids"]):
            usage[project] = usage.get(project, 0) + 1
    return usage


_USAGE_KEYS: Final = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _nonnegative_int(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _usage_breakdown(value: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for key in _USAGE_KEYS:
        wire_key = "".join(
            part if index == 0 else part.title()
            for index, part in enumerate(key.split("_"))
        )
        parsed = _nonnegative_int(value.get(wire_key))
        if parsed is not None:
            result[key] = parsed
    return result


def _usage_columns(prefix: str) -> tuple[str, ...]:
    return tuple(
        (
            f"{prefix}_{key}"
            if not (prefix == "total" and key == "total_tokens")
            else "total_tokens"
        )
        for key in _USAGE_KEYS
    )


def _sum_usage(turns: list[dict[str, Any]]) -> dict[str, int | None]:
    totals: dict[str, int | None] = {}
    for key in _USAGE_KEYS:
        column = "total_tokens" if key == "total_tokens" else f"total_{key}"
        values = [turn.get(column) for turn in turns]
        known = [int(value) for value in values if value is not None]
        totals[key] = sum(known) if known else None
    return totals


def _coverage_state(turns: list[dict[str, Any]], *, expected: bool) -> str:
    if not turns:
        return "unknown" if expected else "unavailable"
    states = {str(turn["coverage"]) for turn in turns}
    if states == {"complete"}:
        return "complete"
    if states == {"unknown"}:
        return "unknown"
    if states == {"observed"}:
        return "observed"
    if states == {"unavailable"}:
        return "unavailable"
    if "partial" in states or "unknown" in states or "unavailable" in states:
        return "partial"
    return "observed" if "observed" in states else "partial"


_SENSITIVE_KEYS: Final = frozenset(
    {"authorization", "password", "secret", "token", "api_key", "access_key"}
)


def _bounded_text(value: str | None, *, limit: int = 16384) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + f"… <{len(value) - limit} bytes omitted>"


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, list):
        return [_bounded(item) for item in value[:200]]
    if isinstance(value, dict):
        return {str(key): _bounded(child) for key, child in list(value.items())[:200]}
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact(item) for item in value[:200]]
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>" if str(key).lower() in _SENSITIVE_KEYS else _redact(child)
            )
            for key, child in list(value.items())[:200]
        }
    return _bounded(value)
