# Hive: independent workers, shared task state

Hive is a set of agent skills and a small, strictly typed Python CLI. Workers
independently scope, claim, implement, review, and deliver work. Beads stores
task intent and ownership; Tollgate validates and promotes project changes.
There is no dispatcher, leader, or scheduled stuck-work monitor. When every
executor stops, pending work waits for a user to start one.

The design keeps Fulcrum's useful worktrees, backlog, dependencies, specialist
roles, task names, cost visibility, and live updates. It replaces its elaborate
coordination and proof machinery with a small core enforcing ownership,
dependencies, capacity, and valid state changes. Skills own execution judgment
and checklists. Structural simplicity reduces opportunities for bugs; it is not
a claim that a concurrent system can be proved bug-free by being small.

Hive is a new codebase at `~/hive`, initially serving one host and multiple
projects. All Hive beads live in a fresh server-mode Beads database under
`~/brain`, with native short IDs prefixed `hv-`. The default capacity is eight
in-flight beads globally. Ordinary task commands target end-to-end p95 below
100ms at eight concurrent clients; this is an acceptance target, not a measured
achievement.

This document specifies the intended system. Producing it does not authorize
installing Hive, initializing storage, creating automations, or migrating work.

## Related information

These references supply evidence and integration context. Historical design
recommendations do not override the decisions in this document.

- [Beads contention measurements][measurements] cover server mode, atomic
  claims, dependencies, and local locking. Their SQLite recommendation and
  250ms budget are superseded here; the measurements remain useful evidence.
- [Live iteration](docs/architecture/live-iteration.md) defines the existing
  consistent-source and connection-continuity requirements to preserve.
- [Blocking MCP wait experiment][wait-experiment] demonstrates one five-minute
  synthetic wait without model polling, not complete Tollgate integration.
- [Tollgate](/Users/dthurn/tollgate/README.md) documents worktrees, candidates,
  validation, promotion, and configured source synchronization.
- [The wt skill](/Users/dthurn/.llms/skills/wt/SKILL.md) provides experience
  with
  isolated workspaces, conflicts, and in-scope CI repair.
