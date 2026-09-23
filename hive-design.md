# Hive: workers drive the system

Hive is a set of agent skills backed by a small, strictly typed Python CLI.
Independent workers decide what to do next. Beads stores tasks and dependencies;
Tollgate owns ordinary code validation and promotion. Hive has no dispatcher,
standing leader, workflow engine, or scheduled stuck-work monitor.

The design replaces Fulcrum's coordination machinery while retaining isolated
worktrees, a useful backlog, enforced CI, specialized review, observability,
mandatory task naming, and live updates. The objective is less infrastructure
between an agent and useful work, with a small core that prevents ownership and
admission mistakes.

Hive is a new codebase at `~/hive`. One host runs workers across registered
projects, sharing a fresh server-mode Beads database under `~/brain`. Bead IDs
use Beads' default short-ID allocation with the `hv-` prefix. The database
is the only task authority. GitHub holds periodic backups, not a competing live
queue.

This document specifies target behavior, not an implemented system. In
particular, the sub-100ms task-operation target is not yet demonstrated. Writing
this document does not install Hive, change existing workers, or migrate data.

## Related information

- [Live iteration](docs/architecture/live-iteration.md) establishes the existing
  local-master, consistent-source, and connection-continuity requirements.
- [Beads contention measurements][beads-measurements]
  establish the measured benefits and limitations of server mode and locking.
- [Blocking-wait experiment](docs/experiments/2026-09-16-desktop-mcp-ci-wait.md)
  demonstrates a five-minute MCP wait without model polling.
- [Tollgate](/Users/dthurn/tollgate/README.md) specifies worktree delivery,
  candidate validation, approval, promotion, and source synchronization.
- [The wt skill](/Users/dthurn/.llms/skills/wt/SKILL.md) supplies practical
  experience with isolated execution and in-scope CI repair. Hive owns the
  workflow described here; it does not inherit every restriction of that skill.
