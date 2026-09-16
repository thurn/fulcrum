# Bead `fc-1ba7f515` Weaver metadata delivery postmortem

**Incident date:** 2026-09-15 PDT (2026-09-16 UTC)  
**Timezone:** America/Los_Angeles (PDT, UTC-07:00)  
**Status:** Source recovered and delivered after 29 minutes 40 seconds; workflow still not closed at the investigation cutoff  
**Severity:** Critical workflow failure; no user-data loss or production outage  
**Bead:** `fc-1ba7f515`  
**Incident source revision:** `afb013db1ec82a319331f2250ca027f63221896c`  
**Initial candidate:** `07470890825f948a375e27822088b8a93d26200e` (not delivered)  
**Recovered and delivered revision:** `a6ab985c1d98ce79ddcc30154b531acd4fb1eeee`  
**Investigation cutoff:** 2026-09-15 21:40:49 PDT

## Executive summary

The request was a two-line metadata change: expose the existing Weaver skill as
`$weaver` and display it as `Weaver`, while retaining the
`skills/fulcrum-weaver/` directory and explicit-only invocation policy. The
initial implementation did exactly that in commit `0747089`. It did not ship.

The immediate trigger was one pre-existing, worktree-sensitive test failure in
`tests/test_fulcrum2_installation.py`. Both Executor and Warden knew about the
failure and classified it as unrelated. That was contrary to the Warden's role:
failed validation remained Warden-owned and should have been repaired before
approval. The deterministic root cause that turned this correctable CI failure
into a stuck workflow was in the Warden finish transition. Child validation
operation `fc-d5f9a1efd9fc5f45a6e4b4ca7e17b2f0` failed before creating retained
delivery state, but the parent finish still recorded
`warden_judgment_sealed`, set `delivery_finish.state` to
`waiting_for_validation`, and instructed the Warden to end. The controller then
polled a delivery that did not exist, repeatedly received
`DELIVERY_NOT_STARTED`, and had no transition back to review.

The latency failure was independent and substantial. The initial Weaver finish
receipt completed at 21:10:35.239, but the Marshal turn did not start until
21:12:01, an 85.8-second handoff. A 45.2-second remote ledger sync occupied the
same background worker that performs reconciliation, preventing the eligible
Marshal decision from running. The complete change did not reach local `master`
until 21:38:07—29 minutes 40 seconds after the Weaver turn started. At the
investigation cutoff, the source was live and all 144 tests passed, but the Bead
still reported `in_progress/delivering` with a stale initial
`delivery_finish.source_oid` and a misleading next action.

The actual path was:

```text
Weaver (2m20s)
  -> 85.8s handoff, including 45.2s remote ledger sync
  -> Marshal (34.0s)
  -> 30.6s dispatch/worktree preparation
  -> Executor (4m20s)
  -> 34.9s handoff
  -> Warden review (2m48s)
  -> CI failure (7.2s check)
  -> invalid sealed state / no delivery record
  -> 10m23s until recovery turn
  -> failed recovery submissions x2
  -> repaired candidate validation, approval, promotion, synchronization
  -> local master fast-forward at 29m40s
  -> closure still blocked
```

No user data was lost. The initial candidate remained recoverable, and the final
source was eventually delivered. The incident affected delivery correctness,
latency, agent cost, and the reliability of Fulcrum's reported state.

## Investigation boundary

The incident begins with the initial Weaver native turn at
2026-09-15 21:08:27 PDT. It includes the original scope, Marshal dispatch,
Executor implementation, Warden review, failed validation, stuck controller
polling, and the later Warden recovery through the final recovery turn's terminal
observation at 21:40:49.

The delivery-duration endpoint is the local `master` fast-forward at 21:38:07,
because local `master` is the repository's authoritative live source. The
workflow-completion endpoint does not exist: the Bead was still open at the
cutoff. Postmortem investigation commands from task
`01a0a877-3623-7110-a393-b5f77a2f6a03` are excluded from workflow durations and
operation counts. Concurrent Beads are excluded.

