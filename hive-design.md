# Hive: decentralized, composable agent workflows

## Summary

Hive is a collection of skills backed by a small, strictly typed Python CLI and
a local SQLite ledger. Workers drive the system forward. There is no central
dispatcher, persistent leadership task, or mandatory sequence of agent roles.

Users have exactly two scheduling choices:

- **Start here:** the current agent claims the task and begins working.
- **Backlog:** save the task for an authorized worker to claim later.

A Worker normally continues through available work, adopting whatever roles
the selected workflow requires. It may scope, implement, request review,
resolve conflicts, repair infrastructure, and then claim another task.
Specialist skills remain independently useful and finish their bounded
requests without automatically becoming backlog workers.

Hive preserves isolated workspaces, Tollgate validation and promotion,
dependencies, backlog prioritization, useful task names, automatic archiving,
cost attribution, and live code updates. It removes mandatory handoffs,
standing coordinator tasks, and general-purpose proof machinery.

The first version supports concurrent agents and multiple repositories on
**one host**. It uses **SQLite directly, without Beads**.

Backlog durability and backlog execution are separate promises: filed tasks
remain recorded, but if every worker stops, a user must start another worker.
Hive does not disguise this limit with a hidden coordinator.

This document specifies the target design. Its performance budgets and
acceptance scenarios are requirements, not claims of completed implementation
or validation. Writing this document does not activate Hive or migrate Fulcrum.

## Related information

- [Fulcrum live-iteration architecture][live]: the existing no-restart and
  consistent-source constraints that Hive preserves.
- [The successful `wt` workflow][wt]: the reference for independent agents and
  isolated delivery.
- [Tollgate delivery contract][tollgate]: the provider responsible for isolated
  validation, promotion, and synchronization.
- [Historical Fulcrum failure analysis][failures]: evidence about retry volume,
  workflow latency, and the limits of historical cost data.
- [Handoff-delay postmortem][handoff]: a concrete case of unrelated failures
  preventing ready work from progressing.
- [Weaver measurements][measurements]: measured lifecycle latency and ledger
  call counts.
- [Dispatch-regression postmortem][entry]: evidence for separating filing
  from starting work and simplifying task naming.
- [Desktop blocking-wait experiment][wait-experiment]: retained evidence for
  waiting without repeated model activity.
- [Local Beads contention measurements][beads-benchmark]: a follow-up comparison
  of embedded/server queries, claims, transaction rollback, and capacity locks.
- [Markdown/YAML versus SQLite measurements][files-benchmark]: a 1,000-task
  prototype comparison showing that plain files can meet sub-second targets.

## What Fulcrum's history establishes

This assessment uses the repository at its 600-commit point,
`4a01a0068ae54e4a7a06175314b17ba54900f11b`, and retained measurements and
incident reports. Historical incidents describe the implementation that
existed at the time. They do not establish that every defect remains present
today.

The important findings are:

- **Unrelated failures stopped progress.** One task spent 1h 54m 38s blocked by
  another bead: 90.7% of its create-to-close interval. The report records 880
  failures. Historical code confirms that a failure in grouped supervision
  could prevent later backlog work. Another worker must be able to claim ready
  work independently. See the [handoff-delay postmortem][handoff].
- **Routine lifecycle operations were expensive.** Baseline warm Weaver entry
  averaged 36.854s with 39 ledger calls; finish averaged 17.586s with 18 calls.
  One candidate entry handler spent 37.187s of 37.202s in ledger calls. Routine
  transitions need a small number of local transactions, not chains of
  subprocess reads and writes. See the [measurement artifact][measurements].
- **Retrying coordination became the workload.** A retained sample recorded
  22,096 thread-park failures among 22,431 deferred events, with roughly 97,761
  events across 24 assignments. Persistent failures need a visible state and a
  bounded next action. See the [earlier failure analysis][failures].
- **Filing had surprising execution effects.** Filing spawned another Weaver;
  after 13m 12s there was no source change. Interrupted dispatch also stranded
  ownership. Title handling contributed about 50s of lifecycle delay in that
  incident. Filing needs unambiguous scheduling semantics, and titles need a
  direct update path. See the [dispatch-regression postmortem][entry].
