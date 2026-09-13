"""Action-specific finish validators shared by prompts and the CLI."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class OutcomeError(ValueError):
    """A finish result is incompatible with the bound action."""


ALLOWED: dict[str, set[str]] = {
    "implement": {"ready_for_review", "checkpointed", "blocked"},
    "correct": {
        "ready_for_review",
        "permitted_repair_complete",
        "checkpointed",
        "blocked",
    },
    "review": {"approved", "changes_requested", "incomplete", "exception"},
    "archon": {"decisions", "deferred"},
    "weaver": {"intake_complete", "future_plan", "blocked"},
    "specialist": {"report", "evidence_needed", "blocked"},
    "interview": {"interview_answer", "blocked"},
}

ARCHON_DECISIONS = {
    "approve",
    "hold",
    "release_hold",
    "resolve_operation",
    "cancel_run",
    "resolve_escalation",
    "set_priority",
    "suspend_policy",
    "request_specialist",
    "set_models",
    "retire_archon",
}


def _required_text(options: dict[str, Any], name: str) -> str:
    value = options.get(name)
    if not isinstance(value, str) or not value.strip():
        raise OutcomeError(f"--{name.replace('_', '-')} is required")
    return value.strip()


def _json_file(options: dict[str, Any], name: str = "input") -> dict[str, Any]:
    path = Path(_required_text(options, name)).expanduser()
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OutcomeError(f"cannot read valid JSON from {path}: {error}") from error
    if not isinstance(result, dict):
        raise OutcomeError(f"{path} must contain a JSON object")
    return result


def validate_outcome(
    action_kind: str, outcome_kind: str, options: dict[str, Any]
) -> dict[str, Any]:
    """Validate one concrete finish form and return its bounded payload."""

    allowed = ALLOWED.get(action_kind)
    if allowed is None or outcome_kind not in allowed:
        raise OutcomeError(f"{outcome_kind!r} is incompatible with {action_kind!r}")
    if outcome_kind in {"ready_for_review", "checkpointed", "future_plan"}:
        return {"evidence": _required_text(options, "evidence")}
    if outcome_kind == "permitted_repair_complete":
        return {
            "repair_category": _required_text(options, "repair_category"),
            "repair_rationale": _required_text(options, "repair_rationale"),
            "evidence": _required_text(options, "evidence"),
        }
    if outcome_kind in {"blocked", "exception"}:
        return {"reason": _required_text(options, "reason")}
    if outcome_kind == "approved":
        payload = _json_file(options)
        assessment = payload.get("assessment")
        if not isinstance(assessment, str) or not assessment.strip():
            raise OutcomeError("approval input requires a nonempty assessment")
        minor_fixes = payload.get("minor_fixes")
        if not isinstance(minor_fixes, list):
            raise OutcomeError("approval input requires an explicit minor_fixes list")
        for fix in minor_fixes:
            if not isinstance(fix, dict) or not all(
                isinstance(fix.get(field), str) and fix[field].strip()
                for field in ("problem", "evidence", "requested_change")
            ):
                raise OutcomeError(
                    "each minor fix requires problem, evidence, and requested_change"
                )
        repair_permissions = payload.get("repair_permissions", [])
        if not isinstance(repair_permissions, list) or not all(
            isinstance(item, str) and item.strip() for item in repair_permissions
        ):
            raise OutcomeError("repair_permissions must contain nonempty strings")
        return {
            "assessment": assessment.strip(),
            "minor_fixes": minor_fixes,
            "repair_permissions": repair_permissions,
        }
    if outcome_kind in {
        "changes_requested",
        "incomplete",
        "decisions",
        "evidence_needed",
        "interview_answer",
    }:
        payload = _json_file(options)
        if outcome_kind == "decisions":
            decisions = payload.get("decisions")
            if not isinstance(decisions, list):
                raise OutcomeError("decisions input requires a decisions list")
            for decision in decisions:
                if not isinstance(decision, dict):
                    raise OutcomeError("each Archon decision must be an object")
                kind = decision.get("decision")
                if kind not in ARCHON_DECISIONS:
                    raise OutcomeError(f"unsupported Archon decision: {kind!r}")
            handled = payload.get("handled_update_ids")
            if handled is not None and (
                not isinstance(handled, list)
                or not all(isinstance(item, int) for item in handled)
            ):
                raise OutcomeError("handled_update_ids must be an integer list")
        if outcome_kind == "changes_requested":
            findings = payload.get("findings")
            if not isinstance(findings, list) or not findings:
                raise OutcomeError("changes-requested input requires findings")
            for finding in findings:
                if not isinstance(finding, dict) or not all(
                    isinstance(finding.get(field), str) and finding[field].strip()
                    for field in ("problem", "evidence", "required_change")
                ):
                    raise OutcomeError(
                        "each review finding requires problem, evidence, and required_change"
                    )
        if outcome_kind == "incomplete":
            missing = payload.get("missing_evidence")
            if (
                not isinstance(missing, list)
                or not missing
                or not all(isinstance(item, str) and item.strip() for item in missing)
            ):
                raise OutcomeError("incomplete input requires missing_evidence strings")
        if outcome_kind == "evidence_needed" and not isinstance(
            payload.get("requests"), list
        ):
            raise OutcomeError("evidence-needed input requires a requests list")
        if outcome_kind == "evidence_needed":
            for request in payload["requests"]:
                if not isinstance(request, dict) or not all(
                    isinstance(request.get(field), str) and request[field].strip()
                    for field in ("subject", "question")
                ):
                    raise OutcomeError(
                        "each evidence request requires subject and question"
                    )
        if outcome_kind == "interview_answer":
            if (
                not isinstance(payload.get("answer"), str)
                or not payload["answer"].strip()
            ):
                raise OutcomeError("interview answer requires nonempty answer")
            if not isinstance(payload.get("evidence"), list) or not all(
                isinstance(item, str) and item.strip() for item in payload["evidence"]
            ):
                raise OutcomeError("interview answer requires an evidence list")
        return payload
    if outcome_kind == "report":
        payload = _json_file(options)
        if (
            not isinstance(payload.get("summary"), str)
            or not payload["summary"].strip()
        ):
            raise OutcomeError("report input requires a nonempty summary")
        if not isinstance(payload.get("coverage"), list) or not all(
            isinstance(item, str) and item.strip() for item in payload["coverage"]
        ):
            raise OutcomeError("report input requires an explicit coverage list")
        if not isinstance(payload.get("findings"), list):
            raise OutcomeError("report input requires an explicit findings list")
        for finding in payload["findings"]:
            if not isinstance(finding, dict):
                raise OutcomeError("each finding must be an object")
            activation = finding.get("activation", "pending")
            if activation not in {"pending", "future"}:
                raise OutcomeError("finding activation must be pending or future")
            if activation == "future" and not (
                isinstance(finding.get("deferral_reason"), str)
                and finding["deferral_reason"].strip()
            ):
                raise OutcomeError("future findings require explicit deferral_reason")
            for field in (
                "identity",
                "problem",
                "evidence",
                "expected_benefit",
                "project",
                "acceptance_criteria",
            ):
                if (
                    not isinstance(finding.get(field), str)
                    or not finding[field].strip()
                ):
                    raise OutcomeError(f"each finding requires nonempty {field}")
            identity = finding["identity"]
            if (
                len(identity) > 200
                or re.fullmatch(r"[A-Za-z0-9._/-]+", identity) is None
            ):
                raise OutcomeError(
                    "finding identity must be at most 200 URL-safe path characters"
                )
        return payload
    if outcome_kind == "deferred":
        payload = _json_file(options)
        conditions = {
            "capacity",
            "dependency",
            "hold",
            "operator_change",
            "next_check_at",
        }
        if not conditions.intersection(payload):
            raise OutcomeError(
                "deferral input requires a concrete reactivation condition"
            )
        if "capacity" in payload and payload["capacity"] is not True:
            raise OutcomeError("capacity reactivation must be true")
        if "dependency" in payload and (
            not isinstance(payload["dependency"], str)
            or not payload["dependency"].strip()
        ):
            raise OutcomeError("dependency reactivation must name a bead")
        if "hold" in payload and (
            not isinstance(payload["hold"], int) or isinstance(payload["hold"], bool)
        ):
            raise OutcomeError("hold reactivation must name an integer hold ID")
        if "operator_change" in payload and payload["operator_change"] is not True:
            raise OutcomeError("operator_change reactivation must be true")
        if "next_check_at" in payload:
            value = payload["next_check_at"]
            if not isinstance(value, str):
                raise OutcomeError("next_check_at must be an ISO timestamp")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise OutcomeError("next_check_at must be an ISO timestamp") from error
            if parsed.tzinfo is None:
                raise OutcomeError("next_check_at must include a timezone")
        return {"reason": _required_text(options, "reason"), "reactivation": payload}
    if outcome_kind == "intake_complete":
        return {}
    raise OutcomeError(f"unsupported outcome: {outcome_kind}")


def finish_examples(action_kind: str) -> dict[str, Any]:
    """Return the actual file contents, keyed by outcome, for executable examples."""

    contracts: dict[str, Any] = {
        "review": {
            "approved": {
                "assessment": "Why the exact candidate satisfies the approved scope",
                "minor_fixes": [
                    {
                        "problem": "Nonblocking issue that may ship unchanged",
                        "evidence": "file/line or observed result",
                        "requested_change": "Bounded follow-up improvement",
                    }
                ],
                "repair_permissions": [],
            },
            "changes_requested": {
                "findings": [
                    {
                        "problem": "specific defect",
                        "evidence": "file/line, command, or observed result",
                        "required_change": "bounded correction",
                    }
                ]
            },
            "incomplete": {"missing_evidence": ["specific missing proof"]},
        },
        "archon": {
            "decisions": [
                {
                    "decision": "approve",
                    "project": "project-id",
                    "beads": ["bead-id"],
                    "scope": {"bead-id": "exact scope"},
                },
                {
                    "decision": "hold",
                    "scope": "project",
                    "target": "project-id",
                    "reason": "why",
                    "release_condition": "exact condition",
                },
                {"decision": "release_hold", "hold_id": 1},
                {
                    "decision": "resolve_operation",
                    "operation_id": 1,
                    "resolution": "confirmed_unsent",
                    "evidence": "how the external result was established",
                },
                {"decision": "cancel_run", "run_id": 1, "reason": "why"},
                {
                    "decision": "resolve_escalation",
                    "assignment_id": 1,
                    "resolution": "complete_non_code",
                    "reason": "why the approved scope needs no repository candidate",
                    "evidence": "concrete proof that the approved result is complete",
                },
                {"decision": "set_priority", "run_id": 1, "priority": 10},
                {
                    "decision": "suspend_policy",
                    "kind": "inquisitor",
                    "scope": None,
                },
                {
                    "decision": "request_specialist",
                    "kind": "inquisitor",
                    "projects": ["project-id"],
                    "prompt": "question",
                },
                {
                    "decision": "set_models",
                    "bead_id": "bead-id",
                    "executor_model": "model",
                    "executor_reasoning_effort": "effort",
                    "overseer_model": "model",
                    "overseer_reasoning_effort": "effort",
                    "rationale": "why these settings are needed",
                },
                {
                    "decision": "retire_archon",
                    "reason": "why",
                },
            ],
            "handled_update_ids": [1],
            "global_limit": 2,
            "project_limits": {"project-id": 1},
            "recurring_policies": [
                {"kind": "inquisitor", "scope": "project-id", "cadence_seconds": 86400}
            ],
        },
        "specialist": {
            "report": {
                "summary": "bounded conclusion",
                "coverage": ["evidence examined"],
                "findings": [
                    {
                        "identity": "review-handoff-loses-evidence",
                        "problem": "demonstrated problem",
                        "evidence": "concrete evidence",
                        "expected_benefit": "specific expected improvement",
                        "project": "project-id",
                        "acceptance_criteria": "testable completion condition",
                    }
                ],
            },
            "evidence_needed": {
                "requests": [
                    {
                        "subject": "task title or thread id",
                        "question": "one concrete question",
                    }
                ]
            },
        },
        "interview": {
            "interview_answer": {"answer": "concrete answer", "evidence": ["reference"]}
        },
    }
    contract = contracts.get(action_kind, {})
    if action_kind == "archon":
        return {"decisions": contract, "deferred": {"capacity": True}}
    return contract


def finish_contract(action_kind: str, *, interviews_allowed: bool = True) -> str:
    examples = finish_examples(action_kind)
    if not interviews_allowed:
        examples.pop("evidence_needed", None)
    lines = (
        [
            "Choose one outcome. Each example below is the top-level content of its "
            "--input file; do not wrap it in the outcome name. Replace example values "
            "with your actual evidence and IDs."
        ]
        if examples
        else []
    )
    for outcome, contents in examples.items():
        lines.append(
            f"{outcome} input file:\n```json\n"
            + json.dumps(contents, indent=2, sort_keys=True)
            + "\n```"
        )
    if action_kind == "archon":
        lines.append(
            "The decisions list above is a catalog, not a batch to copy in full: "
            "include only intended decisions. Omit capacity or recurring-policy fields "
            "unless changing them. Approvals require exact stored scope. Holds accept "
            "global, project, run, or assignment scope; target is required except for "
            "global. Escalation resolution is retry, rescope (with scope), cancel, "
            "or complete_non_code (with evidence) when the approved result is fully "
            "satisfied without a repository candidate. "
            "An exhausted external operation must use resolve_operation with "
            "observed_success, observed_failure, or confirmed_unsent and concrete "
            "evidence; observed success also requires the kind-specific result identity. "
            "Specialist kind is sage or inquisitor. Deferral needs one concrete trigger: "
            '{"capacity": true}, {"dependency": "bead-id"}, {"hold": 1}, '
            '{"operator_change": true}, or {"next_check_at": "2026-10-01T12:00:00Z"}.'
        )
    if action_kind == "review":
        lines.append(
            "Approval's minor_fixes list is explicit and may be empty. Each item is "
            "a nonblocking follow-up request retained with the approval; it does not "
            "delay promotion or return the assignment to Executor. Put every change "
            "required before promotion in changes_requested instead. "
            "repair_permissions is optional and may contain only narrow replacement "
            "categories that Executor may apply without another review."
        )
    if action_kind == "weaver":
        lines.append(
            'For substantial approved plans, `fulcrum intake --input "/absolute/tasks.json"` accepts this graph (replace all example values). Keep intake_key stable across retries. Each task requires title and description, inherits project, and may include activation, context, depends_on, plan_id, plan_commit, executor_model, executor_reasoning_effort, overseer_model, and overseer_reasoning_effort. depends_on may name an earlier graph task\'s intake_key or an existing Beads ID. Include the approved plan reference on every planned task.\n```json\n{"project":"project-id","intake_key":"plan-name","tasks":[{"intake_key":"plan-name:first","title":"Bounded outcome","description":"Change, scope, acceptance and validation","plan_id":"plan-name","plan_commit":"actual-approved-commit","context":["/absolute/brain/plans/plan-name.md"],"depends_on":[]}]}\n```'
        )
    if action_kind == "specialist":
        lines.append(
            "An empty findings list is valid. Optional report fields may hold observations, "
            "limitations, and unresolved questions. A finding may include existing_bead_id "
            "for an inspected matching issue. Omit activation for pending work; use "
            "activation: future only with a nonempty deferral_reason explaining explicit "
            "deferral authority. identity is a descriptive problem key, not a hash."
        )
    if action_kind in {"implement", "correct"}:
        lines.append(
            "--evidence names an absolute path to a readable text file containing the authored evidence; no JSON wrapper is needed."
        )
    if action_kind == "weaver":
        lines.append(
            "For future_plan only, --evidence is the absolute path to the saved "
            "plan document; do not wrap the plan in JSON. intake_complete and "
            "blocked do not use an evidence file."
        )
    return "\n\n".join(lines)


def finish_syntax(action_kind: str, *, interviews_allowed: bool = True) -> str:
    """Complete commands, one per outcome, with explicit selection guidance."""
    commands = {
        "ready_for_review": (
            '--evidence "/absolute/evidence.md"',
            "Implementation or correction is ready for independent review.",
        ),
        "checkpointed": (
            '--evidence "/absolute/evidence.md"',
            "Work is intentionally paused and preserved, with unfinished validation identified.",
        ),
        "blocked": (
            '--reason "Observed blocker and decision needed"',
            "Cannot continue within the authorized scope or available evidence.",
        ),
        "permitted_repair_complete": (
            '--repair-category "allowed_category" --repair-rationale "Why the concrete repair qualifies" --evidence "/absolute/evidence.md"',
            "Repair clearly fits a retained explicit permission and has been validated.",
        ),
        "approved": (
            '--input "/absolute/approval.json"',
            "The exact candidate is ready for promotion; minor fixes are nonblocking follow-up requests.",
        ),
        "changes_requested": (
            '--input "/absolute/findings.json"',
            "Source has concrete blocking defects.",
        ),
        "incomplete": (
            '--input "/absolute/missing.json"',
            "Specific evidence is missing; no substantive defect is established.",
        ),
        "exception": (
            '--reason "Review boundary and decision needed"',
            "A review exception needs adjudication.",
        ),
        "decisions": (
            '--input "/absolute/decisions.json"',
            "Submit supported scheduling decisions and acknowledge the frozen update IDs.",
        ),
        "deferred": (
            '--reason "Why this batch must wait" --input "/absolute/reactivation.json"',
            "Defer the batch until a concrete trigger.",
        ),
        "intake_complete": (
            "",
            "All requested writable intake is retained; do not wait for remote synchronization.",
        ),
        "future_plan": (
            '--evidence "/absolute/plan-reference.md"',
            "An approved plan has been saved with explicit future activation.",
        ),
        "report": (
            '--input "/absolute/report.json"',
            "Analysis is complete, including an explicit findings list.",
        ),
        "evidence_needed": (
            '--input "/absolute/requests.json"',
            "Sage requests its single interview round, then ends this action.",
        ),
        "interview_answer": (
            '--input "/absolute/answer.json"',
            "Answer the current debrief question once.",
        ),
    }
    if action_kind not in ALLOWED:
        raise OutcomeError(f"unknown action kind: {action_kind}")
    if action_kind == "weaver":
        return "\n\n".join(
            [
                "When writable authoring ends, run exactly one command:",
                "Task intake succeeded: every requested task or active plan task "
                "is retained.\n`fulcrum finish intake_complete`",
                "Future plan succeeded: the approved plan was saved and explicitly "
                "deferred.\n`fulcrum finish future_plan --evidence "
                '"/absolute/path/to/plan.md"`',
                "Authoring is blocked: required scope or evidence is unavailable.\n"
                '`fulcrum finish blocked --reason "Observed blocker and decision '
                'needed"`',
            ]
        )
    if action_kind == "correct":
        return "\n\n".join(
            [
                "# Finish\n\nRun exactly one:",
                "Needs independent review:\n"
                '`fulcrum finish ready_for_review --evidence "/absolute/evidence.md"`',
                "Correction only, when the change clearly fits Repair permission:\n"
                '`fulcrum finish permitted_repair_complete --repair-category "allowed_category" '
                '--repair-rationale "Why the concrete repair qualifies" --evidence '
                '"/absolute/evidence.md"`',
                "Intentionally paused with unfinished work identified:\n"
                '`fulcrum finish checkpointed --evidence "/absolute/evidence.md"`',
                "Blocked on a scope or authority decision:\n"
                '`fulcrum finish blocked --reason "Observed blocker and decision needed"`',
                "If Repair permission is unclear, use `ready_for_review`.",
            ]
        )
    if action_kind == "review":
        return "\n\n".join(
            [
                "# Finish\n\nRun exactly one:",
                "Ready for promotion, with any nonblocking minor fixes recorded:\n"
                '`fulcrum finish approved --input "/absolute/approval.json"`',
                "Blocking source defects require correction before promotion:\n"
                '`fulcrum finish changes_requested --input "/absolute/findings.json"`',
                "No defect established because specific evidence is missing:\n"
                '`fulcrum finish incomplete --input "/absolute/missing.json"`',
                "Review authority or scope needs adjudication:\n"
                '`fulcrum finish exception --reason "Review boundary and decision needed"`',
                "Use approval only when the submitted candidate may be promoted unchanged. "
                "A minor fix never blocks this delivery.",
            ]
        )
    rows = []
    for outcome, (arguments, purpose) in commands.items():
        if outcome not in ALLOWED[action_kind] or (
            outcome == "evidence_needed" and not interviews_allowed
        ):
            continue
        rows.append(
            f"{purpose}\n`fulcrum finish {outcome} {arguments}`".replace(" `", "`")
        )
    return "\n\n".join(rows)
