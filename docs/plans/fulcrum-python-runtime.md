# Fulcrum: Python-Driven Runtime and Coordination

Fulcrum should spend model time on implementation, review, and useful decisions.
Its current agent-owned workflow also spends that time registering roles,
assembling handoffs, checking other tasks, and repeating operational context.
Python will own these mechanical operations while agents retain judgment.

The target is one local Python controller connected to the same Codex runtime as
the desktop. It creates and names tasks, supplies instructions, queues updates
until recipients are idle, and advances work from recorded outcomes. **Archon**,
the fleet scheduling decision maker, retains that authority. **Executor**, the
implementation agent, and **Overseer**, its independent reviewer, remain
separate conversations. Only one member of a pair may run at a time.

This is a target design, not a claim that the controller is implemented. It
supersedes the previous runtime-simplification proposal and conflicting runtime
ownership requirements in the older specifications. Writing this document does
not activate tasks or change the running fleet. Tollgate requires no changes.

## Related Information

These sources establish the existing behavior and the integration evidence.
Older role instructions describe the migration source, not additional duties
that agents must retain after Python takes ownership.

- [Desktop Python-control experiments](../codex-desktop-python-control.md):
  demonstrated shared-runtime creation, naming, model selection, messaging,
  status events, restoration, and archival, including timing and limitations.
- [Existing architecture](../technical-design.md): project boundaries, role
  purposes, shared memory, and the original execution-pair design.
- [Original requirements](../original-prompt.md): strategic coordination,
  independent review, and continuous workflow improvement.
- [Data contracts](../contracts.md): Beads intake, plans, identities,
  dependencies, and the agent-owned records being replaced.
- [Operations](../operations.md): existing delivery, recovery, and specialist
  responsibilities; this design replaces its scheduling and patrol mechanics.
- [Hooks](../hooks.md) and [validation evidence](../validation.md): implemented
  compaction refresh, bounded stop reminders, and narrow waiting-tool denial.
- [Setup](../setup.md), [readiness](../readiness-gate.md), and
  [compatibility](../compatibility.md): current installation and observations.
- [Dashboard](../dashboard.md): existing presentation and navigation; its
  readers must reflect the ownership and status defined here.
- [Review accounting](../../src/fulcrum/delivery.py) and [model
  policy](../../skills/fulcrum-shared/models.md): reusable review-count and
  bounded-replacement concepts, with ownership moved into Python.

## Responsibilities and Durable Authority

**Beads** is the issue tracker, and a **bead** is one issue. The **brain** is
the existing private repository containing plans, shared memory, and Beads
history. **Tollgate** owns source worktrees, CI, certification, promotion, and
configured source synchronization. A **candidate** is its retained source
submission; certification records the checks passed by the reconstructed source
it promotes.

A **task** here means a Codex desktop conversation with a runtime thread ID. An
**assignment** binds one approved bead and scope to an execution pair. A **run**
is an Archon-approved, ordered collection of related beads executed by that
pair. Different runs may proceed concurrently within configured limits.

**Weaver** interviews the user and authors tasks and plans. **Sage** assesses
workflow effectiveness. **Inquisitor** reviews whole-project architecture. Their
judgment remains model work while Python manages their operational lifecycle.

| Role | Judgment retained | Python-owned mechanics |
| --- | --- | --- |
| Archon | Priorities, capacity, exceptions | Proposals, dispatch, records |
| Weaver | Interview, scope, plans, beads | Registration, intake, archival |
| Executor | Implementation, checks, repairs | Handoffs, routine delivery |
| Overseer | Review, repair permission, exceptions | Routing, accounting |
| Sage | Workflow findings | Cadence, evidence, interviews, publication |
| Inquisitor | Architecture findings | Cadence, creation, publication |

The **controller** is the single Python process that owns operational mutations.
Run it as a per-user supervised service on the existing single Mac. Use a
process lock and SQLite transactions to prevent duplicate dispatch; configure
WAL, foreign keys, and durable commits. Do not hold transactions while waiting
for Codex, Tollgate, Git, or Beads.

- Beads remains the editable issue source of truth. Store approved scope
  snapshots and actual execution facts in SQLite, not another editable tracker.
- Python owns registrations, scheduling decisions, reservations, outcome
  records, pending messages, recurring occurrences, and delivery obligations.
- Agents invoke narrow commands. They do not edit operational records, set their
  own completion flags, or write another role's state.
- Retain ordinary Git commit IDs for source and approved documents. Do not add
  content hashes, format versions, compatibility aliases, or parallel legacy
  record writers.
- Store concrete external-operation intent before sending a mutating request.
  Apply repeated events and command retries at most once locally.
- Keep output publication and remote synchronization separately observable.
  Failed synchronization retains an identified Python owner and retry action.

For example, a failed brain push must not rerun an otherwise successful Sage
analysis or create the same finding twice:

```text
Sage result retained; findings published locally
brain push failed; controller owns retry
Sage task archived after its turn and helpers finish
retry succeeds; existing findings remain unchanged
```

The existing Night Watchman task, patrol heartbeat, and agent-authored patrol
reports are removed. Python observes runtime state and due work directly. There
is no replacement monitoring agent.

## Shared Desktop Runtime

The controller attaches to the configured app-server used by Codex desktop. It
must not start an unrelated server when that endpoint is unavailable. The
control experiment demonstrates this attachment on the inspected installation;
it does not establish a permanent compatibility guarantee.

- Use the app-server protocol behind a small adapter. Keep the endpoint local
  and preserve saved-project context, tools, permissions, and user history.
- Verify the desktop and controller observe the same task IDs and activity.
  Sharing a data directory alone is insufficient.
- Use version-matched protocol schemas when implementing the adapter. Treat
  model selection, effective working directory, hooks, helper observation,
  interruption, and task control as independently verified capabilities.