Timestamps from Fulcrum and Git were recorded on the same host. Native task
timestamps are second-granularity; operation receipts preserve microseconds.

## Impact and expected versus actual

| Stage | Expected behavior | Actual boundary and duration | Result |
| --- | --- | --- | --- |
| Scope | Recognize and scope the two metadata edits | Native Weaver turn 21:08:27-21:10:47, 139.536 s | Correct scope, excessive duration |
| Weaver to Marshal | Coalesce briefly, then start Marshal independent of remote ledger publication | Weaver finish receipt 21:10:35.239 to Marshal native start 21:12:01, 85.8 s | 45.2 s remote sync blocked the worker |
| Marshal | One bounded decision | Native turn 21:12:01-21:12:35, 33.968 s | Correct decision |
| Dispatch | Prepare an owned worktree and begin Executor | Marshal authorization 21:12:31.416 to Executor turn observation 21:13:02.744, 31.3 s | Included 9.376 s worktree preparation and a 20.050 s dispatch command |
| Executor | Make and verify two metadata edits | Native turn 21:13:02-21:17:22, 259.589 s | Correct initial candidate, but known full-suite red remained |
| Executor to Warden | Start Warden after terminal/release observation | Executor finish receipt 21:17:17.850 to Warden turn observation 21:17:52.790, 34.9 s | Slow but successful |
| Warden | Repair all validation failures, then approve exact green source | Warden entry to failed validation 21:17:52.790-21:20:40.634, 167.8 s | Known failure was not repaired; finish sealed anyway |
| Failed CI recovery | Return immediately to the same Warden with the failed receipt and an unsealed finish | No recoverable transition; delivery was absent and controller polls failed | Deterministic dead end |
| Recovery | Amend the candidate once, validate once, and resume controller-owned delivery | Recovery turn 21:31:04-21:39:41, 516.410 s; two failed submissions before the accepted one | Eventually delivered, with avoidable retries |
| Source live | Local and remote `master` contain the approved source | Local fast-forward at 21:38:07, 29m40s after Weaver start | Delivered far too late |
| Closure | Close Bead after observed promotion, sync, and cleanup | Still `in_progress/delivering` at 21:40:49 | Source and workflow state disagree |

The source change itself was initially two insertions and two deletions across two
files. Recovery expanded it to three files because the Warden also corrected the
worktree-sensitive test. The recovered test change was legitimate, but it should
have occurred during the first Warden review, not after a dead-end state.

## Actors and durable identifiers

| Role | Native task/thread | Native turn(s) | Ownership/entry operation |
| --- | --- | --- | --- |
| Weaver | `01a0a866-66d4-7df3-ab24-232eb00668b6` | `01a0a866-d928-7ee3-ada4-082266ad52ac` | `fc-8ca37efe1e4d49e898157b4712e92ad0` |
| Marshal | `01a0a77c-b76f-7de0-9971-7496424b6116` | `01a0a86a-1c57-7e01-bcd0-5f3acca2e311` | standing leader; request `fc-f9578b1526555249bce474d886173388` |
| Executor | `01a0a86a-ff57-7fb3-b78c-e8605acfdd3e` | `01a0a86b-0b86-70b3-8e38-f6748049ae82` | `fc-9515255e820e566b9a79555cf3fa38fa` |
| Warden | `01a0a86f-6cf4-7842-887d-e7434b5c9992` | initial `01a0a86f-788e-7e13-88c6-fae7dd9bdc6d`; recovery `01a0a87b-8c71-7c12-8084-f6dcc952e1d0` | `fc-945f955c85d35d56ba9fbad689ad8d05` |

Key delivery identifiers:

