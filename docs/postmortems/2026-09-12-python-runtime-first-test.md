# First Python runtime test postmortem

**Incident date:** 2026-09-12  
**Status:** Controller process alive; workflow control plane deadlocked  
**Severity:** Critical test failure; no production impact  
**Scope:** First live test of `docs/plans/fulcrum-python-runtime.md`

## Summary

The first live test of the Python runtime failed as a cascade rather than as one defect. The rewrite reached the live controller before its adapters, installation topology, scheduling loop, recovery paths, and observability had been exercised together. The controller then dispatched more work than its configured project limit, depended on agents to perform delivery mechanics the controller was supposed to own, confused uncertain Tollgate results with deterministic failures, and failed to retry stranded actions. Finally, its reconciliation loop appears to have stopped while the process and socket remained healthy.

The visible result was three frozen executor lanes, two frozen overseer lanes, a frozen Archon, a frozen Weaver, stale reservations, and contradictory state for a candidate that Tollgate had actually promoted. Most underlying Codex turns were no longer running: they had completed or failed. Fulcrum had lost, rejected, or not processed their terminal state, so the deadlock was primarily in Fulcrum's durable orchestration state rather than in the Codex processes.

The incident exposed at least fifteen distinct defects or unsafe assumptions. It also demonstrated that the current logging is not adequate to reconstruct an incident. This account required correlating the Fulcrum SQLite database, Codex app-server thread histories, session JSONL, Git history and worktrees, Tollgate state, Beads state, launchd state, process state, and source code. The nominal controller log was empty.

## Impact and state at capture

The incident began at approximately 16:40 PDT and was still unresolved when this evidence was captured after 17:49 PDT.

- Assignment 0001 was left in `reviewing`. Its first candidate had a legitimate requested change; the executor submitted a corrected candidate, but the next review action was never dispatched.
- Assignment 0002 was left in `recovering`. Its executor committed and pushed the fix but omitted `tg candidate`; Fulcrum accepted `ready_for_review`, then could not find a new immutable candidate. No overseer turn was run. The provisioned overseer thread now has an unreadable/missing rollout lineage.
- Assignment 0003 was left in `recovering` even though its overseer approved it and Tollgate promoted, synchronized, and cleaned up its candidate. Fulcrum could not parse the successful approval output and recorded the operation as failed.
- Two reservations remained charged to assignment 0001 and assignment 0002. Assignment 0003's reservation was released despite its stale `recovering` state.
- Archon had a pending action and a frozen proposal batch. Its runtime thread was currently startable, but Fulcrum had no path that retried the stranded action.
- Weaver's Codex turn had completed and its `intake_complete` outcome had been accepted, but Fulcrum had not processed it. Fulcrum still described Weaver as active and nonterminal.
- The controller process was alive, its socket responded, and it was idle in `kqueue`; nevertheless, the last reconciliation timestamp was more than thirty minutes old. The workflow was logically deadlocked while process-level health still appeared good.
- The configured app-server launchd service was not loaded. A manually attached app-server process was serving the port instead. The controller launch agent was loaded, but installed service definitions had not been brought into agreement with the newly promoted source.

The reported `fulcrum-1` project could not be found as a durable project in either Fulcrum or Codex state. All managed thread records used project ID `fulcrum` except Weaver, whose record had no project ID. The executor for assignment 0001 did have cwd `.worktrees/fulcrum-1`. The best available explanation is that a UI surface interpreted that worktree directory as a project/workspace. This is an inference, not a confirmed cause, because no project-creation event or UI audit record exists.

## Timeline

Times below are PDT. Durable Fulcrum events were stored in UTC and converted by subtracting seven hours.