- Persist returned thread and turn IDs. Titles help humans and recovery; titles
  never grant authority or replace runtime identities.
- Subscribe to runtime events and use targeted reads for reconciliation. Avoid
  routine scans through all archived conversations.
- Keep accepted requests, terminal turns, idle tasks, and completed assignments
  distinct. An accepted `turn/start` response is not a completed operation.

The experiment observed an idle notification before the corresponding turn
completion. Dispatch therefore needs corroborating state rather than a fixed
assumption about notification ordering:

```python
can_start = (
    last_turn_is_terminal
    and runtime_status == "idle"
    and helpers_are_terminal
    and no_unresolved_start_intent
)
```

An archived or `notLoaded` task must be restored or resumed as appropriate and
confirmed ready before receiving another turn. Missing or contradictory state
retains its reservation until reconciled. Silence alone is not a failure.

### Uncertain operations

A local transaction cannot make runtime creation atomic. Recovery must preserve
uncertainty instead of retrying an operation that might already have happened.

- Record creation intent, intended role/run, project, settings, and correlation
  information before sending the request. Prefer creation without a first turn.
- Persist the returned ID, set the canonical title, and register the task before
  starting model work. No model turn is needed to discover its own identity.
- On a lost response, reconcile through supported request evidence and targeted
  history; use bounded discovery only when the ID itself is unavailable.
- Exactly one corroborated match can resolve an unknown creation. Multiple
  matches are an exception. An empty list or elapsed time does not prove the
  request was rejected.
- Record turn-start intent and exact delivered input before sending. Reconcile a
  lost response against that task's history before another send.
- Do not use a matching prompt alone as proof when identical prompts could
  legitimately occur; require the intended task, baseline turn history, and
  available operation correlation.

```text
turn/start response lost
reservation and message batch remain unresolved
thread/read identifies the accepted turn after the retained baseline
controller attaches that turn; no second message is sent
```

Endpoint failure disables new managed starts. The controller reconnects and
reconciles retained work; it does not fall back to agent-driven fleet tools.
Setup must expose capability failures clearly rather than claim unknown hook or
runtime behavior works.

## Setup, Identity, Names, and Models

Setup establishes the controller's connection, enrolled projects, and current
Archon binding. It creates Archon through Python, using explicitly configured
model and reasoning effort. There is no default Archon model and no Watchman
creation step.

- Enrollment verifies the intended repository, saved Codex project, host, and
  existing Tollgate integration. Missing integration blocks that project's
  implementation dispatch, not unrelated observation or intake.
- Setup is repeatable: reuse its retained operation and actual Archon ID rather
  than create another Archon on every invocation.
- Archon's first useful turn establishes initial global/project limits and
  recurring policies. No execution starts until those policies exist.
- Replacement transfers the binding after the previous Archon is inactive and
  pending decisions are reconciled. Reject later commands from the former
  binding; preserve previously approved scope and pending work.
- Preserve Archon's configured model across replacement. Changes require user
  instruction. Use the normal explicit local setup/admin path, without adding
  biometric dialogs or a new authentication subsystem.

### Canonical names

Python allocates names before managed work starts. Use one persistent increasing
numeral sequence. An Executor/Overseer pair shares one allocated numeral; every
other managed task receives its own. Never recycle an allocated numeral,
including after archival or failed provisioning.

Use these role prefixes and a concise description:

```text
👑 Archon 1 — Fleet scheduling
🧵 Weaver 2 — Simplify search indexing
⚒️ Executor 3 — Search indexing
🔎 Overseer 3 — Search indexing
🦉 Sage 4 — Workflow postmortem
🧐 Inquisitor 5 — Fulcrum architecture
```

Runtime ID remains authoritative. Preserve numeral and role prefix through
compaction, restart, restore, and description changes. Python stores the pair
relationship separately. Native subagents are helpers within a parent task and
do not receive fleet numerals.

If a managed title changes unexpectedly, reconcile it to the retained role,
numeral, and approved description on the next relevant event. Do not perform a
fleet-wide rename scan on every tick. Publish and verify the name before
reporting registration complete.

### Model policy

Weaver's argument selects the resulting Executor model, not the model of the
human's current Weaver conversation. Record the selected model on every bead
created by that intake and in the eventual assignment.

| Role or selection | Model | Reasoning effort |
| --- | --- | --- |
| Archon | Explicit setup configuration | Explicit setup configuration |
| Overseer, Sage, Inquisitor | `gpt-5.6-sol` | `high` |
| `$weaver luna` Executor | `gpt-5.6-luna` | `xhigh` |
| `$weaver sol` Executor | `gpt-5.6-sol` | `high` |

Validate aliases and effective settings against the live catalog. Missing Weaver
arguments return a short usage error, not a model choice made by Python.
Unsupported settings block affected dispatch and produce one actionable
condition. Do not silently substitute models or efforts.

Archon may explicitly change an Executor's model with rationale. Apply the
change at a turn boundary, retain earlier attempts, and preserve any stricter
human constraint. Existing policy requiring explicit human authority for Astra
continues. Python never upgrades a model solely because a counter reached a
threshold.

## Weaver Intake and Instruction Delivery

Weaver converts intent into concise tasks or a standalone plan with tasks. It
registers through a fast Python startup operation, receives the prompt needed
for its current mode, and performs its authoring work without fleet discovery or
a registration conversation.

The following are target Fulcrum interfaces, not existing commands promised by
the current installation:

```sh
fulcrum weaver register --model sol
fulcrum instructions
fulcrum intake --input tasks.json
fulcrum finish --input outcome.json
```

Registration binds the actual runtime identity, allocates the numeral once, sets
the canonical name, and returns the relevant instructions in one response. An
identical retry returns that registration. Conflicting model selection after
publication needs an explicit update, not silent relabeling of existing work.

