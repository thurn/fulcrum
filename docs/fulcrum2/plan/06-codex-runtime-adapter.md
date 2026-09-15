# 06 — Codex runtime adapter

Status: implemented and validated.

Dependencies: [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md)

Normative reading: [runtime adapter](../contracts.md#codex-runtime), [design §6](../design.md#6-recovery-and-resource-lifecycle). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and existing code

Implement the `Runtime` protocol and normalized fact types without policy or role
prose. Retain the maintained WebSocket transport already in `src/fulcrum/runtime.py`;
there is no handwritten framing left to replace. Replace unconditional server-request
rejection and old identity assumptions. The installed schema exposes project
creation, task project attachment, indexed task listing and turn cwd overrides;
probe actual behavior before claiming recoverability.

## Public surface and implementation

1. Back `runtime capabilities/status`, `task list/show/start/send/interrupt/release/
archive/unarchive/delete`, and task 10's output/request controls. Return exact native
IDs, project/cwd, active turn, existence/archive/load observations and timestamps.
Capabilities report supported methods/configuration/model-effort pairs; no silent
model substitution or ignored unsupported option.
2. Use one initialized connection and one reader. Deliver responses promptly and
   enqueue observations without awaiting ledger writes or handlers on that reader;
   otherwise a handler awaiting another RPC can deadlock it. Bound queues, coalesce
   redundant observations, retain terminal/error/input events, and reconcile after
   overflow. Fail all pending calls on disconnect, then reconnect with backoff.
3. Persist creation cwd `<instance>/threads/<operation-id>` before thread creation.
   After a lost response, enumerate all indexed matches at that exact cwd, including
   tasks without a first rollout. One match is adopted; multiple/unknown matches
   require recovery. Absence must be established before another create.
4. Persist native ID, configure the real project/worktree and allowed roots, and
   verify attachment. Load project instruction context using supported resume/turn
   configuration before starting work. Send the complete cooked prompt with start
   and ownership-operation correlation; retain its exact input before sending.
5. Inspect input/turn history for the exact start marker after a lost response.
   Native correlation fields may supplement this marker but are not an invented
   exactly-once guarantee. Unavailable history leaves the operation uncertain.
6. Expose native approval/input requests with exact method and request IDs. Preserve
   pending requests on reconnect only when verified; report lost requests explicitly.
   Interruption acknowledgment is not terminal evidence. Release only Fulcrum's
   subscription after observed idle completion or failure, including shutdown paths.
7. Project create/discovery and resource observations support enrollment/fixtures.
   No native-subagent discovery or control is part of this adapter. Do not resume
   a historical task fleet merely to observe it.

## Verification and failure cases

Use protocol fixtures for malformed responses, disconnects, lost create/start
responses, pending input, project mismatch, unsupported config and idempotent
unsubscribe statuses. Add a disposable native capability probe through public CLI
when live validation is authorized: create without first turn, find indexed task,
start in actual workspace, inspect full prompt/output, interrupt and release.
A schema field alone is not execution evidence. Runtime unavailability returns
classified facts; it must not fabricate a task or make investigation refuse useful
local work. Retain `notSubscribed`/`notLoaded` as successful release outcomes and
make no immediate-reclamation guarantee.
