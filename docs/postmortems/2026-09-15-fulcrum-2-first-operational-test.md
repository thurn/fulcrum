# Fulcrum 2 first operational test postmortem

**Incident date:** 2026-09-15 PDT (2026-09-16 UTC)  
**Status:** Workflow eventually completed; the orchestration path failed its operational objective  
**Severity:** Critical test failure; no known production impact  
**Bead:** `fc-c70c6b29`  
**Initial Marshal operation:** `fc-8018c2a0f27555a1a77e250fe5182492`  
**Code under test:** `5fa7bb6`  
**Delivered change:** `db56de4a4` (`docs: remove hooks guide`)

## Executive summary

The first Fulcrum 2 operational test eventually delivered its requested change,
but it failed catastrophically as an orchestration test. A request to delete one
obsolete Markdown file took 21 minutes 43 seconds to close and 32 minutes 53
seconds to disappear through automatic archival. It created three separate
Weaver threads, required three Marshal judgment rounds, let an Executor modify the
live integration checkout before an isolated worktree existed, and required four
Warden completion attempts plus an explicit reconciliation before promotion.

The expected path was one Weaver, one brief Marshal confirmation, one Executor,
and one Warden, all in under five minutes. The actual path was:

```text
Weaver 1 -> Marshal 1 -> Weaver 2 -> Marshal 2 -> Weaver 3
         -> Marshal 3 -> Executor -> Warden finish x4 -> promote -> close
```

The primary cause of the repeated Weaver cycle is confirmed in the code and
durable records. When Weaver marked the Bead ready, Fulcrum transferred ownership
to Marshal but retained `requested_role: "weaver"`. The Marshal brief then derived
its proposed dispatch from that stale field, did not include Weaver's prepared
scope, and still showed the Bead's placeholder unknowns and acceptance. The first
two Marshal turns followed that proposal and dispatched fresh Weavers. The third
Marshal overrode it and selected Executor. Fulcrum had no invariant or cycle
breaker preventing another Weaver dispatch after a successful Weaver `ready`.

That defect explains the most visible loop, but it does not explain the entire
duration. The workflow also suffered from missing structured scope, unexplained
multi-minute scheduler gaps, late worktree provisioning, command-contract retry
friction, validation-environment failure, stale Warden validation state, excess
background source-refresh work, and delayed closure and archival.

The observability failure is a separate incident finding. `fulcrum trace --bead`
did not include any of the three Marshal request/decision rounds because the
Marshal operations had `bead_id: null`. No durable reconciliation spans explain
the two three-minute gaps between completed Weavers and the next Marshal round,
or the nearly two-minute gap after promotion. Reconstructing this report required
correlating multiple CLI views, global service logs, archived Codex session JSONL,
provider records, Git history, historical source, and the post-incident fix. This
should have been a single-command timeline.

## Impact

- The requested deletion did eventually reach `master`; no work was lost.
- A trivial, low-risk change consumed three Weaver threads, one standing Marshal
  thread across three turns, one Executor thread, and one Warden thread.
- The Executor briefly changed and committed on the live root checkout because
  Fulcrum had not prepared the promised isolated worktree. The agent later moved
  the commit into an owned worktree and restored the root checkout.
- The delivered scope removed `docs/hooks.md` while knowingly leaving two incoming
  references, in `README.md` and `docs/fulcrum2/audit.md`, pointing at the deleted
  document.
- Operators received misleading state: the first Weaver said the work had been
  “handed to Marshal” before any Marshal notification was evidenced, and an
  automatically replayed Executor finish later produced a failed
  `FINISH_SEALED` operation even though the successful finish had already advanced
  the work.
- Five worker tasks remained visible until the ten-minute idle-archive threshold,
  extending the perceived incident from about 22 minutes to about 33 minutes.

## Expected versus actual

The target supplied for this test was a complete workflow in less than five
minutes.