- Work creation: `fc-d112fb531d8f54b4bf8ccdef1426f591`
- Weaver finish: `fc-2d3225f48d0d402bbcd7ecdd709cf9cb`
- Ledger sync on the first handoff: `fc-775d34deb7d554e89fb77ce3520450c4`
- Executor finish: `fc-edb5d7aa00f64de8bef0d6bff5379240`
- Initial Warden finish: `fc-d2184cf723c84748aaf340092bbaf03c`
- Failed child validation: `fc-d5f9a1efd9fc5f45a6e4b4ca7e17b2f0`
- Accepted recovery provider handle: `01a0a87f-15dd-7151-a547-ec086f403546`
- Recovery approval: `fc-3047ad50eba34036b3082d74cd34c370`
- Recovery promotion: `fc-7ce55ea2ab5446198ab36a6b350d2236`
- Recovery source synchronization: `fc-40131d19e839422bb21ee50ddaafd145`

## Detailed timeline

All times are PDT on 2026-09-15.

| Time | Event | Durable evidence / gap |
| --- | --- | --- |
| 21:08:27 | Weaver native turn starts. | Native task record; second-granularity timestamp. |
| 21:08:40.289 | Work creation begins; Bead `fc-1ba7f515` is created. | `fc-d112...`; completed at 21:08:40.854. |
| 21:08:42.231-21:08:43.973 | Weaver entry binds the task. | `fc-8ca37...`. |
| 21:10:34.170-21:10:35.239 | Weaver returns the exact two-file scope and transfers the Bead to backlog/Marshal. | `fc-2d322...`. |
| 21:10:37.898-21:10:49.975 | First reconciliation pass observes the work and creates a pending Marshal decision. | Pass `76e79b16-...`; eligible at 21:10:51.411. |
| 21:10:47 | Weaver native turn ends. | Native task record. |
| 21:10:59.812-21:11:45.043 | Automatic `ledger.sync` pushes 14 changed Beads records to the remote brain ledger. | `fc-775d34...`; 45.232 s. This was not required to decide or dispatch this source change. |
| 21:11:47.066 | The next reconciliation pass finally starts, 55.655 s after the Marshal decision became eligible. | Global diagnostic journal. |
| 21:12:00.631 | Marshal request receipt is created. | `fc-f9578...`. |
| 21:12:01 | Marshal native turn starts. | Native task record. |
| 21:12:31.416 | Marshal authorizes Executor dispatch. | Retained dispatch authorization. |
| 21:12:32.167 | Marshal request closes with decisions applied. | `fc-f9578...`. |
| 21:12:45.865-21:13:04.801 | Dispatch operation runs for 18.936 s. | `fc-66f21...`; command event measured 20.050 s. |
| 21:12:47.226-21:12:56.602 | Owned worktree preparation takes 9.376 s. | `fc-f93401...`. |
| 21:13:02.744 | Executor native turn is observed in progress. | `fc-951525...`. |
| 21:15:55 | Executor commits `0747089`, changing only the two requested metadata values. | Git commit. |
| 21:16:42.186 | Executor records that 21 focused tests passed and a broader run had one worktree-source-root failure. | Bead `last_progress`. |
| 21:17:16.363-21:17:17.850 | Executor finish is accepted and exact source `0747089` is handed off. | `fc-edb5d...`. |
| 21:17:52.790 | Warden native turn is observed in progress. | `fc-945f95...`; 34.9 s after Executor finish. |
| 21:20:29.729 | Warden calls finish despite having reproduced the broader-suite failure and describing it as unrelated. | `fc-d2184...` and Warden evidence. |
| 21:20:30.669-21:20:40.634 | Child validation runs `scripts/check`; formatting and Pyre pass, but 1 of 144 tests fails. The complete check takes 7.21 s. | `fc-d5f9a...`, `DELIVERY_REJECTED`. |
| 21:20:42.224 | Parent finish nevertheless closes as `warden_judgment_sealed`, reports `accepted: true`, and stores `waiting_for_validation`. No retained delivery/provider handle exists. | `fc-d2184...`. |
| 21:22:28-21:30:06 | Controller reconciliation repeatedly calls `promotion show`; calls fail with `DELIVERY_NOT_STARTED`. | Global event journal. The work remains `delivering`. |
| 21:25:14-21:26:04 | A later Warden turn inspects context and both finish/validation receipts but does not yet repair source. | Native task record and command journal. |
| 21:28:36.718 | Initial postmortem evidence capture records the Bead still `in_progress/delivering`, `delivery: null`, and `delivery_finish.state: waiting_for_validation`. | Read-only evidence bundle. |
| 21:31:04 | Warden recovery turn begins. | Native turn `01a0a87b...`. |
| 21:34:08.527 | First recovery validation submits `aaeca6...`, which incorrectly includes unpromoted candidate `0747089` as an ancestor. Tollgate rejects it and tells the Warden to rebase one task commit onto release `afb013d`. | `fc-e6291...`, `DELIVERY_UNCERTAIN`. |
| 21:34:27 | Warden creates squashed recovery commit `a6ab985c...` on the promoted release. It contains the metadata change and a worktree-independent test fix. | Git commit. |
| 21:34:40.530-21:34:41.349 | Second recovery validation uses mistyped OID `a6ab9856...`; Git rejects the nonexistent revision. | `fc-e33ac8...`, `DELIVERY_REJECTED`. |
| 21:34:51.153-21:34:57.677 | Correct source `a6ab985c...` is submitted and provider handle `01a0a87f-...` is retained. | `fc-7a677d...`. |
| 21:35:59.264-21:36:33.477 | Another validation-start call observes the retained provider submission. | `fc-7f3a13...`; 34.213 s. |
| 21:36:44.723-21:36:46.019 | Warden directly approves the exact recovered source. | `fc-3047ad...`. |
| 21:36:47.638-21:36:54.167 | Warden directly starts promotion; provider reports promotion, synchronization, and cleanup complete. | `fc-7ce55e...`. |
| 21:37:33.613-21:37:35.020 | Source synchronization operation confirms remote `master` contains `a6ab985c...`. | `fc-40131...`. |
| 21:38:07 | Local `master` and `origin/master` fast-forward to `a6ab985c...`. | Git reflog and refs. This is the delivered-source endpoint. |
| 21:39:41 | Warden recovery turn ends after 516.410 s. | Native task record. |
| 21:40:49 | Final read shows Warden idle and Bead still `in_progress/delivering`; `delivery.source_oid` is `a6ab985c...`, but sealed `delivery_finish.source_oid` remains `0747089...`. | Work and task status. |