Codex Plan Mode still prohibits publishing documents, beads, and source changes.
Weaver naming and operational registration must be provided by controller-side
activation outside the Plan-mode agent's mutating commands; instruction
retrieval inside the turn is read-only. Verify this activation boundary in the
desktop. If the installed runtime cannot provide it, report the capability gap
rather than instruct a Plan-mode agent to bypass its restrictions. Registering a
Weaver never authorizes publication.

- Standalone intake requires project, title, intended outcome, bounded scope,
  and acceptance criteria. Dependencies and context are included when needed.
- All tasks, including tasks from plans and specialists, are pending by default.
  Only explicit future designation excludes them from scheduling proposals.
- Pending means eligible for Archon consideration. It does not approve work.
- Publish substantial plan documents and their task graph after authoring
  approval. Complete the graph before making its members dispatchable.
- Use stable intake identities and actual Beads IDs to reconcile retries,
  refinements, removed dependencies, and partial publication. Reject ambiguous
  duplicates, missing prerequisites, and dependency cycles.
- Retain approved plan commit and exact scope on assignment. A later document
  edit cannot silently change an active Executor's instructions.
- Commit/push Markdown and Beads history through their respective native paths.
  Preserve failed pushes as Python-owned obligations without repeating intake.
- A completed Weaver calls finish and ends. Python archives after retaining the
  outputs and assigning ownership of remaining publication work. Weaver never
  waits for Archon acknowledgment or archives itself directly.

A tiny task should remain tiny:

```json
{
  "project": "fulcrum",
  "title": "Correct the install example",
  "outcome": "README matches the supported install command",
  "scope": "README installation example only",
  "acceptance": "Compare with the actual installation instructions",
  "model": "sol", "activation": "pending"
}
```

### Python-produced prompts

Python assembles canonical role instructions and assignment-specific context at
startup, at each managed wake, and after compaction. Installed skills become
thin entry points rather than a chain of files an agent must repeatedly read.

Each managed prompt supplies the role, current assignment, approved scope,
relevant evidence, permitted action, and exact finish obligation. Include only
changed context and necessary references on follow-up turns. Required scope must
remain complete; do not truncate it merely to hit a prompt budget.

```text
You are Overseer 3. Review candidate c-42 for bead fc-31.
Scope and source references are attached; inspect the actual changes.
Return approved, changes_requested, or incomplete using fulcrum finish.
Do not contact Executor or operate Tollgate. End after finish succeeds.
```

Archon receives compact proposals and exceptions. Python persists any briefing,
NEWS, or project-summary updates authored in Archon's result using existing
brain conventions. Preserve useful strategic memory without requiring Archon to
perform Git administration or rewrite unchanged summaries every turn.

## Scheduling, Capacity, and Holds

Archon is the final approver of task scheduling. Python performs eligibility
checks and proposes concrete work; it cannot infer approval from priority,
pending status, or an unanswered notification.

Approval records exact tasks, grouping, order, scope, and applicable conflicts.
Independent work may be separate runs even within the same project. Related
sequential work may share a pair. A run is project-scoped; cross-project task
dependencies are supported without moving a pair between projects.

```json
{
  "runs": [
    {"project": "fulcrum", "beads": ["fc-31", "fc-32"]},
    {"project": "battlement", "beads": ["bt-18"]}
  ],
  "global_limit": 4,
  "project_limits": {"fulcrum": 2, "battlement": 2}
}
```

Those numbers illustrate a decision, not hard-coded defaults. Approval binds
stored content; additions or material scope changes require a new decision.
Python may start approved work, route review and repairs, and resume bounded
recovery without another approval for each turn.

Before every managed start, recheck activation, dependencies, unique ownership,
project health, holds, conflicts, capacity, and current approval. Prefer
Archon's recorded ordering. At equal priority, resume actionable started work
before starting another assignment; use readiness time and stable ID as
tie-breakers. New unapproved work cannot displace approved authority through
this algorithm.

A prerequisite is satisfied by actual required completion, not just an issue
marked closed. Code completion includes promotion, required source sync, owned
cleanup, and Beads closure. Cancellation does not satisfy a prerequisite unless
an approved scope change removes that dependency.

### Counting active work

A **slot** is a reservation for one managed parent-task turn and its native
helpers. Global and per-project limits count these reservations, not open
conversations or unfinished assignments.

Parents may compose helper prompts and use native spawn and
result-delivery tools. This is the exception to Python-produced role prompts and
fleet coordination, not permission to create another managed role or to replace
the Overseer's independent code review.

- Executor and Overseer alternate within a pair. Reserve capacity before
  dispatch; allow at most one active or uncertain start for the pair.
- Keep the reservation until the parent turn and its helpers are terminal. A
  parent waiting on its native helper still occupies its slot.
- Main roles do not use peer-task waiting tools. Native helper waiting and
  result delivery are an explicit exception, not a way to monitor another fleet
  role or substitute for Overseer review.
- Sage and Inquisitor consume execution slots. Fleet Sage consumes a global slot
  without a software-project slot; Inquisitor consumes both global and
  reviewed-project capacity. Interviews with execution roles consume their
  subject's global/project capacity as well.
- Archon and human-invoked Weaver turns are outside execution limits, including
  their native helpers. Record all their usage nevertheless.
- CI waits, delivery retries, and inactive decision waits consume no model slot.
  Resumption must reacquire capacity. This policy does not bound the number of
  unfinished assignments; show that count separately.
- Unknown runtime activity retains capacity. Reconcile before replacing a task
  or claiming that its slot is available.

The user will not manually start a pair member while its partner is running.
Python guarantees exclusion for managed starts under that operating assumption.
Unexpected direct activity is a conflict to hold and reconcile, not evidence
that strict prevention of manual desktop starts has been implemented.

### Scoped pauses and conflicts

