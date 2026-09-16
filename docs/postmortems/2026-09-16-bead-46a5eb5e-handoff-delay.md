# Bead `fc-46a5eb5e` handoff-delay postmortem

- **Incident date:** 2026-09-15–16 PDT (2026-09-16 UTC)
- **Timezone:** America/Los_Angeles (PDT, UTC-07:00)
- **Status:** Requested source delivered and bead closed; orchestration and instruction remediation required
- **Severity:** Not assigned; no project severity rubric was found
- **Bead:** `fc-46a5eb5e`
- **Initial operation:** `fc-f399418dacff5a92a9761c0befc186d5` (`work create`)
- **Incident source revision:** `1d29f434c2708d3e5741277d47d6b681216822df`
- **Executor source:** `f2e3ae9f6fd776fe567b8dcd4a5ac3c17f028ec0`
- **Delivered revision:** `942985eb6a493ce0067f07376bce2e805006455c`
- **Investigation boundary:** work creation at 2026-09-15 22:44:44.192 PDT through work closure at 2026-09-16 00:51:11.302 PDT. Later investigation activity is excluded. The older bead `fc-1ba7f515` is included only where it causally blocked this bead.

## Executive summary

The request was to expose the existing Fulcrum Weaver skill under the shorter
locator `weaver`. The expected path was Weaver scope, one Marshal decision,
Executor implementation, Warden review, and controller-owned delivery. That path
eventually completed and the exact delivered source passed provider validation,
but the bead took **2 hours 6 minutes 27.110 seconds** from creation to closure.
No source or user data was lost.

The primary cause was not slow agent reasoning. After Weaver finished, an older,
unrelated bead (`fc-1ba7f515`) made every global reconciliation pass throw
`STALE_SOURCE`. At incident revision `1d29f43`, the controller reconciled every
task with one fail-fast `asyncio.gather` before it discovered backlog and asked
Marshal for decisions. The unrelated exception therefore prevented this bead's
Marshal request from being created. The controller repeated the same failure
**880 times** over **1 hour 54 minutes 38.025 seconds**, which was **90.7%** of
the bead's total elapsed time.

Several real instruction defects then made the successful portion noisier. The
downstream compiler used the raw intake outcome and title instead of the exact
Marshal-authorized Weaver scope. Because that intake contained an explicit
`$fulcrum-weaver` invocation, Executor and Warden both treated it as an
instruction they had to reconcile with their active roles. The rendered
terminal-stop commands also omitted the required ownership token, and Warden
was not told that provider submission required exactly one task commit on the
promoted release. Executor additionally mistyped a commit OID, and Warden
initially ran a nonexistent test name. These issues caused failed commands,
extra inspection, and one rejected Warden finish, but they did not cause the
two-hour delay.

The observed path was:

```text
Weaver scope (2m15.5s)
  -> 1h54m38.0s blocked by 880 failures from unrelated bead fc-1ba7f515
  -> Marshal decision (28.8s)
  -> Executor entry (22.0s after decision completion)
  -> Executor implementation and handoff (4m00.3s)
  -> Warden entry (25.0s)
  -> Warden review, rejected two-commit candidate, squash, approval (3m21.8s)
  -> provider validation, promotion, synchronization, cleanup, close (55.2s)
```

## Impact and expected versus actual