| Stage | Expected | Actual | Result |
| --- | ---: | ---: | --- |
| Initial Weaver scope | <= 60 s | 43 s to accepted `ready`; 48 s to the Weaver's final response | Within target, but produced an incomplete contract and initially promised implementation |
| Marshal confirmation | <= 30 s | 9 min 25 s from initial Weaver completion to Executor authorization; three rounds | About 19 times the budget |
| Executor implementation | <= 120 s | 4 min 6 s from authorization decision to accepted finish; 3 min 8 s for the native turn | About twice the budget, including worktree recovery and command retries |
| Warden review | <= 30 s | 5 min 5 s to final accepted finish; 5 min 16 s for the native turn | About ten times the budget |
| Workflow through close | < 5 min | 21 min 43 s | 4.3 times the total target |
| Workflow through last automatic archive | Not expected to add user-visible delay | 32 min 53 s | 10 min 14 s beyond close |

Durations use the initial human prompt at 18:11:06.700 PDT as the start. Work
closed at 18:32:49.688. The fifth and final automatic archive completed at
18:43:59.824.

## Actors and identifiers

Fulcrum created three distinct Weaver tasks for one Bead. Marshal itself remained
one standing task but ran three separate turns.

| Role | Native task/thread | Native turn | Purpose |
| --- | --- | --- | --- |
| Weaver 1 | `01a0a7c4-5d07-7cd2-9c79-81294fbeca7b` | `01a0a7c4-79fd-7073-be58-dac69cf22561` | Original human request and initial scope |
| Marshal | `01a0a77c-b76f-7de0-9971-7496424b6116` | `01a0a7c5-a015-7850-b580-4ffa32e6c877` | First decision; incorrectly selected Weaver |
| Weaver 2 | `01a0a7c6-6a0a-7523-a8db-7b4160b07b59` | `01a0a7c6-7a8c-7132-a5e7-b2f6157fcc06` | Repeated scope pass |
| Marshal | same standing task | `01a0a7c9-c845-7103-830b-3e50283abdf7` | Second decision; incorrectly selected Weaver |
| Weaver 3 | `01a0a7ca-252b-7fb0-b31a-7ffcbd7b92d5` | `01a0a7ca-3495-7113-b781-4a7a3c80c5d5` | Second repeated scope pass |
| Marshal | same standing task | `01a0a7cd-4b50-7221-b9cc-982d785e3f6f` | Third decision; finally selected Executor |
| Executor | `01a0a7ce-a0de-7170-9d3d-48e671ca03a6` | `01a0a7ce-bb60-7532-8404-f962106caa81` | Deleted and committed the file, then repaired workspace placement |
| Warden | `01a0a7d1-bbbf-7043-b0c2-b802275bcf52` | `01a0a7d1-cb3f-72e1-9c57-ef1fa04e6580` | Reviewed, prepared validation, reconciled, and approved |

The user later continued the original Weaver task with a separate request to draft
a broad Fulcrum-improvement prompt. That later turn is not part of this workflow.
It matters only because task-level output defaults to the latest turn and therefore
hid the original turn during reconstruction. A separate improvement Bead,
`fc-4d170d25`, also began during this incident. It produced the later corrective
commit `ade06eb`, but it was not a stage or cause of Bead `fc-c70c6b29`.

## Detailed timeline

All times below are PDT. Durable records use UTC; the values here are converted by
subtracting seven hours. Millisecond precision is retained where it distinguishes
adjacent state transitions, not as a claim about clock accuracy across systems.