Archon may change limits at any time. Decreasing them prevents further starts
until usage falls below the new limits; it does not kill running work. A zero
limit deliberately disables starts in that scope.

- Ordinary pauses let active turns finish and retain their results. Block new
  review, repair, next-bead starts, and new delivery authorization under the
  hold.
- An explicitly urgent pause requests interruption, preserves work, and waits
  for observed termination. Do not equate interrupt acceptance with quietness.
- Scope changes hold the affected assignment, reconcile outstanding candidate
  authority, and require an approved replacement scope. Other runs continue.
- Holds compose: releasing one does not override another. Resume rechecks all
  constraints and retained work before dispatch.
- Archon can require a host/project quiet period or declare particular runs
  incompatible. Python acquires that exclusion before starting either run and
  keeps it through owned delivery/resource cleanup, including slot-free waits.
- A quiet machine requires observed relevant process and Tollgate drainage, not
  merely zero model turns. Do not stop unrelated user processes or treat missing
  measurements as proof of quietness.

```text
global limit changes from 4 to 2 while 4 turns run
all four may finish; no fifth turn starts
usage falls to 1; one eligible approved turn may start
```

Fulcrum does not implement general shared/exclusive resource keys or per-command
permits. Use explicit assignments and existing Tollgate resource admission for
ordinary work; use scoped holds for a quiet benchmark or known conflicting runs.

## Queued Updates and Idle-Only Delivery

Python translates outcomes and observations into useful recipient updates.
Agents report structured results; they do not compose routing messages to other
fleet tasks. A durable outgoing queue survives busy recipients and restarts.

- Retain updates by recipient and condition or assignment. Collapse successive
  status changes into the latest state while preserving unresolved decisions,
  findings, and distinct requested actions.
- Assemble one bounded batch when the recipient can use it. Do not send new task
  input while that recipient or its native helpers are active.
- Coalesce ordinary Archon updates for up to 30 seconds before an eligible
  delivery. Actionable pair handoffs need no artificial delay.
- Urgent exceptions bypass the coalescing delay but still wait for an idle
  Archon. Apply required operational holds immediately in Python.
- Include concise action items, counts, and links to complete records. Never
  silently omit part of the scope Archon is being asked to approve.
- Suppress scheduling-only delivery when there is no eligible project/global
  capacity for pending work. When capacity or eligibility changes, re-evaluate
  once. Other actionable exceptions remain deliverable to idle Archon.
- Routine stage changes and individual completions update status without an
  Archon turn. Deliver completion summaries with another useful batch, or once
  at collection completion when a summary or decision is useful.

A message batch progresses through retained, sending, accepted, and processed
states. Acceptance means the runtime accepted a turn. Processing requires the
matching finish outcome to acknowledge the batch and record decisions or an
explicit deferral. Defer with a reason and relevant reactivation condition, not
an immediate identical wake.

```text
Archon is busy; three completions and two pending tasks accumulate
capacity is full; scheduling remains retained
capacity frees; Archon becomes idle
one batch contains current capacity, pending work, and completion summary
```

Freeze the delivered input and batch membership. Updates arriving during the
recipient's turn belong to the next pending batch. A reply cannot accidentally
acknowledge unseen updates. Validate decisions against current scope and holds;
stale decisions remain historical and return a concise conflict for resolution.

A lost send response leaves the batch unresolved until runtime reconciliation.
Never resend solely because the agent has not acknowledged it yet. If its turn
ends without a required outcome, use the shared completion-recovery policy. The
same unchanged exception does not create repeated Archon wakes.

## Finish Commands and Lifecycle Hooks

`fulcrum finish` is the mandatory end-of-work command for managed role turns. It
records a semantic outcome and returns a short instruction to end the turn. It
does not immediately wake another agent or archive the caller.

Bind commands to the registered runtime task and current managed turn. Derive
identity from the invocation environment and controller dispatch record; do not
allow an arbitrary `--role` or supplied task ID to select someone else's rights.
Validate legal outcome kinds, assignment, current source, and expected state.
This protects cooperative workflow correctness, not against a hostile local
administrator or an agent deliberately bypassing every available interface.

```json
{
  "assignment": "assignment-31",
  "outcome": "ready_for_review",
  "candidate": "c-42",
  "source_oid": "actual-git-commit-id",
  "checks": [{"command": "focused validation", "result": "passed"}],
  "evidence": ["retained-review-artifact"]
}
```

Use role-specific outcomes rather than a generic success flag:

- Executor: ready for review, permitted repair complete, blocked, or
  checkpointed.
- Overseer: approved with repair permissions, changes requested with findings,
  incomplete with missing evidence, or an exception requiring judgment.
- Archon: recorded scheduling/policy decisions, deferrals, and summary updates.
- Weaver: retained authoring/intake result, future plan, or explicit blocker.
- Sage/Inquisitor: report and findings, or a retained request for missing
  evidence.
- An interview answer identifies its occurrence and answers the consolidated
  request. It cannot revive the subject's old implementation assignment.

Expected waits are successful retained states, not errors. An identical finish
retry returns its existing result; conflicting reuse is rejected. An outcome
accepted for a turn is frozen.

After finish, Python advances only after these explicit checks pass:

- The accepted outcome belongs to the dispatched task, turn, and assignment.
- The matching turn completed normally and its native helpers are terminal.
- For candidate outcomes, the native candidate's immutable source OID matches
  the recorded outcome and the candidate is the assignment's expected candidate.
- Current approval and holds permit the next action, with native delivery facts
  checked at the delivery boundary described below.

Missing observations wait for reconciliation. Explicit contradictions hold the
assignment for resolution. An interrupted or failed turn retains its outcome
for inspection without automatically approving, closing, or handing off work.
Python does not interpret arbitrary later tool calls or final prose to decide
whether the agent invalidated its report. Do not build a transcript classifier,
shell-command parser, or general post-finish activity detector. A later worktree
edit does not alter an already submitted immutable candidate or authorize a new
one; advancement remains bound to the recorded candidate.