| Stage | Expected boundary/budget | Actual boundary/duration | Evidence | Result |
| --- | --- | --- | --- | --- |
| Weaver scope | Prepare implementation-ready scope and transfer to backlog | Work creation to Weaver finish receipt: 22:44:44.192–22:46:59.692, **135.500 s** | `fc-f399...`, `fc-518d...` | Correct scope |
| Weaver to Marshal | Event-driven reconciliation with a 15-second periodic fallback; no hard end-to-end SLA is specified | Weaver finish to Marshal request creation: 22:46:59.692–00:41:37.717, **1h54m38.025s** | Global event journal, `fc-8d5e...` | Catastrophic cross-bead blockage; 880 failed passes |
| Marshal | Inspect the bounded decision brief and decide once | Request creation to decisions applied: 00:41:37.717–00:42:06.524, **28.807 s** | `fc-8d5e...`, `fc-2cf4...` | Correct decision; full-scope lookup was required by the brief |
| Marshal to Executor | Dispatch authorized work and create an owned native turn | Marshal decision completion to Executor entry completion: 00:42:06.958–00:42:28.933, **21.975 s** | `fc-2cf4...`, `fc-6915...`, `fc-a33a...` | Slow but not a second large stall |
| Executor | Implement, run focused checks, and hand off exact committed source | Entry completion to accepted finish: 00:42:28.933–00:46:29.249, **240.315 s** | `fc-a33a...`, `fc-49ac...` | Correct change; avoidable command/OID errors |
| Executor to Warden | Observe terminal handoff and enter Warden | Executor finish to Warden entry completion: 00:46:29.249–00:46:54.298, **25.049 s** | `fc-49ac...`, `fc-bbb4...` | Successful, but no explicit latency budget |
| Warden | Independently review and finish one exact source | Entry completion to final judgment: 00:46:54.298–00:50:16.071, **201.773 s** | `fc-599f...`, `fc-472e...` | First finish was rejected for two-commit topology; second succeeded |
| Delivery and close | Controller validates, approves, promotes, syncs, cleans up, and closes | Final Warden judgment to work close: 00:50:16.071–00:51:11.302, **55.230 s** | provider `01a0a931...`; approval, promotion, sync, cleanup and close receipts | Delivered and closed |

The incident start event is creation of `fc-f399418dacff5a92a9761c0befc186d5`.
The completion event is `work close` completion at 00:51:11.302 PDT. Native task
timestamps are second-granularity; operation and event-journal timestamps retain
microseconds.

The Marshal-to-Executor interval is worth improving but was misperceived as a
second long gap. The decision operation completed at 00:42:06.958 and dispatch
operation `fc-6915...` was not created until 00:42:13.693, leaving **6.735 s**
without a more specific span. Within dispatch, worktree preparation took
**7.089 s** and Executor role entry took **5.320 s**. Those spans overlap the
16.554-second dispatch operation and must not be added to it.

## Actors and durable identifiers

| Role | Native task/thread | Native turn | Entry/request operation | Finish/decision operation |
| --- | --- | --- | --- | --- |
| Weaver | `01a0a8be-03a7-7613-a1f0-3bf68b879a9f` | `01a0a8be-b92e-7eb0-b305-1dcfb8bb4a97` | `fc-f2212e866b954e80a8f4912fc7cdf533` | `fc-518d62af234543a58c8b86bd37feeed6` |
| Marshal | standing task `01a0a77c-b76f-7de0-9971-7496424b6116` | `01a0a92a-0602-7c31-a691-ffd835ef7a8b` | request `fc-8d5e051c687b598bbc8a9fd22cc2c3cf` | `fc-2cf4f7e8614c41bfb07cda02715e914d` |
| Executor | `01a0a92a-b9e3-7473-8c59-392029049f1c` | `01a0a92a-c76f-7d22-a92d-c904f9c9e59c` | dispatch `fc-6915e4f96c0d5f409fd64eb817982ab8`; entry `fc-a33ad98387275239b4cf5c3274e31614` | `fc-49acfef4f32f491ea153653661259619` |
| Warden | `01a0a92e-c74d-7b62-97f4-d80c10b570d7` | `01a0a92e-d4ae-7e70-85d9-6d3507cf895d` | `fc-bbb40cf3f6335997bd8c5ef819880fa5` | rejected `fc-599fe8fea71648cf8bec21335b3e3ffc`; final `fc-472ed093908146419aa1d146892e29c1` |

The causally blocking task was the older Warden task
`01a0a86f-6cf4-7842-887d-e7434b5c9992`, owning bead `fc-1ba7f515` through
`fc-945f955c85d35d56ba9fbad689ad8d05`. Its stale sealed finish requested source
`07470890825f948a375e27822088b8a93d26200e`, while the retained validation source
had changed. This investigation does not otherwise include that bead's earlier
incident; its separate history is documented in
[the `fc-1ba7f515` postmortem](2026-09-15-bead-1ba7f515-weaver-metadata-delivery.md).

