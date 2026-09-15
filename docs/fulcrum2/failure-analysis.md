# Operational failures informing Fulcrum 2.0

This appendix records the evidence used to design the replacement and the repair
behavior required by [design.md](design.md) and [contracts.md](contracts.md).
It is not a request to reproduce these incidents manually or add each incident
as a separate promotion gate.

## Evidence and limits

A dedicated research subagent inspected the repository at commit `e89ae32`, the
September 12 incident report, and the retained operational database read-only.
The database was at
`~/Library/Application Support/Fulcrum/fulcrum.sqlite3`. The captured activity
began September 13 at 08:34 PDT; workflow records continued through approximately
21:44 PDT, with later startup events. Counts below describe that retained sample,
not lifetime totals. The design session did not register roles, dispatch recovery,
change the database, or reset the installation to obtain this evidence.

The original incident report provides a narrative and 28 distinct failure
observations: [September 12 runtime incident](../postmortems/2026-09-12-python-runtime-first-test.md).
The superseded Desktop concurrency analysis recorded process measurements and
distinguished demonstrated behavior from proposed remedies; it remains available
in repository history at the cited historical baseline rather than in the shipped
operator documentation.

The hard reset deliberately deletes old operational data. This appendix preserves
the relevant findings and their provenance; implementation must not require the
old database or old incident threads to remain available. Repository links point
to the old implementation as it exists at design time and can be retrieved from
Git history after replacement.

## Findings and rejected assumptions

| Finding | Evidence | Design consequence |
| --- | --- | --- |
| Cleanup produced a retry storm | 22,096 failed `thread_park` operations and 22,431 `resource_reconciliation_deferred` events in approximately 86 minutes. 22,090 failures reported an idle task was not parked. The sample contained roughly 97,761 events and only 24 assignment rows. | Check desired postconditions, settle already-unloaded tasks, retain one operation across retries, and aggregate unchanged failures. |
| App-server exhausted descriptors | Recorded process snapshot: 251 of 256 descriptors, including 128 pipes and 43 retained helper children. Eleven idle Fulcrum tasks held writer locks. Controller logs also contained actual “Too many open files” errors. | Release subscriptions, separate visibility from loading, configure the service limit, and avoid resource accounting based on assumed helper names/counts. |
| Recovery needed the exhausted resource | Hold 19/action 226 blocked the Sage intended to investigate FD exhaustion at 189/256 descriptors after exhausting start retries. | Reserve automatic recovery capacity, reuse an existing task when appropriate, and provide terminal-only repair that needs no model. |
| Coordination amplified work | 21 implementation actions led to 52 correction actions, 64 reviews, and 67 Archon actions. Eleven of 17 completed assignments had review failures. | One-way Executor to Warden transfer; Warden fixes directly. Avoid agent turns for mechanical completion and redispatch. |
| Delivery was slow in this sample | For completed assignments, creation-to-last-update elapsed time had a median of 60.8 minutes and range of 5.5–149.7 minutes. | Measure queueing, active work, delivery, and coordination separately; do not describe this whole duration as implementation time. |
| Stale review evidence sustained a deadlock | Assignment 10 / `brain-p01` remained recovering with retry count 6 and hold 12. The correction request still cited obsolete source `da5b649` while the current candidate contained the requested fix; an earlier review had approved a candidate. | Handoffs identify the current source and unresolved blockers; historical findings do not automatically remain actionable. |
| A task required an action its worker could not perform | Assignment 15 / `brain-9l3`, review action 189, required a successful Sage lifecycle from an unbound human-created task. The assigned Executor was already role-bound, and Sage registration rejected that authority. | Use achievable acceptance criteria and explicit Justiciar authority to waive impossible procedures or reduce scope. |
| Diagnostics depended on healthy historical joins | Direct Sage required a retained bead, assignment, Executor and Overseer records, and valid relations. Retained errors included missing causal workflow, wrong identifiers, incompatible authority, and a uniqueness violation. | Diagnose from available evidence; support permanent role transitions; return useful degraded instructions. |
| Successful mutations looked failed | The September 12 report shows successful Tollgate promotion misclassified after response parsing failed, and successful intake reported failed because unrelated Archon advancement failed. | Separate accepted command results from downstream work. Inspect native outcomes before repeating uncertain mutations. |
| Process health concealed stalled workflow | The controller/socket stayed responsive while fallback reconciliation was more than 30 minutes stale and completed native turns remained active/recovering in Fulcrum. | Supervise critical loops and report actual ownership/next-action progress, not just process availability. |
| Resource cleanup overrode UI intent | Current reconciliation re-parks retired/archived identities when native state becomes unarchived. | Persist one automatic archive disposition per task and respect subsequent manual unarchive. |

The user's specific expired-ten-minute-timer rearchive complaint was **not
independently reproduced** in the retained sample: no duplicate successful
`thread_archive` operations were found. The code inspection supports a related
re-parking mechanism, not proof of that exact timer sequence.

