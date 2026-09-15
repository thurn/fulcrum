# 21 — Hard reset and cutover

Status: implemented and validated.

Dependencies: [15](15-brain-publication.md), [17](17-recovery-and-human-resolution.md), [18](18-task-continuity-and-fleet.md), [19](19-installation-and-service.md), [20](20-source-refresh-and-recovery-launcher.md)

Normative reading: [reset contract](../contracts.md#6-reset-without-a-parallel-workflow-store), [remote history](../contracts.md#brain-git-publication-and-cadence). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and old-code warning

Implement resumable, explicitly destructive replacement of enumerated managed state.
Replace `src/fulcrum/reset.py`: its existing orphan-branch/broad brain deletion is
outside this specification and must not be reused. This task implements reset;
its completion does not itself authorize resetting production during development.

## Public surface and authority

`reset --hard --yes [--input FILE] --request-id UUID` returns the reset receipt,
completed/pending targets, current step and new installation facts. `recover inspect`
can provide read-only inventory first. Setup/service startup refuses ordinary
dispatch while an unfinished deterministic-location reset workspace exists.

## Ordered implementation

1. Acquire exclusive service control and the preserved brain-root lock. Initialize
   the temporary stock Beads workspace at `<instance-parent>/<instance-name>.reset/`
   and record exact inventory before multi-resource deletion. It becomes the sole
   workflow authority during reset; old Beads/store data is read-only deletion input.
2. Enumerate owned tasks, worktrees/branches, provider handles, operational roots,
   dedicated remote ledger ref and expected old ref OID. For old Fulcrum only, read
   its operational store to establish ownership; do not import it into new workflow.
   Titles or directory names alone cannot establish ownership. Retain config needed
   for clean bootstrap, never create a historical backup/export.
3. Stop dispatch, interrupt/observe managed turns and owned tools, and cancel/reconcile
   pending delivery. Record each target's before/after postcondition. Delete managed
   native tasks and remove exact worktrees/local branches; already absent is success.
   A native deletion that would cascade beyond the enumerated owned inventory must
   be refused pending scoped resolution, not assumed safe.
4. Delete only enumerated old ledger/operational/log/generated data. Preserve source
   repositories, authoritative YAML, credentials, installed executables, design docs,
   ordinary brain Git history and unrelated provider/native resources.
5. Initialize a clean normal stock ledger and replace the dedicated remote ledger
   history using supported Beads remote-discard initialization and scoped native Git
   transport replacement. Verify expected old ref before mutation; concurrent change
   requires reconciliation. Inspect a fresh remote clone to prove old ledger history
   is not reachable. Do not rewrite unrelated refs or claim physical host erasure.
6. Independent cleanup failures can be inventoried while other safe targets proceed;
   unresolved required cleanup/remote replacement means reset is incomplete. Resume
   the same receipt by inspecting targets, not replaying every delete blindly.
7. Bootstrap clean leadership without model turns. Write a minimal terminal receipt
   with the same request ID and accepted reset input into the clean ledger before
   removing temporary reset authority. Do not copy old inventory/history there.
   Then remove the temporary workspace and start normal service. Exact retry finds
   that result; a fresh UUID means a new destructive reset.

## Acceptance and failures

Use disposable local and remote ledgers populated with unique sentinel issues plus
unrelated document/ref/task sentinels. Crash at each authority/deletion/remote/final-
receipt boundary and resume. Assert old issues/history absent from a fresh remote
clone, unrelated history/content intact, YAML bytes preserved and exactly one clean
bootstrap. An unreachable remote or inability to initialize temporary Beads must
not report completed. Beads bootstrap failure permits read-only inventory only;
no JSON journal or partial unrecorded destructive reset substitutes for it.
