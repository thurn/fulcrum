"""SQLite state owned exclusively by the Fulcrum controller."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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

_RATE_CAPTURED_AT = "2026-09-13T00:00:00Z"
_RATE_CARDS: Final = (
    ("gpt-5.6-sol", "4.00", "0.40", "5.00", "20.00"),
    ("gpt-5.6-terra", "2.00", "0.20", "2.50", "12.00"),
    ("gpt-5.6-luna", "0.20", "0.02", "0.25", "1.20"),
    ("gpt-6-astra", "10.00", "1.00", "12.50", "50.00"),
)
_TOOL_RATE_CARDS: Final = (
    (
        "web_search",
        "call",
        "0.01",
        "https://developers.openai.com/api/docs/pricing",
    ),
)
_MODEL_ALIASES: Final = {
    "gpt-5.6": "gpt-5.6-sol",
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
    "astra": "gpt-6-astra",
}
_TIER_MULTIPLIERS: Final = {
    "standard": Decimal("1"),
    "default": Decimal("1"),
    "batch": Decimal("0.5"),
    "flex": Decimal("0.5"),
    "fast": Decimal("2"),
    "priority": Decimal("2"),
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
CREATE TABLE IF NOT EXISTS api_rate_cards (
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
  processing_tier TEXT NOT NULL, currency TEXT NOT NULL,
  input_per_million TEXT NOT NULL, cached_input_per_million TEXT NOT NULL,
  cache_write_input_per_million TEXT NOT NULL, output_per_million TEXT NOT NULL,
  long_context_threshold INTEGER, long_context_input_multiplier TEXT,
  long_context_output_multiplier TEXT, tier_multiplier TEXT NOT NULL,
  rules_json TEXT NOT NULL, source_url TEXT NOT NULL,
  effective_at TEXT NOT NULL, captured_at TEXT NOT NULL,
  UNIQUE(provider, model, processing_tier, effective_at)
);
CREATE TABLE IF NOT EXISTS api_tool_rate_cards (
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, tool_name TEXT NOT NULL,
  unit TEXT NOT NULL, currency TEXT NOT NULL, unit_rate TEXT NOT NULL,
  rules_json TEXT NOT NULL DEFAULT '[]', source_url TEXT NOT NULL,
  effective_at TEXT NOT NULL, captured_at TEXT NOT NULL,
  UNIQUE(provider, tool_name, unit, effective_at)
);
CREATE TABLE IF NOT EXISTS cost_contributions (
  id INTEGER PRIMARY KEY, contribution_kind TEXT NOT NULL
    CHECK (contribution_kind IN ('model_response','tool_call')),
  source_key TEXT NOT NULL UNIQUE,
  native_thread_id TEXT, native_turn_id TEXT, response_sequence INTEGER,
  action_id INTEGER REFERENCES actions(id),
  attributed_action_id INTEGER REFERENCES actions(id),
  provider TEXT NOT NULL, requested_model TEXT, effective_model TEXT,
  processing_tier TEXT, currency TEXT NOT NULL,
  rate_card_id INTEGER REFERENCES api_rate_cards(id),
  input_rate TEXT, cached_input_rate TEXT, cache_write_input_rate TEXT,
  output_rate TEXT, tier_multiplier TEXT, applied_rules TEXT NOT NULL DEFAULT '[]',
  input_tokens INTEGER, cached_input_tokens INTEGER,
  cache_write_input_tokens INTEGER, output_tokens INTEGER,
  reasoning_output_tokens INTEGER, ordinary_input_tokens INTEGER,
  tool_name TEXT, quantity TEXT, unit TEXT, unit_rate TEXT,
  input_amount TEXT, cached_input_amount TEXT, cache_write_input_amount TEXT,
  output_amount TEXT, amount TEXT,
  coverage TEXT NOT NULL CHECK (coverage IN ('complete','partial','invalid')),
  assumptions TEXT NOT NULL DEFAULT '[]', exclusions TEXT NOT NULL DEFAULT '[]',
  source_url TEXT, rate_captured_at TEXT, rate_effective_at TEXT,
  finalized_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS costs_by_action ON cost_contributions(action_id);
CREATE INDEX IF NOT EXISTS costs_by_attributed_action ON cost_contributions(attributed_action_id);
CREATE TABLE IF NOT EXISTS model_reroutes (
  id INTEGER PRIMARY KEY, native_thread_id TEXT NOT NULL,
  native_turn_id TEXT NOT NULL, from_model TEXT NOT NULL,
  to_model TEXT NOT NULL, reason TEXT NOT NULL,
  contribution_id INTEGER REFERENCES cost_contributions(id),
  observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reroutes_by_turn
  ON model_reroutes(native_thread_id, native_turn_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_model_reroute
  ON model_reroutes(native_thread_id, native_turn_id, from_model, to_model, reason)
  WHERE contribution_id IS NULL;
CREATE TABLE IF NOT EXISTS workflow_cost_boundaries (
  workflow_id TEXT PRIMARY KEY, origin_action_id INTEGER REFERENCES actions(id),
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','closed')),
  frozen_amount TEXT, currency TEXT NOT NULL DEFAULT 'USD',
  frozen_summary TEXT,
  coverage TEXT, assumptions TEXT NOT NULL DEFAULT '[]',
  exclusions TEXT NOT NULL DEFAULT '[]', closed_at TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_cost_actions (
  workflow_id TEXT NOT NULL REFERENCES workflow_cost_boundaries(workflow_id),
  action_id INTEGER NOT NULL REFERENCES actions(id), causal_role TEXT NOT NULL,
  include_cost INTEGER NOT NULL DEFAULT 1 CHECK (include_cost IN (0, 1)),
  exclusion_reason TEXT,
  created_at TEXT NOT NULL, PRIMARY KEY (workflow_id, action_id)
);
CREATE TABLE IF NOT EXISTS workflow_cost_beads (
  workflow_id TEXT NOT NULL REFERENCES workflow_cost_boundaries(workflow_id),
  bead_id TEXT NOT NULL REFERENCES beads(bead_id), created_at TEXT NOT NULL,
  PRIMARY KEY (workflow_id, bead_id)
);
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
            self._seed_rate_cards()
            self._replace_invariant_triggers()
            self.connection.executescript(INVARIANT_TRIGGERS)
            for role in ROLE_CODES:
                self.connection.execute(
                    "INSERT OR IGNORE INTO role_counters(role, next_number) VALUES (?, 1)",
                    (role,),
                )

    def _seed_rate_cards(self) -> None:
        """Retain the dated public prices used by estimates, without refreshing history."""

        for model, input_rate, cached_rate, write_rate, output_rate in _RATE_CARDS:
            self.connection.execute(
                """INSERT OR IGNORE INTO api_rate_cards(
                       provider, model, processing_tier, currency,
                       input_per_million, cached_input_per_million,
                       cache_write_input_per_million, output_per_million,
                       long_context_threshold, long_context_input_multiplier,
                       long_context_output_multiplier, tier_multiplier, rules_json,
                       source_url, effective_at, captured_at
                   ) VALUES ('openai', ?, 'standard', 'USD', ?, ?, ?, ?,
                             272000, '2', '1.5', '1', ?, ?, ?, ?)""",
                (
                    model,
                    input_rate,
                    cached_rate,
                    write_rate,
                    output_rate,
                    json.dumps(
                        {
                            "long_context": (
                                "input > 272000: 2x ordinary/cached/cache-write input "
                                "and 1.5x output for that response"
                            ),
                            "service_tiers": {
                                "batch": "0.5",
                                "flex": "0.5",
                                "fast": "2",
                                "priority": "2",
                            },
                            "reasoning_output": "included in output; never added again",
                        },
                        sort_keys=True,
                    ),
                    f"https://developers.openai.com/api/docs/models/{model}",
                    _RATE_CAPTURED_AT,
                    _RATE_CAPTURED_AT,
                ),
            )
        for tool_name, unit, unit_rate, source_url in _TOOL_RATE_CARDS:
            self.connection.execute(
                """INSERT OR IGNORE INTO api_tool_rate_cards(
                       provider, tool_name, unit, currency, unit_rate,
                       rules_json, source_url, effective_at, captured_at
                   ) VALUES ('openai', ?, ?, 'USD', ?, ?, ?, ?, ?)""",
                (
                    tool_name,
                    unit,
                    unit_rate,
                    json.dumps(
                        {
                            "quantity": "one completed App Server webSearch item is one call",
                            "search_content_tokens": (
                                "already represented in model input usage when exposed; "
                                "never added as a second inferred quantity"
                            ),
                        },
                        sort_keys=True,
                    ),
                    source_url,
                    _RATE_CAPTURED_AT,
                    _RATE_CAPTURED_AT,
                ),
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

        reroute_table = self.connection.execute("""SELECT sql FROM sqlite_master
               WHERE type = 'table' AND name = 'model_reroutes'""").fetchone()
        if reroute_table is not None and "UNIQUE" in str(reroute_table[0]).upper():
            self.connection.executescript("""DROP INDEX IF EXISTS reroutes_by_turn;
                DROP INDEX IF EXISTS one_pending_model_reroute;
                ALTER TABLE model_reroutes RENAME TO obsolete_model_reroutes;
                CREATE TABLE model_reroutes (
                  id INTEGER PRIMARY KEY, native_thread_id TEXT NOT NULL,
                  native_turn_id TEXT NOT NULL, from_model TEXT NOT NULL,
                  to_model TEXT NOT NULL, reason TEXT NOT NULL,
                  contribution_id INTEGER REFERENCES cost_contributions(id),
                  observed_at TEXT NOT NULL
                );
                INSERT INTO model_reroutes(
                  id, native_thread_id, native_turn_id, from_model, to_model,
                  reason, contribution_id, observed_at
                ) SELECT id, native_thread_id, native_turn_id, from_model,
                         to_model, reason, contribution_id, observed_at
                    FROM obsolete_model_reroutes;
                DROP TABLE obsolete_model_reroutes;
                CREATE INDEX reroutes_by_turn
                  ON model_reroutes(native_thread_id, native_turn_id, id);
                CREATE UNIQUE INDEX one_pending_model_reroute
                  ON model_reroutes(native_thread_id, native_turn_id,
                                    from_model, to_model, reason)
                  WHERE contribution_id IS NULL;""")

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
            "workflow_cost_boundaries": {"frozen_summary": "TEXT"},
            "workflow_cost_actions": {
                "include_cost": "INTEGER NOT NULL DEFAULT 1 CHECK (include_cost IN (0, 1))",
                "exclusion_reason": "TEXT",
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
            connection.execute(
                """UPDATE cost_contributions SET action_id = ?,
                       attributed_action_id = ?
                   WHERE native_thread_id = ? AND native_turn_id = ?
                     AND action_id IS NULL""",
                (action_id, action_id, native_thread_id, native_turn_id),
            )
            self._propagate_helper_ownership(connection, timestamp)
        self._price_pending_response_contributions(native_thread_id, native_turn_id)

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
            connection.execute("""UPDATE cost_contributions AS cost
                   SET attributed_action_id = (
                         SELECT usage.attributed_action_id
                         FROM action_turn_usage usage
                         WHERE usage.native_thread_id = cost.native_thread_id
                           AND usage.native_turn_id = cost.native_turn_id)
                   WHERE cost.action_id IS NULL
                     AND cost.attributed_action_id IS NULL
                     AND EXISTS (
                       SELECT 1 FROM action_turn_usage usage
                       WHERE usage.native_thread_id = cost.native_thread_id
                         AND usage.native_turn_id = cost.native_turn_id
                         AND usage.attributed_action_id IS NOT NULL)""")
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
        self._price_pending_response_contributions(native_thread_id, native_turn_id)
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
        for pending in self.rows("""SELECT DISTINCT native_thread_id, native_turn_id
               FROM cost_contributions WHERE amount IS NULL
                 AND contribution_kind = 'model_response'"""):
            self._price_pending_response_contributions(
                str(pending["native_thread_id"]), str(pending["native_turn_id"])
            )

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
            if last:
                self._retain_response_cost(
                    params=params,
                    total=total,
                    response=last,
                    action_id=action_id,
                    attributed_action_id=attributed_action_id,
                    requested_model=model,
                    timestamp=timestamp,
                )
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
        response_identity = _first_string(
            params.get("responseId"),
            _nested_value(params.get("tokenUsage"), "last", "responseId"),
        )
        if last and (newest or response_identity is not None):
            self._retain_response_cost(
                params=params,
                total=total,
                response=last,
                action_id=action_id,
                attributed_action_id=attributed_action_id,
                requested_model=model,
                timestamp=timestamp,
            )
        return True

    def observe_model_reroute(self, params: dict[str, Any]) -> bool:
        """Retain the App Server model/rerouted event for the next response boundary."""

        values = tuple(
            params.get(name)
            for name in ("threadId", "turnId", "fromModel", "toModel", "reason")
        )
        if not all(isinstance(value, str) and value for value in values):
            return False
        before = int(self.connection.total_changes)
        self.execute(
            """INSERT OR IGNORE INTO model_reroutes(
                   native_thread_id, native_turn_id, from_model, to_model,
                   reason, observed_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (*values, utc_now()),
        )
        return int(self.connection.total_changes) > before

    def _retain_response_cost(
        self,
        *,
        params: dict[str, Any],
        total: dict[str, int],
        response: dict[str, int],
        action_id: int | None,
        attributed_action_id: int | None,
        requested_model: str | None,
        timestamp: str,
    ) -> None:
        """Freeze one response contribution identified by its cumulative boundary."""

        thread_id = str(params["threadId"])
        turn_id = str(params["turnId"])
        cumulative_identity = ":".join(
            str(total.get(key, "unknown")) for key in _USAGE_KEYS
        )
        response_id = _first_string(
            params.get("responseId"),
            _nested_value(params.get("tokenUsage"), "last", "responseId"),
        )
        source_key = (
            f"model-response:{thread_id}:{turn_id}:id:{response_id}"
            if response_id
            else f"model-response:{thread_id}:{turn_id}:totals:{cumulative_identity}"
        )
        if self.row(
            "SELECT 1 FROM cost_contributions WHERE source_key = ?", (source_key,)
        ):
            return
        sequence = self.row(
            """SELECT COALESCE(MAX(response_sequence), 0) + 1 AS sequence
               FROM cost_contributions
               WHERE native_thread_id = ? AND native_turn_id = ?
                 AND contribution_kind = 'model_response'""",
            (thread_id, turn_id),
        )
        reroute = self.row(
            """SELECT id, to_model FROM model_reroutes
               WHERE native_thread_id = ? AND native_turn_id = ?
                 AND contribution_id IS NULL
               ORDER BY id LIMIT 1""",
            (thread_id, turn_id),
        )
        effective_model = _first_string(
            reroute["to_model"] if reroute is not None else None,
            params.get("effectiveModel"),
            params.get("model"),
            _nested_value(params.get("tokenUsage"), "last", "effectiveModel"),
            _nested_value(params.get("tokenUsage"), "last", "model"),
        )
        tier = _first_string(
            params.get("effectiveServiceTier"),
            params.get("processingTier"),
            params.get("serviceTier"),
            _nested_value(params.get("tokenUsage"), "last", "serviceTier"),
        )
        estimate = self.estimate_response_cost(
            requested_model=requested_model,
            effective_model=effective_model,
            processing_tier=tier,
            input_tokens=response.get("input_tokens"),
            cached_input_tokens=response.get("cached_input_tokens"),
            cache_write_input_tokens=response.get("cache_write_input_tokens"),
            output_tokens=response.get("output_tokens"),
            reasoning_output_tokens=response.get("reasoning_output_tokens"),
        )
        # If observation began after the first response, retained contributions
        # cover only the response boundary that App Server actually supplied.
        response_gap = (
            any(
                total.get(key) != response.get(key)
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                )
            )
            and int(sequence["sequence"] if sequence else 1) == 1
        )
        exclusions = list(estimate["exclusions"])
        coverage = str(estimate["coverage"])
        if response_gap:
            coverage = "partial" if coverage != "invalid" else coverage
            exclusions.append(
                "earlier response boundaries preceded the first observed last-response snapshot"
            )
        self.execute(
            """INSERT OR IGNORE INTO cost_contributions(
                   contribution_kind, source_key, native_thread_id, native_turn_id,
                   response_sequence, action_id, attributed_action_id, provider,
                   requested_model, effective_model, processing_tier, currency,
                   rate_card_id, input_rate, cached_input_rate,
                   cache_write_input_rate, output_rate, tier_multiplier,
                   applied_rules, input_tokens, cached_input_tokens,
                   cache_write_input_tokens, output_tokens,
                   reasoning_output_tokens, ordinary_input_tokens,
                   input_amount, cached_input_amount, cache_write_input_amount,
                   output_amount, amount, coverage, assumptions, exclusions,
                   source_url, rate_captured_at, rate_effective_at,
                   finalized_at, created_at
               ) VALUES ('model_response', ?, ?, ?, ?, ?, ?, 'openai', ?, ?, ?,
                         'USD', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_key,
                thread_id,
                turn_id,
                int(sequence["sequence"] if sequence else 1),
                action_id,
                attributed_action_id,
                requested_model,
                estimate["effective_model"],
                estimate["processing_tier"],
                estimate.get("rate_card_id"),
                estimate.get("input_rate"),
                estimate.get("cached_input_rate"),
                estimate.get("cache_write_input_rate"),
                estimate.get("output_rate"),
                estimate.get("tier_multiplier"),
                json.dumps(estimate["applied_rules"]),
                response.get("input_tokens"),
                response.get("cached_input_tokens"),
                response.get("cache_write_input_tokens"),
                response.get("output_tokens"),
                response.get("reasoning_output_tokens"),
                estimate.get("ordinary_input_tokens"),
                estimate.get("input_amount"),
                estimate.get("cached_input_amount"),
                estimate.get("cache_write_input_amount"),
                estimate.get("output_amount"),
                estimate.get("amount"),
                coverage,
                json.dumps(estimate["assumptions"]),
                json.dumps(exclusions),
                estimate.get("source_url"),
                estimate.get("rate_captured_at"),
                estimate.get("rate_effective_at"),
                timestamp,
                timestamp,
            ),
        )
        retained = self.row(
            "SELECT id FROM cost_contributions WHERE source_key = ?",
            (source_key,),
        )
        if retained is not None:
            if reroute is not None:
                self.execute(
                    """UPDATE model_reroutes SET contribution_id = ?
                       WHERE id = ? AND contribution_id IS NULL""",
                    (retained["id"], reroute["id"]),
                )
        terminal = self.row(
            """SELECT terminal_at FROM action_turn_usage
               WHERE native_thread_id = ? AND native_turn_id = ?""",
            (thread_id, turn_id),
        )
        if terminal and terminal["terminal_at"] is not None:
            self.event(
                "cost_late_contribution_incorporated",
                "incorporated response telemetry observed after turn finalization",
                entity_type="cost_contribution",
                entity_id=retained["id"] if retained else source_key,
                detail={
                    "action_id": action_id,
                    "attributed_action_id": attributed_action_id,
                    "amount": estimate.get("amount"),
                    "coverage": coverage,
                },
            )
        self._reconcile_turn_cost_coverage(thread_id, turn_id)

    def _reconcile_turn_cost_coverage(
        self, native_thread_id: str, native_turn_id: str
    ) -> None:
        """Clear an initial boundary gap once explicit response IDs fill it."""

        usage = self.row(
            """SELECT total_input_tokens, total_cached_input_tokens,
                      total_cache_write_input_tokens, total_output_tokens
               FROM action_turn_usage WHERE native_thread_id = ?
                 AND native_turn_id = ?""",
            (native_thread_id, native_turn_id),
        )
        rows = self.rows(
            """SELECT * FROM cost_contributions WHERE native_thread_id = ?
                 AND native_turn_id = ? AND contribution_kind = 'model_response'""",
            (native_thread_id, native_turn_id),
        )
        if usage is None or not rows:
            return
        pairs = (
            ("input_tokens", "total_input_tokens"),
            ("cached_input_tokens", "total_cached_input_tokens"),
            ("cache_write_input_tokens", "total_cache_write_input_tokens"),
            ("output_tokens", "total_output_tokens"),
        )
        if any(
            usage[total_name] is None
            or any(row[value_name] is None for row in rows)
            or sum(int(row[value_name]) for row in rows) != int(usage[total_name])
            for value_name, total_name in pairs
        ):
            return
        boundary_gap = (
            "earlier response boundaries preceded the first observed "
            "last-response snapshot"
        )
        for row in rows:
            exclusions = [
                item
                for item in _json_string_union([row], "exclusions")
                if item != boundary_gap
            ]
            assumptions = _json_string_union([row], "assumptions")
            coverage = str(row["coverage"])
            if coverage != "invalid":
                coverage = "partial" if assumptions or exclusions else "complete"
            self.execute(
                """UPDATE cost_contributions SET exclusions = ?, coverage = ?
                   WHERE id = ?""",
                (json.dumps(exclusions), coverage, row["id"]),
            )

    def mark_open_usage_gap(self, reason: str) -> None:
        timestamp = utc_now()
        self.execute(
            """UPDATE action_turn_usage SET coverage = 'partial',
                   gap_reason = COALESCE(gap_reason, ?), updated_at = ?
               WHERE terminal_at IS NULL AND coverage != 'unavailable'""",
            (reason, timestamp),
        )

    def _price_pending_response_contributions(
        self, native_thread_id: str, native_turn_id: str
    ) -> None:
        """Complete a formerly unpriced response after late ownership/model binding."""

        usage = self.row(
            """SELECT model FROM action_turn_usage
               WHERE native_thread_id = ? AND native_turn_id = ?""",
            (native_thread_id, native_turn_id),
        )
        if usage is None or not usage.get("model"):
            return
        for contribution in self.rows(
            """SELECT * FROM cost_contributions
               WHERE native_thread_id = ? AND native_turn_id = ?
                 AND contribution_kind = 'model_response' AND amount IS NULL
                 AND effective_model IS NULL""",
            (native_thread_id, native_turn_id),
        ):
            estimate = self.estimate_response_cost(
                requested_model=str(usage["model"]),
                effective_model=None,
                processing_tier=contribution.get("processing_tier"),
                input_tokens=contribution.get("input_tokens"),
                cached_input_tokens=contribution.get("cached_input_tokens"),
                cache_write_input_tokens=contribution.get("cache_write_input_tokens"),
                output_tokens=contribution.get("output_tokens"),
                reasoning_output_tokens=contribution.get("reasoning_output_tokens"),
            )
            if estimate.get("amount") is None:
                continue
            prior_exclusions = _json_string_union([contribution], "exclusions")
            retained_exclusions = [
                reason
                for reason in prior_exclusions
                if "model" not in reason and "rate" not in reason
            ]
            coverage = (
                "partial"
                if retained_exclusions or estimate["coverage"] != "complete"
                else "complete"
            )
            self.execute(
                """UPDATE cost_contributions SET requested_model = ?,
                       effective_model = ?, processing_tier = ?, rate_card_id = ?,
                       input_rate = ?, cached_input_rate = ?,
                       cache_write_input_rate = ?, output_rate = ?,
                       tier_multiplier = ?, applied_rules = ?,
                       ordinary_input_tokens = ?, input_amount = ?,
                       cached_input_amount = ?, cache_write_input_amount = ?,
                       output_amount = ?, amount = ?, coverage = ?, assumptions = ?,
                       exclusions = ?, source_url = ?, rate_captured_at = ?,
                       rate_effective_at = ? WHERE id = ? AND amount IS NULL""",
                (
                    usage["model"],
                    estimate["effective_model"],
                    estimate["processing_tier"],
                    estimate["rate_card_id"],
                    estimate["input_rate"],
                    estimate["cached_input_rate"],
                    estimate["cache_write_input_rate"],
                    estimate["output_rate"],
                    estimate["tier_multiplier"],
                    json.dumps(estimate["applied_rules"]),
                    estimate["ordinary_input_tokens"],
                    estimate["input_amount"],
                    estimate["cached_input_amount"],
                    estimate["cache_write_input_amount"],
                    estimate["output_amount"],
                    estimate["amount"],
                    coverage,
                    json.dumps(estimate["assumptions"]),
                    json.dumps(retained_exclusions),
                    estimate["source_url"],
                    estimate["rate_captured_at"],
                    estimate["rate_effective_at"],
                    contribution["id"],
                ),
            )
            self.event(
                "cost_late_contribution_incorporated",
                "priced response after late causal ownership became available",
                entity_type="cost_contribution",
                entity_id=contribution["id"],
                detail={
                    "action_id": contribution.get("action_id"),
                    "attributed_action_id": contribution.get("attributed_action_id"),
                    "amount": estimate["amount"],
                    "coverage": coverage,
                },
            )

    def estimate_response_cost(
        self,
        *,
        requested_model: str | None,
        effective_model: str | None,
        processing_tier: str | None,
        input_tokens: int | None,
        cached_input_tokens: int | None,
        cache_write_input_tokens: int | None,
        output_tokens: int | None,
        reasoning_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Calculate an API-equivalent response estimate using one retained card."""

        assumptions: list[str] = []
        exclusions: list[str] = []
        rules: list[str] = []
        configured = _canonical_model(requested_model)
        effective = effective_model
        if effective is None and configured is not None:
            effective = requested_model or configured
            assumptions.append("effective model unavailable; used configured model")
        if effective is None:
            exclusions.append("effective model and usable configured model unavailable")

        normalized_tier = processing_tier.lower() if processing_tier else None
        if normalized_tier in {None, "auto"}:
            normalized_tier = "standard"
            assumptions.append(
                "effective processing tier unavailable; assumed standard"
            )
        assert normalized_tier is not None
        tier_multiplier = _TIER_MULTIPLIERS.get(normalized_tier)
        if tier_multiplier is None:
            exclusions.append(
                f"no authoritative multiplier for processing tier {normalized_tier}"
            )

        quantities = {
            "input": input_tokens,
            "cached input": cached_input_tokens,
            "cache-write input": cache_write_input_tokens,
            "output": output_tokens,
        }
        missing = [name for name, value in quantities.items() if value is None]
        if missing:
            exclusions.append("missing response quantities: " + ", ".join(missing))
        invalid: list[str] = []
        for name, value in quantities.items():
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                invalid.append(f"{name} tokens must be a nonnegative integer")
        if (
            input_tokens is not None
            and cached_input_tokens is not None
            and cache_write_input_tokens is not None
            and cached_input_tokens + cache_write_input_tokens > input_tokens
        ):
            invalid.append("cached plus cache-write input exceeds input tokens")
        if (
            output_tokens is not None
            and reasoning_output_tokens is not None
            and reasoning_output_tokens > output_tokens
        ):
            invalid.append("reasoning output exceeds output tokens")
        if reasoning_output_tokens is not None and (
            not isinstance(reasoning_output_tokens, int)
            or isinstance(reasoning_output_tokens, bool)
            or reasoning_output_tokens < 0
        ):
            invalid.append("reasoning output tokens must be a nonnegative integer")

        card = (
            self.row(
                """SELECT * FROM api_rate_cards
                   WHERE provider = 'openai' AND model = ?
                     AND processing_tier = 'standard'
                   ORDER BY effective_at DESC, id DESC LIMIT 1""",
                (_canonical_model(effective),),
            )
            if effective
            else None
        )
        if card is None and effective is not None:
            exclusions.append(f"no retained public rate for model {effective}")
        result: dict[str, Any] = {
            "provider": "openai",
            "requested_model": requested_model,
            "effective_model": effective,
            "processing_tier": normalized_tier,
            "currency": "USD",
            "coverage": "invalid" if invalid else "partial",
            "assumptions": assumptions,
            "exclusions": [*invalid, *exclusions],
            "applied_rules": rules,
            "reasoning_output_tokens": reasoning_output_tokens,
        }
        if card is None or missing or invalid or tier_multiplier is None:
            return result

        assert input_tokens is not None
        assert cached_input_tokens is not None
        assert cache_write_input_tokens is not None
        assert output_tokens is not None
        ordinary = input_tokens - cached_input_tokens - cache_write_input_tokens
        input_multiplier = Decimal("1")
        output_multiplier = Decimal("1")
        threshold = card.get("long_context_threshold")
        if threshold is not None and input_tokens > int(threshold):
            input_multiplier = Decimal(str(card["long_context_input_multiplier"]))
            output_multiplier = Decimal(str(card["long_context_output_multiplier"]))
            rules.append(f"long_context_input_over_{threshold}")
        if tier_multiplier != Decimal("1"):
            rules.append(f"service_tier_{normalized_tier}")
        divisor = Decimal("1000000")
        input_rate = Decimal(str(card["input_per_million"]))
        cached_rate = Decimal(str(card["cached_input_per_million"]))
        write_rate = Decimal(str(card["cache_write_input_per_million"]))
        output_rate = Decimal(str(card["output_per_million"]))
        input_amount = (
            Decimal(ordinary)
            * input_rate
            * input_multiplier
            * tier_multiplier
            / divisor
        )
        cached_amount = (
            Decimal(cached_input_tokens)
            * cached_rate
            * input_multiplier
            * tier_multiplier
            / divisor
        )
        write_amount = (
            Decimal(cache_write_input_tokens)
            * write_rate
            * input_multiplier
            * tier_multiplier
            / divisor
        )
        output_amount = (
            Decimal(output_tokens)
            * output_rate
            * output_multiplier
            * tier_multiplier
            / divisor
        )
        amount = input_amount + cached_amount + write_amount + output_amount
        result.update(
            {
                "coverage": "complete" if not assumptions else "partial",
                "rate_card_id": card["id"],
                "input_rate": _decimal_text(input_rate),
                "cached_input_rate": _decimal_text(cached_rate),
                "cache_write_input_rate": _decimal_text(write_rate),
                "output_rate": _decimal_text(output_rate),
                "tier_multiplier": _decimal_text(tier_multiplier),
                "ordinary_input_tokens": ordinary,
                "input_amount": _decimal_text(input_amount),
                "cached_input_amount": _decimal_text(cached_amount),
                "cache_write_input_amount": _decimal_text(write_amount),
                "output_amount": _decimal_text(output_amount),
                "amount": _decimal_text(amount),
                "source_url": card["source_url"],
                "rate_captured_at": card["captured_at"],
                "rate_effective_at": card["effective_at"],
            }
        )
        return result

    def record_tool_cost(
        self,
        *,
        source_key: str,
        tool_name: str,
        quantity: str | int,
        unit: str,
        unit_rate: str | None,
        source_url: str | None,
        native_thread_id: str | None = None,
        native_turn_id: str | None = None,
        action_id: int | None = None,
        attributed_action_id: int | None = None,
        provider: str = "openai",
        currency: str = "USD",
        assumption: str | None = None,
        applied_rules: Sequence[str] = (),
        rate_captured_at: str | None = None,
        rate_effective_at: str | None = None,
    ) -> dict[str, Any]:
        """Freeze an observed tool charge, or an explicit unknown-price exclusion."""

        existing = self.row(
            "SELECT * FROM cost_contributions WHERE source_key = ?", (source_key,)
        )
        if existing is not None:
            return existing
        timestamp = utc_now()
        assumptions = [assumption] if assumption else []
        exclusions: list[str] = []
        amount: str | None = None
        coverage = "complete"
        try:
            parsed_quantity = Decimal(str(quantity))
            if parsed_quantity < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            parsed_quantity = Decimal("0")
            coverage = "invalid"
            exclusions.append("tool quantity must be a nonnegative decimal")
        if unit_rate is None or source_url is None:
            coverage = "partial" if coverage != "invalid" else coverage
            exclusions.append("authoritative public tool rate or source unavailable")
        else:
            try:
                parsed_rate = Decimal(unit_rate)
                if parsed_rate < 0:
                    raise InvalidOperation
                if coverage != "invalid":
                    amount = _decimal_text(parsed_quantity * parsed_rate)
            except (InvalidOperation, ValueError):
                coverage = "invalid"
                exclusions.append("tool unit rate must be a nonnegative decimal")
        self.execute(
            """INSERT INTO cost_contributions(
                   contribution_kind, source_key, native_thread_id, native_turn_id,
                   action_id, attributed_action_id,
                   provider, currency, tool_name, quantity, unit, unit_rate,
                   amount, coverage, assumptions, exclusions, source_url,
                   rate_captured_at, rate_effective_at, finalized_at, created_at,
                   applied_rules
               ) VALUES ('tool_call', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_key,
                native_thread_id,
                native_turn_id,
                action_id,
                attributed_action_id if attributed_action_id is not None else action_id,
                provider,
                currency,
                tool_name,
                _decimal_text(parsed_quantity),
                unit,
                unit_rate,
                amount,
                coverage,
                json.dumps(assumptions),
                json.dumps(exclusions),
                source_url,
                rate_captured_at or (timestamp if source_url and unit_rate else None),
                rate_effective_at or (timestamp if source_url and unit_rate else None),
                timestamp,
                timestamp,
                json.dumps(list(applied_rules)),
            ),
        )
        retained = (
            self.row(
                "SELECT * FROM cost_contributions WHERE source_key = ?", (source_key,)
            )
            or {}
        )
        terminal = (
            self.row(
                """SELECT terminal_at FROM action_turn_usage
                   WHERE native_thread_id = ? AND native_turn_id = ?""",
                (native_thread_id, native_turn_id),
            )
            if native_thread_id is not None and native_turn_id is not None
            else None
        )
        if terminal is not None and terminal["terminal_at"] is not None:
            self.event(
                "cost_late_contribution_incorporated",
                "incorporated tool telemetry observed after turn finalization",
                entity_type="cost_contribution",
                entity_id=retained.get("id", source_key),
                detail={
                    "action_id": action_id,
                    "attributed_action_id": (
                        attributed_action_id
                        if attributed_action_id is not None
                        else action_id
                    ),
                    "amount": amount,
                    "coverage": coverage,
                },
            )
        return retained

    def record_observed_tool(
        self,
        *,
        source_key: str,
        tool_name: str,
        native_thread_id: str,
        native_turn_id: str,
        action_id: int | None = None,
        attributed_action_id: int | None = None,
    ) -> dict[str, Any]:
        """Resolve one observable separately priced tool against dated public rates."""

        card = self.row(
            """SELECT * FROM api_tool_rate_cards
               WHERE provider = 'openai' AND tool_name = ? AND unit = 'call'
               ORDER BY effective_at DESC, id DESC LIMIT 1""",
            (tool_name,),
        )
        return self.record_tool_cost(
            source_key=source_key,
            tool_name=tool_name,
            quantity=1,
            unit="call",
            unit_rate=str(card["unit_rate"]) if card is not None else None,
            source_url=str(card["source_url"]) if card is not None else None,
            native_thread_id=native_thread_id,
            native_turn_id=native_turn_id,
            action_id=action_id,
            attributed_action_id=attributed_action_id,
            rate_captured_at=(str(card["captured_at"]) if card is not None else None),
            rate_effective_at=(str(card["effective_at"]) if card is not None else None),
            applied_rules=(
                (
                    "one completed App Server webSearch item is one call",
                    "search content tokens are represented only by observed model input usage",
                )
                if card is not None
                else ()
            ),
        )

    def link_action_to_workflow(
        self,
        workflow_id: str,
        action_id: int,
        *,
        causal_role: str,
        origin_action_id: int | None = None,
        include_cost: bool = True,
        exclusion_reason: str | None = None,
    ) -> None:
        """Attach one action to an explicit causal boundary, idempotently."""

        timestamp = utc_now()
        self.execute(
            """INSERT OR IGNORE INTO workflow_cost_boundaries(
                   workflow_id, origin_action_id, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (workflow_id, origin_action_id, timestamp, timestamp),
        )
        self.execute(
            """INSERT INTO workflow_cost_actions(
                   workflow_id, action_id, causal_role, include_cost,
                   exclusion_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(workflow_id, action_id) DO UPDATE SET
                 causal_role = excluded.causal_role,
                 include_cost = MIN(workflow_cost_actions.include_cost,
                                    excluded.include_cost),
                 exclusion_reason = COALESCE(workflow_cost_actions.exclusion_reason,
                                             excluded.exclusion_reason)""",
            (
                workflow_id,
                action_id,
                causal_role,
                int(include_cost),
                exclusion_reason,
                timestamp,
            ),
        )

    def link_bead_to_workflow(self, workflow_id: str, bead_id: str) -> None:
        timestamp = utc_now()
        self.execute(
            """INSERT OR IGNORE INTO workflow_cost_boundaries(
                   workflow_id, created_at, updated_at) VALUES (?, ?, ?)""",
            (workflow_id, timestamp, timestamp),
        )
        self.execute(
            """INSERT OR IGNORE INTO workflow_cost_beads(
                   workflow_id, bead_id, created_at) VALUES (?, ?, ?)""",
            (workflow_id, bead_id, timestamp),
        )

    def finalize_workflow_cost(self, workflow_id: str) -> dict[str, Any]:
        """Freeze the all-in total only after the final acknowledgement terminated."""

        boundary = self.row(
            "SELECT * FROM workflow_cost_boundaries WHERE workflow_id = ?",
            (workflow_id,),
        )
        if boundary is None:
            raise StoreError(f"unknown workflow cost boundary {workflow_id}")
        if boundary["state"] == "closed":
            report = self.cost_report(workflow_id=workflow_id, group_by="workflow")
            report["frozen_amount"] = boundary["frozen_amount"]
            report["finalized"] = True
            report["_newly_finalized"] = False
            return report
        report = self.cost_report(workflow_id=workflow_id, group_by="workflow")
        group = report["groups"][0] if report["groups"] else None
        amount = group["attributed"]["amount"] if group else None
        coverage = group["coverage"] if group else "partial"
        assumptions = group["assumptions"] if group else []
        exclusions = group["exclusions"] if group else ["no observed contributions"]
        timestamp = utc_now()
        self.execute(
            """UPDATE workflow_cost_boundaries SET state = 'closed',
                   frozen_amount = ?, frozen_summary = ?, coverage = ?,
                   assumptions = ?, exclusions = ?, closed_at = ?, updated_at = ?
                   WHERE workflow_id = ?""",
            (
                amount,
                json.dumps(group, sort_keys=True),
                coverage,
                json.dumps(assumptions),
                json.dumps(exclusions),
                timestamp,
                timestamp,
                workflow_id,
            ),
        )
        report["frozen_amount"] = amount
        report["finalized"] = True
        report["_newly_finalized"] = True
        return report

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

    def action_cost_summary(self, action_id: int) -> dict[str, Any]:
        report = self.cost_report(action_id=action_id, group_by="action")
        if not report["groups"]:
            return {
                "direct_amount": None,
                "attributed_amount": None,
                "direct_display": None,
                "attributed_display": None,
                "coverage": "partial",
                "estimate_kind": "equivalent public OpenAI API charges",
                "not_actual_billing": True,
            }
        group = report["groups"][0]
        direct = group["direct"]["amount"]
        attributed = group["attributed"]["amount"]
        return {
            "direct_amount": direct,
            "attributed_amount": attributed,
            "direct_display": format_usd(direct),
            "attributed_display": format_usd(attributed),
            "direct": group["direct"],
            "attributed": group["attributed"],
            "coverage": group["coverage"],
            "response_count": group["attributed"]["response_count"],
            "tool_count": group["attributed"]["tool_count"],
            "assumptions": group["assumptions"],
            "exclusions": group["exclusions"],
            "rate_card_provenance": group["rate_card_provenance"],
            "estimate_kind": "equivalent public OpenAI API charges",
            "not_actual_billing": True,
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

    def cost_report(
        self,
        *,
        action_id: int | None = None,
        task_id: int | None = None,
        assignment_id: int | None = None,
        run_id: int | None = None,
        role: str | None = None,
        project_id: str | None = None,
        workflow_id: str | None = None,
        group_by: str = "action",
    ) -> dict[str, Any]:
        """Return frozen API-equivalent estimates; never refresh retained prices."""

        dimensions = {
            "action": "a.id",
            "task": "t.id",
            "assignment": "a.assignment_id",
            "run": "assn.run_id",
            "role": "t.role",
            "project": "t.project_id",
            "workflow": "wca.workflow_id",
        }
        result_keys = {
            "action": "id",
            "task": "task_id",
            "assignment": "assignment_id",
            "run": "run_id",
            "role": "role",
            "project": "project_id",
            "workflow": "workflow_id",
        }
        if group_by not in dimensions:
            raise StoreError(f"unsupported cost grouping: {group_by}")
        filters: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("a.id", action_id),
            ("t.id", task_id),
            ("a.assignment_id", assignment_id),
            ("assn.run_id", run_id),
            ("t.role", role),
            ("t.project_id", project_id),
            ("wca.workflow_id", workflow_id),
        ):
            if value is not None:
                filters.append(f"{column} = ?")
                values.append(value)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        actions = self.rows(
            f"""SELECT DISTINCT a.id, a.task_id, a.assignment_id, a.kind, a.state,
                       t.role, t.project_id, assn.run_id, wca.workflow_id,
                       wca.include_cost, wca.exclusion_reason
                FROM actions a JOIN tasks t ON t.id = a.task_id
                LEFT JOIN assignments assn ON assn.id = a.assignment_id
                LEFT JOIN workflow_cost_actions wca ON wca.action_id = a.id
                {where} ORDER BY a.id""",
            values,
        )
        groups: dict[Any, list[dict[str, Any]]] = {}
        for action in actions:
            key = action.get(result_keys[group_by])
            if key is not None:
                groups.setdefault(key, []).append(action)
        rendered: list[dict[str, Any]] = []
        selected_contributions: dict[int, dict[str, Any]] = {}
        for key, members in groups.items():
            ids = sorted({int(member["id"]) for member in members})
            included_ids = (
                sorted(
                    {
                        int(member["id"])
                        for member in members
                        if member.get("include_cost") != 0
                    }
                )
                if group_by == "workflow"
                else ids
            )
            marks = ",".join("?" for _ in included_ids)
            boundary = (
                self.row(
                    """SELECT state, frozen_summary, closed_at
                       FROM workflow_cost_boundaries WHERE workflow_id = ?""",
                    (key,),
                )
                if group_by == "workflow"
                else None
            )
            closed_filter = (
                " AND created_at <= ?"
                if boundary and boundary["state"] == "closed"
                else ""
            )
            contribution_values: list[Any] = included_ids + included_ids
            if closed_filter:
                assert boundary is not None
                contribution_values.append(boundary["closed_at"])
            contributions = (
                self.rows(
                    f"""SELECT * FROM cost_contributions
                        WHERE (action_id IN ({marks}) OR attributed_action_id IN ({marks}))
                        {closed_filter}
                        ORDER BY id""",
                    contribution_values,
                )
                if included_ids
                else []
            )
            for contribution in contributions:
                selected_contributions[int(contribution["id"])] = contribution
            direct = [row for row in contributions if row["action_id"] in included_ids]
            attributed = [
                row
                for row in contributions
                if row["attributed_action_id"] in included_ids
            ]
            relevant = attributed or direct
            assumptions = _json_string_union(relevant, "assumptions")
            exclusions = _json_string_union(relevant, "exclusions")
            allocation_exclusions = (
                sorted(
                    {
                        str(member["exclusion_reason"])
                        for member in members
                        if member.get("include_cost") == 0
                        and member.get("exclusion_reason")
                    }
                )
                if group_by == "workflow"
                else []
            )
            exclusions.extend(allocation_exclusions)
            if not relevant:
                exclusions.append("no priced response or tool contribution observed")
            coverage = _cost_coverage(relevant, expected=bool(members))
            directly_priced = {
                int(row["action_id"])
                for row in direct
                if row["action_id"] is not None
                and row["contribution_kind"] == "model_response"
            }
            missing_action_boundaries = sorted(set(included_ids) - directly_priced)
            usage_gaps = (
                self.row(
                    f"""SELECT COUNT(*) AS count FROM action_turn_usage
                        WHERE (action_id IN ({marks}) OR attributed_action_id IN ({marks}))
                          AND coverage NOT IN ('complete','observed')""",
                    included_ids + included_ids,
                )
                if included_ids
                else None
            )
            unmatched_reroutes = (
                self.row(
                    f"""SELECT COUNT(*) AS count FROM model_reroutes reroute
                        JOIN action_turn_usage usage
                          ON usage.native_thread_id = reroute.native_thread_id
                         AND usage.native_turn_id = reroute.native_turn_id
                        WHERE reroute.contribution_id IS NULL
                          AND (usage.action_id IN ({marks})
                               OR usage.attributed_action_id IN ({marks}))
                          {('AND reroute.observed_at <= ?' if closed_filter else '')}""",
                    contribution_values,
                )
                if included_ids
                else None
            )
            if allocation_exclusions:
                coverage = "partial" if coverage != "invalid" else coverage
            if missing_action_boundaries:
                coverage = "partial" if coverage != "invalid" else coverage
                exclusions.append(
                    "actions without priced response boundaries: "
                    + json.dumps(missing_action_boundaries)
                )
            if usage_gaps and int(usage_gaps["count"]):
                coverage = "partial" if coverage != "invalid" else coverage
                exclusions.append("raw token telemetry coverage is not complete")
            if unmatched_reroutes and int(unmatched_reroutes["count"]):
                coverage = "partial" if coverage != "invalid" else coverage
                exclusions.append(
                    "model reroute observed without a following response boundary"
                )
            rendered.append(
                {
                    "group_by": group_by,
                    "group": key,
                    "action_ids": ids,
                    "contributing_action_ids": included_ids,
                    "currency": "USD",
                    "estimate_kind": "equivalent public OpenAI API charges",
                    "not_actual_billing": True,
                    "direct": _sum_costs(direct),
                    "attributed": _sum_costs(attributed),
                    "coverage": coverage,
                    "assumptions": assumptions,
                    "exclusions": exclusions,
                    "rate_card_provenance": _rate_provenance(relevant),
                }
            )
        contributions = list(selected_contributions.values())
        if group_by == "workflow":
            for index, group in enumerate(rendered):
                boundary = self.row(
                    """SELECT state, frozen_summary FROM workflow_cost_boundaries
                       WHERE workflow_id = ?""",
                    (group["group"],),
                )
                if (
                    boundary
                    and boundary["state"] == "closed"
                    and boundary["frozen_summary"]
                ):
                    frozen = json.loads(boundary["frozen_summary"])
                    frozen["finalized"] = True
                    rendered[index] = frozen
        limit = 200
        return {
            "filters": {
                "action_id": action_id,
                "task_id": task_id,
                "assignment_id": assignment_id,
                "run_id": run_id,
                "role": role,
                "project_id": project_id,
                "workflow_id": workflow_id,
            },
            "group_by": group_by,
            "estimate_kind": "equivalent public OpenAI API charges",
            "not_actual_billing_or_subscription_usage": True,
            "groups": rendered,
            "contributions": contributions[:limit],
            "contributions_truncated": len(contributions) > limit,
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


def _first_string(*values: Any) -> str | None:
    return next((value for value in values if isinstance(value, str) and value), None)


def _nested_value(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _canonical_model(model: str | None) -> str | None:
    if not model:
        return None
    normalized = model.lower()
    if normalized in _MODEL_ALIASES:
        return _MODEL_ALIASES[normalized]
    for seeded, *_ in _RATE_CARDS:
        if normalized == seeded or normalized.startswith(seeded + "-"):
            return seeded
    return normalized


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_usd(amount: str | Decimal | None) -> str | None:
    """Deterministically display USD, including a visible positive sub-cent value."""

    if amount is None:
        return None
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if Decimal("0") < value < Decimal("0.01"):
        return "<$0.01"
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${rounded:.2f}"


def _sum_costs(contributions: list[dict[str, Any]]) -> dict[str, Any]:
    known = [
        Decimal(str(row["amount"]))
        for row in contributions
        if row["amount"] is not None
    ]
    amount = _decimal_text(sum(known, Decimal("0"))) if known else None
    token_rows = [
        row for row in contributions if row["contribution_kind"] == "model_response"
    ]
    tool_rows = [
        row for row in contributions if row["contribution_kind"] == "tool_call"
    ]
    component_names = (
        "input_amount",
        "cached_input_amount",
        "cache_write_input_amount",
        "output_amount",
    )
    components: dict[str, str | None] = {}
    for name in component_names:
        values = [
            Decimal(str(row[name])) for row in token_rows if row[name] is not None
        ]
        components[name] = _decimal_text(sum(values, Decimal("0"))) if values else None
    tool_values = [
        Decimal(str(row["amount"])) for row in tool_rows if row["amount"] is not None
    ]
    components["tool_amount"] = (
        _decimal_text(sum(tool_values, Decimal("0"))) if tool_values else None
    )
    return {
        "amount": amount,
        "display": format_usd(amount),
        "components": components,
        "contribution_count": len(contributions),
        "response_count": len(token_rows),
        "tool_count": len(tool_rows),
        "action_count": len(
            {
                row["attributed_action_id"] or row["action_id"]
                for row in contributions
                if row["attributed_action_id"] is not None
                or row["action_id"] is not None
            }
        ),
    }


def _json_string_union(rows: list[dict[str, Any]], column: str) -> list[str]:
    values: list[str] = []
    for row in rows:
        try:
            items = json.loads(row.get(column) or "[]")
        except (TypeError, json.JSONDecodeError):
            items = [str(row.get(column))]
        for item in items if isinstance(items, list) else [items]:
            rendered = str(item)
            if rendered not in values:
                values.append(rendered)
    return values


def _cost_coverage(rows: list[dict[str, Any]], *, expected: bool) -> str:
    if not rows:
        return "partial" if expected else "partial"
    states = {str(row["coverage"]) for row in rows}
    if "invalid" in states:
        return "invalid"
    return "complete" if states == {"complete"} else "partial"


def _rate_provenance(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    provenance: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = (
            row.get("provider"),
            row.get("effective_model"),
            row.get("processing_tier"),
            row.get("tool_name"),
            row.get("unit"),
            row.get("unit_rate"),
            row.get("source_url"),
            row.get("rate_captured_at"),
        )
        if key in seen or row.get("source_url") is None:
            continue
        seen.add(key)
        provenance.append(
            {
                "provider": row.get("provider"),
                "model": row.get("effective_model"),
                "processing_tier": row.get("processing_tier"),
                "tool": row.get("tool_name"),
                "unit": row.get("unit"),
                "unit_rate": row.get("unit_rate"),
                "source_url": row.get("source_url"),
                "captured_at": row.get("rate_captured_at"),
                "effective_at": row.get("rate_effective_at"),
            }
        )
    return provenance


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
