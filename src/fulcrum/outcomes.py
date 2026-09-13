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
        repairs = options.get("allow_repair") or []
        if not isinstance(repairs, list) or not all(
            isinstance(item, str) and item.strip() for item in repairs
        ):
            raise OutcomeError("--allow-repair values must be nonempty")
        return {
            "assessment": _required_text(options, "assessment"),
            "allow_repair": repairs,
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


def finish_contract(action_kind: str) -> str:
    """Return exact JSON shapes for file-backed finishes."""

    contracts: dict[str, Any] = {
        "review": {
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
                    "scope": "global|project|run|assignment",
                    "target": "required except global",
                    "reason": "why",
                    "release_condition": "exact condition",
                },
                {"decision": "release_hold", "hold_id": 1},
                {"decision": "cancel_run", "run_id": 1, "reason": "why"},
                {
                    "decision": "resolve_escalation",
                    "assignment_id": 1,
                    "resolution": "retry|rescope|cancel",
                    "reason": "why",
                    "scope": "required for rescope",
                },
                {"decision": "set_priority", "run_id": 1, "priority": 10},
                {
                    "decision": "suspend_policy",
                    "kind": "sage|inquisitor",
                    "scope": None,
                },
                {
                    "decision": "request_specialist",
                    "kind": "sage|inquisitor",
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
                        "identity": "stable semantic identifier",
                        "problem": "demonstrated problem",
                        "evidence": "concrete evidence",
                        "expected_benefit": "measurable benefit",
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
    contract = contracts.get(action_kind)
    return (
        json.dumps(contract, indent=2, sort_keys=True)
        if contract
        else "No JSON file is required for this action."
    )


def finish_syntax(action_kind: str) -> str:
    """Render the accepted finish commands for a prompt."""

    rows = {
        "implement": "fulcrum finish ready_for_review --evidence <path> | checkpointed --evidence <path> | blocked --reason <text>",
        "correct": "fulcrum finish ready_for_review --evidence <path> | permitted_repair_complete --repair-category <category> --repair-rationale <text> --evidence <path>",
        "review": "fulcrum finish approved --assessment <text> [--allow-repair <category>] | changes_requested --input <findings.json> | incomplete --input <missing.json> | exception --reason <text>",
        "archon": "fulcrum finish decisions --input <decisions.json> | deferred --reason <text> --input <reactivation.json>",
        "weaver": "fulcrum finish intake_complete | future_plan --evidence <path> | blocked --reason <text>",
        "specialist": "fulcrum finish report --input <report.json> | evidence_needed --input <requests.json> | blocked --reason <text>",
        "interview": "fulcrum finish interview_answer --input <answer.json> | blocked --reason <text>",
    }
    if action_kind not in rows:
        raise OutcomeError(f"unknown action kind: {action_kind}")
    return rows[action_kind]
