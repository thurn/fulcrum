"""Action-specific finish validators shared by prompts and the CLI."""

from __future__ import annotations

import json
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
        if outcome_kind == "decisions" and not isinstance(
            payload.get("decisions"), list
        ):
            raise OutcomeError("decisions input requires a decisions list")
        if outcome_kind == "evidence_needed" and not isinstance(
            payload.get("requests"), list
        ):
            raise OutcomeError("evidence-needed input requires a requests list")
        return payload
    if outcome_kind == "report":
        payload = _json_file(options)
        if not isinstance(payload.get("findings"), list):
            raise OutcomeError("report input requires an explicit findings list")
        for finding in payload["findings"]:
            if not isinstance(finding, dict):
                raise OutcomeError("each finding must be an object")
            for field in (
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
        return {"reason": _required_text(options, "reason"), "reactivation": payload}
    if outcome_kind == "intake_complete":
        return {}
    raise OutcomeError(f"unsupported outcome: {outcome_kind}")


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
