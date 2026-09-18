# Fulcrum end-to-end validation campaign

This document is one continuous goal for a sequential live-validation campaign.
It extends the basic Weaver-to-delivery exercise through five increasingly
demanding scenarios. The same coordinating Codex task owns the campaign from
scenario 1 through scenario 5; do not create a separate goal or coordinating task
for each scenario. Each scenario uses real Codex tasks, the real Fulcrum ledger
and controller, real isolated worker worktrees, real Tollgate validation, and an
observed source-remote update. Provider doubles and unit tests are useful while
fixing a defect, but they do not pass a scenario.

The campaign is deliberately gated. Work on scenario 1 until it is correct and
fast, then scenario 2, and so on. Do not run later scenarios merely to collect a
larger failure list. A scenario is ready only after two consecutive clean live
runs meet every behavioral assertion and the wall-clock target. If a fix changes
an earlier scenario's boundary, rerun that earlier scenario before continuing.

## Overall goal and operating contract

Repeatedly run the current Fulcrum end-to-end scenario, fixing every
problem found after each iteration and optimizing the critical path once the
behavior is correct. Create new independent Weaver tasks in the Fulcrum project
when the scenario calls for them. Invoke the `$weaver` skill and give each Weaver
only the short single-sentence task specified by the scenario. Do not create a
worktree for a Weaver task; Fulcrum must prepare worker worktrees itself.

I authorize unlimited mutations to Fulcrum's disposable local workflow data and
to the Fulcrum codebase for this campaign. There is no live Fulcrum workflow data
to retain. This is explicit HUMAN break-glass authority over other agents for the
bounded scenario. A request for human approval, credentials, or policy authority
inside that already-authorized boundary is a product bug unless an actual
external credential or irreversible third-party decision is missing. Do not use
the `access_programs` parameter. One-off pre-existing CI defects may be repaired.

At the start of every iteration, discover the currently registered Steward,
Marshal, and Vizier from live Fulcrum status. Never copy task IDs from an earlier
run or from this document. Record their task IDs before creating scenario work.
The current standing records can be inspected with:

    .venv/bin/fulcrum status --json | jq '.result.desktop.standing'

Use only the roles named by the scenario. Verify that other standing roles do not
receive scenario work, create workers, or mutate the scenario beads. Their normal
unrelated health checks are not scenario participation, but must not alter the
scenario.

For every iteration:

- Record the local timezone and the exact start event defined by the scenario.
- Record every bead, operation, action, native task, turn, assignment, candidate,
  provider run, source commit, integration commit, and remote commit involved.
- Observe the workflow rather than implementing the requested change from the
  coordinating task.
- Verify every role title, ownership transfer, worktree boundary, finish outcome,
  and final-tool discipline required by the scenario.
- Verify that an accepted Executor finish seals implementation and that an
  accepted Warden finish is followed by no additional Warden action.
- Verify exact-source CI, promotion, source synchronization, and cleanup from
  durable facts. Agent narration and a green command exit are not delivery proof.
- Verify worker tasks remain visible until at least ten minutes after that
  individual task completes. The archive action itself should be quick. Standing
  Steward, Marshal, and Vizier tasks must never be automatically archived.
- Briefly analyze the bounded run in the manner of the `fulcrum-postmortem`
  skill: reconstruct the critical path first, distinguish confirmed facts from
  inference and unknowns, identify observability gaps separately, and map every
  fix to evidence. Do not create a postmortem document unless separately asked.
- Profile material delays with Fulcrum's operation/profile tools and distinguish
  model time, native task operations, Beads work, provider CI, promotion, source
  synchronization, cleanup, and the intentional archival grace period.
- After a defect is understood, add the narrowest deterministic regression test,
  implement the fix, run the complete repository check, and commit and push it
  using the repository's normal one-off development rules. A committed local
  master must affect the next operation without installation, activation, or a
  controller restart.
- Reset only disposable scenario state as needed, then rerun the same scenario.
  Do not advance until two consecutive live runs pass.

Measure wall-clock duration from the scenario's named start event through the
named completion event. Include model turns, controller work, provider waits,
promotion, and source synchronization. Prepare any explicitly described fixture
before the clock starts and clean it up after recording the run. Report both the
total duration and stage durations; do not subtract unexplained gaps.

Correctness outranks speed. Never relax ownership, isolated-worktree, exact-source
review, CI, one-task-commit, promotion, remote synchronization, or durable-receipt
requirements to meet a target. Faster models and lower reasoning effort are valid
experiments after a correct baseline, provided model and effort are recorded. If
the target appears to require a major architectural change, mark the campaign
blocked only after repeated live evidence identifies the same irreducible critical
path and smaller fixes have been exhausted.

