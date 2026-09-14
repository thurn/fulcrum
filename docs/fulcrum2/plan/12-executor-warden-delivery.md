# 12 — Executor to Warden delivery

Status: not implemented.

Dependencies: [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md)

Normative reading: [finish outcomes](../contracts.md#native-task-control-handoffs-and-delivery), [Executor/Warden](../design.md#executor-and-warden). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and code boundary

Implement the one-way Executor-to-Warden lifecycle with observed delivery. Replace
old Executor/Overseer retries and frozen review-scope assumptions in
`controller.py`, `outcomes.py` and `lifecycle.py`. Retain useful source/cleanliness
inspection but place transition policy in application operations.

## Public contracts

`finish --bead ID --thread-id TASK --ownership-operation OP --outcome
ready_for_review --input FILE` accepts summary, source_oid, checks and evidence.
Warden can use `review approve`, `validation start`, `promotion start`, `source
sync`, or `finish --outcome approved` to initiate the same operations. Each returns
its receipt, observed delivery fields and exact pending action, not a premature
successful closure. `work show`, `promotion show`, `trace` and operation waits
provide every fact needed by a CLI client.

## Transition steps and receipts

1. Verify Executor acquisition, exact committed source/worktree and submitted checks.
   Persist and seal finish input before initiating handoff. An exact retry returns
   it; a different second finish is `FINISH_SEALED`. Do not make later reporting
   failure revoke accepted implementation.
2. Keep old owner accountable in `handoff`. Prepare/find the Warden using recorded
   task creation intent. Observe all conflicting managed turns and owned tools
   stopped; do not wait for the current worker to terminate while blocking its
   finish response. Return accepted so that worker can end its turn.
3. Transfer owner/role/acquisition and current evidence in one work update; settle
   the destination task record before starting Warden. A crash between these steps
   reconciles the original handoff; it does not rerun Executor implementation.
4. Warden reviews and may edit the same workspace. Any changed source or substantive
   acceptance invalidates earlier approval. Run appropriate validation and approve
   the current source explicitly. Failed CI returns to Warden review, not Executor.
5. Promotion requires matching current approval and provider evidence. Observe
   promotion, required source sync and cleanup independently. Promotion followed
   by sync/cleanup failure remains visibly shipped but not fully settled; Warden
   owns delivery repair until its obligations finish or takeover occurs.
6. Close delivered work only from observed settled obligations. Unachievable checks
   and repeated lack of progress route through Marshal/Justiciar. Reduced scope
   retains waived requirements and known defects and is never labelled full delivery.
7. Every accepted finish includes the nonblocking `fulcrum report` reminder. Native
   turn completion triggers usage finalization later without holding delivery open.

## Acceptance

CLI tests must demonstrate one source implementation, one controlled transfer,
Warden-owned fixes with a new approved OID, no stale approval promotion, and no
success inferred from logs alone. Crash before/after owner update and lose promotion
responses; restart must retain one handoff and adopt observed effects. Verify an
old Executor command cannot change new Warden ownership, clean source remains
recoverable after downstream failure, and unrelated work continues during CI waits.