- **Blocking waits appear feasible.** A retained report describes a successful
  five-minute pending tool call with no model responses during the wait. A real
  30-minute CI wait and cancellation behavior still need validation. See the
  [desktop wait experiment][wait-experiment].

The Weaver measurements have small samples. They demonstrate an expensive
integration path, not an inherent lower bound for Beads. Similarly, historical
wall-clock delivery time includes waiting and cannot be treated as agent
implementation time.

The earlier cost report contained 1,339 partial contributions whose priced
portion totaled approximately $93.38. That is useful evidence about available
telemetry, but it is neither a complete cost total nor actual billed spend.

The strongest diagnosis is architectural: progress depended on too many
lifecycle steps, ownership handoffs, and shared supervisory paths. A task could
be ready while no component successfully performed the next required
transition.

Current ledger code also exposes broad dynamic mappings and wraps Beads calls
with locking, read/merge/write operations, and verification. That adds another
coordination layer around the storage system. Earlier Fulcrum implementations
used SQLite too, so changing the database alone would not solve the problem.

Hive therefore changes **who drives progress and how much machinery is
required**, as well as changing storage.

### Why SQLite, without Beads

Beads already offers dependencies, ready-task selection, and atomic claiming.
Follow-up [local contention measurements][beads-benchmark] found fast queries
in server mode, but repeated serialization errors in concurrent ready-task
claims. A shared admission lock removed those errors at a queueing cost.
Embedded mode developed multi-second tails under contention. See also the
[Beads documentation][beads].

SQLite is selected because Hive needs a small set of transactions spanning
task ownership, capacity, dependencies, and lifecycle state. Implementing those
directly avoids a second state model and a subprocess boundary for every
operation.

Beads remains a viable alternative if a local Dolt service and shared admission
wrapper are acceptable. SQLite's selection remains an architectural choice;
the follow-up did not benchmark SQLite or complete agent workflows. SQLite
supports this single-host arrangement, provided write transactions remain
short. See [SQLite application guidance][sqlite-use].

## Skills are the interface

Skills express workflows in ordinary instructions. The CLI supplies reliable
operations and returns concise instructions when a worker reaches an unusual
state. Hive does not introduce a workflow language or a mandatory role graph.

| Skill | Bounded responsibility |
| --- | --- |
| `hive-weaver` | Investigate intent and produce actionable scope. |
| `hive-worker` | Execute, deliver, and continue through eligible backlog. |
| `hive-queen` | Maintain priorities, dependencies, and backlog quality. |
| `hive-guard` | Review a specific change independently. |
| `hive-scout` | Review architecture and identify broader problems. |
| `hive-medic` | Diagnose and repair broken workflows or infrastructure. |
| `hive-keeper` | Improve recurring workflows and their instructions. |

These names describe activities, not permanent identities or authority
classes. A Worker can become a Weaver when a task needs scoping and a Medic
when delivery encounters a broken tool.

A wrapper skill can say:

> Use Hive Worker. Scope unclear requirements with Weaver. Request Guard
> review for changes to authorization or persistence. Deliver through
> Tollgate, then continue through this project's backlog.

Another can say:

> Use Weaver and Scout to produce an architecture proposal. Finish after
> delivering the document.

The second workflow does not acquire a code-execution slot, drain the backlog,
or recruit implementation workers merely because it uses Hive skills.

Independent review is selected by the workflow. Isolated validation and
Tollgate delivery are mandatory for code changes.

### Scheduling and authorization

Creating an implementation task authorizes its in-scope implementation and
Tollgate promotion by default. A workflow may explicitly require human
approval before promotion.

A backlog task may contain only a clear statement of intent. Workers can
investigate and scope it after claiming it. Material decisions beyond that
intent become a request for user input.

"Start here" is an atomic attempt to create and claim the task on the current
agent. If dependencies, capacity, exclusivity, or existing ownership prevent
starting, Hive reports the blocker without silently filing the task or
launching another agent. The user can then choose backlog placement.

"Backlog" persists the task and its dependencies without launching anything
or changing the filing agent into a Worker.

Illustrative CLI:

```text
hive task add --start-here "Fix stale cache invalidation"
hive task add --backlog --after H-42 "Remove the temporary compatibility path"

hive worker next
hive task hold H-43
hive task resume H-43

hive status
hive trace H-43
hive cost H-43
```