| Time | Event |
| --- | --- |
| 18:11:05.348 | The original Weaver session was created. |
| 18:11:06.700 | The user invoked the Weaver with “Please delete docs/hooks.md.” |
| 18:11:09.476 | Before registering its role, the assistant said it would read the workflow and then delete the file. This set an implementation expectation that contradicted the Weaver role it subsequently received. |
| 18:11:15.031 | Weaver called `fulcrum enter`. |
| 18:11:24.200 | Fulcrum created work record `fc-cfad...` and Bead `fc-c70c6b29`. |
| 18:11:27.730-18:11:30.541 | Weaver entry operation `fc-9eab...` completed. The client log reports 11.420 seconds for the entry command. |
| 18:11:35.661 | Weaver inspected the repository and found the requested file plus two incoming references: `README.md:12` and `docs/fulcrum2/audit.md:78`. |
| 18:11:46.888-18:11:49.690 | Weaver finish operation `fc-81b...` was accepted as `ready`. Its summary scoped deletion only and expressly left the two incoming references. |
| 18:11:55.136 | Weaver told the user it had “handed it to Marshal.” The durable evidence at this point proved a backlog handoff, not an actual Marshal notification or decision. |
| 18:12:20.999 | Initial Marshal request operation `fc-8018c2a0f27555a1a77e250fe5182492` began. It was not associated with the Bead in its operation row. |
| 18:12:23-18:12:54 | The same running Marshal operation was observed or retried several times. These are polls/retries of one operation, not additional Marshal judgment turns. |
| 18:13:03.144-18:13:05.766 | Marshal decision operation `fc-1c3d04bc41e747aca5cfc26ec7c592ba` dispatched a new Weaver. The brief's proposed action was `dispatch/weaver`, sourced from the stale author proposal. Its prose simultaneously described the work as ready for an implementer. |
| 18:13:10.223-18:13:20.648 | Dispatch operation `fc-f524...` created Weaver 2. Nested entry operation `fc-1e25...` completed during dispatch. |
| 18:13:17.717-18:13:37.078 | Weaver 2 repeated the scope pass and finished via `fc-e41...`. It made no implementation change. |
| 18:16:53.528-18:17:06.771 | After an unexplained gap of about 3 minutes 20 seconds, Marshal request `fc-c9be6294e91f5a3e875f6af9a4ae8628` and decision `fc-7d70d506375549cbb14c85e673b18dd3` again dispatched Weaver. |
| 18:17:14.697-18:17:24.745 | Dispatch `fc-5a103...` created Weaver 3. Nested entry `fc-f501...` completed during dispatch. |
| 18:17:21.949-18:17:45.597 | Weaver 3 again checked the target and references, then finished through `fc-c401...` without implementation. |
| 18:20:43.639-18:21:14.987 | After another unexplained gap of about 3 minutes 2 seconds, Marshal request `fc-fb4a522c9e5e59a68259a44924631d29` and decision `fc-2316260d78bf47d4afad935632a62c16` finally selected Executor. Marshal explicitly said it was moving beyond repeated grooming. |
| 18:22:06.456-18:22:22.317 | Executor dispatch `fc-c653...` ran. Nested entry `fc-2c2...` completed. The prompt claimed an isolated worktree had been assigned but gave `/Users/dthurn/fulcrum`, the live root checkout, as the workspace. |
| 18:22:18.598 | The Executor native turn began. |
| 18:22:44.345 | Executor deleted `docs/hooks.md` in the root checkout. |
| 18:23:06.509 | Executor committed `db56de4a4` on the root checkout. |
| 18:23:06-18:23:56 | The agent encountered command-contract friction: an invalid progress kind, terminal-stop calls missing required reason or actor identity, and an initial finish whose checks were strings rather than structured objects. |
| 18:23:56.928-18:23:57 | Corrected finish operation `fc-cb301...` reported that the source was not ready because the work had no owned worktree. |
| 18:24:21 | Worktree preparation operation `fc-76d...` completed. |
| 18:24:38-18:24:43 | Executor fast-forwarded its commit into the owned worktree, then manually detached and restored root `master` to test base `5fa7bb6`. No unapproved source activation was observed, but this recovery should never have been necessary. |
| 18:25:21.132 | Executor finish `fc-56d...` was accepted. The turn ended at 18:25:26.476. |
| 18:25:29.912 | An automatic replay of the earlier finish operation stopped with `FINISH_SEALED`, leaving a failed recovery record after the work had already advanced successfully. |
| 18:25:34.318-18:25:40.074 | Warden entry `fc-a6f...` completed; the native review turn began at 18:25:39.271. |
| 18:26:59-18:27:04.815 | Warden's first finish caused child validation `fc-52b...` to run `scripts/check`. It failed immediately with exit 2 because `scripts/prepare-check` had not been run. Parent finish `fc-a66...` returned `warden_validation_pending`. |
| 18:27:22-18:27:44 | Warden read the repository instructions, ran `scripts/prepare-check`, then `scripts/check`. Formatting, strict type checks, and all 110 tests passed. |
| 18:28:10.948-18:28:20.411 | Warden's second finish `fc-cb303...` queued Tollgate validation handle `01a0a7d4-2e8d-7d53-a03d-4ccbe01700e4` and returned pending. |
| 18:28:54.934 | A direct validation read showed that validation had passed and a certificate existed. |
| 18:29:22.878-18:29:25.011 | Warden's third finish `fc-a02...` still returned pending because completion used retained delivery state instead of the live provider result. |
| 18:29:45.325 | Warden explicitly reconciled promotion state after inspecting it. This state-machine operation should have been controller-owned. |
| 18:30:25.545-18:30:44.867 | Warden's fourth finish `fc-8af...` completed. Child approval `fc-51d...` and promotion `fc-8f30...` succeeded. |
| 18:30:55.202 | Warden's user-facing turn ended. |
| 18:32:36-18:32:49.688 | After an otherwise unexplained gap of about 1 minute 51 seconds, Fulcrum observed promotion, synchronized source through `fc-8021...`, cleaned up through `fc-6db...`, and closed the work through `fc-3c846...`. |
| 18:35:04 | A native observation recorded the terminal task state. |
| 18:43:44.926-18:43:59.824 | The five worker tasks were automatically archived in sequence. The configured idle threshold was 600 seconds. |