Relevant old implementation areas:

- Removed `controller.py`, particularly its resource-reconciliation pass,
  direct-target resolver, and role-binding checks.
- Removed `resources.py`: four-active/four-idle ceilings, FD reserve, and admission
  assumptions.
- Removed Sage prompt and nested prompt assembler: mandatory interview rounds,
  large prompt budgets, and layers of clipping/reference assembly.
- The retained [CLI](../../src/fulcrum/cli.py) and removed legacy CLI tests: task
  identity inference and tests that commonly called `main()` with transport patched
  rather than exercising an installed command through the complete system.

### Cost and context claims that the evidence does not support

Not every correction was waste. Several review findings identified real defects.
The design preserves independent initial review, then makes the reviewer
responsible for completing its fixes.

Actual monetary cost was not established. All 1,339 retained cost contributions
were partial. Their priced portions summed to approximately $93.38 API-equivalent,
not actual subscription expenditure or full workflow cost. Archon/Weaver coverage
was unavailable. Native token aggregates need their cumulative-counter semantics
validated before being used as efficiency claims.

Stored Archon action JSON averaged approximately 6,728 characters and reached
57,166 characters. Those are stored payloads, not measured delivered prompts.
The old code already had extensive clipping and prioritization. The replacement
therefore changes what decisions are sent and when, rather than merely lowering
a prompt-size constant.

The old implementation had approximately 28,900 Python source lines, including a
12,569-line controller, 4,416-line store, and 985-line prompt module. This indicates
concentrated responsibilities; it does not prove that Python or SQLite caused
the failures. The Beads decision removes duplicate workflow authority and takes
advantage of an existing work system. Its reliability still depends on correct
cross-system operation handling.

## Recovery sequences

These sequences use the public CLI and correspond to a small number of reusable
failure scenarios. They specify who makes progress rather than prescribing a
manual validation ceremony.

### 1. Executor finishes, but handoff stops midway

1. Retain the accepted finish receipt, exact source, evidence, and old ownership-operation reference.
2. Inspect the source and any recorded provider submission. Do not ask Executor
   to implement again just because a later orchestration step failed.
3. Locate or create the Warden using the recorded native creation locator. If a
   previous creation response was lost, inspect that locator before another send.
4. Observe termination of conflicting managed tasks and tools. Transfer owner, role, and new ownership-operation reference in
   one bead update; an old reference can no longer finish or promote the work.
5. Start or locate Warden's correlated turn. Complete the original handoff receipt.

An unavailable destination keeps the old owner accountable for the retained
handoff until Marshal/Justiciar takes over. It must not leave an empty assignee or
start both workers on the worktree.

### 2. Promotion succeeds and its response is lost

1. Keep the promotion receipt `uncertain` with exact source and provider handle.
2. `operation reconcile` asks the provider and Git what actually happened.
3. If promoted, retain submitted and integration source identities and proceed to
   source synchronization/cleanup. Do not authorize another candidate or repeat
   implementation.
4. If definitively rejected, record failure and the next Warden repair action.
5. If still unknown, stop automatic resends and give Justiciar the current facts.

A cleanup failure after observed promotion is cleanup recovery. Bead status must
show that code shipped; it must not imply that approved source still needs a fix.

### 3. Impossible acceptance criterion blocks Warden

1. Warden records the current criterion, why its authority/environment cannot
   satisfy it, and what available evidence establishes the actual result.
2. Marshal receives a short recovery decision, not the entire review history.
3. Justiciar takes ownership after stopping conflicting writers. It independently
   inspects source and the blocker.
4. It may replace the check, waive the workflow requirement, or reduce substantive
   scope. Record the specific tradeoff and remaining defects.
5. Justiciar completes validation/delivery itself where feasible, then cleans up.

There is no requirement to reproduce a human-only skill lifecycle from an already
managed Executor. An isolated CLI scenario can test that lifecycle without
blocking unrelated promotion on a live production invocation.

### 4. Descriptor pressure prevents new workers

1. Pause new automatic admission and expose observed pressure once.
2. The already-running controller releases its idle subscriptions and completed
   owned terminals. `notLoaded` and `notSubscribed` settle successfully.
3. Transition the existing Marshal task to a
   scoped Justiciar. Do not recursively queue more diagnostic agents.
4. If the runtime cannot run any agent, terminal `recover inspect/repair` operates
   on explicit owned targets without requesting a new model turn.
5. Restore leadership/admission after observed recovery. Preserve unrelated human
   tasks and never archive merely to hide resource use.