The implementation must preserve this distinction across skills, CLI
responses, and native task creation. There is no third implicit scheduling
mode.

## The rigid core

The core owns only:

1. Durable task intent and dependency relationships.
2. Atomic admission, capacity accounting, and assignment ownership.
3. Explicit lifecycle transitions and external delivery status.
4. Enough identity to correlate work, UI, and observations.

Skills own planning, task decomposition, review policy, role selection,
context management, and repair decisions.

The ledger is central storage, but it is not an active coordinator. Every
authorized worker can transact against it independently.

### Storage and transactions

Use Python's SQLite interface directly, with parameterized SQL and a small
typed repository layer. Do not add an ORM or a second persistence abstraction.

The ledger contains normalized records for tasks, dependencies, assignments,
native conversations, project policy, external-operation handles, and compact
transition events. Current state is authoritative; events explain changes and
do not need replaying to reconstruct normal operation.

Each state mutation and its explanatory event commit together. Transactions
that modify admission use `BEGIN IMMEDIATE` and perform no external work
while holding the database lock. See [SQLite transaction behavior][sqlite-tx].

Configuration:

- Local filesystem only.
- WAL mode, foreign keys enabled, `synchronous=FULL`.
- A bounded one-second busy timeout.
- No database transaction held across an agent call, filesystem operation,
  review, or CI wait.
- A separate database for detailed telemetry.
- SQLite 3.51.3 or newer, including its WAL-reset fix; the inspected local
  environment already has 3.53.4. See [SQLite WAL documentation][sqlite-wal].

Busy exhaustion returns a typed, actionable error. It does not trigger an
unbounded retry loop.

Task and assignment IDs are monotonically allocated and never reused.
Human-facing forms are `H-42` and `A-17`. Git and provider identifiers retain
their native meanings. Hive adds no content fingerprints or custom
verification-token scheme.

### Lots of meaningful types

Different concepts receive different types even when their runtime
representation is identical.

```python
HiveTaskId = NewType("HiveTaskId", int)
AssignmentId = NewType("AssignmentId", int)
EventId = NewType("EventId", int)
ProjectId = NewType("ProjectId", int)

CodexTaskId = NewType("CodexTaskId", str)
CodexTurnId = NewType("CodexTurnId", str)
ModelResponseId = NewType("ModelResponseId", str)
TollgateCandidateId = NewType("TollgateCandidateId", str)

SourceCommitOid = NewType("SourceCommitOid", str)
TestedCommitOid = NewType("TestedCommitOid", str)
PromotedCommitOid = NewType("PromotedCommitOid", str)

RepositoryPath = NewType("RepositoryPath", Path)
WorktreePath = NewType("WorktreePath", Path)
TranscriptPath = NewType("TranscriptPath", Path)
SkillName = NewType("SkillName", str)

ByteOffset = NewType("ByteOffset", int)
ElapsedMilliseconds = NewType("ElapsedMilliseconds", int)
InputTokens = NewType("InputTokens", int)
CachedInputTokens = NewType("CachedInputTokens", int)
OutputTokens = NewType("OutputTokens", int)
UsdEquivalent = NewType("UsdEquivalent", Decimal)
```

`NewType` supplies static distinctions, not runtime validation. Boundary
constructors validate incoming values before producing these types.

Domain values use immutable dataclasses, enums, and discriminated unions.
A CI-waiting assignment contains a candidate ID because that state requires
one. An unsubmitted assignment does not carry an optional candidate field
whose meaning depends on convention.

Ownership is an explicit value, such as an executing native task or a
temporarily delegated reviewer. Task completion is a union of a delivered code
result and a completed non-code result, each with its required evidence.

Dynamic JSON enters as `object`. Boundary decoders produce typed values or
typed errors. Domain code contains no `Any`, unchecked casts, or broad type
suppressions. Pyright strict mode is supplemented with a narrow lint rule for
those prohibitions; strict mode alone does not ban explicit `Any`. See
[Pyright configuration][pyright].

Use exhaustive state handling with `assert_never`. Use types for structure,
database constraints for persisted relationships, and tests for behavior over
time. Do not wrap every string mechanically: introduce a type when mixing it
with another concept would constitute a meaningful error.

### Lifecycle