Remain in this one overall goal while advancing through the scenarios. After a
scenario satisfies its advancement gate, immediately continue to the next
scenario in the same coordinating task. Do not mark the overall goal complete,
pause it, or hand it off merely because an individual scenario passed.

### Advancement gate

A scenario advances only when all of the following are true:

1. Two consecutive live runs satisfy every scenario assertion without manual
   workflow repair.
2. Both runs meet the target duration using the specified start and end events.
3. The repository check passes at the final source revision.
4. The source remote contains every required integration commit.
5. Fulcrum reports no unresolved ownership, delivery, cleanup, or recovery gap for
   the scenario beads.
6. Every discovered defect has either a proved fix or a clearly justified,
   non-blocking deferred action.
7. The brief run analysis can be reconstructed from stable identifiers and
   timestamps without relying on agent recollection.

The first run is exploratory and may fail. After any code or policy change, the
two-run clean streak starts again. Cosmetic documentation-only corrections that
cannot affect execution do not reset the streak.

## Scenario 1: single-bead delivery baseline

### Purpose and target

Prove the shortest normal path remains healthy before exercising exceptional
behavior. This is the regression form of the original task-creation experiment.

- **Start:** submission of the single sentence to the new Weaver task.
- **Finish:** the promoted integration commit is observed on the configured source
  remote.
- **Target:** less than **5 minutes** wall clock.

### Test task

Use a harmless one-file documentation change whose precondition is verified before
the run. Vary the exact wording between iterations so that every run produces a
real commit. Example:

```text
Rename the README heading “Control flow” to “Workflow control”.
```

Reverse the change on the next iteration, or use a similarly bounded heading or
sentence edit. Do not add acceptance criteria or orchestration advice to the
Weaver prompt.

### Expected behavior

- A new project-local task invokes `$weaver`; it does not use a Codex worktree.
- Weaver creates exactly one work bead and renames its own task to a concise title
  such as `🧵 [wvr-1234abcd] Rename the README heading`.
- Weaver records a proportionate behavioral summary and nonempty observable
  acceptance criteria, finishes `ready`, and ends. It does not edit the repository,
  prepare a worktree, create a worker, poll the controller, or continue after the
  accepted finish.
- Steward observes ready work without a Marshal approval turn, prepares the
  isolated worktree, and creates exactly one Executor titled like
  `⚒️ [exe-1234abcd] Rename the README heading`.
- Executor registers before repository inspection, edits only the assigned
  worktree, performs proportionate checks, creates exactly one task commit atop
  the retained base, finishes `ready_for_review`, and ends with `finish` as its
  last tool action.
- Steward waits for actual native completion, transfers the same logical work slot,
  and creates exactly one Warden titled like
  `🛡️ [war-1234abcd] Review README heading rename`.
- Warden registers once with its own exact task and session identity, performs a
  concise independent review, submits the current candidate without inventing a
  source identifier, and makes one blocking CI wait. It does not agent-poll.
- Passing CI causes the Warden's next and final tool call to be `finish` with
  `approved` and the exact submitted source.
- Fulcrum observes approval, promotion, remote synchronization, and worktree
  cleanup independently before closing the bead.
- Marshal and Vizier do not participate. No duplicate task, candidate, finish,
  promotion, or source push is produced.

### Evidence and failure signals

The final evidence must join the Weaver, bead, Executor, Warden, candidate, task
commit, integration commit, and remote commit. Treat any of the following as a
failure even if the requested text eventually changes:

- stale standing-task IDs were used;
- Weaver or Executor continued after accepted finish;
- a worker edited the main checkout;
- Marshal approved ordinary ready work;
- Warden supplied a guessed source or candidate identifier;
- CI passed a different source from the approved source;
- promotion was inferred from CI or narration;
- remote synchronization or cleanup remained pending at closure;
- a worker was archived before its ten-minute grace period.

## Scenario 2: concurrent overlapping changes and integration conflict

### Purpose and target

Prove that two legitimate workers can execute concurrently while delivery remains
serialized, and that a textual integration conflict is resolved without losing
either accepted outcome, duplicating workers, or bypassing review.

- **Start:** submission of the first of the two Weaver sentences; submit the second
  within ten seconds.
- **Finish:** the source remote contains both delivered changes and both beads have
  settled delivery and cleanup.
- **Target:** less than **8 minutes** wall clock.

### Fixture and test tasks

Before timing, prepare a checked-in disposable Markdown fixture containing an
ordered list and an explicit invariant that independently added entries must all
be preserved. Both tasks must begin from the same integration base and edit the
same insertion region. Do not pre-resolve the expected merge.

