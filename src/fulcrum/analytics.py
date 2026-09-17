"""Native-turn usage, rate cards, cost estimates, and completion summaries."""

from __future__ import annotations

from fulcrum.coordination import coordinated

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.ledger import (
    Ledger,
    LedgerRecord,
    OperationRecord,
    operation_view,
    random_record_id,
    utc_now,
)

ANALYTICS_NAMESPACE = uuid.UUID("58518da5-edaf-42ba-b4ba-308ab18452c8")
MAX_RESPONSE_RECORDS = 64
RATE_FIELDS = {
    "model",
    "currency",
    "effective_at",
    "retrieved_at",
    "source_url",
    "input_per_million",
    "cached_input_per_million",
    "cache_write_input_per_million",
    "output_per_million",
    "tiers",
    "long_context",
    "tools",
}
FILTER_FIELDS = {
    "bead",
    "workflow",
    "operation",
    "thread_id",
    "role",
    "project",
    "since",
    "until",
    "group_by",
}


def record_desktop_usage(
    ledger: Ledger,
    record: LedgerRecord,
    role: str,
    usage_events: Sequence[Mapping[str, Any]],
    lifecycle_events: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Persist trusted transcript counters without depending on legacy task records."""

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for event in usage_events:
        task_id = event.get("task_id")
        turn_id = event.get("turn_id")
        if (
            isinstance(task_id, str)
            and task_id
            and isinstance(turn_id, str)
            and turn_id
        ):
            grouped[(task_id, turn_id)].append(event)
    written: list[str] = []
    for (task_id, turn_id), events in grouped.items():
        external_ref = f"fulcrum:usage:{task_id}:{turn_id}"
        record_id = _analytics_id(external_ref)
        existing = ledger.show(record_id)
        prior = dict(existing.fc or {}) if existing is not None else {}
        raw: dict[str, Mapping[str, Any]] = {}
        for response in prior.get("raw_responses") or []:
            if isinstance(response, Mapping) and response.get("response_id"):
                raw[str(response["response_id"])] = response
        for event in events:
            identity = event.get("response_id") or event.get("event_id")
            if identity:
                raw[str(identity)] = dict(event)
        normalized_input = [
            {
                "response_id": value.get("response_id") or value.get("event_id"),
                "model": value.get("model"),
                "service_tier": value.get("service_tier"),
                "input_tokens": value.get("input_tokens"),
                "cached_input_tokens": value.get("cached_input_tokens"),
                "cache_write_input_tokens": value.get("cache_write_tokens"),
                "output_tokens": value.get("output_tokens"),
                "reasoning_tokens": value.get("reasoning_tokens"),
            }
            for value in raw.values()
            if value.get("cumulative") is not True
        ]
        responses = _response_records(
            {"usage": {"responses": normalized_input}},
            configured_model=None,
            configured_effort=None,
            reroute=None,
        )
        observed_at = str(
            next(
                (
                    event.get("time")
                    for event in lifecycle_events
                    if event.get("task_id") == task_id
                    and event.get("turn_id") == turn_id
                    and event.get("time")
                ),
                utc_now(),
            )
        )
        priced = [
            _price_response(value, _rate_records(ledger), observed_at)
            for value in responses
        ]
        terminal = next(
            (
                str(event.get("type"))
                for event in lifecycle_events
                if event.get("task_id") == task_id
                and event.get("turn_id") == turn_id
                and event.get("type")
                in {
                    "turn_complete",
                    "turn_completed",
                    "turn_interrupted",
                    "interrupted",
                }
            ),
            None,
        )
        work_fc = record.fc or {}
        workflow_root = work_fc.get("workflow_root")
        fc = {
            "kind": "analytics",
            "subtype": "turn",
            "owner": task_id,
            "thread_id": task_id,
            "turn_id": turn_id,
            "bead_id": record.id if record.kind == "work" else None,
            "workflow_root": workflow_root,
            "role": role,
            "project": work_fc.get("project"),
            "operation_id": None,
            "related_task": task_id,
            "purpose": "coordination" if record.id == "fc-system" else "work",
            "component": "coordination" if record.id == "fc-system" else "direct",
            "attributions": (
                [{"workflow_root": workflow_root, "weight": "1"}]
                if isinstance(workflow_root, str)
                else []
            ),
            "model": {
                "configured": None,
                "effort": None,
                "origin": None,
                "effective": _single_effective_model(priced),
            },
            "effort": None,
            "usage": _aggregate_usage(priced),
            "response_records": priced[:MAX_RESPONSE_RECORDS],
            "response_blocks": [],
            "raw_responses": [dict(value) for value in raw.values()],
            "coverage": (
                "complete"
                if terminal
                and all(value.get("coverage") == "complete" for value in priced)
                else "partial"
            ),
            "missing_reasons": sorted(
                {
                    str(reason)
                    for value in priced
                    for reason in value.get("missing_reasons", [])
                }
                | ({"terminal_lifecycle_missing"} if terminal is None else set())
                | (
                    {"only_cumulative_usage_observed"}
                    if raw and not normalized_input
                    else set()
                )
            ),
            "terminal_state": terminal,
            "observed_at": observed_at,
            "last_observation_at": utc_now(),
        }
        if existing is None:
            ledger.create_record(
                record_id=record_id,
                kind="analytics",
                title=f"Native usage: {task_id}/{turn_id}",
                description="Trusted Desktop transcript usage and pricing evidence.",
                owner=task_id,
                fc=fc,
                external_ref=external_ref,
            )
        else:
            if existing.kind != "analytics" or prior.get("subtype") != "turn":
                raise FulcrumError(
                    "ANALYTICS_ID_CONFLICT",
                    f"analytics identity {record_id} is occupied",
                    exit_code=5,
                )
            ledger.update_fc(record_id, fc)
        written.append(record_id)
    return written


class AnalyticsService:
    def usage(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        rows = self._selected_turns(ledger, request.arguments)
        return CommandResult.query(_usage_report(rows, request.arguments))

    def cost(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        rows = self._selected_turns(ledger, request.arguments)
        report = _cost_report(ledger, rows, request.arguments)
        workflow = request.arguments.get("workflow")
        if isinstance(workflow, str):
            root = ledger.show(workflow)
            annotation = (root.fc or {}).get("completion_cost") if root else None
            report["completion_cost"] = annotation
            summaries = [
                item
                for item in ledger.list_records(kind="analytics", limit=0)
                if (item.fc or {}).get("workflow_root") == workflow
                and (item.fc or {}).get("subtype")
                in {"completion_summary", "completion_correction"}
            ]
            report["completion_history"] = [
                {
                    "summary_bead": item.id,
                    "subtype": (item.fc or {}).get("subtype"),
                    "prior_summary": (item.fc or {}).get("prior_summary"),
                    "completion_boundary": (item.fc or {}).get("completion_boundary"),
                    "coverage": (item.fc or {}).get("coverage"),
                    "currency": (item.fc or {}).get("currency"),
                    "priced_subtotal": (item.fc or {}).get("priced_subtotal"),
                    "total": (item.fc or {}).get("total"),
                }
                for item in sorted(
                    summaries,
                    key=lambda value: (
                        str((value.fc or {}).get("calculated_at")),
                        value.id,
                    ),
                )
            ]
        return CommandResult.query(report)

    def rates_list(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        records = _rate_records(ledger)
        cursor = _optional(request.arguments.get("cursor"))
        if cursor is not None:
            positions = [
                index for index, item in enumerate(records) if item.id == cursor
            ]
            if not positions:
                raise FulcrumError.invalid("INVALID_CURSOR", "unknown rate-card cursor")
            records = records[positions[0] + 1 :]
        limit = _limit(request.arguments.get("limit"))
        items = [_rate_view(item) for item in records]
        selected = items if limit == 0 else items[:limit]
        return CommandResult.query(
            {
                "items": selected,
                "next_cursor": (
                    selected[-1]["id"]
                    if limit and len(items) > len(selected) and selected
                    else None
                ),
                "omitted": len(items) - len(selected),
            }
        )

    def rates_show(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = ledger.show(str(request.arguments["id"]))
        if (
            record is None
            or record.kind != "analytics"
            or (record.fc or {}).get("subtype") != "rate_card"
        ):
            raise FulcrumError.invalid(
                "NOT_FOUND", f"unknown rate card {request.arguments['id']}"
            )
        return CommandResult.query(_rate_view(record))

    @coordinated
    def rates_add(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        card = _validate_rate_card(request.input)
        rate_id = random_record_id()
        operation, reused = ledger.create_operation(
            request,
            planned={"rate_card_id": rate_id, "rate_card": card},
            next_action="Persist the exact documented rate card as immutable analytics input.",
        )
        retained = operation.operation.get("planned")
        if isinstance(retained, Mapping):
            rate_id = str(retained.get("rate_card_id") or rate_id)
            card = dict(retained.get("rate_card") or card)
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        external_ref = (
            f"fulcrum:rate:{card['model']}:{card['currency']}:{card['effective_at']}"
        )
        conflict = next(
            (
                item
                for item in _rate_records(ledger)
                if item.native.get("external_ref") == external_ref
            ),
            None,
        )
        if conflict is not None:
            if dict((conflict.fc or {}).get("card") or {}) != card:
                raise FulcrumError(
                    "RATE_CARD_CONFLICT",
                    "an immutable rate card already occupies this model/effective boundary",
                    exit_code=5,
                    details={"rate_card_id": conflict.id},
                )
            rate_id = conflict.id
        else:
            ledger.create_record(
                record_id=rate_id,
                kind="analytics",
                title=f"Rate card: {card['model']} ({card['effective_at']})",
                description="Immutable documented API-equivalent pricing input.",
                owner=request.thread_id or request.actor.task_id or "HUMAN",
                fc={
                    "kind": "analytics",
                    "subtype": "rate_card",
                    "owner": request.thread_id or request.actor.task_id or "HUMAN",
                    "card": card,
                    "created_by_operation": operation.id,
                },
                external_ref=external_ref,
            )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="immutable_rate_card_retained",
            result={"rate_card": _rate_view(ledger.show(rate_id))},
            next_action="Historical estimates retain this exact rate reference.",
        )
        return _operation_result(operation)

    @coordinated
    def reconcile(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        operation, reused = ledger.create_operation(
            request,
            bead_id=_optional(request.arguments.get("bead")),
            planned={"filters": dict(request.arguments)},
            next_action="Read retained or currently inspectable terminal native usage.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        observed: list[str] = []
        gaps: list[dict[str, Any]] = []
        roots: set[str] = set()
        for retained in ledger.list_records(limit=0):
            if retained.kind not in {"work", "control"}:
                continue
            bead = request.arguments.get("bead")
            if isinstance(bead, str) and retained.id != bead:
                continue
            desktop = (retained.fc or {}).get("desktop")
            observations = (
                desktop.get("observations") if isinstance(desktop, Mapping) else None
            )
            usage = (
                observations.get("usage") if isinstance(observations, Mapping) else None
            )
            lifecycle = (
                observations.get("lifecycle")
                if isinstance(observations, Mapping)
                else None
            )
            if not isinstance(usage, Mapping):
                continue
            usage_rows = [
                value for value in usage.values() if isinstance(value, Mapping)
            ]
            lifecycle_rows = (
                [value for value in lifecycle.values() if isinstance(value, Mapping)]
                if isinstance(lifecycle, Mapping)
                else []
            )
            roles: dict[str, str] = {}
            standing = desktop.get("standing") if isinstance(desktop, Mapping) else None
            if isinstance(standing, Mapping):
                for role, binding in standing.items():
                    if isinstance(binding, Mapping) and binding.get("task_id"):
                        roles[str(binding["task_id"])] = str(role)
            assignments: list[Mapping[str, Any]] = []
            if isinstance(desktop, Mapping):
                active = desktop.get("assignment")
                if isinstance(active, Mapping):
                    assignments.append(active)
                history = desktop.get("assignment_history")
                if isinstance(history, list):
                    assignments.extend(
                        value for value in history if isinstance(value, Mapping)
                    )
            for assignment in assignments:
                if assignment.get("task_id"):
                    roles[str(assignment["task_id"])] = str(
                        assignment.get("role") or "worker"
                    )
            for task_id in sorted(
                {
                    str(value.get("task_id"))
                    for value in usage_rows
                    if value.get("task_id")
                }
            ):
                if request.arguments.get("thread_id") not in {None, task_id}:
                    continue
                observed.extend(
                    record_desktop_usage(
                        ledger,
                        retained,
                        roles.get(task_id, "unknown"),
                        [
                            value
                            for value in usage_rows
                            if value.get("task_id") == task_id
                        ],
                        lifecycle_rows,
                    )
                )
            root_id = (retained.fc or {}).get("workflow_root")
            if isinstance(root_id, str):
                roots.add(root_id)
        bead = request.arguments.get("bead")
        if isinstance(bead, str):
            record = ledger.show(bead)
            if record is not None and record.fc:
                root = record.fc.get("workflow_root")
                if isinstance(root, str):
                    roots.add(root)
        annotations: list[dict[str, Any]] = []
        for root_id in sorted(roots):
            root = ledger.show(root_id)
            if root is not None and root.status == "closed" and root.fc:
                annotations.append(
                    self.finalize_root(
                        ledger,
                        root,
                        operation.id,
                        correction=True,
                    )
                )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="terminal_usage_reconciled",
            result={
                "observed_analytics": observed,
                "gaps": gaps,
                "completion_costs": annotations,
            },
            next_action="No native task was resumed; inspect explicit remaining gaps.",
        )
        return _operation_result(operation)

    def finalize_root(
        self,
        ledger: Ledger,
        root: LedgerRecord,
        completion_operation: str,
        *,
        correction: bool = False,
    ) -> dict[str, Any]:
        if not root.fc or root.fc.get("workflow_root") != root.id:
            return {}
        filters = {"workflow": root.id, "group_by": "workflow"}
        rows = self._selected_turns(ledger, filters)
        report = _cost_report(ledger, rows, filters)
        expected_gaps = _expected_workflow_gaps(ledger, root.id, rows)
        if expected_gaps:
            report["exclusions"] = [*report["exclusions"], *expected_gaps]
            report["missing_reasons"] = sorted(
                {
                    *report["missing_reasons"],
                    "terminal_usage_observation_missing",
                }
            )
            report["coverage"] = "partial"
            report["total"] = None
        prior_annotation = root.fc.get("completion_cost")
        prior_summary = (
            prior_annotation.get("summary_bead")
            if isinstance(prior_annotation, Mapping)
            else None
        )
        identity = (
            f"fulcrum:cost-correction:{root.id}:{completion_operation}"
            if correction and isinstance(prior_summary, str)
            else f"fulcrum:cost:{root.id}:{completion_operation}"
        )
        existing = _record_by_external_ref(ledger, identity)
        included = report["included_native_turn_ids"]
        if correction and isinstance(prior_summary, str):
            prior = ledger.show(prior_summary)
            prior_included = (
                (prior.fc or {}).get("included_native_turn_ids") if prior else None
            )
            prior_rates = (prior.fc or {}).get("rate_card_ids") if prior else None
            if prior_included == included and prior_rates == report["rate_card_ids"]:
                return dict(prior_annotation)
        summary_id = existing.id if existing is not None else _analytics_id(identity)
        completed_at = _completion_time(root)
        summary_fc = {
            "kind": "analytics",
            "subtype": "completion_correction" if correction else "completion_summary",
            "owner": str(root.fc.get("owner") or "HUMAN"),
            "workflow_root": root.id,
            "completion_operation": completion_operation,
            "completion_boundary": completed_at,
            "prior_summary": prior_summary if correction else None,
            "included_native_turn_ids": included,
            "exclusions": report["exclusions"],
            "coverage": report["coverage"],
            "currency": report["currency"],
            "priced_subtotal": report["priced_subtotal"],
            "total": report["total"],
            "components": report["components"],
            "rate_card_ids": report["rate_card_ids"],
            "missing_reasons": report["missing_reasons"],
            "calculated_at": utc_now(),
        }
        if existing is None:
            ledger.create_record(
                record_id=summary_id,
                kind="analytics",
                title=f"Completion cost: {root.id}",
                description="Frozen API-equivalent workflow completion estimate.",
                owner=str(root.fc.get("owner") or "HUMAN"),
                fc=summary_fc,
                external_ref=identity,
            )
        else:
            summary_fc = dict(existing.fc or summary_fc)
        annotation = {
            "estimated_api_cost_usd": (
                summary_fc.get("total") if summary_fc.get("currency") == "USD" else None
            ),
            "priced_subtotal_usd": (
                summary_fc.get("priced_subtotal")
                if summary_fc.get("currency") in {"USD", None}
                else None
            ),
            "currency": "USD",
            "coverage": summary_fc.get("coverage"),
            "state": "finalized",
            "scope": "workflow_lifetime",
            "summary_bead": summary_id,
            "completed_at": summary_fc.get("completion_boundary", completed_at),
            "calculated_at": summary_fc.get("calculated_at"),
            "missing_reasons": summary_fc.get("missing_reasons", []),
            "prior_summary": prior_summary if correction else None,
        }
        current = ledger.show(root.id) or root
        current_fc = dict(current.fc or {})
        current_fc["completion_cost"] = annotation
        ledger.update_fc(current.id, current_fc)
        return annotation

    def _selected_turns(
        self, ledger: Ledger, filters: Mapping[str, Any]
    ) -> list[LedgerRecord]:
        unknown = set(filters).difference(FILTER_FIELDS | {"limit", "cursor"})
        if unknown:
            raise FulcrumError.invalid(
                "INVALID_FILTER", f"unknown analytics filters: {sorted(unknown)}"
            )
        rows = [
            item
            for item in ledger.list_records(kind="analytics", limit=0)
            if (item.fc or {}).get("subtype") == "turn"
        ]
        return [item for item in rows if _matches(item.fc or {}, filters)]


def seed_bundled_rates(ledger: Ledger) -> list[str]:
    """Install dated official cards idempotently during setup."""

    installed: list[str] = []
    existing = {
        str(item.native.get("external_ref")): item for item in _rate_records(ledger)
    }
    for card in bundled_rate_cards():
        external = f"fulcrum:rate:{card['model']}:USD:{card['effective_at']}"
        if external in existing:
            installed.append(existing[external].id)
            continue
        identifier = _analytics_id(external)
        ledger.create_record(
            record_id=identifier,
            kind="analytics",
            title=f"Bundled rate card: {card['model']}",
            description="Dated official OpenAI API pricing input installed by setup.",
            owner="HUMAN",
            fc={
                "kind": "analytics",
                "subtype": "rate_card",
                "owner": "HUMAN",
                "card": card,
                "bundled": True,
            },
            external_ref=external,
        )
        installed.append(identifier)
    return installed


def bundled_rate_cards() -> list[dict[str, Any]]:
    common = {
        "currency": "USD",
        "effective_at": "2026-09-14T00:00:00Z",
        "retrieved_at": "2026-09-14T17:32:00Z",
        "source_url": "https://developers.openai.com/api/docs/pricing",
        "tiers": {
            "standard": "1",
            "batch": "0.5",
            "flex": "0.5",
            "fast": "2",
            "priority": "2",
        },
        "tools": {},
    }
    prices = {
        "gpt-6-astra": ("10", "1", "12.5", "50"),
        "gpt-5.6-sol": ("4", "0.4", "5", "20"),
        "gpt-5.6-terra": ("2", "0.2", "2.5", "12"),
        "gpt-5.6-luna": ("0.2", "0.02", "0.25", "1.2"),
    }
    result: list[dict[str, Any]] = []
    for model, (input_price, cached, cache_write, output) in prices.items():
        result.append(
            {
                **common,
                "model": model,
                "input_per_million": input_price,
                "cached_input_per_million": cached,
                "cache_write_input_per_million": cache_write,
                "output_per_million": output,
                "long_context": (
                    {
                        "threshold_input_tokens": 272000,
                        "input_multiplier": "2",
                        "output_multiplier": "1.5",
                    }
                    if model == "gpt-5.6-sol"
                    else None
                ),
            }
        )
    return result


def _response_records(
    turn: Mapping[str, Any],
    *,
    configured_model: str | None,
    configured_effort: str | None,
    reroute: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    usage = turn.get("usage")
    response_values = None
    if isinstance(usage, Mapping):
        if isinstance(usage.get("responses"), list):
            response_values = usage.get("responses")
        elif isinstance(usage.get("last"), Mapping):
            response_values = [usage.get("last")]
        elif not isinstance(usage.get("total"), Mapping):
            response_values = [usage]
    if response_values is None:
        response_values = turn.get("response_records")
    values = (
        [item for item in response_values if isinstance(item, Mapping)]
        if isinstance(response_values, list)
        else [usage] if isinstance(usage, Mapping) else [None]
    )
    result: list[dict[str, Any]] = []
    for ordinal, value in enumerate(values):
        row = dict(value) if isinstance(value, Mapping) else {}
        effective = (
            row.get("effective_model") or row.get("effectiveModel") or row.get("model")
        )
        tier = row.get("service_tier") or row.get("serviceTier")
        if ordinal == 0 and reroute is not None:
            effective = reroute.get("model") or effective
            tier = reroute.get("service_tier") or tier
        counters, counter_missing = _counters(row)
        response_id = (
            row.get("id")
            or row.get("response_id")
            or row.get("responseId")
            or f"observation:{ordinal}"
        )
        result.append(
            {
                "native_item_id": str(response_id),
                "observation_ordinal": ordinal,
                "configured_model": configured_model,
                "configured_effort": configured_effort,
                "effective_model": str(effective) if effective else None,
                "service_tier": str(tier) if tier else "standard",
                "tokens": counters,
                "tools": _tool_units(row),
                "missing_reasons": counter_missing
                + ([] if effective else ["effective_model_missing"]),
            }
        )
    return result


def _terminal_usage(
    turn: Mapping[str, Any], responses: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, int], list[str]]:
    usage = turn.get("usage")
    total = usage.get("total") if isinstance(usage, Mapping) else None
    if not isinstance(total, Mapping):
        return _aggregate_usage(responses), []
    counters, missing = _counters(total)
    response_totals = _aggregate_usage(responses)
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
    ):
        if key in counters and response_totals.get(key) != counters[key]:
            missing.append(f"response_boundaries_incomplete:{key}")
    return counters, sorted(set(missing))


def _price_response(
    response: Mapping[str, Any], rates: Sequence[LedgerRecord], observed_at: str
) -> dict[str, Any]:
    row = dict(response)
    derived_prefixes = (
        "rate_missing:",
        "tier_price_missing:",
        "price_component_missing:",
        "tool_price_missing:",
    )
    missing = [
        str(item)
        for item in row.get("missing_reasons", [])
        if not str(item).startswith(derived_prefixes)
    ]
    for key in (
        "rate_card_id",
        "currency",
        "priced_components",
        "priced_subtotal",
        "coverage",
    ):
        row.pop(key, None)
    model = row.get("effective_model")
    card_record = (
        _select_rate(rates, model, observed_at) if isinstance(model, str) else None
    )
    if card_record is None:
        missing.append(f"rate_missing:{model or 'unknown'}")
        row.update(
            {
                "rate_card_id": None,
                "priced_components": {},
                "priced_subtotal": None,
                "coverage": "unknown" if not row.get("tokens") else "partial",
                "missing_reasons": sorted(set(missing)),
            }
        )
        return row
    card = dict((card_record.fc or {}).get("card") or {})
    tier = str(row.get("service_tier") or "standard")
    tiers = card.get("tiers")
    multiplier_value = tiers.get(tier) if isinstance(tiers, Mapping) else None
    if multiplier_value is None:
        missing.append(f"tier_price_missing:{tier}")
        multiplier = None
    else:
        multiplier = Decimal(str(multiplier_value))
    tokens = row.get("tokens")
    components: dict[str, str] = {}
    total = Decimal("0")
    if isinstance(tokens, Mapping) and multiplier is not None:
        input_multiplier = Decimal("1")
        output_multiplier = Decimal("1")
        long_context = card.get("long_context")
        if isinstance(long_context, Mapping) and int(
            tokens.get("input_tokens") or 0
        ) > int(long_context["threshold_input_tokens"]):
            input_multiplier = Decimal(str(long_context["input_multiplier"]))
            output_multiplier = Decimal(str(long_context["output_multiplier"]))
        mapping = (
            ("ordinary_input_tokens", "input_per_million", input_multiplier),
            (
                "cached_input_tokens",
                "cached_input_per_million",
                input_multiplier,
            ),
            (
                "cache_write_input_tokens",
                "cache_write_input_per_million",
                input_multiplier,
            ),
            ("output_tokens", "output_per_million", output_multiplier),
        )
        for token_key, rate_key, context_multiplier in mapping:
            value = tokens.get(token_key)
            price = card.get(rate_key)
            if not isinstance(value, int) or price is None:
                missing.append(f"price_component_missing:{token_key}")
                continue
            amount = (
                Decimal(value)
                * Decimal(str(price))
                * multiplier
                * context_multiplier
                / Decimal(1_000_000)
            )
            components[token_key] = _money(amount)
            total += amount
    tools = row.get("tools")
    tool_rates = card.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, Mapping):
                continue
            product = tool.get("product")
            configured = (
                tool_rates.get(product)
                if isinstance(tool_rates, Mapping) and isinstance(product, str)
                else None
            )
            if not isinstance(configured, Mapping):
                missing.append(f"tool_price_missing:{product or 'unknown'}")
                continue
            units = Decimal(str(tool.get("units") or 0))
            amount = units * Decimal(str(configured.get("price")))
            components[f"tool:{product}"] = _money(amount)
            total += amount
    row.update(
        {
            "rate_card_id": card_record.id,
            "currency": card.get("currency"),
            "priced_components": components,
            "priced_subtotal": _money(total) if components else None,
            "coverage": "complete" if not missing else "partial",
            "missing_reasons": sorted(set(missing)),
        }
    )
    return row


def _usage_report(
    records: Sequence[LedgerRecord], filters: Mapping[str, Any]
) -> dict[str, Any]:
    totals = _sum_usage(records, filters)
    missing = sorted(
        {
            reason
            for record in records
            for reason in (record.fc or {}).get("missing_reasons", [])
            if isinstance(reason, str)
        }
    )
    group_by = str(filters.get("group_by") or "workflow")
    groups: dict[str, list[LedgerRecord]] = defaultdict(list)
    for record in records:
        for key in _group_keys(record.fc or {}, group_by):
            groups[key].append(record)
    coverages = [
        str((record.fc or {}).get("coverage") or "unknown") for record in records
    ]
    return {
        "group_by": group_by,
        "totals": totals,
        "groups": [
            {
                "key": key,
                "usage": _sum_usage(
                    rows,
                    {
                        **dict(filters),
                        **(
                            {"workflow": key}
                            if group_by == "workflow" and key != "unattributed"
                            else {}
                        ),
                    },
                ),
                "turn_count": len(rows),
            }
            for key, rows in sorted(groups.items())
        ],
        "coverage": (
            "unknown"
            if not records or all(item == "unknown" for item in coverages)
            else (
                "complete"
                if all(item == "complete" for item in coverages)
                else "partial"
            )
        ),
        "missing_reasons": missing,
        "included_native_turn_ids": [_turn_identity(item) for item in records],
        "excluded_native_subagents": True,
    }


def _cost_report(
    ledger: Ledger,
    records: Sequence[LedgerRecord],
    filters: Mapping[str, Any],
    *,
    include_groups: bool = True,
) -> dict[str, Any]:
    workflow = _optional(filters.get("workflow"))
    total = Decimal("0")
    priced = False
    currencies: set[str] = set()
    missing: set[str] = set()
    rate_ids: set[str] = set()
    components = {
        "direct": Decimal("0"),
        "review": Decimal("0"),
        "coordination": Decimal("0"),
        "overhead": Decimal("0"),
    }
    exclusions: list[dict[str, Any]] = []
    for record in records:
        fc = record.fc or {}
        weight = _workflow_weight(fc, workflow)
        responses = _all_responses(ledger, fc)
        for response in responses:
            # A turn observation retains the rate reference and decimal result.
            # Reports aggregate those frozen facts; only explicit observation
            # reconciliation may price a previously missing contribution.
            repriced = dict(response)
            subtotal = repriced.get("priced_subtotal")
            if isinstance(subtotal, str):
                amount = Decimal(subtotal) * weight
                total += amount
                priced = True
                component = str(fc.get("component") or "direct")
                if component not in components:
                    component = "overhead"
                components[component] += amount
            currency = repriced.get("currency")
            if isinstance(currency, str):
                currencies.add(currency)
            rate_id = repriced.get("rate_card_id")
            if isinstance(rate_id, str):
                rate_ids.add(rate_id)
            missing.update(str(item) for item in repriced.get("missing_reasons", []))
    if len(currencies) > 1:
        missing.add("mixed_currencies")
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    coverage = (
        "complete" if records and not missing else "partial" if records else "unknown"
    )
    result = {
        "currency": currency,
        "priced_subtotal": _money(total) if priced else None,
        "total": _money(total) if coverage == "complete" and priced else None,
        "coverage": coverage,
        "components": {
            key: _money(value) if value or priced else None
            for key, value in components.items()
        },
        "counts": {
            "native_turns": len(records),
            "responses": sum(
                len(_all_responses(ledger, item.fc or {})) for item in records
            ),
        },
        "included_native_turn_ids": [_turn_identity(item) for item in records],
        "missing_reasons": sorted(
            missing or ({"no_managed_turns"} if not records else set())
        ),
        "exclusions": exclusions,
        "rate_card_ids": sorted(rate_ids),
        "estimate_kind": "api_equivalent_not_subscription_billing",
    }
    if include_groups:
        group_by = str(filters.get("group_by") or "workflow")
        grouped: dict[str, list[LedgerRecord]] = defaultdict(list)
        for record in records:
            for key in _group_keys(record.fc or {}, group_by):
                grouped[key].append(record)
        result["group_by"] = group_by
        result["groups"] = [
            {
                "key": key,
                **_cost_report(
                    ledger,
                    rows,
                    {
                        **dict(filters),
                        **(
                            {"workflow": key}
                            if group_by == "workflow" and key != "unattributed"
                            else {}
                        ),
                    },
                    include_groups=False,
                ),
            }
            for key, rows in sorted(grouped.items())
        ]
    return result


def _counters(value: Mapping[str, Any]) -> tuple[dict[str, int], list[str]]:
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens", "total_input_tokens"),
        "cached_input_tokens": ("cached_input_tokens", "cachedInputTokens"),
        "cache_write_input_tokens": (
            "cache_write_input_tokens",
            "cacheWriteInputTokens",
        ),
        "output_tokens": ("output_tokens", "outputTokens", "total_output_tokens"),
    }
    result: dict[str, int] = {}
    missing: list[str] = []
    for target, names in aliases.items():
        found = next((value.get(name) for name in names if name in value), None)
        if isinstance(found, int) and not isinstance(found, bool) and found >= 0:
            result[target] = found
        elif target in {"cached_input_tokens", "cache_write_input_tokens"}:
            missing.append(f"counter_missing:{target}")
        else:
            missing.append(f"counter_missing:{target}")
    if "input_tokens" in result:
        cached = result.get("cached_input_tokens")
        writes = result.get("cache_write_input_tokens")
        if cached is not None and writes is not None:
            ordinary = result["input_tokens"] - cached - writes
            if ordinary < 0:
                missing.append("invalid_disjoint_input_counters")
            else:
                result["ordinary_input_tokens"] = ordinary
    return result, missing


def _tool_units(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    tools = value.get("tool_units") or value.get("toolUnits")
    return (
        [dict(item) for item in tools if isinstance(item, Mapping)]
        if isinstance(tools, list)
        else []
    )


def _aggregate_usage(responses: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for response in responses:
        tokens = response.get("tokens")
        if isinstance(tokens, Mapping):
            for key, value in tokens.items():
                if isinstance(value, int):
                    totals[str(key)] += value
    return dict(totals)


def _sum_usage(
    records: Sequence[LedgerRecord], filters: Mapping[str, Any]
) -> dict[str, str]:
    workflow = _optional(filters.get("workflow"))
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for record in records:
        fc = record.fc or {}
        weight = _workflow_weight(fc, workflow)
        usage = fc.get("usage")
        if isinstance(usage, Mapping):
            for key, value in usage.items():
                if isinstance(value, int):
                    totals[str(key)] += Decimal(value) * weight
    return {key: _number(value) for key, value in sorted(totals.items())}


def _all_responses(ledger: Ledger, fc: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = [
        dict(item)
        for item in fc.get("response_records", [])
        if isinstance(item, Mapping)
    ]
    for identifier in fc.get("response_blocks", []):
        block = ledger.show(str(identifier))
        if block is not None and block.fc:
            result.extend(
                dict(item)
                for item in block.fc.get("response_records", [])
                if isinstance(item, Mapping)
            )
    return result


def _matches(fc: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    direct = {
        "operation": "operation_id",
        "thread_id": "thread_id",
        "role": "role",
        "project": "project",
        "task": "related_task",
    }
    for supplied, field in direct.items():
        if (
            supplied in filters
            and filters[supplied] is not None
            and str(fc.get(field)) != str(filters[supplied])
        ):
            return False
    bead = filters.get("bead")
    if bead is not None and str(fc.get("bead_id")) != str(bead):
        return False
    workflow = filters.get("workflow")
    if workflow is not None and not any(
        isinstance(item, Mapping) and item.get("workflow_root") == workflow
        for item in fc.get("attributions", [])
    ):
        return False
    observed = _parse_time(fc.get("observed_at"))
    since = _parse_time(filters.get("since"))
    until = _parse_time(filters.get("until"))
    return not (since and (observed is None or observed < since)) and not (
        until and (observed is None or observed > until)
    )


def _workflow_weight(fc: Mapping[str, Any], workflow: str | None) -> Decimal:
    if workflow is None:
        return Decimal("1")
    for item in fc.get("attributions", []):
        if isinstance(item, Mapping) and item.get("workflow_root") == workflow:
            return Decimal(str(item.get("weight") or "0"))
    return Decimal("0")


def _group_key(fc: Mapping[str, Any], group_by: str) -> str:
    mapping = {
        "bead": "bead_id",
        "workflow": "workflow_root",
        "operation": "operation_id",
        "task": "related_task",
        "role": "role",
        "project": "project",
    }
    if group_by == "model":
        model = fc.get("model")
        return str(model.get("effective") if isinstance(model, Mapping) else None)
    if group_by not in mapping:
        raise FulcrumError.invalid(
            "INVALID_GROUP",
            "group-by must be bead, workflow, operation, task, role, project, or model",
        )
    return str(fc.get(mapping[group_by]) or "unattributed")


def _group_keys(fc: Mapping[str, Any], group_by: str) -> list[str]:
    if group_by != "workflow":
        return [_group_key(fc, group_by)]
    roots = sorted(
        {
            str(item["workflow_root"])
            for item in fc.get("attributions", [])
            if isinstance(item, Mapping) and item.get("workflow_root")
        }
    )
    return roots or ["unattributed"]


def _expected_workflow_gaps(
    ledger: Ledger, workflow_root: str, observed: Sequence[LedgerRecord]
) -> list[dict[str, Any]]:
    included = {_turn_identity(item) for item in observed}
    gaps: list[dict[str, Any]] = []
    for work in ledger.list_records(kind="work", limit=0):
        fc = work.fc or {}
        if work.id != workflow_root and fc.get("workflow_root") != workflow_root:
            continue
        desktop = fc.get("desktop")
        if not isinstance(desktop, Mapping):
            continue
        assignments: list[Mapping[str, Any]] = []
        active = desktop.get("assignment")
        if isinstance(active, Mapping):
            assignments.append(active)
        history = desktop.get("assignment_history")
        if isinstance(history, list):
            assignments.extend(item for item in history if isinstance(item, Mapping))
        for assignment in assignments:
            task_id = assignment.get("task_id")
            turn_id = assignment.get("turn_id")
            identity = f"{task_id}:{turn_id}"
            if not task_id or not turn_id or identity in included:
                continue
            gaps.append(
                {
                    "bead_id": work.id,
                    "thread_id": task_id,
                    "turn_id": turn_id,
                    "terminal_state": assignment.get("state"),
                    "reason": (
                        "terminal usage has not been persisted"
                        if assignment.get("state") == "finished"
                        else "native lifecycle is not terminal"
                    ),
                }
            )
    return gaps


def _validate_rate_card(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != RATE_FIELDS:
        raise FulcrumError.invalid(
            "INVALID_RATE_CARD",
            f"rate card requires exactly {sorted(RATE_FIELDS)}",
        )
    card = dict(value)
    if not isinstance(card.get("model"), str) or not card["model"]:
        raise FulcrumError.invalid("INVALID_RATE_CARD", "model is required")
    currency = card.get("currency")
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or currency.upper() != currency
    ):
        raise FulcrumError.invalid(
            "INVALID_RATE_CARD", "currency must be an uppercase ISO code"
        )
    for key in ("effective_at", "retrieved_at"):
        if _parse_time(card.get(key)) is None:
            raise FulcrumError.invalid("INVALID_RATE_CARD", f"{key} must be RFC3339")
    source = card.get("source_url")
    if not isinstance(source, str) or not source.startswith("https://"):
        raise FulcrumError.invalid("INVALID_RATE_CARD", "source_url must be HTTPS")
    for key in (
        "input_per_million",
        "cached_input_per_million",
        "cache_write_input_per_million",
        "output_per_million",
    ):
        _nonnegative_decimal(card.get(key), key, nullable=True)
    tiers = card.get("tiers")
    if not isinstance(tiers, Mapping):
        raise FulcrumError.invalid("INVALID_RATE_CARD", "tiers must be an object")
    for key, item in tiers.items():
        if not isinstance(key, str):
            raise FulcrumError.invalid(
                "INVALID_RATE_CARD", "tier names must be strings"
            )
        _nonnegative_decimal(item, f"tiers.{key}")
    long_context = card.get("long_context")
    if long_context is not None:
        if not isinstance(long_context, Mapping) or set(long_context) != {
            "threshold_input_tokens",
            "input_multiplier",
            "output_multiplier",
        }:
            raise FulcrumError.invalid(
                "INVALID_RATE_CARD", "long_context has invalid fields"
            )
        threshold = long_context.get("threshold_input_tokens")
        if (
            not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or threshold < 1
        ):
            raise FulcrumError.invalid(
                "INVALID_RATE_CARD", "long-context threshold must be positive"
            )
        _nonnegative_decimal(
            long_context.get("input_multiplier"), "long_context.input_multiplier"
        )
        _nonnegative_decimal(
            long_context.get("output_multiplier"), "long_context.output_multiplier"
        )
    tools = card.get("tools")
    if not isinstance(tools, Mapping):
        raise FulcrumError.invalid("INVALID_RATE_CARD", "tools must be an object")
    for product, details in tools.items():
        if (
            not isinstance(product, str)
            or not isinstance(details, Mapping)
            or set(details) != {"unit", "price"}
            or not isinstance(details.get("unit"), str)
        ):
            raise FulcrumError.invalid(
                "INVALID_RATE_CARD", f"invalid tool price {product}"
            )
        _nonnegative_decimal(details.get("price"), f"tools.{product}.price")
    return card


def _nonnegative_decimal(value: Any, field: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise FulcrumError.invalid(
            "INVALID_RATE_CARD", f"{field} must be a decimal string"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise FulcrumError.invalid(
            "INVALID_RATE_CARD", f"{field} is not decimal"
        ) from error
    if not parsed.is_finite() or parsed < 0:
        raise FulcrumError.invalid(
            "INVALID_RATE_CARD", f"{field} must be nonnegative and finite"
        )


def _select_rate(
    records: Sequence[LedgerRecord], model: str, observed_at: str
) -> LedgerRecord | None:
    observed = _parse_time(observed_at)
    candidates = []
    for record in records:
        card = (record.fc or {}).get("card")
        if not isinstance(card, Mapping) or card.get("model") != model:
            continue
        effective = _parse_time(card.get("effective_at"))
        if effective is not None and (observed is None or effective <= observed):
            candidates.append((effective, record.id, record))
    return max(candidates, default=(None, "", None))[2]


def _rate_records(ledger: Ledger) -> list[LedgerRecord]:
    return sorted(
        [
            item
            for item in ledger.list_records(kind="analytics", limit=0)
            if (item.fc or {}).get("subtype") == "rate_card"
        ],
        key=lambda item: (
            str(((item.fc or {}).get("card") or {}).get("model")),
            str(((item.fc or {}).get("card") or {}).get("effective_at")),
            item.id,
        ),
    )


def _rate_view(record: LedgerRecord | None) -> dict[str, Any]:
    if record is None or not record.fc:
        return {}
    return {"id": record.id, "card": record.fc.get("card"), "immutable": True}


def _record_by_external_ref(ledger: Ledger, external: str) -> LedgerRecord | None:
    return next(
        (
            item
            for item in ledger.list_records(kind="analytics", limit=0)
            if item.native.get("external_ref") == external
        ),
        None,
    )


def _analytics_id(external_ref: str) -> str:
    return "fc-" + uuid.uuid5(ANALYTICS_NAMESPACE, external_ref).hex


def _single_effective_model(records: Sequence[Mapping[str, Any]]) -> str | None:
    models = {
        str(item["effective_model"])
        for item in records
        if isinstance(item.get("effective_model"), str)
    }
    return next(iter(models)) if len(models) == 1 else None


def _turn_identity(record: LedgerRecord) -> str:
    fc = record.fc or {}
    return f"{fc.get('thread_id')}:{fc.get('turn_id')}"


def _completion_time(root: LedgerRecord) -> str:
    disposition = (root.fc or {}).get("disposition")
    if isinstance(disposition, Mapping) and isinstance(
        disposition.get("completed_at"), str
    ):
        return str(disposition["completed_at"])
    return utc_now()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def _decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _number(value: Decimal) -> str:
    integral = value.to_integral_value()
    return str(integral) if value == integral else format(value.normalize(), "f")


def _optional(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _limit(value: Any) -> int:
    result = int(value) if value is not None else 20
    if result < 0:
        raise FulcrumError.invalid("INVALID_LIMIT", "limit cannot be negative")
    return result


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "a brain root is required", exit_code=4
        )
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    beads = config["beads"]
    return Ledger(
        request.instance.brain_root,
        executable=str(beads.get("executable")) if beads.get("executable") else None,
        timeout=request.timeout,
    )


def _operation_result(operation: OperationRecord) -> CommandResult:
    value = str(operation.operation.get("state", "running"))
    state = (
        CommandState(value)
        if value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    return CommandResult(
        ok=state not in {CommandState.FAILED, CommandState.UNCERTAIN},
        state=state,
        operation_id=operation.id,
        request_id=str(operation.operation.get("request_id")),
        result=operation_view(operation),
    )