The top-level task states distinguish durable intent from admitted work and
from explicit interruption:

| State | Meaning |
| --- | --- |
| `Queued` | Durable intent awaiting admission; readiness is derived. |
| `Assigned` | Admitted work with an assignment and accountable capacity. |
| `Held` | Explicitly stopped by the user. |
| `NeedsInput` | Blocked by authority or unavailable information. |
| `Done` | Required outcome delivered. |
| `Cancelled` | Intentionally abandoned. |

Assigned phases include starting, working, waiting for CI, reviewing,
waiting for approval, and recovering. Each phase has its own required data.

`Held` and `NeedsInput` each distinguish:

- **Draining resources:** execution is revoked, but owned processes or
  external resource use have not settled. The assignment still consumes
  capacity.
- **Settled:** resources have settled, the assignment is closed, and capacity
  is released. Checkpoints and provider handles remain recorded.

Resuming creates eligibility for a **fresh assignment through normal
admission**. It does not reactivate an obsolete owner or bypass capacity and
dependencies.

Only `Done` satisfies a dependency. Held, cancelled, and input-blocked
prerequisites remain visible blockers. A cancelled prerequisite requires
explicit replacement, dependency removal, or cancellation of the dependent.

Dependency creation rejects cycles transactionally. Adding a prerequisite to
already executing work requires first suspending that work; it cannot
retroactively make an active assignment invalid without an explicit
transition.

## Workers provide forward progress

Workers use the ledger to admit work, but independently decide how to complete
their authorized assignments. No supervisor must visit each task for progress
to continue.

### Atomic selection and capacity

Workers select by priority, then FIFO order. Priorities are P0–P4, with P2 as
the default. Routine selection requires no model judgment.

A claim transaction checks:

- Authorized project scope.
- Task state and completed dependencies.
- Global and project capacity.
- Repository exclusivity.
- Whether the claiming agent already owns an assignment.

It then reserves capacity and creates the assignment atomically.

The initial default is four global slots and four per project, configurable
by the user. These are **whole in-flight task slots**. Implementation,
independent review, queued CI, and running CI retain the slot. Waiting does
not make a task free.

Tollgate separately controls build concurrency and CPU/memory resources.
Hive's task limit does not replace those controls.

Read-only investigation and backlog metadata maintenance do not consume
execution slots. Applying code changes or initiating expensive task execution
does.

Repository exclusivity is explicit. Hive does not infer locks from predicted
file overlap. When the highest-priority otherwise-ready task requires
exclusivity, lower-priority work in that repository stops entering until
current assignments drain. Other repositories continue normally.

### Continuing and recruiting

Worker defaults are:

- Drain eligible work within the authorized project.
- Optionally stop after one assignment.
- Recruit peers only within the same authorized scope and shared capacity.
- Keep one unfinished assignment per worker.

Recruitment reserves an assignment before creating a native task. The child
receives the assignment ID and must bind successfully before doing work.
Only one child can bind. A delayed or duplicate child exits if the assignment
is no longer available to it.

Native task creation uses the saved project directly. Tollgate owns the
isolated worktree; Hive must not accidentally create a second native worktree
around it. All implementation commands use the assigned Tollgate workspace
explicitly.

After delivery, the worker closes the assignment and may reserve the next
eligible task in the same transaction. It reuses the conversation for related
work and creates a fresh successor when the next scope would benefit from
fresh context. This is agent judgment, not a similarity score or hash.

A final transactional ready-work check determines retirement. Work filed
before that check can be claimed. Work filed after the last worker retires
remains durable until someone starts a worker.

### Dependencies must not consume every worker

If an executing task discovers an unstarted prerequisite, the worker:

1. Preserves a checkpoint.
2. Settles its write-capable activity.
3. Records the dependency and requeues the original task.
4. Releases the original slot.
5. Claims eligible work, potentially the prerequisite.

It must not retain a parent slot indefinitely while waiting for an unstarted
child. Otherwise every available slot can become occupied by parents whose
children cannot start.

A task awaiting CI or a review of its own change retains its slot because
that work is already in flight.

## Delivery, review, and interruption

Hive records task ownership and delivery state. Tollgate owns the mechanism
that validates and promotes code, including its resource limits and evidence.

### Tollgate owns delivery