## Causal analysis

### Trigger: a pre-existing test encoded the wrong source invariant

The failed test expected a newly prepared setup to point at
`Path(__file__).parents[1]`. In an isolated worktree, that resolves to the
worktree. The production contract deliberately points to canonical
`~/fulcrum`, so the test failed with:

```text
PosixPath('/Users/dthurn/fulcrum') !=
PosixPath('/Users/dthurn/fulcrum/.worktrees/codex-fc-1ba7f515')
```

This failure was already present at incident base `afb013d`; candidate `0747089`
did not touch the test or runtime. Recovery commit `a6ab985c` corrected the test
by patching `master_source_root` to a controlled canonical-master path and
asserting that result.

The test defect was the CI trigger, not a sufficient explanation for the
delivery failure. A working Warden loop would have fixed it during initial review
and resubmitted the amended exact source.

### Contributing agent failure: Warden waived a known red gate

The Executor disclosed the failing broader run. The Warden reproduced it and
recorded the check as passed because it considered the failure unrelated. That
judgment violated the assigned contract: “Fix defects yourself in the same
worktree” and “Failed validation remains yours.” It also contradicted the actual
delivery configuration, which runs all of `scripts/check`, not only a
change-scoped subset.

This was a material contributing factor. If the Warden had repaired the test
before its first finish, validation would have passed and the broken failure path
would not have been exercised.

### Primary root cause: synchronous validation failure was sealed as approval

At incident revision `afb013d`, `CompletionService._warden_finish` did the
following:

1. Called `validation_start` when no matching retained validation existed.
2. Reloaded the work and checked only `work.fc.delivery.validation` for a failed
   state.
3. When validation failed before retaining a delivery object, observed
   `delivery is None`, not `validation.state == failed`.