Reconciliation reads the specific runtime, assignment, and native candidate
facts that failed the checks above; it does not repeat the preceding operation.
If those facts are resolved and only a corrected semantic report is needed, use
the same single correction allowance described below. Its recovery turn may
submit a new outcome that explicitly supersedes the old one; preserve both
records. An exhausted allowance, unresolved external operation, or changed scope
requiring judgment becomes an Archon exception.

### Bounded enforcement

Hooks restore useful instructions and detect missing outcomes. Their purpose is
to prevent common mistakes cheaply, not to implement the whole scheduler inside
synchronous callbacks.

- Startup and `SessionStart` compaction handling retrieve Python's current role
  and assignment instructions. Use local cached controller facts so a refresher
  does not scan Beads, transcripts, or the fleet.
- A stop hook checks whether the active managed turn has an accepted outcome. If
  absent, request the exact finish command once with the relevant allowed
  outcomes. Do not ask the agent to inspect whether it messaged a peer.
- Persist one correction allowance per failed managed-work attempt, shared by
  the hook and controller. A recovery turn is linked to that same attempt and
  cannot reset the allowance.
- If a hook already intervened, a later controller observation cannot grant
  another correction. If hooks were missed and the task is idle, Python may
  schedule the one correction turn under normal capacity constraints.
- Failure after that allowance becomes one retained Archon exception. Preserve
  work; do not infer semantic success from final prose or repeat indefinitely.
- Use narrow tool guards to deny direct fleet messaging, peer waiting, managed
  role spawning, and direct archival. Allow the native-helper exception. Do not
  add a shell-command parser or claim these guards prevent all bypasses.

```text
Executor stops without finish
stop hook uses the attempt's one correction allowance
Executor still stops without a valid outcome
Python retains unfinished work and queues one Archon exception
```

Unrelated tasks and Plan-mode authoring stops receive no finish correction.
Plan-mode Weaver registration/context does not imply an active execution
obligation. Hooks should normally complete from local facts well below their
explicit two-second ceiling. If the controller is unavailable, retain
diagnostics and allow bounded stopping; reconciliation must not assume the hook
ran.

## Pair Execution, Review, and Delivery

Python starts Executor directly with Archon-approved scope. There is no routine
Overseer assignment-preparation turn. Each bead has one active assignment and
one owned source worktree; sequential beads in a run use fresh worktrees.

The Python helper invokes existing Tollgate worktree creation from the actual
registered Executor context and retains the native owner, path, branch, and
base. Do not create an independent Codex worktree as well. Controller operations
must respect the existing Tollgate ownership contract; verify those operations
through the real integration before enabling dispatch.

- Executor investigates, implements, and runs proportionate local validation. It
  supplies relevant rendered evidence for UI changes and tracks owned demo or
  background processes that require cleanup.
- Python helpers submit the immutable candidate without promotion authority and
  retain its actual identities and evidence. Existing speculative CI may proceed
  without a model waiting for it.
- Executor records ready-for-review and ends. After confirmed inactivity, Python
  schedules Overseer with the exact scope, source, and evidence.
- Overseer independently inspects actual source and surrounding code read-only.
  It neither edits the Executor worktree nor runs another build pipeline there.
- Overseer records a review outcome and ends. Python validates the result and
  routes a correction or advances delivery after confirmed inactivity.

The principal stages describe expected work; holds and unresolved obligations
are separate so pausing does not erase progress:

```text
queued, preparing, implementing, review_pending, reviewing,
correcting, delivering, recovering, completed, canceled
```

An escalation can occur at any stage without losing its source, candidate,
review history, or next responsible action. Overseer handles in-scope judgment
calls; Archon handles scheduling, scope, model changes, and unresolved
escalations. Neither starts a turn merely to relay an unchanged status report.

### Review authority and bounded repairs

A **mandate** is Overseer's retained approval of the assignment scope and exact
candidate, including any expressly permitted replacement categories. It is
Fulcrum approval evidence; it is not a new Tollgate certification mechanism.

- Review outcomes are approved, changes requested, or incomplete. Approval
  requires an explicit scope assessment and no unresolved blocking findings.
- Findings identify incorrect behavior, evidence, and a requested correction.
  Preserve stable finding references within a review cycle.
- Count each newly assessed source once. Missing evidence, duplicate reports,
  transient infrastructure failures, and repeated candidate observations do not
  increment unsuccessful substantive reviews.
- After three unsuccessful substantive reviews, pause further correction and
  retain an Archon exception. Archon chooses the next bounded approach without
  erasing prior attempts. There is no automatic model upgrade or promotion.
- Repair permissions may cover ordinary merge-conflict resolution and bounded
  in-scope CI fixes. Permissions must be explicit; absence means review again.
- A covered replacement retains its new source/candidate, original approval,
  predecessor, repair reason, and validation. Executor reports the concrete
  repair; Python checks identity and permission mechanically.
- If classifying the repair requires uncertain scope judgment, route to
  Overseer. Do not ask Python to infer semantic equivalence from an arbitrary
  diff, and do not require routine re-review for clearly covered repairs.
- A source change outside the mandate or an approved scope change requires a new
  review. New source still requires Tollgate certification in every case.

```json
{
  "outcome": "approved", "candidate": "c-42",
  "scope_assessment": "Matches the approved indexing task",
  "blocking_findings": [],
  "allowed_repairs": ["ordinary_merge_conflict", "bounded_in_scope_ci_fix"]
}
```

Source immutability, registered caller, current assignment, and explicit review
outcome are validated in Fulcrum. Do not introduce a verifier child process,
biometric override, or Tollgate review-enforcement policy. Normal local tools
remain outside a hostile-agent security boundary; this design does not claim
that every direct Tollgate invocation is made impossible.