Create two independent project-local Weaver tasks with only these sentences:

```text
Add an “Alpha” entry to the concurrency fixture list.
```

```text
Add a “Beta” entry to the concurrency fixture list.
```

If the provider merges the two additions automatically, the scenario still tests
concurrent execution and serialized integration but does not count as a conflict
run. Adjust only the disposable fixture on the next iteration until the provider
reliably exposes a real conflict. Do not make the requested outcomes mutually
exclusive; the correct final document contains both entries exactly once.

### Expected behavior

- Each Weaver creates one distinct bead, uses its own `wvr` title, records concise
  scope, and stops without implementation.
- Steward admits both beads when capacity permits and creates two distinct
  Executors with distinct worktrees, assignments, branches, and task titles.
- The Executors may overlap in wall time. Neither sees or edits the other's
  worktree, and neither uses the main checkout.
- Each Executor creates exactly one commit atop its retained base and ends after
  an accepted `ready_for_review` finish.
- Steward creates one Warden per bead. Wardens review only their assigned source.
- Tollgate serializes integration. The first candidate may promote normally. The
  second must be evaluated against the now-current integration base rather than
  blindly promoted from stale ancestry.
- If the second candidate conflicts, that bead remains owned by its existing
  Warden. The Warden receives actionable retained evidence, resolves the conflict
  in the same assigned worktree, preserves both Alpha and Beta, restores exactly
  one task commit atop the current retained base as required, submits a new exact
  source, and waits again.
- A conflict is not routed back to the old Executor, treated as human approval,
  or solved by force-pushing, deleting the other change, or editing the main
  checkout.
- Each Warden approves only a source that passed CI. Both integration commits are
  observed on the source remote, both worktrees are cleaned, and both beads close.
- Marshal and Vizier remain uninvolved unless a deterministic repair allowance is
  exhausted. A first ordinary merge conflict must not wake either role.

### Evidence and failure signals

Record overlap between Executor turns, both retained bases, provider admission
order, the changed integration base, conflict evidence, repaired source, both CI
runs, and remote ancestry. The final file content alone is insufficient.

Fail the run for lost entries, duplicate entries, duplicate workers, a second
candidate submitted without reconciling an uncertain first submission, stale
approval surviving a source change, parallel promotion that violates provider
serialization, or either bead closing before its own delivery obligations settle.

## Scenario 3: expected CI failure and same-Warden repair

### Purpose and target

Prove that a real, actionable CI failure returns through the Warden's single
blocking wait, remains in the same task and assignment, consumes one bounded
repair cycle, and ultimately delivers a corrected exact source.

- **Start:** submission of the Weaver sentence.
- **Finish:** the corrected integration commit is observed on the source remote.
- **Target:** less than **8 minutes** when the configured CI run completes within
  30 seconds; less than **12 minutes** when exercising a real five-minute CI job.

### Fixture and test task

Use a disposable project fixture with a two-file invariant. The Executor's natural
focused check must pass after changing the primary file, while the configured
Tollgate validation deterministically fails until a related generated/manifest
file is updated. The CI diagnostic must clearly identify the violated invariant.
Do not add a random failure, a one-shot provider error, or a test that passes on
an unchanged resubmission: the Warden must make a real source correction.

Create one project-local Weaver task with only a sentence such as:

```text
Change the CI repair fixture value from “old” to “new”.
```

Do not tell Weaver or Executor how CI is expected to fail. If Weaver's legitimate
investigation discovers the invariant and scopes both files, reset the disposable
fixture and choose another equivalent invariant; do not penalize correct scoping
or weaken the worker instructions merely to manufacture a failure.

### Expected behavior

- The normal Weaver, Steward, and Executor path occurs once. Executor's submitted
  source is a valid one-commit candidate and its reported focused checks are
  truthful, even though provider validation will find the additional defect.
- Warden reviews, submits once, and calls the blocking CI wait once for that
  candidate. It remains in the same native turn, assignment, worktree, and worker
  slot while CI is pending. Steward may perform unrelated work; Marshal receives
  no routine status message.
- The wait returns a terminal failed result with the exact candidate, source,
  failed check, and bounded diagnostic. Warden does not poll, start a new turn,
  create a new task, or send the work back to Executor.
- Fulcrum records one repair cycle from retained failure evidence. Unchanged status,
  transport retries, and local edit/test iterations do not increment that count.
- Warden repairs the actual two-file invariant, runs a focused local check,
  rewrites the complete task result to exactly one commit atop the retained base,
  submits the new exact source, and makes one new blocking wait.
