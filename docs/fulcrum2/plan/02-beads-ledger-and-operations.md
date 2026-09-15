# 02 — Beads ledger and operations

Status: implemented and validated.

Dependencies: [01](01-cli-application-spine.md)

Normative reading: [contracts §3](../contracts.md#3-beads-representation), [contracts §4](../contracts.md#4-native-beads-setup-and-formulas). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and code boundary

Make stock Beads the only Fulcrum workflow authority. Build `ledger` and the shared
operation lifecycle. Inspect `src/fulcrum/beads.py` and `store.py`; reuse bounded
subprocess/error ideas, never SQLite tables or the old `metadata.fulcrum` model.

## Public behavior and records

Support receipt-backed `operation show/list/wait/cancel/reconcile`; task 05 enriches
inspection. `operation show ID --json` returns accepted input, planned IDs, step,
attempts, external locator, result/error and executable next action. Exact retries
compare parsed values directly; changed input is `REQUEST_CONFLICT`/exit 5.

Implement work, installation, task, memory, analytics and operation record classes
from §3. Operations use `fc-` plus hyphenless request UUID; other records preselect
random IDs and check collisions. Queries filter record classes explicitly. Preserve
unrelated metadata by replacing only the complete current `fc` top-level member.

## Implementation steps

1. Invoke an absolute `bd` path with literal argv, explicit workspace/actor,
   timeouts and capped stdout/stderr. Bound concurrency to four subprocesses.
   Separate rejected/transient/uncertain/unavailable/unsupported facts.
2. Prove explicit-ID creation, metadata merge, issue/dependency reads, native
   status/assignee updates and formula compilation against a disposable real Dolt
   server. Record response fixtures with credentials removed. Do not fork Beads,
   import its internals, or use private SQL.
3. Create intent before effects. If creation response is lost, read its known ID
   and compare input before retrying. Persist child/resource ID mappings before
   creating them. A metadata write carries `last_transition=<operation>`.
4. Serialize read-modify-write and admission decisions in a short critical section;
   keep per-bead operation ordering. Release the section for slow external work.
   Re-read current facts before each subsequent write so observations do not
   overwrite another completed operation's fields.
5. Model operation state/step explicitly. One logical external action has one
   receipt across retries; child receipts exist only for independently meaningful
   operations. Close settled receipts without making them backlog work.
6. Inspect uncertain postconditions before resending. Three proved-transient sends
   maximum with 2/10-second waits. Inconclusive inspection leaves uncertainty and
   an actionable recovery fact. Cancellation inspects what actually happened.

## Acceptance and limits

With real Beads, inject a lost create/update response and resume using the same
ID. Assert one issue, preserved unrelated metadata, correct complete nested fields,
no duplicated children, and recoverable partial dependency steps. Repeat after a
controller restart. Rejected writes cannot look successful; uncertain writes cannot
be treated as proved unsent. Prove work lists omit every non-work record class.
A temporary fixture builder needed here is consolidated by task 22; this task does
not depend on task 22 completion. Beads outage returns unavailable/degraded facts,
never a disk replay queue.