- [Beads](https://github.com/gastownhall/beads) owns issue persistence, native
  dependencies, short IDs, supported command behavior, and database maintenance.
- [Warden's review reference][review] advocates aggressive simplification,
  cohesive modules, explicit types, and removal of unnecessary abstractions.
- [Codex app-server documentation][codex] describes task names, activity,
  interruption, and archiving, including descendant archival behavior.
- [Codex MCP configuration][mcp] describes the client configuration needed for
  long blocking tool calls.
- [Handoff-delay postmortem][handoff] and [dispatch regression][dispatch]
  explain
  why unrelated failures and multi-role handoffs must not govern all progress.

[beads-measurements]: docs/experiments/2026-09-19-beads-contention.md
[review]: https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md
[codex]: https://learn.chatgpt.com/docs/app-server
[mcp]: https://learn.chatgpt.com/docs/extend/mcp
[handoff]: docs/postmortems/2026-09-16-bead-46a5eb5e-handoff-delay.md
[dispatch]: docs/postmortems/2026-09-17-bead-cdf5657c-weaver-dispatch-regression.md

## Authority and the small core

**A bead** is a durable work item in the shared Beads database. **A Codex task**
is a conversation in which an agent works. One executor conversation can finish
several beads in sequence; a bead can survive several execution attempts.

A **native turn** is one Codex execution of an agent responding to input in that
conversation. Its identity distinguishes an ended attempt from a later resumed
attempt in the same task.

A **Tollgate candidate** identifies submitted source for validation and
integration. **Promotion** makes validated, integrated source the project's
accepted release. **Configured source synchronization** updates the local and
remote branches selected by the project's Tollgate configuration. Ordinary code
delivery is complete only after promotion and that synchronization finish.

Hive's rigid core has four responsibilities:

- Maintain valid ownership and lifecycle transitions in Beads.
- Enforce dependency readiness and the shared concurrency ceiling at admission.
- Preserve the identities needed to relate beads, workers, worktrees, and
  Tollgate candidates.
- Expose small typed operations with explicit outcomes and recovery guidance.

Skills decide scope, priority, decomposition, review responses, conflict risk,
and whether current resources make more work sensible. They also enforce the
completion checklist. Hive does not persist a checklist proof, require a
review certificate, or reconstruct Tollgate's validation evidence.

The division of authority is concrete:

| System | Authority |
| --- | --- |
| Beads | Task intent, priority, dependencies, status, and ownership |
| Hive admission | Whether a worker may claim a bead now |
| Executor | In-scope implementation and response to review findings |
| Tollgate | Ordinary validation, integration, promotion, and configured sync |
| Codex | Native task activity, titles, interruption, and archival |
| Telemetry store | Derived observations and cost estimates only |
| Justiciar | Explicit emergency intervention described below |

Shared storage is centralized data, not centralized coordination. Every worker
uses the same admission operation independently. Losing one worker must not
stop unrelated ready work. Losing every executor leaves durable pending work
until a user starts another executor; Hive makes no autonomous recovery promise
for that condition.

### Authorization and project scope

Filing a normal bead authorizes its in-scope implementation and ordinary
Tollgate promotion without another approval. This includes actionable findings
from reviewers, workflow audits, and completion checklists. Unresolved material
product decisions still require clarification.

- Explicit user restrictions override the default execution authority.
- Implementation governed by an unapproved design remains deferred until that
  design is approved; authoring and reviewing the design can proceed.
- A user pause remains paused until the user resumes it.
- Each executor drains only its starting project. Cross-project storage and
  visibility do not grant cross-project execution authority.
- A user can explicitly assign another project. Recruitment inherits the
  recruiter's project scope.

For example, an executor in `search` may see that a `storage` bead blocks its
next task, but it cannot silently switch repositories to implement that bead.
It can explain the dependency and continue other eligible `search` work.

## Skills and agent behavior

Skills contain ordinary instructions with progressive disclosure. Their normal
path stays short; detailed repair, conflict, interruption, and recovery guidance
is loaded when the worker encounters that situation. Roles describe current
activities rather than permanent agent identities.

| Skill | Emoji | Responsibility |
| --- | --- | --- |
| `$executor` | ⚒️ | Scope, file, implement, deliver, and continue work |
| `$warden` | 🛡️ | Cold code review or broader architecture review |
| `$weaver` | 🧵 | Design larger work, obtain approval, and file its beads |
| `$sage` | 📖 | Investigate workflow, tooling, and performance improvements |
| `$justiciar` | 🔥 | Restore a broken swarm without delegation |
| `$vizier` | 🔮 | Answer questions and investigate in a read-only phase |
| `$archivist` | 📁 | Archive inactive Hive-managed Codex tasks |
| `$bead` | 📿 | File the requested work immediately |

### Executor

Executor accepts a description containing one or several tasks, bug reports, or
feature requests, or an existing bead ID. It asks material scope questions and
inspects existing work before deciding whether implementation should start.

Project-file changes, including documentation, use a Tollgate worktree and
candidate delivery. A non-code bead whose output is an external document or
research report still uses admission, a fresh warden review of the artifact and
its relevant invariants, and the completion checklist. It records the delivered
artifact location and acceptance result instead of inventing a code candidate.
It needs a worktree only if producing that artifact changes project files.

The ordinary loop for project-file changes is:

1. Clarify intent, search for duplicates and pending prerequisites, and file
   appropriately sized beads with priorities and dependency links.
2. Inspect current owners, candidate status, concurrency, resource pressure,
   and likely merge conflicts. Choose ready work or leave it in the backlog.
3. Claim through Hive admission. Create a Tollgate worktree before editing
   project files. Read-only scoping and filing do not require a worktree.
4. Implement the claimed scope and run proportionate local checks.
5. Commit the source to review, then run a fresh `$warden` subagent with no
   forked conversation history. Address its findings and record reasons for
   rejecting suggestions. Commit repairs before reviewing the changed diff.
6. Submit and authorize the exact candidate through Tollgate when permitted;
   block for results, fix in-scope failures, and complete delivery.
7. Run the completion checklist, record the outcome, and close the bead.
8. Search for and claim the next eligible bead in the same project.

If capacity is full, executor still files the requested work and exits. Filing
must not fail merely because implementation cannot start. An explicit bead
argument skips creating a replacement bead, but never skips admission checks.

Conflict risk is judgment based on task scope and active work. A likely overlap
can justify leaving a bead queued, choosing a different task, or adding a real
sequencing dependency. Do not turn uncertain file predictions into another
mandatory locking system or invent semantic dependencies solely to explain a
momentary load decision.

An executor normally continues while eligible work exists. It may stop when
remaining work is blocked, paused, outside its project, at capacity, or not
sensible to start under current resource pressure. It states that reason rather
than claiming the backlog is empty.

### Warden

Warden reviews a specific diff or the architecture of a project. Its review is
aggressive about simplifying structure, but findings must explain a concrete
maintenance, correctness, or testing consequence.

For project-file work, a cold reviewer receives the bead, agreed scope,
workspace, exact source commit, project invariant document, and relevant test
commands. For artifact work, it receives the artifact instead of a workspace
and commit, plus its acceptance criteria and relevant project invariants. It
reads independently; the author's conversation and reasoning are not inherited.

Warden examines:

- Single responsibilities, cohesive modules, and opportunities to delete layers
  instead of distributing the same complexity across more files.
- Clear source documentation, including rustdoc where applicable.
- Illegal states, excessive optional fields, unchecked dynamic data, and
  different concepts represented by interchangeable values.
- Recent relevant bugs: whether a better type could prevent the category of
  mistake, and whether a black-box test could catch the observable failure.
- Test quality: a few meaningful black-box tests are preferable to a large
  collection of implementation replicas, change detectors, or assertions about
  arbitrary aesthetic choices.

**Project invariants** are critical properties recorded in a project-owned
Markdown document. Project registration identifies that document. It is read
for each review, and findings identify which invariant is affected. If a
project lacks one, warden reports the gap and files work to establish it; it
does not invent supposed user requirements or silently claim a complete audit.

Cold review is mandatory for ordinary executor work, but warden has no veto.
Executor fixes valid findings, explains rejected ones, and files worthwhile
out-of-scope improvements. Removing brittle tests happens in executor scope,
with valuable behavioral coverage preserved. Broad audits may file beads or
transition to executor through admission.

A source-changing repair after review requires review of the changed diff
before delivery. Review does not loop until warden approves: executor retains
judgment over findings. Mechanical conflict resolutions are included in that
review when they change source behavior or structure.

### Weaver

Weaver handles larger scopes that need a technical design, usually in the
project's `/plan` area. It explores first, asks clarifying questions, writes a
standalone document, and runs a fresh cold-reader subagent with only the
document and reading instructions.

When the design changes project files, authoring is its own planning bead.
Weaver claims it through normal admission, writes in a Tollgate worktree, and
delivers the cold-reviewed document through Tollgate. If admission is refused,
it files the planning bead and exits. Publishing a draft and completing that
planning bead do not approve its implementation; the document's approval state
and any deferred implementation beads continue to make that distinction clear.
An external design artifact instead follows the artifact delivery rules.

It resolves comprehension gaps and obtains user approval of the design. It then
files implementation beads with useful acceptance criteria and dependency
links, each pointing to the approved document, and transitions to executor.
Beads filed while approval is pending remain deferred. Approving the document
releases only its intended work, not a later unrelated expansion.

### Sage and vizier

Sage uses logs, task traces, cost reports, profiling, and actual tool behavior
to
find workflow improvements. Typical subjects include build and CI time,
formatting and linting overhead, documentation discovery, useful diagnostics,
and the clarity of Hive's own skills. It files evidence-backed beads and may
transition to executor through admission.

Vizier begins read-only. It answers codebase and system-state questions using
available evidence. Once that inquiry is complete, it may explain and perform a
transition to executor if an actionable problem was identified. All mutations
then follow executor rules. It can also finish as a research task.

Warden and sage can run as optional scheduled audits. Each run is bounded, uses
a fresh task, and has a stated project and audit purpose. They are not disguised
backlog monitors. Their ability to transition to executor is the same as that
of an explicitly invoked specialist.

### Bead

In a new session, `$bead` means file the issue or issues and exit. In a reply to
another role, it means file this item now, then return to the enclosing role.
It does not change that role's name or start execution itself.

Filing includes enough scope, priority, project identity, and dependencies for a
future executor to act. A large unclear request can be recorded as planning
work; it must not masquerade as an implementation-ready task. Before filing a
follow-up, search for an existing bead that already covers the issue.

### Justiciar

Justiciar is invoked when the swarm is broken: resource exhaustion, Beads
corruption, a serious coordination defect, or a failure that ordinary owners
cannot safely resolve. It does not delegate, create a repair swarm, or wait for
an execution slot before stopping the damage.

It may:

- Close admission and interrupt affected workers or processes.
- Cancel or inspect external work and settle unsafe ownership.
- Repair Beads, runtime configuration, or the tooling required for recovery.
- Bypass broken Tollgate when Tollgate itself prevents necessary repair.

Ordinary code delivery remains subject to Tollgate. Its emergency exception is
limited to restoring the broken system, not shipping unrelated feature work.
Justiciar records what failed, what it stopped or changed, which checks were
bypassed, and what validation or reconciliation remains. If Beads is
unavailable,
local recovery notes are sufficient until the database is restored.

Before reopening admission, it checks the repaired shared state, reconciles
interrupted owners and candidates, and makes unfinished work visible. Any
emergency code change receives normal validation once that path works again.
An explicit user pause is not erased by global recovery. There is no requirement
to obtain a second permission merely because the requested recovery needs the
emergency authority described here.

## Beads persistence and lifecycle

All projects use one canonical Hive database under `~/brain`, connected through
a local Dolt server. Resolve that database explicitly on every invocation;
never discover a different store from the current code worktree. Do not fall
back to embedded mode or a second task store when the server is unavailable.

Use native Beads fields for title, description, acceptance criteria, priority,
status, assignee, and dependencies. Store project identity and the small amount
of Hive-specific lifecycle data in bead metadata. Do not maintain a shadow
issue table, separate capacity counter, custom ID allocator, event-sourced task
engine, or general-purpose operation receipt system.

**Infrastructure records** are native Beads records for Hive configuration and
conversation enrollment. They are explicitly excluded from the work backlog,
dependency selection, and capacity count. Their record type distinguishes them
from implementation beads; titles and emojis do not determine that distinction.

The additional metadata has a concrete purpose:

- The registered project identifies the repository and execution boundary.
- Pause reason distinguishes user pause, pending design approval, missing
  input, and a worker's temporary checkpoint.
- Workspace and candidate identifiers support delivery and recovery.
- A terminal outcome distinguishes successful completion from cancellation.
- A concise recovery note explains an uncertain external outcome when needed.

Assignee holds the native Codex task ID for a claimed bead. Metadata also holds
the owning native turn ID, so a resumed conversation can be distinguished from
an ended attempt during recovery. Workspace or candidate data is added only
after that resource exists; phases expose typed
variants that require those values when applicable. Keep native status and
Hive metadata consistent in one supported update when changing both.

### State model

Hive decodes native records into explicit states. It does not introduce another
persisted status field that competes with Beads status.

| State | Beads representation and meaning |
| --- | --- |
| Queued | Open, no assignee; dependency readiness is derived |
| Owned | In progress, assignee present; consumes one slot |
| Deferred | Deferred, reason present; not eligible for selection |
| Done | Closed, successful outcome recorded; satisfies dependencies |
| Cancelled | Closed with cancelled outcome; does not satisfy dependencies |

An owned bead progresses through preparing, implementing, reviewing, and
waiting for delivery. Store the current phase in Hive metadata together with
the resource references required by that phase, in one native update. This is
one current-state value, not a persisted workflow graph. Review completion is
not a core proof flag.

Preparing retains the intended branch until the workspace exists. Implementing
and reviewing require the workspace; waiting for delivery also requires the
candidate and source commit. A lost creation or submission response leaves the
last valid phase intact. Recovery inspects Tollgate using the branch or source
commit before advancing it, as described in
[Reconciling retained work](#reconciling-retained-work).

Artifact work uses a drafting or reviewing-artifact phase instead; neither
requires a workspace, and reviewing-artifact requires an artifact location.
Its terminal outcome retains that location and the acceptance result.

A deferred bead may retain its assignee while outstanding work settles. It
continues consuming a slot until the assignee is cleared. Thus capacity is the
count of owned records, including deferred records with unsettled owners, not
just a count of native `in_progress` status.

Examples make the distinction visible:

```text
open, unassigned                  -> queued
in_progress, owner=task-A         -> owned; one slot
deferred, owner=task-A, user-stop -> paused; resources still settling
deferred, unassigned, user-stop  -> paused; no slot
closed, outcome=delivered         -> done
```

Completion and cancellation clear the assignee with the terminal state and
outcome in the same native update, after resource settlement. Deferred work
resumes to queued, not directly to its former owned state; it must be admitted
again. Owner-only mutations check the current owner under the admission lock
and reject a stale caller. Recovery changes ownership through its explicit
operation rather than an unrestricted metadata edit.

Resumption preserves the checkpoint's workspace, source, and candidate so that
admission can continue retained work rather than repeat it. It requires settled
ownership and a resolved deferral reason:

- User pause requires an explicit user resumption.
- Pending design approval requires approval of the intended design scope.
- Missing input requires the needed answer; an executor can then resume it.
- A temporary checkpoint can be resumed by a peer after its recorded blocker
  is resolved and any interrupted dependency changes are repaired.
- Recovery uncertainty requires the stopped-writer and external-outcome checks
  in [Peer recovery](#peer-recovery-and-live-state-changes).

Changing the pause reason cannot remove an outstanding user pause or approval
requirement. For an interrupted prerequisite-discovery sequence, inspect the
checkpoint, find or file its prerequisite, and establish the native dependency
before reopening the parent. If that intent is unclear, keep it deferred.

Only successful completion satisfies a prerequisite. Beads may treat any
closed prerequisite as resolved; Hive additionally checks the recorded outcome
before admission. A cancelled prerequisite needs an explicit dependency change,
replacement, or cancellation of its dependent.

Task operations validate their relevant records at the boundary. Malformed
ownership cannot disappear from the capacity count. Fail admission visibly if
unknown active ownership makes the count unreliable; ordinary reads can still
report the offending records. Do not turn unrelated bad historical data into a
reason to stop every current task.

### Dependencies and interrupted changes

Use native Beads dependency edges and cycle detection. Project boundaries do
not prevent cross-project dependencies, but execution authority remains
project-scoped. Descriptions and document links are not substitutes for edges.

Changes to readiness participate in the admission lock. This includes changing
an eligible bead's prerequisites, pausing it, and resuming it. A direct claim
must check the same dependencies as selecting the next ready bead; the local
benchmark showed that native direct claiming alone did not enforce them.

If a worker discovers an unstarted prerequisite:

1. Checkpoint and defer its current bead before changing the dependency graph.
2. Settle outstanding writers and release that bead's ownership.
3. File the prerequisite and attach its dependency edge.
4. Reopen the original bead only after the edge exists, then claim useful work.

A crash leaves deferred work or an extra visible prerequisite, not runnable
work missing a known prerequisite. A parent must not hold a slot indefinitely
while waiting for a child that cannot obtain capacity. Waiting for its own
already-started review or CI is different and retains the slot.

Use `bd batch` when its supported operations fit a genuine transaction. Do not
assume its grammar supports arbitrary reads, metadata changes, or conditional
capacity admission. A local lock serializes callers; it does not roll back a
partially completed series of independent commands.

### Native interfaces and exceptional direct access

The normal adapter uses supported `bd` JSON interfaces. Keep calls few, request
bounded results, avoid rereading successful writes solely to manufacture proof,
and disable per-mutation remote synchronization. Native Beads short IDs are
used as returned, including longer IDs when its allocation algorithm needs them.

Direct Dolt access is an exception requiring strong justification:

- Retain a reproducible profile showing the supported interface cannot meet a
  specific performance or atomicity requirement after straightforward tuning.
- Describe the exact query or transaction and the Beads behavior it must retain.
- Confine it to one typed adapter with real-server behavioral tests covering
  that behavior, including dependencies and any derived Beads fields.
- Reject unsupported schema changes visibly. Do not guess or maintain a chain
  of compatibility implementations.

This exception does not create another task authority. It also does not justify
writing arbitrary issue tables from skills. Prefer a supported Beads improvement
when that removes the need for internal schema coupling.

## Atomic admission and independent recruitment

**Admission** is the short operation that checks eligibility and claims a bead.
The global default is eight in-flight beads. Projects can have a lower explicit
limit; absent an override, the global ceiling is the only capacity ceiling.

Keep registered project identities, repository and invariant-document locations,
native project bindings, and capacity settings in a Beads infrastructure
configuration record. Admission reads its current limits under the lock; limit
changes use the same lock and never cancel existing work merely because a lower
ceiling was configured. Existing work drains before more is admitted. Local
bootstrap settings identify the server and database without querying that
database first. The maintenance stop is a local durable control marker so it
remains usable when Beads is broken.

One permanent host-local advisory lock protects compound admission decisions.
All ownership changes and changes that affect readiness or capacity cooperate
with it. Keep the lock outside synchronized data, resolve a canonical local
identity, and never unlink a live lock file. Process death releases the kernel
lock.

Hive skills use Hive operations for these changes; direct `bd` ownership or
dependency writes can bypass the shared checks and are not a supported worker
path. Justiciar may use lower-level repair tools while admission is stopped.
This is coordination among cooperating local agents, not a security boundary
against arbitrary database access.

Reads and independent title, description, or priority edits use native atomic
operations without that lock unless they also change admission-relevant state.
Priority observed at selection is advisory ordering; it does not authorize a
claim. Never hold the admission lock during model work, resource sampling,
Codex requests, worktree creation, CI, Git operations, or network backup.

The claim operation has the following semantics:

```text
under admission lock:
    reject paused admission or an existing claim by this worker
    read current ownership and enforce global/project capacity
    validate requested project, bead state, and prerequisites
    atomically set this bead's owner and in-progress status
return the claimed bead, or a specific reason it cannot start
```

Selecting the next bead uses priority P0 through P4, then oldest creation time,
then ID as a deterministic tie-breaker. P2 is the filing default. Executor may
propose another eligible bead based on resource or conflict judgment; the exact
claim still performs all checks. Bounded ready queries must continue far enough
to avoid reporting no work merely because the first page was unsuitable.

Use the native atomic claim where suitable. If an update also needs metadata,
verify the selected supported operation commits all required changes together;
do not split ownership and capacity reservation into separate records. A claim
response lost in transit is resolved by reading that bead and owner, not by
blindly claiming a second bead.

Lock acquisition has a two-second failure deadline. `Busy` is a visible
retryable outcome, not an internal retry loop. That deadline is not the
performance target: successful operations still have the sub-100ms budget under
normal load.

### What consumes capacity

One bead retains one slot from claim until delivery or settled deferral:

- Preparing its workspace, implementation, and merge repair consume the slot.
- A cold reviewer shares the bead's slot while its executor waits.
- Queued and running CI, and promotion or synchronization waits, retain it.
- Requested cancellation or an interrupted conversation does not by itself
  prove resource use has stopped.

Read-only scoping, filing, and bounded specialist audits do not claim execution
slots. Their agents still consume resources, so recruitment and optional audit
schedules must consider actual load. Hive's ceiling limits in-flight work
items, not every process or model request. Tollgate separately manages builds.

Resource reporting includes active beads and phases, recent CPU load, memory
pressure, and available Tollgate queue information with observation times.
Missing metrics are reported as unknown. Avoid a mandatory expensive full-system
scan before every task operation; workers inspect a lightweight snapshot and
make a judgment outside the lock.

### Recruiting peers

Executor may create independent Codex tasks when work is parallelizable and
there appears to be capacity. Each new task receives the project, optional
suggested bead, executor skill, and relevant scope. The child claims its own
work through normal admission before making any project changes.

No slot is reserved in a second record before native task creation. Racing
recruiters may create more conversations than available slots, but only eight
beads can be admitted. A child denied admission reports the reason and exits.
A child given an exact bead does not silently substitute different work if that
initial claim fails.

Recruiters are peers, not supervisors. Stopping the recruiter does not stop
recruited executors. They have independent native task identities and lifetimes,
and each drains its starting project's backlog. Use the saved project for the
native task; Tollgate creates the implementation worktree, avoiding nested
Codex and Tollgate worktrees.

If native creation returns an uncertain result, inspect the native task list
for the attempted creation before retrying. If it cannot be identified, report
the uncertainty and stop recruitment rather than creating duplicates in a loop.
No central process reconciles these attempts.

## Delivery, completion, and stopping

Tollgate owns ordinary code delivery. Hive retains its candidate identifier and
uses its result; it does not reinterpret logs to invent a passing build or copy
Tollgate's certificates into its own proof format.

The executor creates each new implementation workspace through Tollgate, based
on its promoted release: the latest code Tollgate has validated and integrated.
It stops workspace-rooted background processes before
promotion because Tollgate may remove the clean source workspace afterward.

After local work and cold review:

```text
confirm HEAD is the source commit covered by cold review
tg candidate HEAD
tg approve <candidate-id> --wait
record the delivered outcome and close the bead
```

These commands illustrate Tollgate's existing interface. Where a user requires
approval, validation can run with `candidate --wait`, but promotion waits for
that approval. The exact source and candidate must still be the ones approved.

A validation pass alone is not completion. Require promotion and the configured
source synchronization to finish. Handle a merge conflict against Tollgate's
current promoted release, preserve the task scope, review material changes,
and submit the replacement candidate. Do not push worktree branches directly.

An in-scope CI failure stays with the same executor and bead. Out-of-scope
pre-existing failures receive follow-up beads. If such a failure blocks
delivery,
checkpoint and defer with the repair dependency instead of bypassing CI or
claiming success. Temporary infrastructure failure is reported with a bounded
next action; repeated blind submissions are not recovery.

### Blocking waits

Expose a foreground `hive delivery wait` command and a thin per-session MCP
`wait_for_delivery` operation. The adapter starts a fresh source-pinned command
for each request and has no scheduling authority or shared job queue.

Use Tollgate's candidate-specific blocking wait. Changed provider status records
may be consumed internally; the model receives a terminal result rather than
repeated instructions to poll. Hold no admission lock while waiting.

- Results distinguish delivery, validation failure, conflict, cancellation,
  timeout, provider error, and unresolved outcome.
- Use a default one-hour wait deadline and an MCP timeout of at least 3,900
  seconds. The surrounding tool invocation must support that pending duration.
- On timeout or lost connection, retain the candidate and ownership. Inspect its
  status before deciding whether to attach a new wait, repair, or defer.
- Canceling a client wait does not imply that Tollgate canceled the candidate.
- Once a wait returns, later mutations enter fresh CLI code.

The existing five-minute synthetic experiment supports feasibility, not the
whole delivery contract. Acceptance includes a real 30-minute CI wait with zero
intermediate model polling, working cancellation, and no archival of the active
task. A tool environment that repeatedly yields back to the model must be fixed
or use the supported blocking MCP path; agent polling is not the fallback.

### Completion checklist

The executor performs this checklist in the skill, without a persisted approval
matrix:

- Confirm the intended behavior and proportionate validation.
- Address cold review findings or record concrete reasons for disagreement.
- Confirm Tollgate promotion and configured synchronization, or the required
  artifact outcome for non-code work.
- File or link beads for relevant pre-existing defects, tool failures or
  slowness, CI failures or slowness, and architecture debt.
- Preserve useful documentation and project-invariant changes within scope.
- Record a concise outcome with source/candidate or artifact references, settle
  owned processes, and close the bead.
- Attempt the next project-scoped claim before ending normal execution.

No category requires inventing an issue when none was found. Search before
filing; repeated observations should strengthen an existing bead rather than
create a backlog of duplicates.

### Stop hook and explicit interruption

A stop hook gives one reminder when an executor is about to end normally while
eligible project work appears available. It reads bounded state, directs the
executor to the normal next-work operation, and does not launch another task.

Use the hook's continuation guard so the same stopping attempt cannot produce a
reminder loop. A new completed bead can lead to a new legitimate next-work
check. Capacity refusals, all-blocked work, explicit user stops, and emergency
suspension do not trigger pressure to keep working.

An explicit user stop defers the bead with a user-pause reason. No peer or
specialist may automatically resume it. Existing external work may already be
running; state what is still settling and retain capacity until it settles.

Stopping cannot retract an already-authorized Tollgate promotion. Ordinary
one-step approval grants Tollgate authority before its blocking wait, so a stop
during that wait may arrive too late to prevent promotion. Inspect the
candidate,
attempt supported cancellation where applicable, and report the actual result.
Do not promise that pausing a conversation rolls back code delivery.

If promotion finishes after a user stop, record its observed result and settle
any finished writers, but leave the bead deferred with its user-pause reason.
Observation and settlement do not authorize new implementation or a next claim.
On explicit resumption, admit the retained work, reconcile synchronization and
the completion checklist, and close it without repeating delivered changes.

A native interruption signal should record the pause promptly. If Hive cannot
distinguish a user stop from a crash, recovery preserves the pause/uncertainty
until it is resolved rather than treating silence as permission to restart.

## Peer recovery and live state changes

At work boundaries, executors can inspect abandoned ownership, including when
apparent capacity is full. Recovery does not require a leader, elapsed lease,
or periodic global reconciliation pass.

### Establishing that an owner has stopped

A peer recovers only after establishing that:

- The recorded native turn has ended, no replacement turn is executing or
  queued, and there is no explicit user pause or unexplained interruption.
- Outstanding write-capable commands and child review activity have stopped.
- The workspace, retained candidate, and any external action have been
  inspected.

A stale timestamp, missing transcript, lost connection, or missing heartbeat
is insufficient. If native state or process activity cannot be established,
leave ownership visible and escalate to justiciar. Recorded ownership cannot
fence an arbitrary process that is still writing files.

An ended turn is not a permanently dead conversation. Every executor turn must
enter Hive before editing: while holding the admission lock, it checks current
ownership and records its native turn ID. Recovery inspects the previously
recorded turn, then checks under that lock that both owner and turn are
unchanged
before releasing the claim. A resumed turn that enters first invalidates that
recovery attempt; one that enters afterward sees it no longer owns the bead and
must not edit. This uses native identities, not a lease or a generated token.

### Reconciling retained work

Recover the retained work before deciding what should run again. An ended
conversation alone does not determine whether its external operation succeeded.

Recovery records the checkpoint, settles the old owner under the admission
lock, and uses ordinary admission to claim continuation. A recoverer may adopt a
preserved workspace only after confirming quiescence and recording the new
owner. Existing candidates are inspected, not resubmitted blindly. For an
already promoted candidate, finish any required synchronization before closing
the bead; do not implement the source again. Pending or failed synchronization
retains a visible delivery wait or blocker.

External operations and Beads are not one transaction. For worktree creation,
record the intended bead-derived branch before creation and use Tollgate's
inventory to resolve a lost response. For candidate submission, use the
workspace and native source commit to find the retained candidate. If inspection
cannot determine what happened, preserve the uncertain state and request
recovery. Do not add a universal retry journal or custom fingerprints.

### Hot reloading

Each Hive CLI invocation resolves committed local `master` in `~/hive` before
importing application code. It runs a consistent immutable snapshot of that
commit, reusing an already-prepared snapshot and dependency environment when
possible. Git's commit identity is sufficient; add no content-hash scheme.

- New invocations use the current local commit without remote discovery.
- Existing invocations retain their selected code and assets, including delayed
  imports and long waits.
- Skills are direct links to source; subsequent reads see edits immediately.
- A broken source preparation fails visibly rather than silently using old code.
- Ordinary code and instruction changes need no installation, manual activation,
  or service restart.

The background collector keeps only observation transport and bounded job
launching resident. Parsing and reporting batches use fresh CLI code. A
per-session MCP adapter transports calls without retaining application policy.
Native task operations normally use Codex's existing tools. Where a persistent
connection is needed for observations, its transport owner preserves pending
requests and subscriptions across ordinary source changes and never schedules
beads.

Incompatible state changes or dependency changes require explicit maintenance,
not backwards-compatible readers or numbered format chains. A separate local
maintenance guard protects mutation calls: acquire its shared side before
selecting source, check the maintenance stop, and retain it through the short
Beads mutation. It does not serialize ordinary callers with each other.

Maintenance first records a durable stop and then takes the exclusive guard.
Existing mutation calls finish; new calls refuse while stopped. Back up the
database, convert and validate the affected state, and clear the stop before
releasing the guard. Failure retains the stop for repair. Acquire this guard
before the admission lock; neither spans external provider calls.

Pure CI waits can remain pending: they do not mutate Hive state, and their next
state change enters current code through the guard. Ordinary source edits need
no exclusive maintenance action and never interrupt active turns. A broken
maintenance owner leaves a visible stop that justiciar can investigate.

## Task names and archiving

Task names are Hive's primary live interface. Invoking a Hive role enrolls the
current conversation, even if the user created it. Retain the native task ID and
project in the Beads-backed Hive registry so enrollment and archival scope do
not depend on recognizing an emoji in a title.

Registry records use the infrastructure record type described above. They store
enrollment and native identity, not a second copy of bead ownership. A
conversation that later changes roles remains the same registered task.

The name format is role emoji, current bead when one exists, and a clear subject
with an optional short state:

```text
⚒️ [hv-fg3] Search indexing · implementing
🛡️ [hv-fg3] Review search architecture
⚒️ [hv-fg3] Search indexing · waiting for CI
🧵 Search redesign · clarifying scope
📿 [hv-m8k] Record stale cache failure
```

Rename on role changes, bead changes, review, meaningful waits, pause, recovery,
and completion. While the cold warden runs, the parent displays the warden role;
it restores the executor role afterward. A one-off `$bead` reply preserves its
parent's role and title. Do not rename after every command or keep a completed
bead's ID when moving to unrelated work.

Use direct native naming tools. Attempt one bounded retry on failure, expose
the desired name and drift in status, and retry at the next meaningful
transition. UI failure does not strand delivery. Keeping names current remains
a required workflow action even though failure is nonblocking.

### Archivist

Archivist is a bounded scheduled agent, initially invoked every five minutes,
and can also be run explicitly. It archives registered Hive tasks that have had
no user input or agent output for more than 15 minutes and have no active turn.
Use native activity and turn state, not the bead's last edit or title timestamp.
Tool activity counts as activity; a pending long tool call also has an active
turn and cannot qualify.

- Recheck activity and active-turn state immediately before archiving.
- Include paused or input-waiting tasks when their turn has ended and they meet
  the inactivity rule. Archiving is UI cleanup, not cancellation or completion.
- Do not archive unregistered tasks, the running archivist itself, or a task
  whose activity is unknown.
- Respect an explicit manual unarchive by exempting that conversation until
  the user opts it back into automatic archival.
- Preserve bead ownership, dependencies, and outcomes regardless of archive
  status. Archive failure must not change work state.

Persist archive eligibility and the last Hive-initiated archive or unarchive
result in the conversation's registry record. An observed archived-to-visible
change without a matching Hive action is treated as a manual unarchive and
sets the exemption. Record Hive's own race-repair unarchive before making that
request and reconcile its result on the next run. Ambiguous origin
conservatively
sets the exemption; it must not cause repeated re-archiving. If the collector
missed a visibility change entirely, no manual action can be inferred: record
that observation gap rather than claiming complete manual-unarchive detection.

Codex's documented archive operation may also archive descendant tasks. Before
archiving a parent, verify affected descendants are eligible; skip it if a child
is active or unknown. Independent recruited executors are separate root tasks,
so archiving their recruiter does not hide them.

A read followed by a native archive request is not atomic with new user input.
Use a native conditional operation if available. Otherwise reread immediately
afterward and unarchive tasks that gained activity or became active, including
affected descendants. State this as eventual UI repair, not atomic exclusion;
never stop a running turn to make it archivable.

## Observability without execution overhead

A background collector incrementally reads registered native activity and
transcripts, preserving offsets and bounded chunks in a separate local SQLite
telemetry store. It never claims beads, restarts workers, changes priorities, or
releases ownership. Collector failure delays reports but cannot stop execution.

Hook work is limited to small identity/activity records. Hooks must not reparse
whole conversations or inject generic lifecycle instructions before every tool
call. The collector follows archive moves by native task identity, handles
incomplete trailing records, and avoids counting data again after a restart.

Keep observations sufficient to explain:

- Which bead, role, native task, model response, and candidate were involved.
- Queue time, model activity, tool execution, CI waiting, and integration time.
- Command startup, source selection, `bd` time, lock waiting, and collector lag.
- Missing intervals and unknown outcomes, without silently treating them as
  zero.

Do not add overlapping spans as if they were sequential wall time. Keep
transcript parsing behind a typed boundary because native formats can change.
Malformed records produce a visible coverage gap, not an execution failure.

The initial reports are CLI outputs, not a new dashboard:

```text
hive status --project search
hive trace hv-fg3
hive cost hv-fg3
hive cost --project search --group-by role
```

### API-equivalent cost

Report an estimate of API-equivalent spend, not subscription billing. Preserve
model, service tier when known, input/output and cached-token counts, the price
schedule used, and pricing coverage. Unknown model prices remain unknown.

Attribute a model response to the bead and role active when that response
started. Do not invent token splits when a response spans a role transition.
Unassigned planning and setup remain a visible separate category. Include cold
review usage in the bead total once; independent executors charge their own
beads. Native response identity prevents duplicate ingestion, not a custom hash.

Cost reports include freshness, missing usage, unpriced usage, and partial
coverage. Compare strategies only with compatible coverage. A displayed zero
requires observed zero usage, not a missing transcript.

Default retention is 30 days for detailed telemetry and indefinite compact
per-bead aggregates until explicit pruning. Bound collector resource use and
make lag visible rather than permitting a growing parser backlog to exhaust the
machine.

## Backup and cutover

Local Beads transactions provide task persistence. Remote backup is independent
and asynchronous, initially every 15 minutes when changes exist. It does not
run in a claim's critical section or require an executor to remain alive.
A host timer runs this deterministic backup command. Codex's scheduler invokes
archivist and any explicitly configured specialist audits. Neither timer is
used to inspect the backlog and restart executors.

For GitHub, export a consistent issue snapshot using supported Beads facilities,
including Hive infrastructure records, dependencies, comments, and metadata.
Commit and push the export to a dedicated configured backup Git repository.
Treat it as an issue-level recovery snapshot, not a full Dolt database backup or
an editable task queue. Use native database backup separately before maintenance
that needs complete database restoration.

The backup job:

- Serializes with another backup job, not every task mutation.
- Captures a supported consistent database snapshot while normal work continues.
- Records the snapshot time and last successful remote publication.
- Leaves a local snapshot/commit when networking fails and retries at the next
  scheduled interval or explicit request.
- Never force-pushes, imports remote edits into live ownership, or stages
  unrelated user files.

If the supported exporter cannot guarantee a coherent snapshot, use a supported
transactional read or native snapshot export. Do not hold the admission lock
through a potentially large export to disguise that gap. Concurrent-write
snapshot consistency is an acceptance test.

Restoring an issue export reconstructs task intent and relationships, not
running processes. Close admission and reconcile every retained owner and
candidate before resuming work. Missing native history, old timestamps, or an
archived conversation do not prove an executor has stopped.

### Moving from Fulcrum

The existing `~/brain` contains a Fulcrum server-mode Beads database. Its Git
context currently resolves to the home-directory dotfiles repository. Neither
is an empty Hive installation target.

Create a distinct Hive database under `~/brain`; preserve Fulcrum's database,
configuration, and running workers. Configure explicit database routing and a
dedicated backup destination instead of inheriting the parent Git remote.
Initialize the new database with `hv-` IDs using native Beads allocation.

Deliberately refile selected unfinished intent and recreate required dependency
edges using the newly returned IDs. Retain links to original records for
context. Do not import active ownership, assignment tokens, workflow receipts,
or historical agents into Hive. Avoid executing the same intent in both systems:
settle or explicitly suspend its Fulcrum work before making its Hive replacement
eligible.

Install Hive's role skills from `~/hive` through the user's normal skill setup.
During parallel setup, Hive test sessions read their explicit Hive skill paths;
the overlapping unqualified names continue resolving to Fulcrum. Rebind those
names only after retained Fulcrum assignments are completed or explicitly
retired. This prevents an old worker's later skill read from changing workflow
underneath it. Historical Fulcrum recovery uses explicit Fulcrum skill paths,
not the newly rebound names. There is no compatibility mode or automatic
translation of Fulcrum protocols.

## Typed interfaces and progressive disclosure

Expose the smallest command surface that workers need. The following is the
proposed Hive interface, not a claim that these commands already exist:

```text
hive task add --project search --title "Repair stale index"
hive task claim hv-fg3 --owner <codex-task-id>
hive task next --project search --owner <codex-task-id>
hive task defer hv-fg3 --reason user-pause
hive task resume hv-fg3
hive task complete hv-fg3 --candidate <tollgate-candidate-id>
hive delivery wait <tollgate-candidate-id>
hive status --project search
```

Provide task show, priority update, dependency add/remove, outcome recording,
owner settlement, and recovery inspection through the same typed adapter. A
read-only ready list is advisory; only claim grants ownership. Completion
validates owner and lifecycle structure and records the supplied provider
outcome; the executor checks delivery, not a second Hive certification engine.

Every result has a discriminated outcome and relevant typed values. Examples
include `Claimed`, `CapacityFull`, `DependencyBlocked`, `Paused`, `Busy`,
`NoReadyWork`, `ProviderUnavailable`, and `RecoveryRequired`. Errors include
concise evidence, whether the outcome is known, and the next appropriate action.
Do not infer structured status by parsing prose.

```text
DependencyBlocked: hv-fg3 waits for hv-k2m in project storage.
No claim was made. Continue eligible search work or inspect the prerequisite.
```

The CLI emits concise text for interactive use and JSON for tools. MCP mirrors
only the operations that benefit from native tool invocation, especially long
waits. Both adapters call the same implementation and return equivalent
outcomes.
No business policy lives permanently inside the MCP transport.

### Type safety

Python's internal domain model contains no `Any`. JSON, subprocess output,
telemetry, and external APIs enter as `object`, are validated at their adapters,
and become immutable typed values or explicit errors.

Use distinct types for bead IDs, native task IDs, candidate IDs, project IDs,
and paths whose confusion would cause a real error. Do not mechanically wrap
every string. Native source commit IDs are retained for Tollgate integration,
not turned into Hive fingerprints.

```python
@dataclass(frozen=True)
class WaitingForDelivery:
    bead: BeadId
    owner: CodexTaskId
    workspace: WorktreePath
    candidate: TollgateCandidateId
```

Use unions for meaningful alternatives: a waiting-for-delivery state requires a
candidate; an unclaimed bead does not have a collection of optional ownership
fields. Pure transition functions accept an old state and an event and return
an allowed new state or a typed refusal. Persistence and external effects remain
explicit adapter operations.

Use strict static checking, exhaustive handling with `assert_never`, and narrow
checks against unvalidated casts, explicit `Any`, and broad suppressions.
Negative type fixtures demonstrate that conceptually different IDs cannot be
interchanged. Types protect structure; real multi-process tests protect temporal
behavior and locking. Neither replaces the other.

## Validation and performance

Validate the assembled product early with two dependent beads in a disposable
registered project. Start a real executor, observe its title and isolated
workspace, perform cold review, block on real Tollgate delivery, and verify that
the same executor claims the newly unblocked bead. Inspect the resulting trace
and cost coverage. Component tests alone cannot establish this workflow.

Use a small set of meaningful black-box scenarios with real temporary Beads
server databases and independent processes. Assert externally observable
behavior, not exact internal call ordering or arbitrary formatting.

Required scenarios cover:

- Racing direct and next claims never duplicate ownership or exceed eight
  slots; blocked,deferred, and cross-project beads cannot bypass admission.
- Dependency edits race correctly with admission, cycles are rejected, and
  cancelled prerequisites do not silently satisfy work.
- Killing a lock holder releases its lock; an interrupted multi-step change
  leaves a visible safe state rather than pretending to roll back.
- A lost claim response does not cause another claim by the same executor.
- All occupied beads can obtain cold review without needing another slot.
- All workers can discover prerequisites without retaining every parent slot.
- Explicit stops stay paused, uncertain interruptions are not resumed, and
  peer recovery requires stopped writers and reconciled external effects.
- In-scope CI repair, pre-existing blockers, conflicts, failed synchronization,
  and lost provider responses preserve honest delivery state.
- Title failure, collector outage, and remote backup failure do not halt normal
  execution; status makes those failures visible.
- Archiving respects active turns and descendants and repairs observed races.
- Ordinary source updates affect the next invocation without changing code or
  assets underneath an existing wait or dropping native connections.
- Concurrent backup is consistent; restore never treats historical ownership
  as permission to resume.

### Latency acceptance

Measure complete local task commands from process start to parsed result,
including source selection, imports, lock waiting, Beads work, boundary
validation, and ordinary observability overhead.

The target is **p95 below 100ms** for task reads, ready queries, creation,
ordinary updates, and successful claims with eight concurrent clients and
1,000 unfinished beads. Measure each operation separately. Include realistic
dependencies and a substantial completed history; do not average cheap reads
with expensive claims to hide a miss.

- Run observability and scheduled backup during representative measurements.
- Report cold source preparation separately, targeting p95 below one second
  with unchanged dependencies. Normal calls cannot hide repeated preparation.
- Report one-, eight-, and sixteen-client contention and saturation separately,
  including denied claims and lock timeout rates.
- Characterize 100 and 10,000 unfinished beads and increasing completed history
  to expose scaling costs; these do not silently change the 1,000-bead target.
- Use sufficient repeated samples for tail estimates, record host load and
  sample counts, and report failures alongside latency.
- Exclude remote publication, Codex title RPC latency, and actual CI duration
  from local task-command latency, but report their workflow cost separately.
  A local command must not secretly wait for those effects before returning.

The retained September 19 measurements used Beads 1.2.2 and Dolt 2.2.0. At 1,000
issues, single-client server reads were roughly 73–91ms p95 and independent
updates 92ms. Four-client updates reached 220ms; sixteen-client updates reached
986ms. Locked ready-claim bursts reached roughly 471ms and 1,929ms respectively.
The exact Hive workload at eight clients was not measured.

Those results support server-only operation, but do not establish Hive's target.
Unwrapped server ready-claim operations also showed serialization failures.
Locking removed those failures in the probe at a latency cost. Start with a
minimal adapter and profile actual costs; do not assume another wrapper makes
these numbers disappear. Missing the budget remains a failed target requiring
optimization or an explicit future requirement change, not redefinition of the
measurement.

In addition, compare representative tasks against direct agents using the same
model, review policy, and Tollgate checks. Report added wall time and
API-equivalent cost, including scoping, naming, filing, and follow-up overhead.
The comparison must expose orchestration that becomes the workload even if
individual CLI commands are fast.

## Manual QA

Use disposable projects and an isolated Hive database for destructive and
failure cases. Provide seeded dependent beads, configurable capacity, a
controllable CI fixture, and readable status/trace output to make each case
repeatable without touching production work.

1. **Assembled flow:** create two dependent beads and start an executor. Observe
   the executor, warden, and CI-wait names; verify the Tollgate worktree, cold
   review, promotion, synchronization, completion, and next claim.
2. **Admission:** occupy eight slots, including review and CI waits. File more
   work and start competing executors. Verify filing succeeds, excess claims
   fail clearly, and the cold reviewers do not need extra slots.
3. **Project boundaries:** put ready work in two projects and a dependency
   across them. Verify each executor stays in its project and reports the
   cross-project blocker without silently switching repositories.
4. **Recruitment:** recruit independent executors, then stop the recruiter.
   Verify peers continue, have separate worktrees, and obey the shared ceiling.
5. **Prerequisites:** have every active bead discover unstarted prerequisite
   work. Verify parents checkpoint and release settled ownership, prerequisites
   become runnable, and an interrupted dependency edit leaves safe deferred
   work.
6. **Review and specialists:** exercise a valid finding, a reasoned rejection,
   a missing invariant document, and a brittle-test finding. Verify a standalone
   specialist enters admission before making implementation changes. Check
   that design authoring can execute while its unapproved implementation beads
   remain deferred.
7. **Bead filing:** invoke `$bead` in a new session and inside an executor
   reply.
   Verify immediate durable filing, useful dependencies, no implicit execution
   by the filing skill, and preservation of the enclosing role's title.
8. **Long wait:** hold real CI for at least 30 minutes. Verify one pending tool
   call with no intermediate model polling, retained capacity, a useful wait
   title, and no archival while the turn is active.
9. **Interruption:** stop during implementation and during an already-authorized
   delivery. Verify the user pause persists and the reported candidate outcome
   reflects what actually happened, including any promotion already underway.
   Resume delivered work and verify it closes without repeated implementation.
10. **Recovery:** terminate an executor unexpectedly, including a case with a
    surviving write-capable process. Verify peers recover only the settled case
    and escalate uncertain ownership. Simulate a lost submission response and
    confirm the existing candidate is found instead of duplicated.
11. **Justiciar:** in the disposable environment, break admission or Tollgate.
    Verify a nondelegating justiciar stops the damage, documents any emergency
    bypass, restores valid ownership, and preserves explicit user pauses.
12. **Continuation:** attempt an ordinary stop with eligible work remaining.
    Verify a single reminder, no reminder loop, and no scheduled worker revival
    after all executors have stopped.
13. **Task UI:** fail a rename and restore access. Verify bounded retry, visible
    drift, continued delivery, and correction at the next transition. Archive
    an inactive eligible task; verify recent input, an active child, and manual
    unarchive exemption prevent incorrect cleanup.
14. **Observability:** interrupt collection, append an incomplete record, and
    include an unpriced model. Restore collection and inspect trace and cost:
    gaps are visible, child usage is counted once, and missing usage is not
    zero.
15. **Live update:** change and commit ordinary Hive code while another task is
    waiting. Verify the next invocation uses it, the old wait remains intact,
    and a broken new commit fails visibly without a service restart.
16. **Backup and cutover:** export while workers change tasks, disable remote
    access, restore it, and restore a snapshot into a disposable database.
    Verify consistent dependencies, visible backup age, ownership
    reconciliation,
    preserved Fulcrum state, and no accidental dotfiles publication.
17. **Performance:** run the eight-client command workload with observation and
    backup enabled. Inspect per-operation p95, error counts, lock wait, and
    startup costs. Record a target miss honestly rather than substituting
    database-only timings for complete commands.
18. **Artifact delivery:** complete an external research report through
    admission and fresh review. Verify completion records its artifact and
    acceptance result without requiring a nonexistent Tollgate candidate.