## Detailed timeline

All times are PDT. The date changes at midnight.

| Time | Event | Durable identifiers/evidence | Gap from prior stage |
| --- | --- | --- | ---: |
| Sep 15 22:44:44.192 | Work creation begins for `fc-46a5eb5e`. | `fc-f399...`; completed 22:44:45.439 | — |
| 22:44:48.564–22:44:51.816 | Weaver entry binds the task. | `fc-f221...`; native task observed at 22:44:52 | — |
| 22:46:58.435–22:46:59.692 | Weaver records ready scope and correctly says Marshal notification is not yet confirmed. | `fc-518d...`; command result at 22:47:00.563 | — |
| 22:47:00.575 | First post-Weaver reconciliation fails after 8.695 s. | Pass `f684ad2c-...`; `STALE_SOURCE` from `fc-1ba7f515` | 0.883 s after finish |
| Sep 15 22:47:00–Sep 16 00:41:09 | The controller repeats 880 failed reconciliation passes. Every pass attempts the same stale approval on `fc-1ba7f515`; every global `reconcile` command fails. | 880 `review approve` failures, 880 `reconciliation_failed` events, 880 failed `reconcile` commands | **1h54m** hot retry interval |
| Sep 16 00:41:09.786 | Last failed pass records the same error after 8.652 s. | Pass `9d49c054-...` | — |
| 00:41:12.483–00:41:40.352 | A reconciliation pass finally succeeds. It sees the old Warden task as `notLoaded`, records recovery request `fc-88bed...`, and starts this bead's Marshal request. | Pass `8a802001-...`, `fc-88bed...`, `fc-8d5e...` | The cause of the runtime-status change is not retained |
| 00:41:37.717 | Marshal request is created. | `fc-8d5e...` | **1h54m38.025s after Weaver finish** |
| 00:42:05.347–00:42:06.958 | Marshal authorizes Executor after fetching full scope and memory. | `fc-2cf4...`; request records decisions applied at 00:42:06.524 | 28.807 s from request creation |
| 00:42:13.693 | Dispatch operation is created. | `fc-6915...` | **6.735 s** after decision completion, unbroken by a more specific span |
| 00:42:15.177–00:42:22.265 | Owned worktree preparation runs. | Child preparation receipt | 7.089 s |
| 00:42:23.614–00:42:28.933 | Executor role entry runs; native task is observed at 00:42:28. | `fc-a33a...` | 5.320 s |
| 00:45:36–00:46:03 | Executor attempts finish with a mistyped OID, follows a formula-rendered terminal-stop command missing the ownership token, inspects help, and retries. | Native turn transcript and failed command events | Avoidable retries; exact counterfactual time is not measurable |
| 00:46:14.673–00:46:29.249 | Final Executor finish runs. Fulcrum's configured full check finds a real activation-test isolation failure, but the Executor handoff is retained for Warden repair. | `fc-49ac...`; source `f2e3ae9...` | 14.576 s |
| 00:46:49.383–00:46:54.298 | Warden role entry completes. | `fc-bbb4...`; native task observed at 00:46:53 | 25.049 s after Executor finish |
| 00:48:48.907–00:49:03.691 | Warden commits repair `bee3a2b...` atop the Executor commit and calls finish. Tollgate rejects the two-commit candidate because an ordinary candidate must be one task commit atop release. | `fc-599f...`, `warden_validation_unresolved` | First judgment not deliverable |
| 00:49:03.691–00:49:56.024 | Warden squashes/rebases to `942985e...`, corrects a nonexistent focused-test name, and reruns checks. | Git and native transcript | 52.333 s recovery interval |
| 00:49:56.024–00:50:16.071 | Final Warden finish seals the corrected exact source. | `fc-472e...` | — |
| 00:50:44.878–00:50:46.901 | Controller approval completes. | approval receipt | — |
| 00:50:49.221–00:50:58.531 | Provider promotion completes. | promotion receipt | — |
| 00:50:59.883–00:51:04.196 | Source synchronization and cleanup complete. | sync and cleanup receipts | — |
| 00:51:05.191–00:51:11.302 | Work closes as delivered. | work-close receipt; provider `01a0a931-ce2e-7091-beb9-9c64bd3130b2` | **2h06m27.110s total** |