### Python-owned delivery

Python performs routine delivery after review, using existing Tollgate status,
authorization, diagnosis, retry, push, and cleanup interfaces. No agent remains
active to observe CI or retry an ordinary network failure.

- Recheck candidate/source, scope, mandate, inactivity, and holds before new
  authorization. If a native authorization also covers pending dependencies,
  inspect that set and require appropriate authority for each affected item.
  Refuse uncertain coverage rather than authorize extra work implicitly.
- Continue existing speculative CI instead of launching duplicate validation.
  Read native certification and promotion results, not inferred success flags.
- On failure, obtain retained native diagnosis and logs. Permit one unchanged
  retry when evidence supports a transient infrastructure problem. A source
  repair resumes Executor with the specific failure and applicable mandate.
- Bound transient connection and synchronization retries with backoff; after
  repeated failure retain one exception and retry observation at low frequency.
  Reconcile uncertain mutations before retry regardless of elapsed time.
- Preserve promoted-but-unsynchronized work as delivery recovery. Never create
  another implementation assignment merely to push or clean up existing work.
- Verify configured source synchronization, owned process termination, and
  supported worktree/branch cleanup before Beads closure. Keep brain-history
  pushes as separately owned obligations.
- Archive a completed pair only after both conversations/helpers are inactive
  and all remaining administrative obligations have durable ownership.

An assignment is complete only when required review or covered replacement
approval, certification, promotion, source synchronization, owned cleanup, and
Beads closure are observed. A run proceeds to its next bead only then.

Tollgate may already have accepted authorization when a pause arrives. Use its
existing cancellation or suspension capability where available and report the
observed disposition. A local hold cannot guarantee revocation of an accepted
operation or undo promotion. If a required stop cannot be established through
existing interfaces, hold further work and expose the limitation; do not require
a Tollgate change or silently claim safe revocation.

When reusing the pair, confirm the previous bead's cleanup and bind both roles
to the new assignment. Update Executor's effective worktree and permissions
through supported runtime configuration and verify its Git root before edits. If
the runtime cannot safely change that context, hold the run as an integration
failure rather than continue in the previous worktree. Overseer receives the new
scope independently of Executor's narrative; earlier mandates do not apply.

## Recurring Specialists and Single-Round Interviews

Archon approves standing recurring policies. Python tracks due times, creates
occurrences, and dispatches them under those policies without seeking approval
for each daily occurrence. Archon may revise or suspend the policies.

Default cadence is one fleet Sage run every 24 hours and one Inquisitor run per
enabled project every 24 hours, offset twelve hours from the Sage anchor.
Preserve configured anchors across restart.

- Maintain one unfinished occurrence per job. After downtime, create one
  catch-up occurrence, not one per missed day.
- On completion or explicit skip, advance to the first future cadence point.
  Failed analysis or publication stays attached to the same occurrence.
- Apply holds, limits, project eligibility, and Archon's priorities. Policy
  approval does not exempt specialists from capacity or conflict checks.
- Sage reads workflow measurements, failures, and retained evidence. Inquisitor
  examines the whole project at a recorded certified source commit; recent
  changes receive no privileged review scope.
- Findings include the problem, evidence, expected benefit, affected project,
  and acceptance criteria. Deduplicate against existing issues using stable
  problem identity and explicit reconciliation, not another generated hash.
- Publish actionable findings as pending unless explicitly deferred. Archon
  still approves execution. Specialist default implementation model is Sol/high
  unless its approved policy selects another supported model; persist it on the
  resulting beads and permit normal Archon revision.
- Retain reports, including explicit empty findings. Publication retries reuse
  the report and occurrence without repeating model analysis.

### Interviews

Sage first uses retained evidence, then may request one consolidated interview
per selected task per postmortem. There is no follow-up questioning round.

```text
Sage records requests for Executor 3 and Overseer 3, then ends
Python queues each interview until eligible and available
subjects answer once through finish; Python restores prior archival state
Sage resumes once with answers and explicitly missing responses
```

Python retains subject identity, prior archival state, request, due time, and
answer status. Default collection timeout is 24 hours from the request, and the
standing policy may override it. This prevents an unavailable subject from
holding an occurrence forever without treating silence as an answer.

- Do not interrupt active work for interviews. Obey pair exclusion and normal
  subject capacity; an interview does not authorize old code work.
- Restore archived subjects only when ready to interview. Rearchive only
  subjects Python restored, after their answer turn and helpers finish.
- Distinguish interview-only outcomes from implementation outcomes and hooks.
- Resume Sage when all requests are resolved or the collection deadline passes.
  Report absent answers as missing evidence, without reminders to the subject or
  another interview round. Late answers are retained without reopening the
  completed postmortem automatically.
- At the deadline, expire interview requests that have not started and remove
  their pending delivery. They must not wake subjects later. Reconcile an
  uncertain start before classifying it as expired. Already-running interviews
  may finish; retain their late answers and restore prior archival state without
  interrupting them merely because collection closed.
- An interrupted interview may consume the same bounded finish-recovery
  allowance as other managed turns; it is not permission for new questions.

Specialists require no source worktree or fabricated promotion candidate for a
read-only report. Python archives them after their result is retained,
publication is reconciled, helpers are inactive, and remaining synchronization
has a durable owner.

## Recovery, Status, and Efficiency

Python compares expected workflow state with actual runtime and native delivery
facts. It should explain a concrete invalid state, not assign another agent to
ask whether work is still happening.

- Reconcile startup state, connection recovery, terminal events, unexpected
  archival, failed turns, missing outcomes, and overdue concrete obligations.
- A quiet long-running task is not presumed dead. A bounded liveness check may
  read its state, but elapsed silence alone never authorizes replacement.
- Retain uncertain ownership and capacity until actual inactivity is known.
  Inventory worktree, candidate, and processes before any approved replacement.