4. Fell through to the success path, wrote `delivery_finish` with
   `waiting_for_validation`, recorded the task finish, and closed the parent
   operation as `warden_judgment_sealed`.

The failed child result was available in `children["validation"]`, but the
success decision ignored it. An existing helper named `_finish_child_unresolved`
would have returned the work to review, but it was not called anywhere in this
path.

The counterfactual is strong: if the parent transition had treated the failed
child receipt as unresolved, it would not have sealed Warden judgment, the work
would have returned to `reviewing`, and the same Warden could have amended the
test and submitted a new source normally.

### Recovery defect: the controller cannot reconcile a sealed finish with no delivery

`ControllerSupervisor._advance_warden_delivery` begins by calling
`promotion show`. With no retained delivery, that command raises
`DELIVERY_NOT_STARTED`. The controller catches the error and returns
`advanced: false`; it does not inspect the Warden finish's failed child, clear the
seal, or transition back to review. The async-failure recovery branch is reached
only when `promotion show` returns structured delivery facts with
`validation == failed` or `promotion == failed`.

The existing regression test covered an asynchronous provider failure with a
retained delivery object. It did not cover synchronous validation failure before
provider submission—the exact incident mode.

### Recovery defect: repaired source did not invalidate the old sealed judgment

The later Warden recovery created and delivered source `a6ab985c`, but
`delivery_finish` remained sealed to initial source `0747089`. Because one
accepted finish is immutable, the Warden could not produce a new finish for the
repaired source through the intended protocol. It instead drove validation,
approval, promotion, and synchronization directly. The source shipped, but the
controller's closure state continued to describe the obsolete candidate.

### Closure defect: provider states and closure guard use incompatible values

After successful recovery, delivery reconciliation stored provider states
`promotion: promoted`, `synchronization: complete`, and `cleanup: complete`.
`work._delivery_settled`, however, accepts `promotion.state == observed`,
`synchronization.state in {observed, not_required}`, and
`cleanup.state == observed`. The Warden specifically observed the cleanup
`complete`/`observed` mismatch while attempting closure. At cutoff all three
provider-facing values differed from the guard's internal vocabulary. This left
the Bead open despite proven delivery.

### Latency root cause: remote ledger publication serialized behind reconciliation

The background worker runs `supervisor.run_once()` and only then awaits
`supervisor.publication.tick()` in the same process. The resident permits only
one `background` job at a time. During the initial handoff:

- reconciliation made the Marshal decision eligible at 21:10:51.411;
- automatic ledger sync ran from 21:10:59.812 to 21:11:45.043;
- the next reconciliation did not start until 21:11:47.066;
- Marshal did not start until 21:12:01.

The 45.232-second ledger sync is confirmed by its durable operation receipt. The
claim that it delayed reconciliation is an inference supported by the serialized
worker source and timestamps; there is no explicit publication span joined to
the reconciliation trace. Remote ledger publication was not required for the
Marshal decision and should not have occupied its execution lane.

### Other latency contributors

- Weaver used 139.536 seconds to scope two known metadata substitutions.
- Worktree preparation took 9.376 seconds, and dispatch took about 20 seconds.
- Executor used 259.589 seconds and ran overlapping focused checks before the
  Warden repeated independent validation.
- Executor-to-Warden handoff added 34.9 seconds.
- Initial Warden review used 167.8 seconds before exposing the known CI failure
  to the delivery gate.
- The recovery turn used another 516.410 seconds and made two invalid validation
  submissions before the accepted one.

These durations are confirmed, but the internal allocation of model time is not.
Historical `fulcrum task output` returned zero items for all three worker tasks
despite native summaries reporting nonzero item counts. It would be speculation
to assign the unattributed seconds to particular shell commands, reasoning, or
network waits.

## Failure inventory