For code work, Tollgate creates the isolated workspace and owns validation,
integration, conflict handling, promotion, and configured source/remote
synchronization.

The normal path is:

1. Prepare the task's clean source commit in its isolated workspace.
2. Submit a candidate without promotion authority.
3. Obtain successful validation and any workflow-selected review.
4. Confirm the current clean source still matches the reviewed source.
5. Approve through Tollgate.
6. Mark `Done` after promotion and required synchronization succeed.

Hive records native candidate and commit identifiers. It does not reconstruct
Tollgate's proof system, duplicate its evidence, or publish with raw
`git push`.

Review and CI may proceed concurrently when the author is quiescent. A Guard
reviews an exact source commit. Any source change invalidates that review.

A reviewer uses the same task slot while the author waits. Delegation does
not grant either agent an additional unfinished assignment, and a Guard does
not become a backlog worker on completion.

When a workflow requests human approval, approval refers to the displayed
source commit and candidate. Source edits or a replacement candidate require
renewed approval. A provider-generated integration/tested commit alone does
not; Tollgate owns that integration step.

### Ownership does not fence arbitrary processes

Ledger ownership prevents stale actors from performing Hive transitions. It
cannot stop an already-running process from writing files.

Any command that can outlive its tool call runs through a Hive process wrapper
that records its process group and assignment. Short direct commands must
finish before ownership transfers.

Ordinary delegation requires explicit quiescence: outstanding write-capable
tool calls and process groups have completed or stopped, and no external
effect request is still being initiated. Recovery also requires positive
evidence that the previous native executor has terminated.

A missing heartbeat or elapsed timeout is not sufficient evidence for
takeover. If quiescence cannot be established, Hive exposes a blocker instead
of allowing two writers.

This is a cooperative system for agents acting under the same user's
authority, not a security sandbox against malicious local processes.

### External effects and uncertain responses

A brief per-task OS lock serializes external mutations. Hive never holds a
SQLite transaction across an external call, and neither lock is retained
through a CI wait.

For operations whose response can be lost—native task creation, worktree
creation, candidate submission, or approval—Hive records the small amount of
intent and provider identity needed to inspect the outcome.

Examples:

- A child binds an existing assignment after native creation.
- A deterministic assignment-derived branch identifies a created workspace.
- Candidate submission records the source and workspace for reconciliation.
- An uncertain approval is inspected through Tollgate rather than blindly
  issued again.

There is no universal receipt object or automatic replay framework for every
CLI command. Unknown outcomes become explicit states with specific recovery
instructions.

### User stop means hold

An explicit user interruption holds the assignment. It does not trigger an
automatic restart.

Existing validation may continue, but new implementation and new promotion
initiation stop. Capacity remains charged until the retained resource use
settles.

The promotion race has a defined ordering:

- If the hold commits first, entering `Approving` is rejected.
- If `Approving` commits first, that approval attempt is already authorized
  and may reach Tollgate after the hold. The hold response must disclose
  that promotion may already be underway.

A hold cannot retract an initiated or accepted external action.

Use the native Interrupt hook for prompt recording where supported. Its
coverage is limited to active main tasks, so missing hook evidence must not
be interpreted as proof of a crash or permission to restart. See
[Codex hooks][hooks].

## Recovery and field promotion

Every Worker can diagnose failures and invoke Medic. Recovery is part of an
agent's evolving role, not a separate permanent office.

At next-work boundaries, surviving workers inspect abandoned or failed
assignments as well as queued work—even if apparent capacity is full.
Recovery requires terminal-executor evidence, settled writers, and
reconciliation of outstanding provider operations.

When repair requires code changes, the worker checkpoints and suspends its
original task, then admits a repair task through the ordinary capacity and
Tollgate path. The repair can target Hive itself.

If every slot is occupied by stalled CI, recovery has one bounded escape:

1. An authorized owner or recovering worker requests cancellation of an
   affected candidate.
2. It confirms that the candidate's resource use has actually settled.
3. It suspends the original assignment.
4. It uses the released capacity for repair.

A cancellation request alone does not release capacity. If settlement cannot
be established, user intervention is required. There is no secret emergency
slot and no bypass around a broken Tollgate gate.

Medic's static instructions remain available when the high-level CLI fails.
A minimal bootstrap entry point can invoke the **same tested ledger admission
function** without loading skills, pricing, or other optional modules.

