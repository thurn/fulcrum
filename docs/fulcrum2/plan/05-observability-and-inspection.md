# 05 — Observability and inspection

Status: not implemented.

Dependencies: [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [04](04-work-and-ownership.md)

Normative reading: [diagnostics](../design.md#7-diagnostics-and-self-improvement), [operator results](../contracts.md#observable-operator-results). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and code boundary

Make the system inspectable before native delivery is assembled. Build `diagnostics`
and CLI projections; replace old SQLite-dependent `doctor.py`/`readiness.py` paths
while retaining useful runtime/resource probes and credential redaction.

## Commands and output

Implement `status`, `doctor`, `logs`, `logs prune`, `trace`, `wait --bead --until`,
and operation show/list/wait/cancel/reconcile projections. `status --bead fc-example
--json` must explain current owner, ownership operation, phase, next action, each
waiting reason, native observation and delivery/publication facts. Follow §10's
field schemas. No plain-text parsing is required for callers to choose the next
structured argv command.

## Implementation steps

1. Assemble work views from Beads, with live adapter observations carrying time,
   availability and gaps. Do not silently return stale cached task state as live.
   Read-only status/trace works with the controller stopped. Ledger failure returns
   available component evidence and names the unavailable work projection.
2. Maintain per-critical-loop last-success/deadline facts in the live service health
   response; show unavailable loop observations offline. Distinguish service PID,
   responsive socket, stale reconciliation, provider outage and blocked work.
3. Write structured correlated JSONL logs, capturing duration, adapter, outcome,
   IDs, errors and capped command output. Rotate to 14 days/1 GiB; cap each external
   stdout/stderr stream at 256 KiB with explicit truncation. Redact credentials
   before both log and retained-evidence writes.
4. Preserve essential outcome/source/error excerpts in receipts, not solely logs.
   Trace orders transitions and operation links with `(timestamp,id)` tie breaking;
   deleted logs add gaps. Dedupe unchanged failure notices by operation/category
   and count, without hashes or routine idle log spam.
5. Enforce bounded pagination and documented streaming exception. `logs --follow`
   is explicit JSONL; every normal `--json` response is one envelope. Querying
   does not create analytics/work issues or resume native tasks.
6. Implement waits using observations/events plus bounded fallback reads. Wait
   expiry returns IDs and current facts without changing the operation. Waiting
   for `closed` establishes closure, not an assertion that its outcome is delivered.

## Acceptance and failure behavior

A scripted operator can discover a failed handoff from status, retrieve its exact
operation and evidence, and obtain a concrete repair command without opening Codex
or a private database. Exercise unavailable runtime/ledger, stale loop with live
socket, truncated output, secret redaction, multiple-page traces and log pruning.
After pruning, authoritative source/delivery evidence must survive. Assert an idle
installation does not generate routine issue/log records. Task 10 adds native
output/requests to this same projection, and task 16 adds cost without blocking it.