## The repeated-Weaver root cause

At incident revision `5fa7bb6`, Weaver completion and Marshal briefing formed a
bad feedback loop:

1. Weaver entered with `requested_role: "weaver"`.
2. The Weaver `ready` transition saved only a free-form summary, changed current
   ownership to Marshal, and placed the Bead in backlog.
3. That transition did not replace `requested_role` with `executor` and did not
   persist structured acceptance or prepared scope.
4. Marshal briefing selected its `proposed_action` from `requested_role`, labeled
   the source as the author proposal, and therefore proposed Weaver again.
5. The serialized brief omitted the completed Weaver summary and acceptance. It
   continued to report the original `unknowns` values, including `benefit` and
   `uncertainties`, and `last_progress: null`.
6. The first and second Marshal turns followed the proposed action. Each created a
   new Weaver task, and each Weaver's successful finish left the same stale role
   behind.
7. No state invariant rejected a second Weaver after `ready`, no authoring-cycle
   counter triggered a hold, and no deterministic rule selected Executor for a
   prepared scope.
8. The third Marshal escaped the loop only through model judgment, explicitly
   overriding the recommendation to “move beyond repeated grooming.”

The brief for the initial operation makes the contradiction visible. Its proposed
action was `dispatch` with role `weaver`, while its generic decision text was to
resolve scope/readiness and its eventual dispatch reason described an
implementer. The durable operation reported three attempts over 43.879 seconds,
but those attempts belong to one request operation; they are distinct from the
three actual Marshal turns described above.

This is a deterministic product defect amplified by agent behavior. The first two
Marshal turns should have recognized that the trivial work was already scoped,
but Fulcrum gave them the wrong recommendation and insufficient evidence. The
third turn demonstrated that a model could override the defect; the system should
not have depended on that discretion.

## Failure inventory

The incident contains at least the following distinct product, protocol, and
operational failures.

### Intake and scope

1. **The first response promised the wrong role behavior.** Before role entry, the
   assistant said it would delete the file. After entry, Weaver was correctly
   prohibited from editing. This made the very first interaction internally
   inconsistent.
2. **Ready scope was not a structured contract.** Weaver completion saved a
   summary but did not replace placeholder acceptance. The final workflow still
   carried “Acceptance not yet specified” instead of observable completion
   criteria.