- The old failed CI evidence and source remain historical. They cannot approve or
  promote the repaired source.
- Passing CI on the repaired source is followed immediately by Warden's final
  approved finish. Fulcrum promotes, synchronizes, cleans, and closes normally.
- Marshal and Vizier do not participate because the bounded deterministic repair
  succeeds within the ordinary allowance.

### Evidence and failure signals

Join the two source OIDs, two candidate IDs, both CI results, the repair-cycle
record, one Warden task/turn/assignment, final approval, promotion, and remote
commit. Fail the run if the Warden returns `pending` and agent-polls, a second task
is created, the failed source is promoted, the old approval survives the edit,
repair count changes for a transport retry, or the task closes without retaining
the failed diagnostic.

## Scenario 4: interrupted Steward action and idempotent recovery

### Purpose and target

Prove that Fulcrum recovers an interrupted or uncertain native creation at the
durable action boundary, resumes the same standing Steward, and never creates a
duplicate worker or loses the bead.

- **Start:** submission of the Weaver sentence.
- **Finish:** the recovered workflow's integration commit is observed on the
  source remote.
- **Target:** less than **8 minutes** wall clock when the immediate failure alert
  path is available. Falling back to the 15-minute Marshal heartbeat is a failed
  latency run even if it eventually recovers correctly.

### Fault injection and test task

Create a normal, harmless one-file task with a new project-local Weaver, for
example:

```text
Add a short “Recovery fixture” sentence to the validation fixture.
```

After Weaver finishes and Steward claims the durable Executor-creation action,
inject exactly one bounded fault at a recorded boundary: end Steward's active turn
after native creation is issued but before Fulcrum records the returned task ID,
or cause the creation reply to become uncertain while leaving the provider's
actual outcome observable. Do not delete the action receipt, bead, or possible
native task. Do not manually resume Steward or repair the bead after injection.

Alternate the two useful cases across iterations:

1. the provider created the Executor but Fulcrum lost the response;
2. the provider definitely did not create the Executor.

### Expected behavior

- Weaver behavior before the injection is identical to scenario 1.
- The action is durable before the native side effect. Uncertainty retains its
  action, reservation, expected result, and possible task locator; it does not
  free capacity or authorize a new unrelated action.
- Fulcrum emits one bounded failure/recovery incident. Marshal, and only Marshal,
  may participate because this scenario requires exceptional recovery. Vizier
  remains uninvolved.
- Marshal receives one coalesced recovery brief with the exact action and current
  facts. It does not reconstruct the request from transcript narration or apply a
  stale whole-backlog snapshot.
- If the task exists, recovery adopts and registers that exact Executor. If it
  definitely does not exist, the same retained creation action is retried safely.
  At no time are two Executors authorized or active for the bead.
- Marshal resumes the same standing Steward task only after reconciling its ended
  turn and action state. It does not create a replacement Steward for a routine
  recoverable interruption.
- The recovered Executor then follows the ordinary one-commit finish path; one
  Warden reviews, waits for CI, finishes, and the commit is promoted and pushed.
- The incident settles with no ownership, capacity, action, or native-task gap.
  Ordinary archival timing begins from each task's actual completion, not from
  the interrupted action.

### Evidence and failure signals

Record the action state immediately before injection, provider truth immediately
afterward, the alert/incident, Marshal decision operation, Steward resumption,
native task identity, and final delivery chain. Distinguish an attempted create,
an observed task, a reconciled action, and a new model turn.

Fail the run for duplicate Executors, a replacement Steward, capacity released
solely because a response timed out, Marshal retrying before observing provider
truth, Vizier involvement, use of a new bead to escape uncertainty, or recovery
that depends on a human manually sending a continuation message.

## Scenario 5: mixed-backlog Marshal grooming under load

### Purpose and target

Prove that Marshal can make compact, current, independent decisions across a
mixed backlog while Steward continues mechanical dispatch, stale judgment cannot
overwrite progress, and routine work completes without turning leadership into a
per-bead approval bottleneck.

- **Start:** creation of the first scenario intake bead.
- **Finish:** every seeded item has reached its expected delivered, duplicate,
  deferred, clarification, or blocked state; all required integration commits are
  observed on the source remote.
- **Target:** less than **12 minutes** total, with the first bounded Marshal
  decision applied within **3 minutes** and the first eligible Executor created
  within **90 seconds**.

### Backlog fixture

Seed 8–12 disposable items through normal public Fulcrum intake paths. Keep the
set small enough to fit one bounded Marshal brief. Include all of the following:

- two independent, high-priority, implementation-ready documentation changes;
- one lower-priority ready change;
- a two-item dependency chain whose second item must not dispatch first;
- two reports describing the same defect, with enough evidence to choose one
  canonical bead;
- one materially ambiguous request that needs a specific Weaver clarification;
- one future-plan or explicitly deferred item whose activation condition is not
  satisfied;
- one item held by a project pause, overlap reservation, or other typed waiting
  reason.

Use harmless fixture files and mutually compatible delivered outcomes. Give raw
intake only the benefit, uncertainty, priority, dependency, and evidence needed
to make the intended choice. Do not pre-write Marshal's decision or use private
database mutations.

After Marshal has received its brief but before it applies the answer, advance
one selected bead through a legitimate concurrent state change. This creates one
stale decision row while leaving the other rows valid.

### Expected behavior

- Steward may immediately dispatch already-authorized ready work without waiting
  for Marshal. Marshal is not an approval gate for those beads.
- One scheduled or explicitly triggered Marshal cycle claims one bounded decision
  operation and receives a compact brief containing the necessary choice,
  evidence, unknowns, priority, dependency, and waiting facts. It does not ingest
  full transcripts or create a second backlog store.
- Marshal identifies the canonical duplicate, preserves its evidence, defers the
  future item with a typed reconsideration condition, requests concrete Weaver
  clarification for the ambiguous item, and leaves typed holds intact.
- Marshal applies valid rows independently. The deliberately stale row conflicts
  explicitly and cannot overwrite the bead's newer owner, phase, scope, or
  progress. The valid rows are not rolled back because one row is stale.
- No second Marshal turn is generated merely because capacity frees, an Executor
  completes, or an unchanged wait remains. Healthy/no-op checks are concise and
  make no native mutations.
- Steward fills available capacity in priority order, respects dependencies and
  holds at actual start, and dispatches newly clear authorized work without a new
  Marshal approval turn.
- At least the two high-priority ready changes complete the full
  Executor-to-Warden-to-Tollgate-to-remote path during the scenario. The dependent
  child begins only after its prerequisite's observed completion if it is among
  the required deliveries.
- Vizier remains uninvolved. No Justiciar is created for ordinary grooming,
  duplicate handling, a stale row, or a typed wait.
- Final status truthfully distinguishes delivered, duplicate, deferred,
  clarification, and held work. It does not close or silently discard the latter
  categories merely to empty the backlog.

### Evidence and failure signals

Retain the initial backlog snapshot, decision brief and operation, per-row apply
results, stale bead transition, capacity reservations, dispatch order, dependency
release, duplicate link, clarification transfer, deferral trigger, and delivery
facts. Measure Marshal model time separately from Steward action and worker time.

Fail the run if Marshal becomes a per-bead ready-work gate, a stale row overwrites
new state, one conflict rolls back valid decisions, a held/dependent bead starts,
capacity is exceeded, freed capacity requires a new leadership approval, Vizier
is awakened, or the total is made fast by dropping unresolved items from status.

## Reporting each scenario

At the end of an iteration, report a compact table with these fields:

| Field | Required value |
| --- | --- |
| Scenario and iteration | Stable scenario number plus attempt number |
| Boundary | Exact start and finish events, timezone, and wall-clock duration |
| Result | Passed, failed, or invalid fixture |
| Actors | Standing IDs and every scenario worker task ID |
| Durable work | Bead, ownership operation, action, incident, and repair IDs |
| Source and delivery | Base, submitted, tested, approved, integration, and remote OIDs |
| Stage timing | Weaver, admission, Executor, handoff, Warden, CI, repair, promotion, sync, cleanup |
| Defects | Confirmed cause, contributing factors, and observability gaps |
| Changes | Fix commit, regression test, full-check result, and live proof |
| Advancement | Current clean-run streak and whether the next scenario is unlocked |

Use `unknown` for an unobservable duration or causal link. Never silently allocate
an unexplained gap to Beads, a model, the network, Codex, or Tollgate. Archive
timing is reported separately from the measured completion path because the
intentional grace period begins after task completion.

## Campaign completion

The campaign is complete when scenario 5 has passed its advancement gate and no
earlier scenario invalidated by later fixes remains unrerun. The final handoff
should summarize:

- clean-run durations and target margins for all five scenarios;
- the largest confirmed bottlenecks and their measured improvements;
- every system rule intentionally changed or relaxed, or an explicit statement
  that none were relaxed;
- model/effort choices and their observed reliability tradeoffs;
- unresolved risks, inferred causes, and observability gaps;
- final repository, integration, and source-remote revisions;
- confirmation that no `access_programs` parameter was used and no postmortem
  document was created as part of the routine run analysis.
