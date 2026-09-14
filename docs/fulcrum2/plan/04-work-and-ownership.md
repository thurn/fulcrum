# 04 — Work and ownership

Status: not implemented.

Dependencies: [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md)

Normative reading: [work contracts](../contracts.md#role-entry-and-work), [ownership](../contracts.md#ownership-and-native-writes). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and code boundary

Implement the work lifecycle in `application`, replacing old run/assignment
coordination from `intake.py`, `lifecycle.py`, `outcomes.py` and `store.py`.
Keep stock native fields authoritative where specified and preserve original
outcome/acceptance separately from the compiled role description.

## Commands and durable facts

Implement `work create/show/list/adopt/update/transfer/children/close/reopen`,
`work dependencies`, `progress`, `report`, and `context`'s factual provider.
Creation returns `bead_id` for the root plus `children_by_key`; graph dependencies
accept local keys or existing IDs. `work dependencies ID --input -` accepts
`{"add":["fc-other"],"remove":[]}`. Inspect through `work show/children`.
Owner writes carry `--thread-id ID --ownership-operation OP`; mismatch is
`OWNERSHIP_CONFLICT` before effects. Check schemas from contracts §§2–3, 10.

## Implementation steps

1. Validate graph keys, external targets, priorities and cycles before writing.
   Persist all planned IDs and edges first; reconcile interrupted creation without
   replacing delivered children or duplicating roots. Render native acceptance
   from the canonical acceptance array. Accept optional `intake.benefit` and
   `intake.uncertainties` on work create/update and reports. Retain unknown values
   without blocking native intake; outcome/dependencies/evidence remain in their
   existing fields, not a duplicated intake specification.
2. Normalize raw role-assigned intake to Marshal accountability, preserving the
   original native text. Do not treat a role string as an executing owner. Ambiguous
   project evidence remains inspectable and routes to clarification.
3. Store acquisition receipt ID in `fc.ownership_operation`. Transfers/reopening/
   permanent role changes replace it; progress/title edits change only
   `last_transition`. Source and destination task facts must agree before starts.
   Runtime stop/start methods are dependency boundaries until task 06 lands.
4. Seal accepted finishes by acquisition. Preserve old responsibility while a
   successor is provisioned, inspect old turn/tool termination, then update owner,
   role, acquisition and handoff together. An unresolved old writer fences new work.
5. Detect external assignee/scope/reopen edits and record requests before
   normalization. Do not silently overwrite them or import compiled instructions
   as a new user outcome. Direct reserved metadata edits require reconciliation.
6. Treat waits as independent named reasons. Dependency closure wakes inspection;
   cancellation/rejection does not fulfill an outcome. Follow duplicate canonical
   links with cycle detection. Only explicit authorized scope changes waive work.
7. `report` creates an independently attributed root without changing the reporter's
   role or revoking accepted work. `progress` requires a substantive category,
   summary and evidence; timer-only text is not useful progress evidence.

## Acceptance

Run installed CLI create/adopt/show through both writer paths with real Beads.
Prove metadata/assignee agreement, stale writes from the same task after reacquisition,
partial-graph recovery, native actor/assignee interference, independent waits and
report idempotency. Close/reopen must preserve prior delivery facts and invalidate
old ownership. Delivered closure requires observed adapter evidence; blocked,
rejected, duplicate and answered dispositions use their own truthful semantics.
Do not implement a generic CLI that directly assigns arbitrary workflow phases.