3. **The scope knowingly allowed broken documentation references.** All three
   Weavers and the Warden found the two incoming references and treated them as
   out of scope. For a deletion, directly affected links should be removed or
   corrected unless there is evidence for preserving them.
4. **Role scope and repository policy disagreed.** The scope said no tests were
   needed, while project delivery policy configured `scripts/check`; Warden later
   had to run all 110 tests.
5. **The user-facing handoff claim exceeded the evidence.** “Handed it to Marshal”
   described an actual delivery, while the durable result at that moment only
   showed a backlog transition.

### Marshal and scheduling

6. **Weaver `ready` retained `requested_role: "weaver"`.** This was the direct
   state defect behind the cycle.
7. **Marshal promoted stale intent as its recommended action.** The brief derived
   `dispatch/weaver` from that stale role rather than from the completed scope.
8. **Marshal did not receive Weaver's decisive output.** Prepared summary,
   acceptance, and resolved unknowns were absent or stale, so the decision payload
   made a completed scoping pass look unfinished.
9. **No repeated-authoring invariant existed.** Fulcrum permitted an arbitrary
   number of new Weaver threads after successful readiness. A second Weaver did
   not require a new material question or explicit clarification decision.
10. **The first two Marshal turns accepted an incoherent proposal.** Their reasons
    described implementation readiness but selected Weaver. Agent judgment did
    not catch the mismatch, and no deterministic validation rejected it.
11. **Scheduling pauses are unexplained.** About 3:20 elapsed between Weaver 2's
    completion and Marshal round 2; another 3:02 elapsed before round 3. Configured
    reconciliation and coalescing intervals do not account for these waits, and no
    pass-level records identify the cause.

### Executor isolation and command protocol

12. **Fulcrum dispatched Executor before provisioning its isolated workspace.**
    The prompt asserted isolation but named the root checkout as the project and
    workspace. Incident source explicitly fell back to the project root when no
    worktree was present.
13. **Executor mutated the live integration checkout.** It deleted and committed
    on root `master`, then had to prepare a worktree, copy the commit, detach, and
    restore the root. This risked self-reload and unrelated-work corruption.
14. **The finish contract was needlessly difficult to discover.** The agent made
    repeated attempts for an invalid progress kind, incomplete terminal-stop
    arguments, and incorrectly shaped checks. These were avoidable protocol costs
    for a one-file deletion.
15. **An old finish operation replayed after a later finish succeeded.** The replay
    failed with `FINISH_SEALED` and advised explicit recovery even though there was
    nothing left to recover. Fulcrum did not supersede the earlier operation when
    the work advanced.

### Warden, validation, and promotion

16. **The review environment was not prepared.** Fulcrum invoked the configured
    check in a worktree that could not run it until Warden manually executed
    `scripts/prepare-check`. The first validation was guaranteed to fail in that
    environment.
17. **The controller delegated delivery mechanics to Warden.** Warden had to learn
    environment setup, retry finish, inspect provider state, and invoke explicit
    reconciliation. Review judgment should be the Warden's responsibility;
    validation submission, polling, reconciliation, approval, and promotion
    should be controller-owned.
18. **Completion used stale validation state.** Direct inspection proved a passing
    certificate at 18:28:54, but the next finish still returned pending. Only an
    explicit reconciliation made the following finish advance.
19. **One review required four finish calls.** The sequence was failed unprepared
    validation, queued validation, stale pending after validation passed, and final
    success after manual reconcile. A trivial approval took more than five
    minutes.
20. **Closure lag is unexplained.** Promotion succeeded by 18:30:44, but source
    synchronization, cleanup, and close did not run until approximately 18:32:36.
    No durable event says why.

### Efficiency and lifecycle

21. **The workflow multiplied small work into many subprocesses.** The later
    controlled benchmark against incident revision `5fa7bb6` measured 42 Beads
    subprocesses per entry and 19 per finish. The corrective candidate still used
    39 and 18. These benchmark timings are not the live incident timings, but the
    process counts demonstrate architectural amplification far beyond the expected
    one or two `bd` calls.