## Causal analysis

### Primary root cause: one unrelated task could abort the entire global pass

At incident revision `1d29f43`, `ControllerSupervisor._run_once` first gathers
all `_ordered_task_reconcile` coroutines. Only after the entire gather returns
does it inspect backlog and call `_request_marshal_judgment`; see
[`src/fulcrum/supervision.py`](../../src/fulcrum/supervision.py) around lines
440–495.

For the older Warden task, `_advance_warden_delivery` tried controller-owned
`review approve` with the source from its sealed finish. `review_approve` compares
that source to the retained validation submission and raises `STALE_SOURCE` on a
mismatch; see
[`src/fulcrum/delivery_service.py`](../../src/fulcrum/delivery_service.py) around
lines 345–362. `_advance_warden_delivery` catches errors around `promotion show`
but not around the approval dispatch. Its later stale-source handling only
examines a returned `CommandResult`, which is unreachable when dispatch raises.

That exception propagated out of the task coroutine and out of the fail-fast
gather. The code therefore never reached backlog discovery for `fc-46a5eb5e`.
The event journal confirms the complete chain on every attempt:

```text
fc-1ba7f515 review approve -> STALE_SOURCE
  -> reconciliation_failed (global, no bead association)
  -> reconcile command failed
  -> fc-46a5eb5e backlog discovery and Marshal request skipped
```

The counterfactual is direct. If task reconciliation isolated errors per task,
or if Warden stale-source approval returned that bead to review instead of
throwing, the pass could have continued to the target bead's Marshal request.

### Trigger and escape: an old inconsistent delivery state remained actionable

The poisoned bead retained a finish for source `0747089...` but validation facts
for another source. Earlier remediation had added logic intended to return stale
source to Warden review, but it assumed approval failure would be returned as a
failed command result. The actual service raises before it creates such an
operation. The integration boundary between controller and command service was
therefore untested in this exact form.

The 881st pass succeeded only because the older Warden task's runtime status was
then `notLoaded`. The controller defines a terminal task as having no active
turn, a retained last turn, and status in `idle`, `completed`, or `failed`.
`notLoaded` bypassed `_advance_warden_delivery`; the stalled-task branch instead
created recovery request `fc-88bed...`. The pass then reached backlog discovery
and created this bead's Marshal request. Durable evidence does not explain why
that task changed to `notLoaded`, so the escape mechanism is confirmed but its
initiating cause is unknown.

### Why agents appeared confused

The agent behavior came from four distinct sources, not one general reasoning
failure:

1. **Raw intake was compiled instead of the authorized Weaver scope.** Weaver
   correctly retained its curated implementation contract in `fc.scope.summary`,
   `fc.scope.acceptance`, and `fc.scope.evidence`. Downstream role compilation
   nevertheless populated `Requested outcome` from the original `fc.outcome`
   and populated the native task title from the original work title. It placed
   Weaver's curated summary only inside `Current evidence`. The workers did not
   receive the full Weaver transcript, but they did receive the raw user request
   where they should have received the exact scope revision Marshal authorized.
   That raw intake contained the literal Markdown invocation
   `$fulcrum-weaver`, so Executor announced that it was using the Weaver skill
   while also noting that Executor now owned the bead; Warden made the same
   reconciliation. The defect is the task-contract source, not merely missing
   escaping. Raw intake belongs in durable provenance available to Weaver and
   Marshal, not in downstream worker instructions. Its latency contribution is
   confirmed to be nonzero but not measurable from the transcript.
2. **Marshal's clarification was expected.** The decision brief deliberately
   caps the whole brief at 6,000 characters and memory at 750 characters. This
   scope was marked `prepared_scope.complete: false`, and the Marshal prompt
   explicitly required `work show` in that case. Marshal's full-record and
   memory lookups were compliant, not evidence of a confused decision. The UI
   could still explain the intentional truncation more plainly.