- Never adopt another Executor's owned worktree implicitly. Replacement work
  needs explicit authority and an ownership path supported by existing tools.
- Reconcile interrupted operations before retry. Retain the same operation ID,
  expected inputs, observed result, and next action across controller restarts.
- A controller crash cannot create a second scheduler: the process lock and
  reconciliation gate precede dispatch after service restart.

If Archon itself exhausts finish recovery or becomes unavailable, retain its
pending decisions and show one operator-attention condition in local status and
the dashboard. Do not escalate endlessly back to the same broken task or give
another role scheduling authority. Previously approved work may continue where
safe; new approvals wait for explicit recovery or setup replacement.

Read-only status and the dashboard use the same controller readers:

```sh
fulcrum status --run run-3 --events 20
fulcrum status --queue
fulcrum status --capabilities
```

Expose role/title/ID, project, current bead, stage, runtime activity, helpers,
slot usage, unfinished-work count, holds, pending decisions, last observation,
review/source identities, and remaining delivery obligations. Explain why a
queued run cannot start. Missing observations are unavailable, never zero or
complete. Task links must open the actual desktop context.

### Audit every recurring process

Measure cost at each boundary before expanding its machinery. The target
successful path has no registration conversation, peer-status turn, routine
Overseer setup, model-driven CI wait, or acknowledgment-only Archon wake.

| Existing cost | Target behavior | Evidence to retain |
| --- | --- | --- |
| Role discovery | Python creates and records ID | Setup duration |
| Repeated context reads | Action-specific prompts | Size and cache usage |
| Peer coordination | Outcomes and idle-only batches | Latency, wake reason |
| Patrol agent | Events and targeted reads | Exceptions, recovery time |
| CI/push monitoring | Native observations, Python retries | Repair wakes |
| Repeated findings/interviews | Reuse, single round | Findings and turns |
| Stop reminders | One correction allowance | Misses, hook duration |

Record task creation, registration, eligible-to-start delay, Archon decision
wait, implementation, review, correction, CI, push, and cleanup separately.
Include failed attempts and helper turns. When available, record input, cached
input, output, and reasoning tokens without double-counting parent/helper
aggregates. Unavailable usage remains unavailable; API price estimates are not
proof of Codex subscription savings.

Do not impose the old proposal's fixed model-comparison trial, financial
threshold, or automatic model-routing change. Compare representative tiny,
related-task, failure-recovery, and UI workflows against observed prior costs.
Report elapsed time and review quality alongside token use so fewer tokens do
not conceal more defects or unfinished work.

Keep full external evidence outside prompts and return concise results with
references. Do not add verbose per-tool logging, duplicate CI, continuous full
history reads, generic resource locks, or extra analysis agents without a
specific demonstrated need.

## Drain-Before-Cutover Migration

There must be one owner of dispatch. Transition only after the old scheduler
stops assigning new work and existing runs and delivery obligations finish or
are explicitly resolved by an operator.

- Inventory active roles, worktrees, candidates, holds, interviews, publication,
  source pushes, cleanup, and recurring occurrences before changing ownership.
- Do not automatically adopt active conversations or incomplete candidates into
  the new controller. Unresolved work blocks cutover; do not discard it to make
  the inventory appear clean.
- Preserve projects, plans, Beads identities/dependencies, model constraints,
  strategic memory, cadence anchors, and historical review/delivery evidence.
- Initialize the numeral counter above all historical allocated numerals.
  Historical names remain history; new managed names follow the shared sequence.
- Disable Watchman's heartbeat, legacy specialist launch paths, and legacy
  scheduler ownership before enabling the controller. Verify their disablement.
- Import durable data atomically and retain a cutover record. Create the new
  Archon through setup or reuse the already recorded setup operation; do not
  accidentally appoint an old inactive role through its title.
- Replace agent-owned operational writers, readiness requirements, role skills,
  hook instructions, and dashboard readers with the new ownership model. No
  dual-write system, backward-compatibility layer, or versioned record format is
  required. Retain an inert historical export for diagnosis.
- Verify shared runtime capabilities and initial Archon policies before the
  controller can dispatch. Failures leave dispatch disabled with named reasons.

```text
legacy dispatch disabled; old assignments and delivery resolved
historical state preserved; durable data imported
new setup and capability checks pass; Archon establishes policies
controller dispatch enabled; old launch paths remain disabled
```

A restart during cutover reads the retained operation and reconciles actual
external state before continuing. After new dispatch begins, recover the new
controller rather than restart the legacy scheduler against the same beads. No
Tollgate policy installation, service patch, or review-enforcement migration is
part of this transition.

## Automated Validation

Tests must distinguish workflow correctness from runtime capabilities. Use
controlled adapters for state-machine and failure injection, then exercise the
actual desktop and Tollgate boundaries described in Manual QA.

- Check concurrent command/event handling, unique bead ownership, durable
  numbering, paired starts, global/project limits, holds, and duplicate
  outcomes.
- Deliver idle and terminal notifications in both orders. Lose creation and send
  responses before persistence, replay events after restart, and verify no
  duplicate starts, lost batches, or premature capacity release.
- Test busy-recipient coalescing, full-capacity scheduling suppression,
  exception delivery, explicit deferral, and updates arriving during a
  recipient's turn.
- Test finish success, missing hook, hook-used correction, failed correction,
  candidate/source identity mismatch, interrupted turn, and non-Fulcrum/Plan-mode
  stops. Verify that a later worktree edit cannot substitute source for the
  recorded immutable candidate. Verify superseding-outcome history and rejection
  of another correction when the shared allowance was already consumed.
- Exercise review counts, missing evidence, covered and uncovered replacements,
  model changes only by Archon decision, and source repair followed by native
  certification. A pending native dependency cannot gain implicit authority.
