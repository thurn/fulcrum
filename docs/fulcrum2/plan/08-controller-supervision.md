# 08 — Controller supervision

Status: not implemented.

Dependencies: [04](04-work-and-ownership.md), [05](05-observability-and-inspection.md), [06](06-codex-runtime-adapter.md)

Normative reading: [controller lifecycle](../design.md#controller-lifecycle), [retry defaults](../design.md#retry-and-progress-defaults). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and existing code

Replace the monolithic `src/fulcrum/controller.py` with a small supervised controller
calling application operations. Retain useful event/wait and source-watcher
patterns only where they fit the new authority model. Do not transplant the old
run, lineage, publication-obligation or role-specific retry engines.

## Public behavior and timers

`serve`, `serve --once`, and `reconcile [--bead ID] [--operation ID]` run the same
processing. One pass discovers current facts, advances eligible bounded steps and
returns operation/ownership/next-action summaries; it does not wait for a whole
model workflow. Startup acquires the writer lock, inspects incomplete receipts
and known tasks, and reconciles before opening automatic admission.

## Implementation steps

1. Supervise the runtime reader, intake discovery, reconciliation loop and operation
   runners. Give each a health/deadline observation exposed by doctor. Persistent
   critical failure exits so the service manager can restart; a socket alone is
   insufficient health evidence.
2. Poll native intake every two seconds with work, ten seconds when idle; reconcile
   known active work every 15 seconds as event fallback. Batch ledger reads and
   bound subprocess concurrency. No routine idle issue creation or model polling.
3. Keep per-bead ordered queues, a short shared write/admission section and bounded
   asynchronous external runners. Persist intent before releasing the section for
   network/model/CI waits. Query and independent-bead operations remain responsive.
4. Reuse one operation receipt across three proved-transient sends with 2/10-second
   delays. Inspect uncertain effects before replay; inconclusive inspection ends
   automatic replay. Reconciliation after exhaustion is a recorded explicit/relevant
   recovery authorization, never fresh budget on every clock tick.
5. No runtime event for two minutes triggers a read-only task inspection. After ten
   minutes without substantive progress, inspect active tool/output; send one concise
   checkpoint request at the next safe idle boundary only if appropriate. At thirty
   minutes without progress, request one Marshal recovery decision. Active tools
   producing new evidence remain live. Never interrupt productive CI solely by age.
6. After terminal work without required finish, send one scope-preserving reminder
   for that acquisition. Continued omission escalates through the same recovery
   mechanism. Explicit review-task result obligations use the same reminder logic.
7. Detect resource pressure to pause admission; release only owned idle subscriptions
   and completed terminals. At four occupied slots, request existing-task or terminal
   repair, not a reserved/new worker. Shutdown drains or explicitly interrupts per
   service command, records unresolved work and attempts a bounded publication flush.

## Acceptance and failure evidence

Advance a deterministic clock through idle, progress, timeout and reminder cases.
Crash after effect/before receipt settlement, restart and prove no duplicate effect.
A stale critical loop must appear in doctor despite a responsive socket. Exhausted
retries remain exhausted across new intake/ticks/restarts. Long-running external
operations cannot hold the global write section or stop unrelated inspection.
Task 22 owns reusable CLI controls; no production scheduler or periodic model job
is introduced by the test clock.