| Time | Event |
| --- | --- |
| 15:28-16:43 | A single implementation thread performed the Python rewrite. Its history contains 175 command executions and 21 failed commands. The first large commit replaced the runtime at 16:22; eight follow-up fix commits landed through 16:43. |
| 16:40:48 | The first live Python controller started before the implementation thread and follow-up fixes had finished. |
| 16:41:43-16:41:45 | Archon supplied initial policy: global concurrency 4, project concurrency 2, daily Sage and Inquisitor anchors. |
| 16:43:04 | A source change caused a hot reload. Multiple `controller_starting` events were written because startup was recorded before process-lock ownership was established. |
| 17:00:04-17:02 | The scheduled Sage ran before any real assignments existed. It reported no runs, holds, obligations, or findings and then archived. It therefore supplied no evidence about the subsequent failure. |
| 17:01:33 | Weaver was registered for the persistent Vizier task. |
| 17:03:24 | Weaver's first `fulcrum intake` failed because the controller launchd environment could not find `bd`. Fulcrum recorded a failed external operation and failed publication obligation. |
| 17:04:29 | The controller restarted with a usable inherited path. |
| 17:04:58 | Retrying intake successfully created the original task, but the earlier failed operation and obligation remained. Weaver also filed tasks for the service-path and retry-state defects. |
| 17:05:29-17:06:03 | Archon approved three tasks. Fulcrum created three worktrees and dispatched all three executors even though the project limit was 2. |
| 17:05-17:15 | All three worktrees lacked `.venv`; agents encountered missing command, formatting, test, and path-identity failures and worked around them by invoking the root environment. |
| 17:10:51 | Executors 0001 and 0002 submitted `ready_for_review`. Executor 0001 had created a Tollgate candidate. Executor 0002 had committed and pushed but had not run `tg candidate`. |
| 17:10:53 | Assignment 0002 failed candidate capture with `submitted outcome has no new matching immutable Tollgate candidate`, entered `recovering`, and retained its reservation. Its overseer was never started. |
| 17:15 | Executor 0003 submitted the launch-agent path fix. Overseers for 0001 and 0003 began review. |
| 17:18:17 | Overseer 0003 approved its candidate. Tollgate promoted it to `master`, synchronized the remote, and cleaned the worktree. |
| 17:18:27 | Promotion changed the controller's own source checkout and triggered another hot reload during delivery. Tollgate's approval output was not valid JSON for Fulcrum's adapter. Fulcrum recorded a deterministic failure and left assignment 0003 in `recovering`, contradicting Tollgate's durable state. |
| 17:18:32 | Overseer 0001 correctly rejected the first CLI-path candidate because creating `~/.local/bin/fulcrum` did not prove an existing user path would resolve it or detect a collision. |
| 17:18:57-17:20:13 | Executor 0001 rebased onto the newly promoted `master`, resolved a conflict, and submitted a corrected candidate. Fulcrum created the next review action but never dispatched it. |
| 17:20:27 | Fulcrum recorded its last reconciliation timestamp. No later fallback reconciliation occurred despite the designed 30-second interval. |
| 17:26:20 | Weaver successfully created another Beads task, then the same client request failed because post-request advancement tried to start Archon while the app-server reported it was not ready. The successful intake was presented as a failed command. The Archon action and its proposal batch remained pending/frozen. |
| 17:26-17:29 | Weaver continued planning, encountered a divergent Beads Git history and push rejection, used a temporary worktree/cherry-pick workaround, and filed more defects. Its `intake_complete` outcome was accepted at 17:29:08 but was never processed. |
| After 17:49 | Direct inspection found the controller alive and responsive but idle, with stale reconciliation state. Direct app-server reads showed the executor and active overseer turns terminal. Fulcrum still presented the lanes as active, reviewing, or recovering. |

## Failure inventory

The 21 failed shell commands in the implementation transcript should not be equated one-for-one with product defects: some were formatting iterations, incorrect ad hoc commands, or missing developer tooling. The incident nevertheless contains at least the following distinct product failures and unsafe assumptions.

1. Brain reset assumed an existing Beads project.
2. Reset attempted an operation unsupported by embedded Beads mode.
3. The Tollgate repository adapter assumed an object response where the real command returned a list.
4. Newly created app-server threads were assumed to have a readable rollout before their first turn materialized them.
5. Setup silently reused an already running shared app server rather than establishing and verifying the specified supervised service topology.
6. Setup/readiness did not reliably repair stale installed runtime links and launch-agent definitions after the source changed.
7. The installed `fulcrum` command and the controller's launchd environment had incomplete path handling; `bd` was unavailable in the service context.
8. The scheduler evaluated a batch against one stale capacity snapshot. It admitted two additional assignments after one slot was already used, resulting in three active assignments under a project limit of two.
9. Managed worktrees did not contain or expose the environment assumed by repository checks and agent instructions.
10. Candidate creation was delegated to an executor prompt even though the design assigns routine delivery mechanics to Python. One omitted command permanently stranded assignment 0002.
11. Concurrent overlapping setup/install changes created an avoidable merge conflict between assignments.
12. A mutating Tollgate approval that returned unparseable output was classified as a deterministic failure. The side effect had succeeded, but Fulcrum neither marked it uncertain nor reconciled Tollgate's authoritative state.
13. Promoting into the controller's live source checkout caused a hot reload in the middle of the delivery path.
14. Startup events were emitted before lock acquisition, so failed controller contenders polluted the durable event sequence with apparent restarts.
15. A pending action that failed to start was never retried after its target became ready. A current pending action caused later delivery attempts to short-circuit rather than redispatch.
16. Client command success was coupled to a subsequent global `advance`. An unrelated dispatch failure turned an already completed intake mutation into an apparent command failure.
17. Weaver's accepted terminal outcome was not processed, and the fallback reconciliation mechanism did not repair it.
18. The fallback task appears to have died without affecting process health or emitting a durable failure. There was no task-level supervision or stale-reconciliation health check.
19. Overseer 0002's provisioned thread has an invalid history lineage with a missing source rollout, and Fulcrum has no recovery path for it.
20. The UI-visible `fulcrum-1` workspace/project cannot be attributed from durable audit data.