3. **Rendered commands contradicted the CLI.** Executor and Warden formulas
   rendered `task terminal stop ... --all-owned` without
   `--ownership-operation`, while the owner-restricted command required that
   acquisition token. Both agents followed the supplied command, received a
   failure, and had to rediscover the actual syntax. The role-context design says
   formulas should contain exact commands, so this is a command-contract defect,
   not agent discretion.
4. **Warden lacked the candidate-topology invariant.** Warden was told to fix
   defects in the same worktree and commit the repair. It reasonably added a
   second commit, but Tollgate requires one task commit atop promoted release.
   The first finish returned a completed `warden_validation_unresolved` result;
   Warden then had to squash/rebase and finish again. This also conflicts with
   the prompt's statement that one accepted finish seals judgment and it must not
   call finish again. The command result needs to distinguish a sealed accepted
   finish from a correctable unresolved attempt.

Executor's mistyped full OID and Warden's initial nonexistent test name were
ordinary agent/tool mistakes. Better structured source selection and discoverable
test commands would reduce them, but neither is a controller root cause.

### Failure inventory

| Failure | Layer | Evidence | Causal role | Confidence |
| --- | --- | --- | --- | --- |
| Approval dispatch raises `STALE_SOURCE` outside the controller's recovery branch | Delivery/reconciliation | Historical source and 880 command failures | Triggered every failed pass | Confirmed |
| One task exception aborts all reconciliation before Marshal discovery | Orchestration | Historical source ordering and pass events | Primary cause of 1h54m38 delay | Confirmed |
| Identical failed state retries without isolation, backoff, or circuit break | Recovery/performance | 880 repetitions | Amplified duration and resource use | Confirmed |
| Successful pass depends on old task becoming `notLoaded` | Runtime/recovery | Successful pass facts | Accidental escape from livelock | Confirmed; cause of status change unknown |
| Raw intake outcome/title are compiled instead of the authorized Weaver scope | Prompt/context | Role compiler, retained `fc.scope`, and Executor/Warden native transcripts | Supplied the wrong task contract and caused role/skill reconciliation | Confirmed; duration unmeasured |
| Terminal-stop template omits ownership token | Command contract | Formula text and failed calls | Caused deterministic retries | Confirmed |
| One-commit provider invariant absent from Warden instructions | Delivery contract | Warden commit history and first finish rejection | Added a rejected finish and squash/retest loop | Confirmed |
| Executor mistypes source OID; Warden guesses a test name | Agent/tool use | Native transcripts and failed commands | Minor extra latency | Confirmed |

## Observability assessment

The public bead trace was materially misleading for this incident. It returned
87 items, reported `gaps: []`, and jumped from Weaver finish at 22:46:59 to the
Marshal request at 00:41:37. None of the 880 failures appeared because the
failing command was associated with `fc-1ba7f515` and the reconciliation failure
was global with no target-bead association. The trace could not answer why this
bead waited.

Reconstruction required all of the following:

- the two global diagnostic JSONL files for failed-pass counts and timing;
- cross-bead inspection of `fc-1ba7f515`;
- historical source at the active deployment's commit `1d29f43`;
- operation receipts for the target bead; and
- direct reads from the native task-history SQLite database because
  `fulcrum task output` returned no items for turns that `task show` reported as
  having 32, 11, 52, and 45 items.

The collector's broad log probe also exceeded its bounded output and produced a
truncated, non-parseable payload. Later reconciliation success reset the current
service-health snapshot to zero consecutive failures and no error, erasing the
failure episode from that surface. `controller.log` and `controller-error.log`
were empty; the diagnostic journal was the only durable source for the 880-pass
history.

The smallest sufficient observability changes are:

- associate a failed pass with the exact blocking bead and with every eligible
  bead whose next stage was skipped;
- emit a target-bead wait span such as `marshal_discovery_blocked` with blocker,
  pass ID, error signature, first seen, last seen, and repetition count;
- retain the last reconciliation failure episode after health recovery;
- make archived task output retrievable through the public command; and
- paginate bounded log queries rather than returning invalid truncated JSON.

## Corrective actions

No orchestration or instruction fix was implemented during this investigation.
The successful delivery proves only that this instance escaped the bad state; it
does not prove the failure mode is repaired.

