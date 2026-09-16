# 20 — Source refresh and recovery launcher

Status: implemented and validated.

> **Normative override:** Fulcrum has no installed source or package to refresh.
> `~/fulcrum` on master is the only behavior and skill source. Any “installation,”
> “deployment,” or “installed pointer” language below is historical and cannot be
> used to justify a copied Fulcrum artifact or a post-change installation step. See
> the
> [master-only source invariant](../../architecture/live-iteration.md#master-only-source-invariant).

Dependencies: [17](17-recovery-and-human-resolution.md), [19](19-installation-and-service.md)

Normative reading: [installed refresh](../design.md#implementation-boundaries), [recovery launcher](../contracts.md#installation-recovery-and-continuity-commands). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and source boundary

Retain automatic source refresh and emergency repair without executing mutable
controller modules mid-operation. Rework installed snapshot/recovery artifacts in
`install.py` and watcher/recovery code; remove old version counters, source hashes,
and journal-dependent repair entry points.

## CLI and artifacts

`service update [--source ABSOLUTE_PATH]` returns a resumable build/probe/activation
receipt with current/pending installed paths and errors. `fulcrum-recover
inspect|takeover|repair|release` accepts the same explicit scope/instance/JSON inputs
as the main repair CLI. It is independently installed with only essential shared
application/adapter modules, never an alternate dispatcher.

## Implementation steps

1. A retained source-change event or explicit update records an operation, pauses
   new automatic dispatch, and waits for in-flight controller mutations to reach
   settled boundaries. Native workers/CI may continue under their existing IDs.
2. Build into a separate private installation directory with an opaque operation-based
   path, not a content hash or release-version scheme. Validate imports, CLI help,
   assets and configuration compatibility before activation. Do not edit active
   interpreter modules or depend on the source checkout's development environment.
3. Atomically switch the installed pointer after quiescence. Keep/reacquire the same
   writer lock before replacement reconciliation and new admission. Record intended
   and observed active paths so a crash before/after pointer replacement is inspected,
   not another untracked install. Failed build/probe leaves the working service active.
4. Source watching is event-driven maintenance, with bounded coalescing and no periodic
   agent work. Disable it through explicit `source_watch_root=null`. Watching cannot
   treat its own generated runtime artifacts as new source edits.
5. Build the independent recovery artifact in its own dependency environment and
   atomically replace it only after import/help probes. It must not import main
   installation or checkout modules via inherited PYTHONPATH. A failed new recovery
   artifact leaves the existing one usable.
6. Recovery may stop only the configured wedged controller service before acquiring
   the writer lock. Use existing Beads receipts/typed repair and YAML authority checks.
   Beads unavailability uses truthful degraded essential repair, never a JSON replay
   store. A failed repair must not clear takeover fences accidentally.

## Verification

Break the source checkout, development environment and main installed import in
separate disposable tests; the recovery executable must still inspect and repair.
Inject a failed build/probe, crash around pointer activation and restart, and assert
one selected installation with preserved work/task/operation identities. An in-flight
mutation prevents activation until quiescent while read-only health stays useful.
Check main/recovery entry points from environments with no checkout import path.
No task restart, shared-runtime restart or production update is authorized merely
by running this task's disposable validation.