Unsubscription does not guarantee immediate process reclamation: the current
[official lifecycle](https://learn.chatgpt.com/docs/app-server) has an inactivity
grace period after the last subscriber leaves. Service configuration and finite
working-set size must accommodate that fact.

### 5. Beads or the controller is unavailable during investigation

1. Role entry returns packaged instructions and accessible logs/native facts with
   `state=degraded`; it explicitly identifies which registration writes failed.
2. Sage/Mason/Justiciar investigates the available scope without requiring an
   assignment join, interview, new thread, or healthy controller.
3. Controller-offline repairs acquire the same writer lock. Dependency failures
   are repaired through native tools and explicit targets.
4. Once Beads works, create/adopt the real bead from the retained user request and
   reconcile observed effects. Do not replay a shadow JSON workflow journal.

When the available interface can only provide instructions because even the
native model service is unavailable, report that limit accurately. “Never
refuses” means useful best-effort work, not invented execution or ownership.

### 6. Controller restarts after an acknowledged or uncertain command

1. Acquire the exclusive writer lock and announce startup afterward.
2. Read nonterminal receipts and work with pending transitions from Beads.
3. For each, inspect the recorded external locator and the work bead's
   `last_transition`. Settle already-applied effects before replaying anything.
4. Reconcile native terminal turns and resume only tasks that have actual work.
5. Admit new automatic dispatch after this initial reconciliation; independent
   unresolved beads remain visible without blocking every project.

This addresses the old failure in which a live socket masked a dead fallback
loop. Critical task supervision and `doctor` detect the difference directly.

### 7. A manually unarchived task has an expired completion deadline

1. Inspect the retained task record's archive state and native state.
2. If the one automatic archive was already requested/done, treat unarchive as
   human intent and suppress further automatic archival.
3. Leave the bead closed unless the human explicitly reopens or enters new work.
4. Release unnecessary Fulcrum subscriptions independently of sidebar state.

Use one automated scenario with an injected clock and unarchive event. No real
ten-minute sleep or recurring archival validation job is needed.

### 8. Hard reset loses the old ledger midway

1. The temporary stock Beads reset workspace holds the one active reset receipt
   and exact target inventory after old workflow is stopped.
2. On retry, already-deleted native tasks/worktrees count as settled.
3. Resume remaining cleanup, then initialize clean normal Beads and leadership.
4. Remove the temporary reset workspace only after the clean installation exists.
   A subsequent invocation can observe completed clean setup without importing
   old work or treating absent old resources as failures.

The temporary reset workspace is not kept as a second operational store. If it
cannot be initialized, do not begin destructive multi-resource cleanup; continue
dependency repair and report a concrete degraded result.

## Capabilities that failures do not justify removing

The [capability audit](audit.md) found omissions in the initial replacement
documents. Operational defects are reasons to repair a capability's implementation,
not automatically to delete the capability:

- Partial cost data calls for explicit coverage, correct managed-task attribution,
  and retained rate provenance. It does not justify removing the implemented
  `usage` and `cost` reporting surfaces.
- Publication failures call for independent receipts and inspected remote results.
  Plans, incremental graph refinement, future plans, and curated knowledge still
  need a durable authoring/publication path. Keep the shared ledger in `~/brain`
  and push pending changes to its GitHub remote every five minutes; failed remote
  publication remains visible without revoking accepted local work.
- Fragile source reload and recovery require a quiescent installed-package swap
  and a separately installed emergency launcher. A recovery command that cannot
  import when the main environment breaks is insufficient.
- Archive and resource failures require independent ownership, visibility, loading,
  and active-work facts. Preserve task continuity and associated-plan visibility
  without retaining idle subscriptions or restoring repeated archive timers.
- Broken prerequisites must not disable investigation. Setup still diagnoses
  missing capabilities, and compaction still restores concise context; neither
  should impose a healthy-world gate on role entry.

These findings come from comparing existing code and product contracts, not from
additional live incidents. Detailed provenance and intentional removals are in
the audit. No new production experiment was necessary for this comparison.

## Focused verification

The highest-value automated coverage is at the public CLI boundary with a real
isolated stock Beads backend and deterministic runtime/delivery providers. Inject
response loss, applied-but-unacknowledged effects, stale ownership references, and terminal
events; observe final ownership and external source outcomes. Do not grow a test
suite that merely detects prompt wording or internal table/module changes.

The separate 30-task Luna/low smoke is a short native concurrency sanity check.
It can establish task starts, real tool calls, completion, and subscription
release within its deadline. It cannot certify long-term absence of memory leaks
or prove that every task at arbitrary model/tool intensity will have the same
resource footprint. No prolonged stress suite or manual role interview program
is required by this design.

## Replacement validation after specification review

The current requirement adds real Luna workflows for all eight roles and observed
Tollgate/Git delivery, bounded to 50 minutes, followed by the ten-minute concurrency
smoke. Four active managed tasks is the default, with no recovery reserve; the
smoke explicitly raises its isolated limits to 30. Independent reviews are ordinary
Codex tasks. Historical helper observations above describe the old incident, not
a native-subagent subsystem to implement. [The task plan](plan/README.md) assigns
these checks; this appendix does not assert that they have passed.