22. **Background source refresh was excessive.** During the 21-minute incident
    window, logs show 237 `service update` commands with 339.413 seconds of summed
    recorded duration, an average of 1.432 seconds and maximum of 5.012 seconds.
    The sum may include overlap and is not wall-clock occupancy. Contention may
    have contributed to delays, but current evidence cannot prove causation.
23. **Every new worker received a large generic context.** Full framework,
    developer, and plugin instructions were injected into each repeated Weaver,
    Executor, and Warden task. Repeating that context increased latency and noise
    for a one-line documentation change.
24. **Completed tasks remained visible for ten minutes.** Five sequential archive
    operations began only after the 600-second idle threshold. That behavior is
    configured, not mysterious, but it materially worsened the user's perception
    of a roughly half-hour workflow.

Across the Bead, the evidence shows 11 finish-command invocations: three Weaver
finishes; four Executor-related attempts or replays, including malformed and
superseded operations; and four Warden finishes. There were three dispatches and
three actual Marshal judgment turns. The initial Marshal request's internal
polling must not be counted as additional human/model decisions.

## Contributing factors

- The task was so small that the system's fixed orchestration costs dominated all
  useful work. The actual implementation was one deletion and one commit.
- Fulcrum used model judgment to repair state-machine ambiguity instead of making
  the legal next role deterministic after an accepted Weaver scope.
- Worker prompts described desired isolation and readiness rather than verified
  facts. Agents were expected to discover when those claims were false.
- The same root checkout was both the controller's live source and an available
  fallback workspace, increasing the consequence of missing worktree state.
- Polling, automatic replay, and explicit finish calls were not unified around one
  durable desired-state transition. Later success did not reliably cancel stale
  pending work.
- The configured archive policy optimized for idle cleanup rather than prompt UI
  disappearance after terminal workflow completion.

## What worked

- The initial Weaver did locate the target and its incoming references quickly.
- The third Marshal recognized and escaped the repeated-grooming loop.
- Git history preserved the Executor's work, and the agent recovered it into an
  owned worktree without losing the commit.
- The full repository check passed after preparation: formatting, strict type
  checking, and 110 tests.
- Tollgate validation, approval, promotion, source synchronization, cleanup, and
  final close all eventually completed.
- Operation, turn, provider, and commit identifiers were retained somewhere in
  the system. Although poorly correlated, they made a high-confidence
  reconstruction possible.

## Observability and logging failure

Logging was not sufficient to make this reconstruction trivial. This is a
separate failure, not merely inconvenience while investigating the orchestration
bugs.

### Missing or misleading evidence

1. `fulcrum trace --bead fc-c70c6b29` omitted all Marshal request and decision
   operations because their operation rows had `bead_id: null`, even though each
   brief selected this Bead.
2. Role entry retained a native task/thread ID but not a native turn ID. The
   original Weaver task was later reused, so default task output showed the newer,
   unrelated turn. The incident turn had to be found in archived session JSONL.
3. At least one Marshal task-output view exposed only the user prompt and an
   intermediate update, while the decision operation proved that a terminal
   command ran. No single task record contained the whole exchange.
4. Reconciliation loops emitted no structured start, end, duration, candidate
   selection, skip, wait, or exception events. The two three-minute scheduling
   gaps and the post-promotion gap therefore cannot be explained exactly.
5. `background.log` was empty. Global command logs primarily preserved completion
   envelopes, not consistently joined request, response, decision evidence, and
   state changes.
6. Repeated source-update output lacked enough timestamped context to connect an
   update to scheduler contention or to rule it out.
7. The system had no one correlation chain spanning Bead, Marshal operation,
   native task and turn, validation handle, Tollgate candidate, source activation,
   cleanup, close, and archival.
8. Operation attempts did not clearly distinguish a new model turn from a poll,
   retry, or observation. The initial Marshal operation's `attempts: 3` is easy to
   misread as three Marshal rounds even though it represented one turn.

### Evidence required for this report

The reconstruction required:

- `fulcrum trace`, `operation show`, global logs, and task list/output;
- direct archived Codex session JSONL for original turn boundaries and content;
- Git commits, reflog-relevant history, and worktree/source state;
- Tollgate validation and promotion records;
- service `update.log` command counts and durations;
- source inspection at historical revision `5fa7bb6`; and
- comparison with corrective commit `ade06eb` and the resulting
  [Weaver workflow analysis](../architecture/weaver-workflow.md).

Even after correlating those sources, the exact reasons for approximately 8
minutes 13 seconds of combined scheduler/closure gaps remain unknown. A postmortem
can identify surrounding completed events but cannot honestly reconstruct events
that Fulcrum never recorded.

### Required observability changes

1. Propagate the selected or associated Bead ID onto every Marshal request,
   decision, dispatch, and provider operation.
2. Persist native task ID and native turn ID for every role entry and continuation.
3. Emit append-only spans for every reconciliation pass, including trigger,
   selected candidates, skipped candidates with reason codes, operations started,
   elapsed time, and terminal exception.
4. Record stage timestamps and explicit wait reasons so total latency can be
   attributed to queueing, model execution, external commands, backoff,
   validation, or configured policy.
5. Preserve bounded, redacted request and result payloads for every external
   operation, with poll/replay observations distinguished from new mutations.
6. Emit old state, new state, causal operation, and authoritative evidence for
   every lifecycle transition.
7. Timestamp source-refresh start/end and record whether source identity changed,
   whether a reload occurred, and what scheduling work was delayed.
8. Add a unified incident export, for example
   `fulcrum incident export --bead fc-c70c6b29`, that produces one immutable,
   ordered bundle across Fulcrum, native tasks/turns, Beads, Tollgate, Git, and
   service activity.

## Corrective actions

### Completed after the incident

Commit `ade06eb6b184842fb600d8e2dda02f9654612ab9` addressed the principal
Weaver-to-Marshal contract defects:

- Weaver instructions now state role expectations before promising edits.
- `ready` requires a nonempty, structured acceptance array and persists prepared
  scope.
- Weaver completion replaces the placeholder acceptance and changes the requested
  next role to Executor.
- Marshal brief generation includes prepared scope and acceptance, removes stale
  unknowns, and proposes Executor.
- Tests cover the handoff contract and retry behavior.
- Responses were compacted and detailed timing made opt-in.

These changes make the intended next role explicit and recoverable, but they do
not establish a performance win. The measured analysis in
[the Weaver workflow document](../architecture/weaver-workflow.md) found no
end-to-end latency speedup: warm entry was essentially unchanged and finish was
slower in the controlled comparison. Process counts also remained high. The loop
fix is necessary, not sufficient for the user's five-minute objective.

### Resolution before the next operational test

The 2026-09-16 remediation implements the safety and orchestration actions below.
The optional Marshal bypass was deliberately not introduced: the retained scope
does not yet carry a trustworthy machine-checkable risk class, so silently
skipping human/model judgment would turn a latency target into an authorization
change. Marshal coalescing remains two seconds and the no-repeat invariant makes
that one bounded judgment rather than repeated authoring rounds.

1. **Provision isolation before dispatch.** Create and verify the owned worktree,
   set the native task's cwd and workspace roots to it, and reject Executor start
   if either still points at the live integration checkout.
2. **Prepare validation mechanically.** Configure and execute repository
   preparation before Warden review, or provide a validated image/environment.
   Warden should never need to discover `scripts/prepare-check` after a guaranteed
   first failure.
3. **Make the controller own the delivery state machine.** Warden should submit one
   evidence-backed judgment. Fulcrum should submit validation, poll/reconcile its
   result, approve, promote, synchronize, clean up, and close without repeated
   agent finish calls.
4. **Resume automatically on provider terminal events.** A passed validation
   certificate must wake the pending transition or be read live by completion.
   Stale cached state must not require an agent-issued reconcile.
5. **Enforce a no-repeat authoring invariant.** After accepted Weaver `ready`, a
   subsequent Weaver dispatch must be illegal unless Marshal records an explicit
   clarification decision containing a new material question. Cap or visibly
   hold repeated authoring cycles.
