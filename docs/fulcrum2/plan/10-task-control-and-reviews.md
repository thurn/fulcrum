# 10 — Task control and independent reviews

Status: implemented and validated.

Dependencies: [06](06-codex-runtime-adapter.md), [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md)

Normative reading: [task controls](../contracts.md#native-task-control-handoffs-and-delivery), [plan reviews](../contracts.md#plan-authoring-reviews-and-closure). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and existing code

Make native task management and independent plan reviews executable from the
terminal. Extend application/runtime projections, replacing caller-only context
and blanket pending-request rejection in the existing runtime/CLI. Independent
reviews are ordinary tasks; do not build a native-subagent abstraction.

## Public commands and results

Finish `task list/show/start/send/output/wait/interrupt/requests/respond/terminals/
terminal stop/release/archive/unarchive/delete`. Return native IDs, observations,
output pages, request methods/IDs and explicit history gaps. For example:

```sh
fulcrum task output TASK --turn-id TURN --max-bytes 262144 --json
fulcrum task respond TASK --request REQUEST --input response.json --json
fulcrum plan review start --bead BEAD --perspective cold_reader --json
```

Task-response JSON must match the observed native request type. `task send` requires
owner/Marshal/human authority and may not send system messages to Vizier. Pending
human input is surfaced once; no blanket approval or rejection is manufactured.

## Implementation sequence

1. Normalize native output and pending requests through bounded non-resuming reads.
   Retain request method and ID on operation evidence; after reconnect, unavailable
   request identity is a recovery gap, not a replay of an obsolete response.
2. Correlate send/respond/interrupt actions with receipts and inspect uncertain
   outcomes. Do not create competing turns while an incompatible turn is active.
   Targeted terminal stop must not invoke an all-terminals API that would stop
   additional running tools; return unsupported and expose the explicit
   `--all-owned` alternative for stopping the whole managed task’s terminals.
3. `plan review start` snapshots the candidate draft and perspective in its receipt,
   creates a separately attributed task with role Weaver and purpose `plan_review`,
   and supplies complete review input. It must not take the plan owner's acquisition.
   Before task 13's draft API lands, accept only a valid retained draft structure;
   build/test the provider and review contracts without inventing publication.
4. Cold reader gets draft plus review instructions only. Requirements review gets
   original request/discussion plus draft. Different native tasks provide actual
   independence. Both use ordinary admission, model overrides and task lifecycle.
5. `plan review finish --task ID --input FILE` accepts review operation, summary and
   typed findings only from that task. Snapshot findings before updating the root's
   review reference. Stale reviewed input remains historical and cannot approve a
   changed draft. A missing finish gets one normal reminder, then recovery.
6. Link review tasks through task records and operation relationships for trace and
   later cost accounting. Stop observed conflicting tasks before shared-source
   transfers. Release subscriptions on idle completion regardless of archive timer.

## Acceptance

A terminal can initiate both perspectives, read outputs, answer a native request,
submit valid findings and inspect the resulting evidence without Desktop UI or
private runtime reads. Test wrong-task result rejection, old request after reconnect,
lost send response, stale draft results, missing finish, targeted-terminal capability
failure and archive/delete preconditions. Review initiation must leave the author
as plan owner. All actions expose operation status and a bounded wait.