| Failure | Layer | Causal role | Confidence |
| --- | --- | --- | --- |
| Worktree-sensitive test asserted the checkout path rather than canonical master | Test contract | Triggered CI red | Confirmed |
| Executor and Warden treated the known full-suite failure as unrelated | Agent/protocol | Allowed a red candidate to reach finish | Confirmed |
| Warden finish ignored failed child validation when no delivery record was retained | Completion state machine | Primary root cause of stuck workflow | Confirmed |
| Controller handled `DELIVERY_NOT_STARTED` as a passive no-op | Reconciliation | Prevented automatic return to Warden | Confirmed |
| Sealed finish remained tied to obsolete `0747089` after source repair | Exact-source contract | Prevented normal post-repair completion | Confirmed |
| Provider delivery values do not satisfy closure guard vocabulary | Closure bookkeeping | Left delivered Bead open | Confirmed |
| Remote ledger publication shared the reconciliation worker | Performance architecture | Added most of the 85.8-second Weaver-to-Marshal delay | Confirmed design; inferred attribution |
| No enforced latency budget or trivial-work fast path existed | Product/performance | Allowed role and handoff overhead to dominate | Confirmed |
| Recovery first stacked a new commit on the rejected candidate, then used a mistyped OID | Recovery execution | Added retries and delay | Confirmed |
| Public trace omitted most causally relevant operations | Observability | Made routine reconstruction impossible | Confirmed |

## Observability assessment

`fulcrum trace --bead 1ba7f515` returned only five items at the initial evidence
capture: the Warden entry, Warden finish, one native observation, and the
postmortem's own `work show` and `context` reads. It omitted work creation,
Weaver finish, the Marshal request/decision, dispatch, worktree preparation,
Executor entry/finish, the validation child, the blocking ledger sync, and every
controller `promotion show` failure.

Reconstruction therefore required:

- the durable Beads work and operation records;
- global diagnostic JSONL rather than the Bead trace;
- native task summaries;
- Git commits, refs, and reflog;
- Tollgate delivery facts retained after recovery;
- historical source at `afb013d`;
- the Warden's final narration.

Additional gaps:

- Historical native task output returned zero items without reporting a gap,
  even though task summaries recorded 44, 73, and 100 items in relevant turns.
- The diagnostic journal recorded reconciliation spans but no causally joined
  span for the subsequent publication tick.
- The work view said “End the Warden turn; the controller will observe
  validation” while the child validation had already failed and no retained
  submission existed.
- At evidence capture, service health reported event-buffer overflow and required
  reconciliation, but the status did not connect that condition to this Bead.
- Recovery changed the exact source and delivery handle without updating or
  invalidating the sealed Warden finish, so a single status record contained two
  incompatible source identities.

The smallest adequate telemetry improvement is a stable causal chain from Bead
to parent operation, child operation, provider handle, native turn, reconciliation
pass, and publication span. The trace should retain state-transition summaries
and bounded redacted errors, not full prompts or unbounded logs.

## Recovery and Warden note

Recovery commit `a6ab985c1d98ce79ddcc30154b531acd4fb1eeee`
contained the requested metadata edits plus the test correction. The provider's
buildset `01a0a87f-177a-7b13-a271-f2ad64afed35` passed its `ci` step in
5.447 seconds. Certificate `01a0a87f-2d35-7af2-b52a-fec4c3c25d92` bound the
tested source. Remote and local `master` both reached the recovery commit.

The Warden supplied this final note:

> Fixed and promoted successfully.
>
> - Commit: `a6ab985c1d98ce79ddcc30154b531acd4fb1eeee`
> - Local `master` and remote `master` now match.
> - Weaver is live as `$weaver` with display name `Weaver`.
> - The directory and symlink remain `fulcrum-weaver`.
> - `allow_implicit_invocation: false` remains unchanged.
> - `scripts/check`: all 144 tests passed.
> - Skill validator passed.
> - Tollgate validation, promotion, synchronization, and provider cleanup completed.
>
> I also fixed the worktree-dependent test that originally blocked promotion.
>
> One Fulcrum bookkeeping defect remains: the bead cannot close because cleanup
> records state `complete`, while the closure guard only accepts `observed`. This
> does not affect the promoted source or live Weaver skill.