## Root cause

The primary root cause was releasing a big-bang orchestration rewrite directly into its own live control plane without an end-to-end stabilization gate.

Commit `afc51f8` changed 140 files, with 7,011 insertions and 17,242 deletions. The Python test surface at that point was substantially smaller than the suite it replaced and had no controller-level test that exercised the real Beads, Tollgate, app-server, launchd, Git worktree, source-reload, and recovery boundaries together. The live test started eighteen minutes after the large rewrite commit and about three minutes before the implementation thread's last follow-up fix. The running controller was therefore both the test subject and an automatically reloading consumer of a checkout still being edited and later promoted into.

That release decision interacted with four design defects:

### Non-atomic orchestration decisions

Capacity and readiness were calculated, then multiple actions were performed from the stale result. Reservations were not atomically acquired one at a time with a capacity recheck. Likewise, completing an intake request and globally advancing the fleet were one response path, so a later orchestration error changed the reported result of an earlier successful mutation.

### Incomplete ownership of state transitions

The controller relied on agents to invoke `tg candidate`, then tried to infer which candidate belonged to an outcome. It did not own the whole transition from completed implementation to immutable candidate. When any inferred step was missing, the state machine retained reservations and entered `recovering` without an effective recovery worker. Pending actions were durable, but being durable was not enough: there was no general retry transition that made them runnable again.

### Incorrect treatment of uncertainty

The design requires reconciliation after an uncertain external mutation. The implementation instead treated malformed Tollgate output after `approve` as proof that approval failed. It did not query the exact candidate before committing the state transition. This created the most serious contradiction in the incident: Tollgate says candidate 0003 was promoted and cleaned; Fulcrum says its delivery failed and needs recovery.

### Process health substituted for workflow health

The controller had separate event and fallback background tasks, but the fallback loop lacked an outer exception boundary, a task-completion callback, and a durable heartbeat. A background coroutine could stop while the parent process, socket, and launchd job continued to look ready. Readiness did not include the age of the last successful reconciliation, the liveness of each critical loop, or the existence of stranded pending actions.

## Contributing factors

- Installation was not treated as a reconciled resource. Source fixes existed, but the installed launchd jobs and their environment were not necessarily rewritten and restarted before testing.
- The manually running app-server masked the absence of its expected launchd service.
- Agent instructions and repository checks assumed `.venv` existed in each worktree, while the environment was actually anchored in the root checkout.
- Three tasks touched adjacent setup/runtime behavior concurrently. Even if the configured limit had been respected, task-scope conflict detection was absent.
- The daily Sage occurrence ran before meaningful workflow activity. Calendar scheduling made it irrelevant to an incident that began minutes later.
- Evidence references pointed into `/tmp` in places. Paths without durable captured content are poor forensic records.
- Source hot reload did not wait for a quiescent boundary after an external mutation and before process replacement.

## What worked

- Overseer 0001 found a genuine acceptance gap and requested a correction rather than approving a superficial fix.
- Overseer 0003 completed a review and approved a valid fix. Tollgate successfully promoted and synchronized it, proving that the happy path can reach the repository even though Fulcrum then misrecorded it.
- External operation, obligation, action, and outcome tables retained enough identifiers to join portions of the incident across systems.
- The executor worktrees and Git branches preserved work for assignments 0001 and 0002.
- Weaver continued to file observed defects despite command failures, leaving useful issue-level evidence.

## Observability assessment

Logging did not make this reconstruction trivial; it made it unnecessarily difficult and left important conclusions probabilistic.

`controller.log` was zero bytes. `controller-error.log` contained only two messages from losing process-lock contenders. The 45 durable workflow events were too coarse to establish turn starts, state transitions, fallback passes, request boundaries, background-task termination, or most failure causes. External-operation rows recorded some conditions and native IDs but did not preserve a consistent structured request, raw response, stdout/stderr, duration, and correlation chain. No durable record explains the `fulcrum-1` project display. No record contains the exception that stopped reconciliation after 17:20:27.

The sequence above required all of the following:

- SQLite joins across assignments, tasks, actions, batches, outcomes, obligations, reservations, operations, and metadata;
- direct app-server thread reads and session JSONL inspection;
- Git log, branch, remote, and worktree inspection;
- Tollgate candidate, certificate, promotion, synchronization, and cleanup state;
- Beads state and repository history;
- launchctl definitions and live process inspection; and
- source inspection to infer how a static ready list, pending-action short circuit, client/advance coupling, and unsupervised fallback loop produced the durable state.