6. **Keep trivial authorization bounded.** Marshal remains explicit until scope
   records a trustworthy risk class. Its two-second coalescing window is retained,
   and dispatching a prepared scope to Weaver is rejected unless the same Marshal
   decision records a nonempty material clarification question.
7. **Reduce ledger process amplification.** Local formula rendering, mutation
   response reuse, fresh-ID creation, and empty-dependency elision reduce the
   isolated follow-up to 25 Beads calls per entry and 13 per finish. This remains
   a visible performance boundary for future batching rather than a closed claim
   that 25/13 is intrinsically low.
8. **Replace unconditional source refresh.** Use a cheap source-identity probe or
   event notification, perform the expensive update only when identity changes,
   and instrument lock/contention time.
9. **Supersede stale operations.** When a newer finish advances the work, cancel or
   mark earlier finish attempts satisfied. Automatic replay must evaluate the
   desired postcondition before producing a recovery failure.
10. **Improve deletion scope policy.** A file deletion should include removal or
    correction of incoming links unless the prepared scope records a reason to
    leave each reference intact.
11. **Hide completed tasks immediately.** Separate UI completion from the native
    archive retention interval, or archive terminal worker tasks promptly after
    durable evidence has been captured.

Items 1-5 and 8-11 are now enforced in code and covered by regression tests.
Provider-terminal reconciliation supplies the automatic resume in item 4; the
controller owns approval through closure, and a failed asynchronous validation
reopens review while preserving its failed evidence. `trace --bead` now supplies
the correlated Fulcrum/task/turn/reconciliation timeline requested by the logging
findings. It is bounded and redacted; raw external-provider retention remains with
the provider rather than being duplicated indefinitely in Fulcrum logs.

### Test gates

Focused deterministic regressions now cover the contract gates that do not depend
on model timing:

- a prepared scope cannot create an unasked-for Weaver, while one matching
  clarification decision can;
- structured scope and acceptance survive every handoff;
- Executor's verified worktree exists before its turn begins;
- no mutation touches the integration checkout before promotion;
- validation is prepared and completes without agent shell repair;
- one Warden judgment is sufficient and no explicit agent reconcile is required;
- passed validation automatically resumes approval and promotion;
- stale operations are superseded without false recovery errors;
- close follows promotion within a bounded interval;
- dangling references are rejected or explicitly justified; and
- command and reconciliation spans expose per-stage timing and causal IDs.

The suite also injects dropped operation responses, delayed and failed validation,
and failed background work to prove that retries remain idempotent and that
health/telemetry exposes the wait instead of silently adding minutes. It does not
pretend to prove model wall-clock behavior; the next live operational test remains
the acceptance measurement for exact role counts and the user's under-five-minute
objective.

## Evidence quality and confidence

The sequence of role creation, Marshal decisions, repository mutations,
validation, promotion, close, and archival is supported by durable identifiers
and is high confidence. The stale-role root cause is confirmed by incident source
and by the shape of all three briefs. The live source-update count is directly
measured, but any claim that it caused scheduling latency would be speculative;
this report does not make that claim.

The causes of the two multi-minute pre-Marshal gaps and the post-promotion closure
gap are indeterminate because pass-level evidence does not exist. They are marked
as unexplained rather than attributed to model time, process contention, or a
specific scheduler branch. Likewise, automatic archival is a confirmed configured
delay, not a scheduler mystery.

## Final assessment

The system completed the requested Git change, but success at the repository
boundary does not make this workflow successful. Fulcrum converted seconds of
useful work into a 22-minute control-plane sequence and a 33-minute user-visible
lifecycle. Its most important handoff carried stale intent instead of completed
scope, and every later boundary relied on agents to repair missing orchestration
facts or mechanics.

The next test is ready only when Fulcrum can make the intended path boring: one
scope, one authorization, one isolated implementation, one review judgment, and
automatic delivery. It must also be able to explain every wait from one correlated
timeline. Without both properties, another successful commit could still be
another failed operational test.