- [Beads](https://github.com/gastownhall/beads) owns supported task interfaces,
  native IDs, persistence, and dependencies.
- [Warden's review reference][review] motivates strict architecture review,
  simplification, and explicit type boundaries.
- [Codex app-server documentation][codex] describes native conversations,
  activity, and archival; [MCP configuration][mcp] covers tool-call timeouts.
- [Handoff-delay postmortem][handoff] and [dispatch regression][dispatch]
  illustrate the costs of coupling progress to elaborate role handoffs.

[measurements]: docs/experiments/2026-09-19-beads-contention.md
[wait-experiment]: docs/experiments/2026-09-16-desktop-mcp-ci-wait.md
[review]: https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md
[codex]: https://learn.chatgpt.com/docs/app-server
[mcp]: https://learn.chatgpt.com/docs/extend/mcp
[handoff]: docs/postmortems/2026-09-16-bead-46a5eb5e-handoff-delay.md
[dispatch]: docs/postmortems/2026-09-17-bead-cdf5657c-weaver-dispatch-regression.md

## Authority and execution scope

A **bead** is a durable work item in Beads. A **Codex task** is a conversation
that may execute several beads in sequence. A **native turn** is one execution
responding to input in that conversation; its identity distinguishes successive
attempts by the same conversation.

A **Tollgate candidate** identifies submitted source. **Promotion** accepts the
validated, integrated result as the project's release. **Configured
synchronization** updates the local and remote branches selected by Tollgate's
project policy. Validation alone does not mean delivery is complete.

Each system has a narrow authority:

- Beads holds task intent, priority, dependency edges, status, and ownership.
- Hive's core permits claims and valid state transitions.
- Workers judge scope, conflict risk, resources, review findings, and priority.
- Tollgate owns ordinary CI, integration, promotion, and synchronization.
- Codex supplies native identity, activity, naming, and archival operations.
- Observability stores derived measurements; it never authorizes task changes.

Shared storage is central data, not a central coordinator. Every worker uses the
same admission operation independently. No permanent agent chooses assignments,
restarts idle executors, or determines which worker may make progress.

Filing a normal bead authorizes its in-scope implementation and ordinary
Tollgate delivery by default. Explicit user restrictions take precedence.
Unapproved designs and unresolved product decisions remain deferred; filing them
is not permission to guess the missing decision. A user pause requires user
resumption.

An executor continues through its starting project's eligible backlog. Shared
visibility does not authorize it to change another project. Cross-project
prerequisites can be recorded and reported without silently switching
repositories. Recruited peers inherit the same project scope unless the user
explicitly assigns a different one.

## Skills and worker behavior

Roles describe an agent's current activity. Their instructions use progressive
disclosure: keep the ordinary path short and load conflict, interruption, or
repair guidance when the corresponding condition occurs.

| Skill | Emoji | Purpose |
| --- | --- | --- |
| `$executor` | ⚒️ | Scope, file, implement, deliver, and continue work |
| `$warden` | 🛡️ | Review a diff or broader project architecture |
| `$weaver` | 🧵 | Design larger work and obtain approval |
| `$sage` | 📖 | Improve workflows, tools, and diagnostics |
| `$justiciar` | 🔥 | Restore a broken swarm without delegation |
| `$vizier` | 🔮 | Answer questions in an initially read-only phase |
| `$archivist` | 📁 | Archive inactive Hive-managed conversations |
| `$bead` | 📿 | File the requested issue or issues immediately |

### Executor

Executor accepts descriptions of tasks, bugs, or features, or an existing bead
ID. It asks material clarifying questions, searches existing work, and maintains
a prioritized backlog with native dependency links. A supplied bead is claimed
directly rather than duplicated; all admission checks still apply.

Its ordinary loop is:

1. Clarify scope and file appropriately sized beads with acceptance criteria,
   project identity, priority, and prerequisites.
2. Inspect active work, conflict risk, CPU and memory pressure, and CI load.
   Choose useful work or leave it queued, even when capacity remains.
3. Claim through Hive and create an isolated Tollgate worktree before changing
   project files. Read-only scoping and filing need no worktree or slot.
4. Implement and run proportionate checks. Commit the source to be reviewed.
5. Run a fresh warden subagent without inherited conversation history. Fix
   valid findings and explain disagreements. Review source-changing repairs.
6. Submit to Tollgate, authorize ordinary delivery, and block for its result.
   Repair in-scope CI failures or merge conflicts within the same bead.
7. Complete the checklist, finish promotion and synchronization, and close the
   bead with a concise outcome.
8. Search for the next eligible bead in the starting project and repeat.

At capacity, executor files the work and exits. Filing is always possible even
when implementation cannot start. Conflict concerns may justify choosing a less
overlapping task or leaving capacity unused. Add a dependency only when a real
sequencing relationship exists, not to encode a temporary resource guess.

Executor can recruit independent executor conversations for parallelizable work.
Each receives its scope and project, claims its own bead, and creates its own
Tollgate worktree. Recruitment does not reserve a slot or bypass admission. A
child denied a requested claim reports the reason and exits. Independent peers
continue if their recruiter stops.

The ceiling limits in-flight beads, not every process or model request. A cold
reviewer shares the parent bead's slot. Read-only specialists still consume
resources, so recruitment and optional audit schedules must consider host load.

### Warden

Warden reviews a specific diff or general architecture using the linked strict
review reference. It seeks substantive simplification and small files with
single responsibilities, rather than merely redistributing complexity.

A **project invariant document** is a project-owned Markdown file listing
critical properties the project must maintain. Project registration identifies
it. Warden reads it for each review and names affected invariants in findings.
If it is missing, report the gap and file work to establish it; do not claim a
complete invariant audit.

Review includes:

- Module boundaries, single responsibilities, unnecessary abstractions, and
  useful source documentation such as rustdoc.
- Explicit types, immutable values, and opportunities to make illegal states
  unrepresentable.
- Recent bugs: whether the category could have become a type error, and whether
  a black-box test could have prevented the observable failure.
- Test quality: favor a few meaningful black-box tests over implementation
  replicas, change detectors, or arbitrary aesthetic assertions.

The executor gives its cold reviewer only the scope, bead, workspace, source
commit, invariant document, and relevant checks. The reviewer independently
reads the code; it does not inherit the author's reasoning. Findings are
advisory, but obtaining fresh review is mandatory. Executor fixes or explains
findings and files useful out-of-scope work. Removing brittle tests occurs
through executor scope while preserving meaningful behavioral coverage.

A source-changing CI repair or conflict resolution requires review of the
changed diff before resubmission. Review is not a requirement to repeat until
warden agrees. A standalone architecture audit may file findings and enter
executor through normal admission. Warden may optionally run on a schedule.

### Weaver, sage, and vizier

Weaver writes a standalone technical design for larger work, usually in the
project's `/plan` area. It asks questions, runs a fresh cold reader with only
the document, resolves comprehension gaps, and obtains user approval before
implementation. Its implementation beads link to the design and carry useful
dependencies. Beads filed before approval remain deferred.

Authoring a project document can be its own admitted planning bead, using a
Tollgate worktree and delivery. Publishing a draft does not approve its
implementation. Once the design is approved, weaver transitions to executor
through normal admission rather than a special planning-to-execution channel.

Sage investigates workflow improvements using logs, traces, cost reports, and
profiling. It examines CI and build times, formatting and linting, documentation
discovery, debugging tools, log quality, and clarity of skills including Hive's
own. It files evidence-backed beads and can enter executor normally. Optional
scheduled audits have a bounded purpose and project; they are not stuck-work
recovery jobs.

Vizier starts read-only, answering questions about code and system state. After
answering, it can remain a research task or state its transition to executor
when it identifies actionable work. The transition requires filing or selecting
a bead and normal admission before implementation changes.

### Bead and justiciar

In a new conversation, `$bead` files the issue or issues and exits. In a reply
to another role, it files the item immediately and returns to that enclosing
role, preserving the role's title. It never starts implementation itself. Filed
work still carries the normal execution authorization for future executors.

Justiciar is invoked for emergencies such as resource exhaustion, corruption, or
a global coordination failure. It does not delegate and does not need a capacity
slot before stopping damage. It may close admission, interrupt workers, stop
processes, inspect external actions, and repair storage or tooling.

If broken Tollgate prevents necessary recovery, justiciar may bypass it to
restore the system. This authority covers emergency repair, not unrelated
feature delivery. Record the cause, actions, bypassed checks, and remaining
validation; use local notes if Beads is unavailable. Reconcile ownership and
candidates before reopening admission, preserve user pauses, and obtain normal
validation for emergency code changes once Tollgate works again.

## Beads storage and task states

Every invocation explicitly selects the canonical Hive database under `~/brain`,
served by a local Dolt server. A worktree's current directory must never select
a different task store. Server failure returns an explicit error; there is no
embedded-mode fallback.

Use native Beads fields for task text, priority, status, assignee, and
dependency edges. Use metadata for project identity, lifecycle phase, owning
turn, workspace, candidate, pause conditions, and outcome. Use Beads' returned
IDs, including any longer IDs its native allocation requires. Do not allocate
custom IDs, maintain a shadow task table, or keep a separate capacity counter.

Configuration and enrolled conversation identities may use distinctly typed
Beads infrastructure records. Exclude these records from the work backlog and
capacity count. They store project bindings, capacity settings, and enrollment;
they do not duplicate bead ownership. Bootstrap settings outside Beads identify
the server and database needed to reach those records.

The domain decodes native records into these alternatives:

| State | Meaning |
| --- | --- |
| Queued | Open and unassigned; readiness is derived from dependencies |
| Owned | In progress with an owner and a valid execution phase |
| Deferred | Ineligible until its recorded conditions are resolved |
| Done | Closed with a successful delivery outcome |
| Cancelled | Closed without satisfying dependent work |

Owned phases are preparing, implementing, reviewing, and waiting for delivery.
Each phase requires its relevant values: implementing needs a workspace; waiting
for delivery also needs a source commit and candidate. Preserve native status
and metadata together in one supported atomic write. Do not add another status
field that competes with Beads status.

Ownership identifies both the native conversation and turn. Owner mutations
compare both identities. A delayed callback from an ended turn cannot update a
bead owned by a newer turn of the same conversation.

Deferred work may retain ownership while writers or external work settle. It
still consumes a slot. Capacity counts every work bead retaining an assignee,
including deferred work, rather than counting only `in_progress` status.

```text
queued, no owner                 -> eligible if prerequisites are done
owned, reviewing                -> one occupied slot
deferred, user pause, old owner  -> paused; one occupied slot
settled, user pause, no owner    -> paused; no occupied slot
resumed, no remaining conditions -> queued; fresh admission required
```

A pause can coexist with pending design approval or missing input. Resolving one
condition does not erase the others. Only explicit user resumption removes a
user pause. Completion or cancellation clears ownership and records its terminal
outcome in the same native update after settlement.

Only successful completion satisfies a dependency. If Beads treats every closed
prerequisite as resolved, Hive additionally checks its outcome. A cancelled
prerequisite requires an explicit dependency change, replacement, or
cancellation of dependent work. Invalid active ownership makes admission fail
visibly rather than silently lowering the occupied count. Unrelated malformed
historical data does not have to block ordinary reads or healthy work.

### Dependencies and interrupted mutations

Use native dependency edges and cycle detection, including across projects.
Dependency changes affecting readiness participate in the same local lock as
admission. Direct claims perform the same dependency checks as next-work claims.
The retained measurements show that native direct claim alone was insufficient.

Do not change an owned bead's prerequisites underneath its executor. If new
prerequisite work is discovered, checkpoint and defer the parent, settle its
writers and ownership, then file and attach the prerequisite before reopening
the parent. Other workers can file proposed prerequisite work meanwhile.

```text
defer parent -> settle owner -> file prerequisite -> add edge -> reopen parent
```

A crash in this sequence leaves deferred work or an extra visible prerequisite,
not a runnable parent missing a known dependency. Recovery examines the saved
intent before reopening it. Parents release settled ownership rather than
exhausting all eight slots while waiting for unstarted children.

Use native atomic operations and supported transactional batches when their
actual grammar covers the change. A local lock serializes cooperating callers;
it does not make separate writes crash-atomic or roll them back on failure.
Uncertain writes require inspection before retrying. Losing a client or lock
holder does not prove that an already-issued server mutation has stopped:
validate this boundary and refuse competing admission while its effect remains
unresolved.

Use one durable local marker for the serialized admission-relevant write in
flight. Under the lock, record its native bead ID and intended change before
sending the write; clear the marker only after a conclusive result. It is an
uncertainty stop, not a second task record or replay journal. Include its
durable write cost in latency measurements.

After unexpected process death, the next lock holder sees the marker and refuses
every competing admission-relevant mutation, including claims, transitions,
settlement, pause/resume, and dependency edits. It cannot overwrite the marker.
Recovery must establish that the old server request can no longer commit, then
inspect the affected Beads state before clearing it. A timeout or an apparently
unchanged bead is insufficient. Use supported server session inspection when it
provides that guarantee; otherwise justiciar closes admission and performs
controlled server recovery before reconciling the bead. Do not automatically
replay the intended change. A surviving mutation process retains the lock
through its result even if its client disconnects.

### Supported storage access

Normally use supported `bd` JSON interfaces. Keep calls few, avoid full backlog
reads when bounded queries suffice, and keep remote synchronization off the
mutation path. Do not reread every successful write merely to manufacture proof.

Direct Dolt access requires a measured performance or atomicity need that
straightforward supported-interface tuning cannot resolve. Document the exact
query or transaction and the Beads behavior it must preserve. Confine access to
a narrow typed adapter with real-server behavioral tests. Unsupported schema
changes fail visibly; do not build fallback chains or expose raw writes to
skills. Prefer an upstream supported interface when it can meet the need.

## Admission, capacity, and priority

**Admission** is the short operation that validates readiness and claims a bead.
All workers use one permanent host-local advisory lock for compound decisions.
Ownership changes, relevant dependency edits, pause/resume, and capacity changes
cooperate with it. Direct lower-level writes bypass these guarantees and are
reserved for recovery while admission is closed.

The default ceiling is eight owned beads across every project. Review, CI,
promotion, and synchronization retain the parent's slot. Lowering a configured
limit does not cancel existing work; it prevents new claims until work drains.

The critical section is small:

```text
acquire admission lock
reject closed admission or an existing claim by this worker
read occupied ownership and the current capacity limit
check project, state, pause conditions, and prerequisite outcomes
atomically assign the bead and its initial phase to this task and turn
release lock and return Claimed, or a specific refusal
```

Keep the lock on a stable local path outside synchronized data; never unlink its
live file. Process death releases the kernel lock. Acquire it with a bounded
deadline and return `Busy` instead of retrying forever. A two-second failure
deadline does not replace the sub-100ms successful-operation target.

Never hold it during model work, resource sampling, Codex requests, Git,
worktree creation, CI waits, or backup. Reads and independent task text or
priority edits use native atomic operations without the admission lock when they
cannot affect eligibility or ownership.

Choose next work by native priority P0 through P4, then oldest creation time,
then ID. Default new tasks to P2 unless their impact warrants another priority.
Workers can propose a different eligible bead based on scope or resources; the
claim still checks every invariant. Pagination must not hide eligible work
behind an unsuitable first page.

A lost claim response is resolved by inspecting the bead and this worker's
ownership, not claiming a second bead. Racing recruitment may create extra
conversations, but only eight beads can be owned. If native task creation has an
unknown outcome, inspect it before retrying instead of spawning duplicates.

Resource snapshots include active phases, recent CPU and memory pressure, and
available Tollgate queue information with timestamps. Missing metrics are
unknown, not zero. These observations guide judgment outside the critical
section; admission does not perform an expensive full-host scan.

## Delivery and continuation

Project-file changes use isolated Tollgate worktrees based on its promoted
release. Stop workspace-rooted background processes before promotion because
Tollgate may remove the source workspace afterward. Preserve the exact reviewed
source and native candidate identity for delivery and recovery.

The ordinary interaction is:

```text
review committed source with a fresh warden
submit that source as a Tollgate candidate
authorize delivery within the bead's approved scope
block on candidate-specific delivery
finish configured synchronization, then complete the bead
```

Use Tollgate's native results. Hive does not issue another pass certificate,
store proof fingerprints, or reconstruct validation from log text. A candidate
must belong to the selected project and submitted source before Hive accepts its
result. Retain meaningful distinctions between validation success, promotion,
and synchronization.

In-scope CI failures stay with the executor. Repair conflicts against the
current promoted release, review source changes, and submit the replacement
candidate. File pre-existing failures separately; if they block delivery,
checkpoint and defer with a repair dependency instead of bypassing CI. Missing
provider evidence remains unresolved rather than being inferred as success.

External research or document artifacts that do not change project files still
use admission, fresh review, and the completion checklist. Record their artifact
location and acceptance result instead of inventing a Tollgate candidate.

### Blocking delivery interface

Provide a foreground CLI wait and a thin MCP `wait_for_delivery` operation. Each
call uses Tollgate's candidate-specific blocking wait and returns a terminal
outcome or explicit interruption. Native status events can be consumed inside
the adapter; the model must not poll for updates.

- Distinguish delivered, validation failure, conflict, cancellation, timeout,
  provider error, and unknown outcome.
- Hold no admission lock during the wait.
- Retain candidate and ownership on timeout or disconnection. Inspect native
  state before reattaching a wait or attempting a repair.
- Canceling a client wait does not cancel the provider's candidate.
- Support a one-hour default wait, with a larger configured MCP timeout and an
  invocation wrapper that does not force repeated model continuations.

The five-minute synthetic experiment demonstrates feasibility only. Acceptance
requires a real 30-minute CI wait, no intermediate model polling, working
interruption, retained capacity, and no archival of the active conversation.

### Completion checklist and stop hook

The executor's skill performs the checklist; the core does not persist a review
receipt or checklist proof:

- Confirm scope, intended behavior, and proportionate validation.
- Address review findings or explain disagreements.
- Confirm promotion and configured synchronization, or artifact acceptance.
- File or link follow-ups for pre-existing defects, tooling failures or
  slowness, CI failures or slowness, and architectural debt.
- Preserve useful source documentation and invariant updates.
- Record the outcome, settle writers, close the bead, and try the next claim.

Search before filing follow-ups. Empty categories do not require invented work.
A small set of useful beads is preferable to duplicate observations scattered
through a large backlog.

A stop hook gives a single reminder when an executor ends normally despite
eligible work in its project. It never launches another conversation. Record
that the reminder was emitted for this native turn and completion boundary so
repeated callbacks cannot loop. Completing another bead permits one new
reminder. Guard uncertainty allows stopping rather than repeated nagging.

Explicit user stops, emergency suspension, blocked work, capacity exhaustion, or
resource-based refusal allow stopping. The executor states the reason. Nothing
periodically revives executors after all have stopped.

## Pauses and recovery

An explicit user stop pauses the bead until the user resumes it. Record the
pause promptly through a native interruption callback when available. If the
runtime cannot distinguish a user stop from a crash, preserve uncertainty;
silence is not permission to restart.

A pause does not undo already-authorized delivery. Inspect the provider and use
supported cancellation where appropriate, but report promotion that actually
occurred. Preserve the pause even after recording an observed delivery result.
On resumption, finish synchronization and the checklist without implementing the
same change again.

### Recovering abandoned ownership

Peers may investigate abandoned work at normal work boundaries, including when
capacity appears full. There is no expiring lease or scheduled recovery leader.
Before releasing ownership, establish all of the following:

- The recorded native turn ended and no replacement execution or continuation
  is running or queued.
- Its write-capable commands and descendants have stopped.
- The retained workspace and any external action have been inspected.
- Interruption cause is understood; explicit pauses remain protected.

Timestamps, archived status, a missing transcript, or a lost connection are
insufficient. Unknown activity means no takeover. Escalate to justiciar when
writers or provider state cannot be established. Workers must not detach
write-capable work from inspectable native sessions.

Native activity inspection returns active, ended, queued, or unknown with the
identities observed. Writer inspection covers associated command sessions, child
reviews, descendants, and workspace writers. Process identity must include
enough native information to distinguish PID reuse. Prove that the deployed
Codex adapter can supply these distinctions before enabling recovery; missing
capabilities disable that path visibly.

Recovery observes outside the admission lock, then conditionally settles the
exact old task-and-turn owner under the lock. If ownership or relevant state
changed, reject the stale observation. Clearing ownership queues ordinary
abandoned work but leaves deferred work and its pause conditions deferred.
Settlement itself never grants implementation authority; continuation uses
normal admission.

A pending candidate retains the old owner's slot even after its executor ends. A
peer can attach a blocking wait without claiming or editing. Only after local
writers and provider work settle may it release the old owner. At capacity this
means eight occupied slots remain eight during CI, then seven after settlement,
then eight after a successful new claim.

Same-conversation reentry is different: once the old turn's local writers stop
and interruption is resolved, a new turn may conditionally replace its owning
turn ID while preserving phase, candidate, and the occupied slot. During pending
delivery this authorizes observation and waiting only. A deferred bead cannot
use reentry to erase its pause.

Every returning executor validates current ownership before editing. An old
turn's delayed stop or completion cannot overwrite a newly admitted owner.
Unrelated input to a paused conversation permits answering, not implementation.
Explicit resumption resolves the user pause; remaining conditions and settled
ownership still precede fresh admission.

### Uncertain external effects

Beads and Tollgate are not one transaction. Record the intended branch before
worktree creation and retain the native source commit before submission. Resolve
lost responses through Tollgate's inventory and candidate state. Do not blindly
create replacement worktrees or submit duplicate candidates.

Retain checkpoints through deferral and recovery. Adopt a workspace only after
confirming quiescence and claiming continuation. An already-promoted candidate
needs any remaining synchronization and completion, not repeated source edits.
Unknown external effects retain ownership or a visible recovery blocker until
inspection establishes what happened.

## Task names and archival

Invoking any Hive role enrolls the conversation for naming and archiving, even
when the user created it. Persist enrollment by native task identity in Beads;
do not infer management from an emoji. Changing roles preserves enrollment.

Names show the role emoji, current bead when present, and a clear subject:

```text
⚒️ [hv-fg3] Search indexing · implementing
🛡️ [hv-fg3] Review search architecture
⚒️ [hv-fg3] Search indexing · waiting for CI
🧵 Search redesign · clarifying scope
📿 [hv-m8k] Record stale cache failure
```

Update names on bead or role changes, review, meaningful waits, pause, recovery,
and completion. The parent uses the warden symbol while its cold reviewer runs,
then returns to executor. A `$bead` reply preserves the enclosing role. Do not
retain a completed bead ID when starting unrelated work.

Renaming is mandatory workflow behavior, but a failed rename cannot strand
execution. Retry once, report desired versus observed name as drift, continue,
and correct it at the next meaningful transition. Avoid renaming after every
command.

Archivist runs explicitly or on a configurable schedule, initially every five
minutes. It archives only enrolled conversations with more than 15 minutes
without input or output and no active turn. A pending CI tool call remains an
active turn regardless of elapsed time. Use native activity, including tool
activity; bead edits and title timestamps are not substitutes.

Before archiving:

- Recheck activity and active-turn state. Unknown activity is ineligible.
- Check affected descendants; active or unknown children prevent parent
  archival when the native operation would archive them too.
- Exclude the running archivist and unregistered conversations.
- Respect manual unarchive by exempting the conversation until the user opts
  it back in. Track Hive archive actions to distinguish its own race repairs.

Use native conditional archival when available. Otherwise immediately reread
activity and unarchive if new activity raced with the request, including
indirectly affected descendants. This is eventual UI repair, not atomic
exclusion. Never stop a turn to make it archivable. Archive operations leave
bead ownership and outcomes unchanged; archival is not cancellation or recovery.

## Observability and API-equivalent cost

An independent background collector reads native activity and transcripts
incrementally into a local telemetry store. It retains offsets, consumes bounded
chunks, tolerates incomplete trailing records, and avoids duplicate ingestion
using native record identity. It never claims beads or restarts workers.

Collector failure makes reports stale or incomplete without blocking execution.
Hooks emit only small identity and activity records; they must not reparse
entire conversations before each operation. Bound CPU, memory, and backlog
growth and expose collector lag.

Traces associate bead, role, native conversation, model response, command, and
candidate where known. Report model activity, tool duration, CI waiting,
integration, startup, source selection, Beads work, and lock waiting.
Overlapping spans are not added as if they were sequential wall time.

```text
hive status --project search
hive trace hv-fg3
hive cost hv-fg3
hive cost --project search --group-by role
```

Cost is an estimate of API-equivalent USD usage, not subscription billing.
Retain model, service tier when known, input/output and cached-token counts, and
the price schedule used. Unknown models or prices remain unpriced rather than
zero.

Attribute a response to its bead and role when that response started. Keep
unassigned scoping and setup visible. Count a cold review once in its parent
bead's cost; recruited executors charge their own beads. Do not invent a token
split when a response spans a role transition.

Every report exposes freshness, missing usage, unpriced usage, and partial
coverage. Observed zero is distinct from missing data. Default retention is 30
days of detailed telemetry and compact per-bead aggregates until explicit
pruning. The telemetry store is disposable derived data, never task authority.

## Typed interfaces and live updates

The CLI and MCP adapters share a typed implementation. CLI output is concise
text for people or structured JSON for tools. MCP exposes operations that
benefit from direct tool invocation, especially blocking waits, without owning
business policy or a work queue.

The minimum proposed surface is:

```text
hive task add --project search --title "Repair stale index"
hive task show hv-fg3
hive task claim hv-fg3 --owner <native-task-id>
hive task next --project search --owner <native-task-id>
hive task defer hv-fg3 --reason user-pause
hive task resume hv-fg3
hive delivery wait <candidate-id>
hive task complete hv-fg3 --candidate <candidate-id>
```

Also provide priority and dependency edits, phase transitions, outcome
recording, recovery inspection and settlement, turn entry, status, trace, and
cost reporting. Read-only ready lists are advisory; only claim grants ownership.
The adapter obtains the invoking native turn and validates it alongside the
conversation. A bare task ID is insufficient for owner mutations.

Core signatures make the important boundaries explicit:

```text
claim(project, bead, owner) -> ClaimResult
transition(bead, expected_owner, event) -> TransitionResult
enter_turn(bead, task, turn) -> Entered | Paused | EntryRefused
inspect_activity(owner) -> KnownActivity | UnknownActivity
settle_owner(bead, expected_owner, observer) -> SettlementResult
wait_for_delivery(project, source, candidate) -> DeliveryResult
cost(scope) -> CostReport
```

Results distinguish `Claimed`, `CapacityFull`, `DependencyBlocked`, `Paused`,
`Busy`, `NoReadyWork`, `OwnershipChanged`, `ProviderUnavailable`, and
`RecoveryRequired`. Settlement distinguishes settled, still active, unknown, and
changed ownership. Errors include what is known and the next useful action;
callers do not parse prose to infer status.

### Domain types

Confine JSON, subprocess output, and external APIs to validated boundaries.
Accept dynamic input as `object`, validate it, and expose trustworthy domain
values internally. Do not spread `Any`, unchecked casts, or broad type-checker
suppressions through business logic.

Use distinct types for bead IDs, project IDs, native task and turn IDs,
candidate IDs, and paths whose confusion would cause errors. Retain native Git
commit identity for source integration; do not invent another fingerprint.

```python
@dataclass(frozen=True)
class Owner:
    task: CodexTaskId
    turn: CodexTurnId

@dataclass(frozen=True)
class WaitingForDelivery:
    owner: Owner
    workspace: WorktreePath
    source: SourceCommitId
    candidate: TollgateCandidateId
```

Represent queued, owned, deferred, and terminal states as immutable
alternatives. Use phase variants requiring their resources rather than optional
fields and conventions. A deferred value retains its checkpoint and unresolved
conditions; a terminal value requires an outcome. Artifact delivery has an
explicit artifact outcome rather than a nullable candidate pretending to be code
delivery.

Pure transition functions return a new valid state or a typed refusal.
Persistence and external effects remain explicit. Strict checking and exhaustive
handling prevent category mistakes; real multi-process tests validate ordering,
locking, and crash behavior. Neither types nor tests replace the other.

### Source selection and connection continuity

Each invocation resolves committed local `master` in `~/hive` before importing
application code. Prepare an immutable source snapshot automatically when needed
and reuse its dependency environment for ordinary source edits. Keep one
selected source for the invocation's imports and assets.

- New invocations use current local master without fetching a remote.
- Existing operations retain consistent source, including long waits and
  delayed imports. A moving source symlink cannot supply their imports.
- Skills link directly to source; subsequent reads see edits immediately.
- Broken preparation fails visibly, never silently reverting to older behavior.
- Ordinary updates require no installation, activation, or restart.

The collector and MCP transport retain only the continuity they need; bounded
parsing and application work use fresh CLI code. Persistent native connections
preserve subscriptions and pending requests during source changes. No resident
component acquires scheduling authority.

Dependency or incompatible state changes use explicit maintenance. A local
durable admission stop and a shared/exclusive mutation guard let short mutations
finish before conversion. Mutating invocations acquire the shared guard before
selecting source and retain it through their protected storage calls. An
invocation that released the guard for outside work must reacquire it and check
that its source is still current before writing; otherwise it refuses and
requires a fresh invocation. This prevents pre-maintenance code from writing
converted state. Backup, transform, and validate state before reopening; failure
leaves the stop visible for repair. Take this guard before admission's lock.
Both guards cover the Beads calls they protect; neither spans unrelated Codex,
Git, Tollgate, or backup calls. Existing waits return observations using their
pinned source, then a new invocation validates and applies any resulting
mutation using current code. An old wait never imports new code into its running
process. Do not build compatibility readers or numbered schema chains into
ordinary operation.

## Backup and cutover

Back up to a configured GitHub repository periodically, initially every 15
minutes when changes exist. A deterministic host timer runs backup independently
of executors. Codex scheduling may invoke archivist and optional warden/sage
audits. None of these jobs monitors stuck work or restarts executors.

Export a consistent snapshot through supported Beads facilities, including
issues, dependencies, comments, metadata, and infrastructure records. Commit and
publish it to a dedicated backup repository. Do not copy live database files or
inherit an unrelated parent Git remote. An issue export restores task intent;
use native database backup for maintenance requiring full database restoration.

Backup serializes with other backups, not ordinary task mutations. It records
snapshot time and last successful publication, retains a local result on network
failure, and retries on the next interval or explicit invocation. No force push
or automatic import of remote edits into live ownership is needed.

Snapshot consistency during concurrent writes must be demonstrated. If a
supported exporter lacks that guarantee, use a supported transactional read or
native snapshot mechanism. Do not hold the admission lock through a large
export. Backup failures leave task operations available and backup age visible.

For cutover, preserve Fulcrum's database, configuration, and running work.
Create fresh Hive state under `~/brain` with explicit database routing and `hv-`
IDs. Inspect existing Git context before configuring backup; the retained design
identified the home-directory dotfiles repository as a hazard here.

Deliberately refile selected unfinished intent and recreate dependencies using
newly allocated IDs. Link original records for context. Do not import runtime
ownership, active assignments, receipts, or historical agents. Settle or
explicitly suspend the old work before making its replacement eligible, avoiding
duplicate execution across systems.

During coexistence, use explicit Hive skill paths. Rebind overlapping skill
names only after old assignments are completed or retired so a live Fulcrum
worker cannot load a different workflow midway through its work. There is no
compatibility mode translating old protocols.

Restore also begins with admission closed. Reconcile retained owners, writers,
and candidates before resuming. Historical ownership is context, not permission
to resume a process or claim that it has stopped.

## Validation and performance

Exercise the assembled product early at the real integration boundary. In a
disposable registered project, create two dependent beads and start a real Codex
executor. Observe its naming, Beads claim, Tollgate worktree, fresh warden
review, blocking delivery, promotion, synchronization, and follow-on claim of
the newly unblocked bead. Inspect the trace and cost coverage. Component
fixtures cannot substitute for this interaction.

Use a small set of meaningful black-box scenarios with real temporary Beads
server databases and independent processes. Check observable behavior instead of
duplicating implementation details or asserting arbitrary wording.

Required behavioral checks include competing direct/next claims, capacity
exhaustion, blocked dependencies, cancelled prerequisites, dependency-edit
races, interrupted mutations, lost responses, abandoned owners, user pauses,
review at full capacity, CI failures, title drift, archival races, collector
outages, backup failures, and hot reload during a wait.

### Latency acceptance

For ordinary reads, ready queries, creation, updates, and successful claims,
measure each operation end to end from process startup to parsed result. Include
source selection, imports, boundary validation, lock waiting, Beads work, and
normal observability overhead.

The acceptance target is **p95 below 100ms at eight concurrent clients**, using
1,000 unfinished beads with realistic dependencies and substantial completed
history. Report every operation separately with enough repeated samples for a
tail estimate, sample counts, host load, and error rates.

- Run the collector and periodic backup during representative measurement.
- Measure synchronized contention bursts separately at one, eight, and sixteen
  clients, including denied claims, lock timeouts, and saturation throughput.
- Characterize 100 and 10,000 unfinished beads and increasing completed history
  without quietly changing the primary acceptance workload.
- Report cold source preparation separately, targeting p95 below one second
  with unchanged dependencies. Do not hide repeated preparation from ordinary
  command startup measurements.
- Remote publication, Codex rename latency, and CI duration have separate
  workflow measurements; local commands cannot secretly await them.

The September 19 experiment used Beads 1.2.2 and Dolt 2.2.0. With 1,000 tasks,
single-client server reads were approximately 73–91ms p95 and independent
updates 92ms. Four-client updates reached 220ms, sixteen-client updates 986ms,
and locked ready-claim bursts roughly 471ms and 1,929ms respectively. Small
samples, synthetic data, and host load limit generalization. The exact Hive
eight-client workload was not measured.

Those results support server mode but do not establish the target. Native server
ready-claim bursts also had serialization failures; the tested local lock
removed those failures at a latency cost. Profile actual startup, calls, and
contention before making performance claims. A missed target requires
optimization or an explicit requirement change, not database-only timings or an
average that hides slow claims.

Also compare representative Hive tasks with direct agents using the same model,
review policy, and Tollgate checks. Report added wall time and API-equivalent
cost for scoping, naming, filing, and follow-ups. Fast commands alone do not
prove that orchestration has low total overhead.

## Manual QA

Use disposable projects and an isolated Hive database for destructive cases.
Seed dependencies, expose a configurable capacity limit, and provide controlled
CI outcomes plus readable status, trace, and cost output.

1. **Assembled workflow:** deliver the first of two dependent beads through real
   Codex, Beads, cold review, and Tollgate. Verify names, isolated workspaces,
   promotion, synchronization, closure, and the same executor's next claim.
2. **Admission races:** race direct and next claims against the same bead and
   the final free slot. Verify unique ownership and no more than eight owned
   beads. Ensure filing still works at capacity.
3. **Dependencies:** attempt direct claims on blocked and cancelled-prerequisite
   work. Race dependency edits with claims, reject cycles, and verify an
   executor does not silently cross projects to resolve a blocker.
4. **Interrupted mutations:** kill a lock holder and lose a write response.
   Verify lock release without assuming rollback, resolve any server-side work
   still running, and prevent competing claims, pauses, settlement, and
   dependency edits from overwriting the uncertainty marker. Interrupt
   prerequisite filing between steps; the parent must remain safely deferred.
5. **Full-capacity review:** occupy eight beads including CI waits and run cold
   reviews without extra slots. Have all owners discover prerequisites; settled
   parents must release capacity so useful prerequisite work can start.
6. **Delivery failures:** exercise in-scope CI repair, pre-existing blockers,
   source-changing conflicts, wrong-project candidates, failed synchronization,
   and lost submission responses. Verify honest state and no blind duplication.
7. **Long wait and live update:** hold real CI for at least 30 minutes. Verify
   one blocking call, no model polling, retained capacity, and no archival.
   Commit an ordinary Hive update; new commands use it while the old wait keeps
   consistent code and connections. Broken new code fails visibly.
   Suspend an invocation around source selection and mutation-guard acquisition
   during maintenance; old code must never write converted state.
8. **Pauses:** explicitly stop during editing and already-authorized delivery.
   Preserve the pause even if promotion finishes. Unrelated input permits
   questions only; resumption resolves its condition without clearing another
   approval requirement or repeating delivered work.
9. **Abandoned ownership:** terminate a worker with and without surviving
   writers. Recover only the settled case. Pending CI retains its slot until
   provider settlement; peers can wait but cannot edit. Race recovery with a
   resumed turn and verify stale stop/completion callbacks cannot mutate it.
10. **Recruitment and continuation:** recruit independent executors and stop
    their recruiter. Peers continue with separate worktrees and shared capacity.
    Exercise duplicate stop callbacks: one reminder is emitted, explicit stops
    are respected, and no scheduler revives work after every executor exits.
11. **Specialists and filing:** exercise all eight role invocations, design
    approval, a read-only vizier, a missing invariant document, review
    disagreements, and brittle tests. Standalone `$bead` files and exits; a
    `$bead` reply preserves the enclosing role. Implementation transitions use
    normal admission.
12. **Emergency recovery:** break admission or Tollgate. A nondelegating
    justiciar stops damage, records any bypass, restores valid ownership, and
    preserves user pauses before reopening work.
13. **Task UI and archival:** fail a rename, observe bounded retry and drift,
    then correct it at a transition. Race new input with archival, include an
    active descendant, and test manual unarchive. UI repair changes no task
    ownership or outcome.
14. **Observability:** stop the collector, include incomplete records and an
    unpriced model, then restart it. Verify continued execution, visible gaps,
    incremental catch-up, and review usage counted once. Missing data is never
    shown as zero cost.
15. **Backup and cutover:** export during mutations, interrupt remote access,
    and restore a snapshot into a disposable store. Verify consistent edges,
    visible backup age, ownership reconciliation, intact Fulcrum state, and no
    publication of unrelated files or migration of active ownership.
16. **Performance:** run the eight-client workload with observation and backup
    enabled. Report per-operation p95, startup and lock costs, failures, and
    separate contention bursts. Record any target miss without substituting a
    narrower measurement.