The conclusion that the fallback coroutine died is an inference from an alive idle process, a responsive socket, a reconciliation timestamp that stopped advancing, and a loop implementation with no exception supervisor. The exact exception cannot be recovered. The explanation for `fulcrum-1` is also an inference because project-creation auditing is absent.

## Corrective actions

### Before another live fleet test

1. Preserve an incident snapshot of the current SQLite database, thread histories, Tollgate records, Git/worktree state, Beads state, service definitions, and process metadata before attempting repair.
2. Add an operator-only, read-first reconciliation flow. It must recognize assignment 0003's completed promotion, resume review of assignment 0001's replacement candidate, and require an explicit decision for assignment 0002's unsubmitted commit before releasing reservations.
3. Disable new dispatch while reconciliation is stale, any critical background loop is dead, or the installed service topology differs from configuration.
4. Install and verify both launchd services from the current source, including their complete executable paths. Do not accept a manually attached listener as proof that setup is complete.
5. Run a full failure-injection integration test in an isolated checkout and isolated state directory before pointing the controller at its own repository.

### State-machine and adapter changes

1. Acquire reservations atomically and recalculate capacity after every admitted assignment.
2. Make Python own candidate creation and capture. An agent's terminal implementation outcome should not depend on a separate manually remembered Tollgate command.
3. Treat every transport, timeout, or parse error after a mutating external command as uncertain. Reconcile against the exact native object before choosing success, retry, or recovery.
4. Add explicit retry transitions for pending actions, failed delivery, lost thread materialization, and incomplete outcome processing. Retry must be idempotent and driven by durable state.
5. Return a client mutation's committed result independently of later fleet advancement. Persist advancement errors separately.
6. Reconcile runtime-terminal turns with Fulcrum task state, including outcomes accepted immediately before a reload or loop failure.
7. Do not reload executable source while a mutation or delivery transition is in flight. Restart only at a recorded quiescent boundary.
8. Provision a supported check environment for every worktree or emit one absolute, valid check command that does not assume a worktree-local `.venv`.

### Test gates

Add controller-level tests using realistic adapter responses and failure injection for:

- three ready assignments under a project limit of two;
- an executor outcome without a Tollgate candidate;
- a successful mutation followed by malformed output or a dropped connection;
- a source change during candidate approval;
- a successful intake followed by an unrelated Archon dispatch failure;
- an app-server thread that is initially unmaterialized or transiently `notLoaded`;
- a terminal runtime turn whose outcome is not yet reflected in Fulcrum;
- a failed pending action becoming runnable later;
- a critical background task raising an exception;
- missing service `PATH`, stale installed links, and an unmanaged process occupying the app-server port; and
- real Tollgate list/object/output shapes rather than adapter-only mocks.

No live test should begin until these cases pass and setup proves the expected services, paths, readiness invariants, and reconciliation heartbeat.

### Logging and health

1. Emit structured append-only records for every command request/result, state transition (`from`, `to`, reason), runtime turn start/terminal event, external operation request/result, and fallback pass start/end.
2. Record exit status, duration, bounded/redacted stdout and stderr, native object IDs, and one correlation ID spanning request, action, operation, outcome, and transition.
3. Supervise every critical background task. On unexpected termination, durably record the stack, mark the controller degraded, and stop dispatch.
4. Make health fail when reconciliation age exceeds its bound, a critical loop is absent, a pending action exceeds its retry deadline, or Fulcrum disagrees with authoritative external state.
5. Persist evidence content or copy it into durable incident storage; do not rely on ephemeral `/tmp` paths.
6. Audit project/workspace creation with origin, request, cwd, project ID, and responsible process so a name such as `fulcrum-1` can be explained.

## Sage interviews

No new Sage interview was started for this postmortem. The scheduled Sage run completed before the incident and therefore had no relevant subjects or evidence. Starting additional managed work through the already wedged controller would also have mutated the incident scene. The executor, overseer, Archon, and Weaver transcripts supplied direct evidence superior to retrospective recollection. Interviews may be useful after state is safely snapshotted, but only to identify confusing instructions or UI behavior that the durable artifacts cannot explain; they should not replace the evidence above.

## Final assessment

This was not fifteen independent unlucky bugs. It was one insufficiently tested orchestration system encountering normal boundary failures—missing executables, real response shapes, transient runtime readiness, concurrent changes, malformed output after a successful mutation—and lacking the atomicity, reconciliation, supervision, and evidence needed to contain them. Each failure left durable residue for the next one. Once the fallback loop stopped, nothing remained to make progress, yet the process-level health signal stayed green.

The next test should be considered safe only when Fulcrum can prove both halves of its job: it can perform a workflow transition, and it can recover the truth when any boundary returns an ambiguous or incomplete result.