- Verify permitted helper use, denied peer operations, helper termination before
  pair handoff, and unavailable helper observations retaining the reservation.
- Test future-work exclusion, default pending findings, partial intake, stable
  retries, graph changes, unsupported model settings, and title restoration.
- Advance an injected clock through downtime, delayed specialist dispatch,
  publication failure, single-round interviews, collection timeout, and no
  duplicate recurring occurrences or repeat interviews. Expire pending
  interviews while one start is uncertain; reconcile that start and ensure
  expired requests cannot be delivered afterward.
- Fail push and cleanup after promotion; verify delivery recovery without new
  implementation. Interrupt drained cutover and ensure one scheduler remains.
  Exhaust Archon's finish recovery and verify one operator condition, retained
  decisions, no self-escalation loop, and no new approval authority.

The first real integration check must include a shared desktop task, a genuine
Executor/Overseer alternation, and existing Tollgate delivery. A fixture that
accepts a fabricated completion record cannot establish that boundary.

## Manual QA

Use disposable desktop tasks, a disposable enrolled source repository with
existing Tollgate configuration, and isolated brain/operational state. Provide
read-only queue, run-event, capability, and obligation views plus an isolated
failure-injection harness. The harness must not mutate production observations.

### Early assembled-product flow

Exercise this flow before relying on broad component-test results. It crosses
Weaver intake, Archon authority, Python runtime control, real review, and native
delivery.

1. Verify desktop and Python share the intended runtime. Configure Archon model
   and effort, let Python create and name Archon, and record initial limits and
   recurring policies. Confirm setup creates no Watchman or duplicate Archon.
2. Invoke Weaver with `sol` for a small documentation task. Check its numeral,
   title, returned instructions, and selected bead model. In Plan Mode verify
   registration/context without document or bead publication before approval.
3. Publish the task and inspect its pending status. Confirm Executor does not
   start until Archon approves the exact run and scope.
4. Observe Python start Executor without an Overseer preparation turn. Open the
   task and verify model, working directory, relevant prompt, and numbered name.
5. Submit ready-for-review through finish. Delay the final turn completion and
   verify Overseer remains idle. Then observe Overseer start with the same run
   numeral after Executor and any helpers are terminal.
6. Have Overseer approve the actual candidate with explicit repair permissions.
   Verify Python waits for that turn to finish, then drives existing Tollgate
   authorization, certification, promotion, source synchronization, and cleanup.
7. Confirm Beads closure follows the required evidence. Both pair tasks archive
   only when inactive; status retains the completed work and any separately
   owned brain push. Inspect timings for absence of coordination-only turns.

### Queue, capacity, and prompt checks

Use small independent runs in two projects and inspect queue state and actual
runtime activity together.

- Fill global capacity and one project limit. Add pending work and verify no
  scheduling-only Archon wake. Free relevant capacity and expect one useful
  proposal batch, including changes accumulated while Archon was busy.
- While Archon is active, create an exception and additional updates. Verify any
  required hold applies immediately but no message enters its active turn.
- Arrange several updates to the same condition and a separate decision. Expect
  the latest condition plus the distinct decision, not lost actions or a flood.
- Spawn a native helper and end or delay its parent. Verify the pair
  reservation persists until all required activity is terminal; no Overseer
  overlap. Inspect helper usage separately from managed slot counts.
- Put an assignment in CI wait, use its released capacity, then make a repair
  actionable. Verify it waits for reacquisition without a model polling turn.
- Reduce a limit below active usage, apply ordinary and urgent holds, and
  exercise a host quiet interval. Verify drainage, interruption reconciliation,
  process/Tollgate checks, and no claim that already-completed promotion
  reversed.
- Complete two related beads in one run. Verify pair reuse, fresh owned source
  worktree, correct runtime directory, no stale mandate, and no routine setup
  review. Restart and restore tasks; numbers and pair mapping must remain
  stable.
- Force compaction. Inspect concise Python-produced current instructions, active
  holds, and the exact finish obligation rather than a legacy skill-reading
  loop.

### Failure and specialist checks

Inject failures at recorded boundaries and use the diagnostic history to verify
that recovery preserves identity, authority, and completed work.

- Lose creation and turn-start responses, disconnect the controller, and replay
  events after restart. Verify one task and one accepted batch, or explicit
  unresolved state with retained capacity; never an optimistic duplicate retry.
- Omit finish once with working hooks and once with unavailable hooks. Each
  attempt gets at most one correction total. A second failure creates one
  exception; a Plan-mode authoring stop gets none.
- Interrupt after an accepted finish and separately inject a candidate/source
  identity mismatch. Verify neither case grants advancement. Edit the worktree
  after ready-for-review and verify review still targets the recorded immutable
  candidate; the edit is neither included nor automatically treated as report
  invalidation.
- Request missing review evidence, reject three new sources, and retry an
  infrastructure failure. Confirm only substantive rejections count and Archon
  decides the next approach or model change.
- Perform a clearly covered repair, then an out-of-scope repair. Verify retained
  original mandate plus replacement evidence for the first, renewed review for
  the second, and native certification for both.
- Fail source push, cleanup, and specialist publication separately. Recovery
  must reuse existing candidates, reports, and finding identities. No agent
  starts just to check CI or repeat a network push.
- Make Sage and project Inquisitors overdue. Verify one occurrence per policy,
  normal capacity accounting, whole-project Inquisitor scope, and pending
  findings that still require Archon approval before implementation.
- Request a single Sage interview round with an active subject, an archived
  subject, and an unavailable subject. Verify idle-only delivery, correct prior
  archival restoration, no new questions, and one Sage continuation containing
  answers plus explicit missing evidence after timeout.
- Attempt cutover with unresolved legacy delivery, then resolve it and retry.
  Verify unresolved work blocks transition, repeated setup/import is safe, old
  launch paths remain disabled, and new dispatch has exactly one owner.