If the ledger or admission core itself is unusable, code repair cannot
pretend admission succeeded. Medic may diagnose read-only and identify the
operator recovery needed. Hive does not introduce a fallback ledger.

Optional scheduled activities create a fresh bounded task each time—for
example, a six-hour architecture review. They do not run persistent
Queen/Marshal/Vizier conversations or serve as mandatory recovery polling.

## Blocking waits without model polling

Provide a thin per-session stdio MCP adapter and a foreground CLI equivalent.
The adapter has no scheduling authority, shared queue, or central broker.
Each new request invokes current CLI code.

Wait operations cover a specific task transition or Tollgate candidate. They
release database and external-effect locks while preserving assignment and
capacity.

For Tollgate, use its candidate-specific wait operation and consume changed
status records. This reduces repeated agent calls; it does not imply that
Tollgate internally uses push notifications.

Local task waits use a bounded one-second database check without model
turns. Default operation deadline is one hour, with the MCP client timeout
set longer, such as 3,900 seconds. The client timeout must be configured:
the standard MCP timeout is much shorter. See
[Codex MCP configuration][mcp].

Results distinguish completion, cancellation, timeout, and provider failure.
A timeout prompts diagnosis of the current assignment rather than creating a
replacement task. Any mutation after a wait reenters current CLI code.

The retained five-minute experiment supports feasibility. A real
30-minute CI wait with zero intermediate model responses, working
cancellation, and correct capacity accounting remains an acceptance test.

## Task names and observability

Native task names show current work at a glance. Detailed reports explain
latency and cost without putting observation on the execution path.

### Names are a primary interface

Native task names contain role, durable task ID, subject, and meaningful
state:

```text
🐝 [H-42] Fix cache invalidation · implementing
🛡️ [H-42] Fix cache invalidation · reviewing
🐝 [H-42] Fix cache invalidation · waiting for CI
🩺 [H-51] Repair candidate submission · investigating
```

Update names directly at lifecycle transitions, not after every tool call.
The ledger retains the desired title.

A naming failure does not block delivery. It records visible title drift and
the next capable agent retries. A stopped task may retain an outdated native
title until a capable agent can repair it; Hive must show that discrepancy in
status rather than claim the UI is current.

Automatically archive completed Hive-created workers and reviewers. Preserve
user-origin conversations and tasks needing input or recovery. Respect a
manual unarchive. Missing final telemetry must not indefinitely prevent
archiving.

### The only resident component observes

A background collector reads registered transcripts incrementally using byte
offsets and bounded chunks. It writes to the telemetry database, independently
of task admission and delivery.

It does not claim tasks, repair state, restart agents, or decide priorities.
Its failure delays reports but cannot stop work.

Hooks register identities and important lifecycle events. They do not scan
full conversation history or inject generic workflow instructions before
every tool call.

Correlate observations using task, assignment, native task, turn, response,
tool, and candidate IDs. Distinguish:

- Queued time.
- Model activity.
- Tool execution and waiting.
- CI queueing and execution.
- Handoffs and infrastructure preparation.
- Unattributed intervals.

Overlapping spans must not be added as if they were sequential elapsed time.

### Cost attribution

Report API-equivalent cost, clearly labeled as an estimate rather than
subscription billing.

Preserve model, service tier when available, token counters, rate-card
effective date, and pricing coverage. Unknown models or missing prices remain
unknown. Missing, partial, and zero usage are distinct values.

Charge each model response to the task and role active at response start.
Do not invent a split when one response crosses a workflow boundary.
Unassigned setup is reported separately. Child usage is counted once.
Context reuse is charged according to the provider's reported usage.

Reports provide task totals, role totals, workflow comparisons, and links
between slow intervals and tool activity. Every report includes collector
freshness and coverage gaps.

Retain detailed spans for 30 days by default. Retain compact task outcomes
and usage aggregates until explicit pruning. Transcript parsing is an
isolated adapter because the native transcript format is not a stable
contract.

No custom dashboard is required for the first version: good native names,
`status`, `trace`, and `cost` are the product.

## Hot reloading

Each CLI invocation resolves committed local `master` before importing
business code. It runs from an immutable source snapshot derived from that
Git commit, using the existing dependency environment.