| Status | Action | Failure prevented/detected | Proof | Remaining acceptance |
| --- | --- | --- | --- | --- |
| Required P0 | Isolate reconciliation failures per task/bead and continue independent backlog discovery and dispatch | One poisoned bead cannot block unrelated work | Two-bead integration test | With bead A throwing stale approval, bead B receives a Marshal request within one documented reconciliation interval |
| Required P0 | Catch `FulcrumError` around controller approval and map `STALE_SOURCE` to deterministic Warden review/recovery | Removes this incident's trigger | Service and controller regression | Old finish is superseded or cleared, exact retained source is visible, and no pass exception escapes |
| Required P0 | Add repeated-error suppression/circuit breaking while keeping unrelated work live | Stops hot retry and 880-pass churn | Resident test with stable clock and repeated signature | One recovery record is retained, retries are bounded/backed off, and unrelated beads advance |
| Required P0 | Add the exact two-bead incident-shape regression | Prevents local unit success from masking the cross-bead failure | End-to-end test against real dispatch behavior | Test fails on `1d29f43` and passes only with failure isolation and stale-source recovery |
| Required P1 | Compile Executor and Warden instructions exclusively from the exact Marshal-authorized Weaver scope revision; retain original intake only as provenance available to Weaver and Marshal | Makes the curated scope, rather than raw user text, the downstream task contract | Authorization-to-role prompt integration test | `Requested outcome`, acceptance, evidence, and derived title match the authorized `fc.scope`; raw intake is absent from worker instructions, and dispatch is bound to that exact scope revision |
| Required P1 | Generate terminal-stop commands from the command schema, including ownership acquisition | Prevents deterministic syntax failures | Formula/CLI contract test | Rendered Executor and Warden commands succeed unchanged |
| Required P1 | Make one-task-commit topology controller-owned or state and enforce it before Warden finish | Prevents a repair commit from reaching provider as an invalid two-commit candidate | Delivery integration test | Warden can repair source once without a provider topology rejection |
| Required P1 | Reserve “accepted/sealed finish” for a judgment the controller can own; return a clearly correctable state otherwise | Removes contradiction between one-finish instruction and `warden_validation_unresolved` | Completion-service regression | An unresolved result explicitly permits correction; a sealed result cannot require a second finish |
| Required P1 | Instrument Marshal decision receipt through dispatch enqueue, worktree preparation, role entry, and native observation | Makes the 6.735-second unspanned interval attributable | Trace golden test and live measurement | Every substage has correlation IDs; set a budget only after live percentiles exist |
| Required P1 | Join blocking cross-bead reconciliation events into `trace --bead` and retain failure episodes in health | Detects this incident from supported surfaces | Trace/health golden tests | The same incident is reconstructable without global log or SQLite access and `gaps` is nonempty |
| Required P2 | Explain bounded Marshal brief continuation explicitly while preserving full-record lookup | Reduces perceived Marshal confusion without removing the safety bound | Prompt/UI test | Truncated scope is labeled as intentional and gives one canonical continuation command |

## Evidence quality and final assessment

The primary failure chain, counts, durations, role/operation identities, source
revisions, and delivery result are confirmed by durable receipts, global event
records, native turn history, active-deployment metadata, and historical source.
The instruction contradictions are confirmed by rendered formulas and native
agent transcripts. The exact time saved by removing each prompt defect is not
measurable and is not estimated here.

The successful pass shows why the loop ended: the poisoning Warden task was
reported as `notLoaded`, which bypassed delivery advancement and allowed a
recovery request plus Marshal discovery. What changed that runtime status is not
retained and remains unknown. The 6.735 seconds between Marshal decision
completion and dispatch-operation creation also has no narrower durable span.

The next operational test is not complete merely when a single bead ships. It
must hold an unrelated bead in this exact stale-source condition and demonstrate
that: the stale bead enters one bounded recovery path; a ready second bead reaches
Marshal within the documented reconciliation interval; Marshal-to-Executor spans
are fully correlated; downstream roles receive the exact authorized Weaver scope
without raw intake text; rendered terminal commands succeed; a Warden repair
produces an acceptable candidate without topology rediscovery; and the public
trace alone shows both the blocker and the continued progress.