The delivery claims in that note are corroborated by Git and provider records.
The note is narration, not proof by itself. Its closure diagnosis is also visible
in current source and durable work state.

## Corrective actions

| Status | Action | Failure prevented or detected | Proof / remaining acceptance |
| --- | --- | --- | --- |
| Completed for this source | Correct the worktree-sensitive setup test and deliver the metadata change in one commit based on promoted release | Removes the immediate CI trigger | `a6ab985c...`; 144 tests passed; provider CI passed; local and remote `master` match |
| Required P0 | In `_warden_finish`, treat any failed or unresolved child validation as non-acceptance before writing `delivery_finish`; retain the child receipt, keep Warden ownership, and return to `reviewing` | Prevents a failed validation from becoming a sealed approval | Add a real test where `validation_start` fails before retaining delivery; assert no seal, `phase=reviewing`, and a later amended finish is accepted |
| Required P0 | In controller reconciliation, recover a sealed finish whose provider delivery is absent by inspecting the child operation and deterministically returning it to review | Repairs already-created orphan states and defends against response-loss variants | Regression begins with this incident's durable shape and reaches a new Warden finish without manual direct delivery commands |
| Required P0 | Invalidate or supersede Warden approval whenever the workspace source changes; require the repaired exact source to receive a new finish/approval identity | Prevents stale `delivery_finish.source_oid` after repair | Test changes source after failed validation and proves closure references only the new OID |
| Required P0 | Normalize provider facts once and use one delivery-state vocabulary in reconciliation and `_delivery_settled` | Allows delivered work to close | Test provider states `promoted/complete/complete` through reconciliation and assert `work close --outcome delivered` succeeds exactly once |
| Required P1 | Run publication independently from reconciliation; a slow remote ledger push must never occupy the only background workflow lane | Removes the confirmed 45.2-second handoff blocker | Integration test holds publication for 60 seconds and proves Marshal eligibility/dispatch still advances; validate with a live timing run, not only mocks |
| Required P1 | Make the full configured validation result authoritative in Warden UI/instructions; do not allow “unrelated” to satisfy a red required gate | Ensures Warden repairs CI as assigned | A red `scripts/check` must leave Warden active with exact failure evidence and no accepted finish |
| Required P1 | Make `trace --bead` join parent/child operations, standing-leader turns, provider handles, reconciliation passes, and publication spans | Makes this incident reconstructable from one command | Golden trace contains all stages and both initial/recovery source OIDs without global-log searches |
| Required P2 | Record explicit stage budgets and flag overruns for trivial changes, including model time, worktree preparation, handoffs, and provider waits separately | Exposes needless overhead before users do | Live benchmark reports named boundaries and percentiles; no latency claim is complete from mocks alone |

No orchestration corrective action above is marked complete. The recovery commit
fixed the immediate test and delivered this particular source; it did not repair
the completion, reconciliation, publication-scheduling, trace, or closure defects.

## Evidence quality and final assessment

Confirmed facts include all operation states and timestamps, the 45.232-second
ledger sync, both source commits and diffs, the failed 144-test run, the absent
initial delivery record, the sealed `delivery_finish`, the recovery retries,
provider validation/promotion/synchronization, local and remote master, and the
still-open Bead.

The attribution of the Weaver-to-Marshal delay to serialized publication is a
strong inference from operation timing and historical worker/resident source;
there is no direct joined span. The reasons for most intra-model elapsed time are
unknown because historical native output was unavailable. Those gaps should not
be converted into claims about model reasoning, locks, or network time.

The next operational test must demonstrate all of the following in one run:

1. a required CI failure returns the same Warden to review without sealing;
2. the Warden can amend and submit one new exact source through the ordinary
   finish contract;
3. a concurrent 60-second ledger publication does not delay Marshal or delivery
   reconciliation;
4. successful provider cleanup closes the Bead automatically;
5. one Bead trace contains the complete causal path and exact stage durations.

Until those behaviors are observed live, this incident is source-recovered but
not system-remediated.