This preserves consistent delayed imports and asset reads without requiring
installation, manual activation, service restart, or remote fetching for
ordinary edits. Source preparation failure is visible; Hive must not
silently run older code.

Existing calls finish with their selected code. Subsequent calls use the new
commit. Skills remain direct source links, so subsequent skill reads see edits
immediately.

Do not add a snapshot-lease service. Retain prepared snapshots until explicit
cleanup. Git's existing commit identifiers are sufficient; no additional
fingerprints are needed.

The collector's resident portion only watches and dispatches fresh parser
batches. The MCP process transports requests and starts fresh CLI calls.
Neither permanently owns imported business logic.

### Exceptional schema and dependency changes

Ordinary code changes need no maintenance mode. Schema changes are explicitly
exceptional because running old code and a newly incompatible database cannot
safely coexist.

Every mutation-capable invocation acquires a shared maintenance guard
**before selecting its source and schema**. It retains that guard through any
external call and subsequent database write.

Explicit maintenance:

1. Closes admission and acquires the exclusive guard after existing
   mutation-capable calls settle.
2. Takes a database backup using SQLite's backup facility.
3. Performs one transactional, idempotent conversion.
4. Reopens admission only after success.

Long pure waits release the guard and reenter fresh code before mutation.
If an old external operation cannot settle, maintenance fails visibly; it
does not kill the operation or mutate underneath it.

Failed maintenance leaves the fence in place for recovery. There is no
schema-version compatibility chain or promise that old business code remains
supported. Dependency and bootstrap changes are similarly explicit
maintenance, not part of routine hot reload.

## Interfaces and progressive disclosure

CLI responses expose typed outcomes that skills can act on without parsing
free-form error text.

Important outcome families include:

- `Claimed`, `NoReadyWork`, `CapacityFull`, `DependencyBlocked`.
- `Held`, `NeedsInput`, `StaleAssignment`.
- `CandidateRunning`, `CandidateFailed`, `Delivered`.
- `OutcomeUnknown`, `RecoveryRequired`, `MaintenanceRequired`.

An exceptional result contains the relevant IDs, concise facts, permitted
next actions, and a pointer to the appropriate skill section.

For example:

```text
Recovery required: H-42 / A-17

Candidate C-81 failed because the configured validation command is missing.
The source workspace is preserved. This assignment still occupies one slot.

Next:
- Inspect the validation failure with Medic.
- Repair within this task if the fix is within its scope.
- Otherwise checkpoint and suspend this task before claiming a repair task.
```

Ordinary success stays short. Agents receive conflict-resolution, recovery,
and maintenance instructions when those states arise.

Persist concrete delivery requirements—such as independent review or human
approval—but do not persist an executable workflow graph. Wrappers remain
skills that compose the same small operations.

## Acceptance criteria

Hive cannot literally be incapable of bugs. Its simplicity must instead be
demonstrated by a small state model, narrow authority boundaries, and failure
tests that exercise real interleavings.

### Correctness and failure tests

Use real temporary databases and multiple processes for admission and
ownership tests. Do not rely exclusively on mocked repository methods.

Required scenarios include:

- Concurrent claims cannot exceed capacity or assign one task twice.
- Dependency insertion rejects cycles; cancelled prerequisites remain
  actionable blockers.
- Filing races correctly with the last worker's retirement.
- All slots can request review without requiring extra reviewer slots.
- All active tasks can discover prerequisites without stranding those
  prerequisites behind occupied parent slots.
- Held CI retains capacity until settlement; resume requires fresh admission.
- Stale owners and delayed duplicate children cannot mutate current state.
- Ownership transfer refuses to reuse a workspace with an unsettled writer.
- Lost creation, submission, and approval responses reconcile without blind
  duplication.
- Hold and approval obey both possible transaction orderings.
- Recovery frees capacity only after confirmed resource settlement.
- Maintenance waits for an older call's post-effect write.
- Collector failure, malformed transcript data, missing pricing, and title
  failures do not stop task delivery.
- Ordinary source edits affect the next invocation while an existing wait
  remains intact.

Add negative type-check fixtures proving that conceptually different IDs,
paths, and token quantities cannot be interchanged. Test transition
functions and persisted constraints together.

### Performance targets

These are acceptance targets, not measurements already achieved:

- Warm ordinary local CLI operations: p95 at or below **250ms**.
- First operation using a new source commit: p95 preparation at or below
  **one second**, with dependencies unchanged.
- Added median wall time and API-equivalent cost: **at most 5%** against a
  matched direct-agent baseline with the same model, Tollgate checks, and
  review policy.

Benchmark one, four, and sixteen concurrent clients with 100 and 10,000
historical tasks and observability enabled. Report contention and tail
latency separately.

Use at least ten matched task pairs for workflow comparisons. Match cache
conditions and host load, report sample sizes and tails, and reject cost
acceptance when pricing coverage is incomplete.

Zero-work waits should produce no repeated model activity. No orchestration
hang or retry storm is acceptable merely because median overhead passes.

### Initial deployment

Start with a new Hive ledger and deliberately refile selected task intent.
Do not import Fulcrum's live runtime ownership or silently switch existing
agents into Hive.

Validate the full native-task, naming, worktree, CI, delivery, and follow-on
path early. A collection of passing local unit tests is insufficient evidence
that the assembled agent workflow works.

## Manual QA

Exercise the assembled workflow first, then inspect interruptions, recovery,
observability, and live updates through the same user-facing interfaces.

1. Start a task on the current agent. Verify its name, isolated workspace,
   capacity record, validation, promotion, and final outcome.

2. File a backlog task. Confirm that filing starts no agent. Start a Worker
   and verify that it claims the task.

3. File Y depending on X. Complete X and verify that a surviving Worker
   continues to Y without a coordinator.

4. Exhaust capacity, including with queued and running CI. Verify that new
   workers cannot bypass the limit and that independent review uses its
   parent task's slot.

5. Stop a worker during CI. Verify that validation continues, new promotion
   initiation is held, capacity settles correctly, and no replacement starts
   automatically.

6. Terminate one worker unexpectedly. Verify that a surviving worker recovers
   only after confirming terminal execution and settled writers. Terminate
   every worker and verify that backlog remains visible without claiming
   automatic progress.

7. Exercise a lost external response and a delayed child start. Confirm that
   Hive reconciles the existing operation rather than duplicating it.

8. Break a validation configuration and exercise field promotion. Confirm
   that repair respects ordinary admission and Tollgate, including the
   all-slots-occupied case.

9. Disable the collector and fail a title update. Complete a task anyway,
   restore the components, and verify visible gaps and title repair.

10. Hold one real CI wait open for at least 30 minutes. Confirm no intermediate
    model polling, working cancellation, and correct slot accounting.

11. Change ordinary Hive code while another task waits. Confirm that the next
    CLI invocation runs new code while the existing operation remains
    undisturbed. Separately verify that incompatible schema maintenance waits
    for old mutation-capable calls.

12. Inspect the resulting task trace and cost report. Confirm useful names,
    distinct waiting and working time, honest pricing coverage, and automatic
    archiving that preserves user-owned and attention-needed conversations.

[live]: docs/architecture/live-iteration.md
[wt]: /Users/dthurn/.codex/skills/wt/SKILL.md
[tollgate]: /Users/dthurn/tollgate/README.md
[failures]: docs/fulcrum2/failure-analysis.md
[handoff]: docs/postmortems/2026-09-16-bead-46a5eb5e-handoff-delay.md
[measurements]: docs/measurements/weaver-2026-09-16.json
[entry]: docs/postmortems/2026-09-17-bead-cdf5657c-weaver-dispatch-regression.md
[wait-experiment]: docs/experiments/2026-09-16-desktop-mcp-ci-wait.md
[beads]: https://github.com/gastownhall/beads
[beads-benchmark]: docs/experiments/2026-09-19-beads-contention.md
[files-benchmark]: docs/experiments/2026-09-20-task-files.md
[sqlite-use]: https://www.sqlite.org/whentouse.html
[sqlite-tx]: https://www.sqlite.org/lang_transaction.html
[sqlite-wal]: https://www.sqlite.org/wal.html
[pyright]: https://github.com/microsoft/pyright/blob/main/docs/configuration.md
[hooks]: https://learn.chatgpt.com/docs/hooks
[mcp]: https://learn.chatgpt.com/docs/extend/mcp
