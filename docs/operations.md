# Fulcrum Scheduling and Operational Recovery

This appendix specifies how **Fulcrum**, the local Codex coordination system,
schedules work and recovers failures. The **Archon** is its sole current
strategic coordinator. **Tollgate** supplies CI and certified Git promotion;
the **brain** stores private tasks and documents. Local JSON records current
coordination state.

## Related Information

These contracts supply the identities and evidence used by the policies below:

- [Main design](technical-design.md): roles and intended behavior.
- [Data contracts](contracts.md): ownership, assignments, messages, and
  persistence.
- [Dashboard](dashboard.md): operational visibility and process serving.
- [Hooks](hooks.md): compaction refresh and limited handoff reminders.
- [wt][wt]: normal worktree ownership, evidence, and promotion lifecycle.
- [implement-plan][implement]: the paired-agent workflow being adapted.
- [Tollgate README][tg-readme] and [technical design][tg-design]: supported
  queue, diagnosis, resource, and promotion behavior.
- [Codex task scheduling][schedules] and [app-server][app-server]: external
  integration capabilities and constraints.
- [Beads server operation][beads-server]: Beads-managed local server and backups.

[wt]: /Users/dthurn/.llms/skills/wt/SKILL.md
[implement]: /Users/dthurn/.llms/skills/implement-plan/SKILL.md
[tg-readme]: /Users/dthurn/tollgate/README.md
[tg-design]: /Users/dthurn/tollgate/docs/technical-design.md
[schedules]: https://learn.chatgpt.com/docs/automations?surface=app
[app-server]: https://learn.chatgpt.com/docs/app-server
[beads-server]: https://github.com/gastownhall/beads/blob/main/docs/architecture/dolt.md

## Scheduling Decisions

The Archon schedules in response to ready work, completion, escalation,
human direction, and Watchman reports. It reads compact structured summaries
and project memory, delegating detailed source investigation when needed.

The Archon checks dependencies, holds, existing assignments, and available
capacity before dispatch. Helpers can collect these facts, but V1 does not add
a separate scheduling service. Overseers check the same constraints before
starting another bead in their run. These remain agent responsibilities;
there is no default pre-tool policy layer.

The Archon considers candidates in this order:

1. Active incident recovery or an explicit human priority instruction.
2. Work required to unblock active assignments.
3. Ready activated work ordered by Beads priority and dependency impact.
4. Routine improvements and recurring analysis, with aging to avoid starvation.

Within a priority class, consider dependency impact and how long work has
waited. The Archon may depart from this order for a named
reason such as a large conflicting refactor. Record that reason with the
affected scheduling decision rather than inventing an invisible priority.

One pair executes one assignment at a time. Parallelism comes from independent
pairs. The Archon can start several plan runs when their code interactions and
resource demands allow. It does not split one active pair into concurrent
Executors without creating explicit independent runs and ownership.

### Dependencies and holds

A **hold** is a durable restriction with a scope, reason, owner, and release
condition. Scope can be the fleet, host, project, plan, or assignment.

- Task dependencies use Beads edges; plan dependencies use plan metadata.
- Conditional restrictions such as “until the benchmark finishes” use holds
  referencing an observable run or assignment outcome.
- Human indefinite holds require explicit release; an agent cannot expire them
  merely because work has waited a long time.
- Automatically releasable holds name the exact completion evidence. A failed
  or canceled prerequisite does not satisfy a successful-completion condition.
- Multiple holds compose: satisfying one does not release the others.
- Recovery work may receive an explicit exception to a broad pause. The
  exception names the repair scope and permitted resource use.

For example:

```text
Hold: battlement-layout-refactor
Scope: other Battlement implementation runs
Reason: shared component contracts will change across many files
Release: required refactor beads promoted and remotely synchronized
Exception: startup-crash incident repair approved by current Archon
```

The dashboard shows why a ready issue cannot start and which actor or outcome
can release it. “Ready in Beads” and “eligible for Fulcrum dispatch” are
distinct.

## Resource Management

The goal is maximum useful throughput, not the maximum number of busy agents.
The Archon considers both Tollgate jobs and heavy commands launched directly
by Executors. It uses host observations and project memory to decide how many
runs can make progress together.

- Read CPU use, memory pressure, relevant process counts, and Tollgate queue
  state before dispatching resource-intensive work.
- Give each pair an initial expectation for heavy work, such as “one native
  rebuild at a time on this host.” Record exclusive reservations with holds.
- Before starting a heavy command outside that expectation, the Executor asks
  its Overseer; the Overseer coordinates capacity with the Archon.
- Lightweight inspection, editing, and planning may continue while build
  capacity is occupied, unless an explicit pause includes them.
- When contention causes repeated failures or slower throughput, reduce
  concurrency and dispatch improvements to the affected tooling.

Tollgate keeps ownership of its own queue and resource admission. Fulcrum does
not duplicate it with per-command permits or a second process supervisor.
Executors retain the process identities needed to stop their own commands and
demo services under `$wt`. A process is finished when actual observations show
it has exited, not merely because an expected duration elapsed.

### Learning without memory bloat

Raw measurements remain operational evidence. High-level memory stores compact
conclusions that are useful for future scheduling.

```text
Battlement validation: full native rebuilds contend heavily with Tollgate.
Prefer one full native rebuild at a time until measurements improve.
Evidence: recent run IDs; last checked: <date>.
```

The Sage challenges stale conclusions and identifies tooling changes that
could improve throughput. Resource observations alone do not authorize new
product scope or automatically turn every optimization into required work.

## Pausing and Resuming

A pause is a cooperative, verified transition. Sending a message is not proof
that active work or resource use has stopped.

The Archon first records the hold and stops new dispatch. It sends scoped pause
instructions through each Overseer, naming urgency and any permitted recovery
work. Executors then:

1. Stop expanding the assignment and reach the next safe command boundary.
2. Preserve intended changes and record owned dirty paths, commits, candidates,
   commands, and live runtime resources.
3. Cancel or reconcile active candidates when the pause must prevent promotion.
4. Stop relevant owned background processes and report checkpoint evidence.

Use `$wt`'s preservation targets: inventory within five minutes and preserve
intended changes in local Git within ten minutes, without waiting for broad
validation. An unvalidated checkpoint is explicitly marked and is not a
promotable candidate.

For a host-wide quiet interval, the Archon also invokes supported Tollgate
pause controls. Tollgate pause prevents new dispatch and promotion while active
commands drain; it is not equivalent to force-killing a running build. Only
declare the host quiet after the affected processes have actually drained or
their exact owned executions have been explicitly canceled and reconciled.

If pause acknowledgment is late, the Archon investigates the specific agent or
process. It does not start a benchmark on the assumption that the pause worked.
An unavailable interrupt API is reported as a capability limitation; metadata
cannot stop a process by itself.

Resume removes only satisfied holds. The Executor rechecks its contract,
worktree identity, candidate state, and certified base before continuing. An
unvalidated checkpoint must undergo normal checks before candidate submission.

## Review and Certified Promotion

The normal execution contract follows `$wt` with explicit Fulcrum adaptations.
These rules apply to software changes, including changes to Fulcrum itself.

### Worktree and candidate ownership

The Executor creates each new assignment worktree through
`tg --no-launch worktree create` after checking registration and health. The
base must be the captured certified `release` OID. Once implementation starts,
investigation and validation use that worktree, not the mutable primary tree.

A **source OID** identifies the Executor's immutable commit. A **tested OID**
identifies the prospective integrated commit Tollgate actually validates; it
can differ from the source when earlier queued changes must precede it. The
**queue revision** identifies the queue ordering observed with that candidate.
These values must not be substituted for one another.

- Worktrees belong to their creating Executor task. Names do not transfer
  ownership, and another Executor cannot silently take over a retained tree.
- Follow-up changes remain in the same owned tree until promotion is resolved.
- Complete proportionate local validation and review artifacts before freezing
  a candidate. Do not create extensive unit tests for trivial reversible edits.
- Submit the exact clean commit as an unauthorized immutable candidate.
- Immediately hand off the candidate and review artifacts to the Overseer;
  do not wait for speculative CI results before requesting review.
- Include the exact candidate ID, full source OID, tested OID when available,
  queue revision, and branch/base identity.

Visual changes also include a focused native or browser walkthrough and retained
screenshots as appropriate. The Executor owns cleanup of any demo processes.

### Review and authority

The Overseer reviews the exact source against its approved contract. Requests
for missing evidence or duplicate delivery do not increment the review count.
Substantive assessment of a new submitted version does.

- Satisfied review produces an explicit mandate for that assignment and exact
  candidate, including permitted replacement scope.
- Unsatisfied reviews one and two produce concrete prioritized feedback.
- A third unsatisfied review produces escalation to the Archon, not authority
  to promote by default.
- The Archon may narrow an ambiguous assignment, provide expert investigation,
  or upgrade the Executor to Sol. Material product changes still need human
  authorization or an approved plan revision.
- After escalation, the Archon chooses and records the next approach. Preserve
  review history rather than restarting the same unsuccessful loop.

The Overseer owns review and the mandate; Tollgate owns certification.
A hook-generated reminder grants no new human approval.

The mandate covers the reviewed intent, ordinary merge-conflict repair, and
bounded in-scope CI repairs. A materially changed contract requires renewed
Overseer review and appropriate plan authority. Silence never grants a mandate.

### Tollgate lifecycle

The Executor validates the immutable candidate against its clean worktree,
stops worktree-rooted runtime processes, authorizes that exact candidate, and
waits for certification and promotion through Tollgate.

```text
local checks complete; clean source commit retained
unauthorized candidate submitted; Overseer review requested
Overseer grants exact scope-bound mandate
Executor authorizes candidate; Tollgate validates and promotes
Executor verifies source push and cleanup; bead may close
```

Tollgate owns queue ordering, speculative reconstruction, evidence reuse,
promotion to local `release`, and configured remote synchronization. Executors
do not manually move `release`, cherry-pick to it, or push worktree branches.
They report whether prior evidence was reused or a new CI run was required.

For example, source B may be tested after queued source A as the prospective
commit `release + A + B`. If A lands exactly as tested and the other validation
inputs remain valid, Tollgate may reuse B's result. If A changes or is removed,
Tollgate reconstructs and validates the affected result. Fulcrum observes this
decision instead of deciding that two apparently similar source diffs must
share a certificate.

If repository automatic source pushing is disabled, the Executor uses
`tg --no-launch push` after certified promotion. Required source synchronization
must complete before the code bead closes. This is separate from pending
brain-data synchronization, which may lag without blocking the assignment.

The Executor does not manually update the user's primary checkout. Current
Tollgate policy may itself synchronize a clean user checkout; observe its
configured behavior and report any needs-attention result accurately.

After success, verify candidate terminal state, promoted OID, configured remote
tip, worktree cleanup, and owned runtime cleanup. A promoted change with failed
source push remains a promotion-recovery obligation, not an implementation redo.

## Getting Unblocked

An escalation is a claim supported by evidence, not permission to abandon work.
Executors escalate to Overseers; Overseers escalate to the Archon. A role
records its expected next actor and may end its turn after successful sending.
A stop hook can remind the pair to check a handoff once. It trusts existing
progress and does not independently verify delivery. If messaging still fails,
retain the error and unresolved progress for normal Watchman/Archon recovery.

Every escalation states the exact boundary, tool output or failure handle,
attempted recovery, plausible untried recovery, retained work, and requested
decision. The receiving role checks the actual state before accepting the
claim that work cannot continue.

The default order is:

1. Correct a misunderstood tool state, identity, or authorization boundary.
2. Use a documented operational recovery with preserved identities.
3. Assign a scoped specialist investigation or repair.
4. Change the approach or permitted model within existing scope.
5. Ask the human only when the Archon finds a genuine unresolved product,
   authority, credential, or external-state requirement.

### CI repair and investigation

For a candidate failure, invoke `tg diagnose` first and inspect retained
evidence. A structured repair is evaluated as a proposal, not blindly executed.

- Ordinary regressions stay in the Executor's original worktree.
- Permit at most one unchanged retry for a stated hypothesis or changed
  resource condition. A second failure at the same boundary ends blind retries.
- Allow fifteen further minutes of focused diagnosis at that boundary, then
  make a concrete repair, rollback, or evidence-bearing escalation.
- Source changes create a new exact candidate. The old mandate covers only
  replacements within its recorded scope.
- Suspected CI unreliability suspends the active assignment with preserved
  evidence and work. A same-project investigation uses a fresh owned worktree
  in the same Executor task, with a suspension stack for later resumption.
- A cross-project infrastructure fix receives a project-scoped pair, such as a
  Tollgate repair pair, while the original Executor retains its worktree.
- Resume the original assignment after the dependency is resolved. Do not
  reset review or failed-boundary history merely because an investigation ran.

A no-code investigation can resolve a false hypothesis with retained evidence
and clean candidate-free worktree removal. It does not need a fabricated commit
or candidate to count as completed investigation work.

### Lost agents and retained work

First try to resume or message the original task. If it is irrecoverable, the
Archon records its loss and authorizes replacement work with fresh ownership.

- A replacement Executor creates a new worktree; it never adopts the old one.
- Recover committed content from retained immutable Git objects where possible.
- The original Overseer may inspect retained work read-only and preserve a
  recovery artifact for a fresh owner. Never store source patches as brain
  project memory; retain an evidence reference to the actual artifact.
- Preserve the unavailable owner's dirty worktree until its recovery or
  explicit disposition is understood.
- Reconcile any candidate already promoted before deciding work must be redone.

An archival interview is different from replacement: the original task returns
only to answer questions, without reviving its implementation assignment.

## Tollgate Emergency Recovery

An **emergency repair authorization** is the Archon's narrow exception to
Tollgate-created worktrees when Tollgate cannot create the worktree needed to
repair itself. It grants no exception to certification when landing source.

First attempt supported operational recovery: inspect health and diagnostics,
restart the service, recover configuration where justified, or restore a
known certified installation. Preserve service state and evidence before any
replacement. Do not repeatedly retry commands whose boundary has not changed.

If this cannot restore worktree creation:

1. Record the outage, attempted recovery, emergency scope, repair owner,
   permitted runtime changes, and current certified release identity.
2. Dispatch a Tollgate-scoped repair pair. The Executor creates an ordinary Git
   worktree on a unique local branch from a verified certified commit.
3. Implement only the authorized recovery, retaining local checks, diffs,
   build evidence, and a rollback path to the prior installation.
4. The Overseer reviews the recovery before installing or running a provisional
   repaired Tollgate build against real service state. Any provisional build
   is explicitly unpromoted and must preserve certification checks.
5. Once service operation is restored, create a normal Tollgate worktree and
   bring the reviewed recovery commit into that fresh owned tree for normal
   candidate submission, certification, promotion, and source push.
6. Verify the installed service corresponds to the final certified version;
   clean emergency resources only after successful reconciliation.

The provisional installation exists to restore the gate, not to manufacture a
passing certificate. Never disable failing voting checks, falsify evidence,
rewrite service databases to assert success, or manually move certified refs.
If the repaired gate cannot establish valid evidence, preserve the repair and
escalate to the Archon for further recovery or human input.

This exception applies to Tollgate recovery only. Ordinary feature work cannot
use an outage as a reason to bypass its workflow. Fulcrum outages normally use
existing skills and direct use of the normal tools, with Tollgate still
providing worktree and promotion services.

## Recurring Work and Patrols

The Night Watchman is one persistent human-created task with one hourly Codex
scheduled wake. Create or update the existing schedule rather than adding
duplicates. All other recurring work is requested through its patrol.

At each patrol:

- Reconcile registered tasks, expected waits, pending creation, active jobs,
  candidates, and stale or contradictory observations.
- Check unexplained stops, failed handoffs, and unexpected archives using
  current progress and Codex state. Hook errors may help diagnosis, but a
  missing hook event does not establish inactivity or a missing message.
- Report pending Git or Beads pushes to the Archon for a normal retry.
- Identify due Sage and Inquisitor occurrences.
- Send the Archon a deduplicated report of anomalies and due work.
- Stay quiet to the human when the patrol finds no meaningful change or action.

The Archon creates the Sage and Inquisitor tasks. The Watchman reports due work
and the Archon records the resulting task ID, keeping dispatch in one place.

### Cadence and overlap

Record the next due time and active task for each recurring role and scope in
the Archon's local state. The Sage runs every 24 hours; each project's
Inquisitor runs every 24 hours, twelve hours offset. Choose the initial Sage
time during setup and store timestamps in UTC, displaying local time.

- Each Sage run covers fleet execution since the previous postmortem.
- Each enabled project gets a fresh Inquisitor task for its review.
- Do not dispatch another run for a role/scope that already has an active task.
- The Archon may delay analysis for incidents or holds, showing it as due.
- After downtime, run one catch-up review per scope rather than creating a
  backlog of missed daily jobs, then advance to the next future due time.
- Disabling a project prevents new reviews without erasing existing findings.

Local scheduled work depends on the Mac and Codex application being available.
An hourly patrol is not an independent availability monitor for a powered-off
machine. On recovery, reconcile before dispatching catch-up work.

### Sage interviews and findings

Agents report workflow friction at each handoff so postmortems do not depend
solely on recollection. The Sage combines those reports with available tool,
build, CI, and usage observations, clearly marking unavailable measurements.

Improving Fulcrum itself is a core Sage responsibility. She examines the cost
and usefulness of its skills, scripts, handoffs, review loops, state management,
and scheduling practices. Recommendations should remove unnecessary work and
make agents more efficient, including revising the workflows themselves. File
these as Fulcrum tasks with concrete expected benefits, and have the Archon
highlight their outcomes in `NEWS.md` and the dashboard. Include hook latency,
unnecessary reminder turns, stale briefings, forgotten constraints, and excess
context. Use existing logs first; add selective diagnostics only for a concrete
missing signal. See [Sage diagnostics](hooks.md#diagnostics-for-the-sage).

Before reopening an archived task, the Sage writes an active interview record
in her own local state with the task ID, Sage ID, postmortem run, and prior
archival state. She then unarchives it, sends a scoped interview request, and
expects a debrief. The interview does not authorize implementation, task
selection, or promotion.

The interview request establishes debrief-only scope. It does not reactivate
the completed implementation run or its stop reminder. After the response, the
Sage rearchives only tasks she reopened, then closes her interview record.
Interrupted interviews retain that record for the next patrol.

Allow one reminder on a later patrol for an unanswered interview. Then finish
the postmortem using existing evidence and record the missing response; an
unavailable interviewee must not stall the daily process indefinitely.

Each actionable finding includes:

- The recurring problem and evidence from specific runs or tool boundaries.
- Its effect on elapsed time, token use, correctness, or human intervention.
- A concrete change and acceptance criteria for verifying improvement.
- The affected project, suggested priority, and related existing beads.

Deduplicate by the underlying problem, not title wording. Update an existing
open bead with new evidence instead of filing the same problem every day.
The Archon may schedule these tasks, retain them for later, or reject a proposal
with a recorded reason. A postmortem cannot silently expand an active mandate.

### Inquisitor reviews

Each Inquisitor examines the entire codebase. This is explicitly not a review
of recent commits, and recently written code has no special priority. Choose
areas for their architectural importance and the scale of the problems they
contain: overloaded responsibilities, opportunities to remove complexity,
type-system modeling, repeated conditional logic, and boundary clarity.

Findings must describe the existing problem, a credible behavior-preserving
direction, affected interfaces, and validation expectations. A large file is
evidence to investigate, not sufficient justification for arbitrary splitting.
The Inquisitor files or updates beads and archives; it does not grant itself
implementation authority. No new findings is a valid result.

The Archon prominently reports meaningful refactors and architectural
improvements in `NEWS.md` and the dashboard, explaining what became simpler or
more reliable and linking to the relevant work.

## Compatibility and Setup

Setup verifies prerequisites and records capabilities before normal dispatch.
It must be repeatable without duplicating projects, schedules, or role tasks.

- Confirm a Git repository, saved Codex Project, and healthy Tollgate identity
  for each enabled software project.
- Configure one private brain remote and one Beads-managed local Dolt server
  for that brain. Use loopback TCP so Beads' automatic startup can manage it;
  no remote database host is required. Keep credentials outside tracked data.
- Pin compatible Beads/Dolt versions and use supported server commit/sync
  commands. Do not assume server-mode writes automatically create the history
  needed for a push; configure and verify explicit commit behavior.
- Verify Beads' automatic startup and its `bd dolt start`, `status`, and `stop`
  commands from the brain directory. Beads owns server identity, port selection,
  and logs. Do not add a database LaunchAgent, PID store, or restart supervisor.
- Discover Codex creation, messaging, listing, archival, and model capabilities.
- Record whether the installed runtime exposes read-only task observation.
- Install one Fulcrum hook source using Codex's review/trust mechanism. Verify
  desktop compaction refresh and bounded stop reminders, including their cost,
  as specified in [hook compatibility](hooks.md#performance-and-compatibility).
  A CLI feature flag alone is insufficient evidence of desktop coverage.
- Install the role skills and Python CLI with their own portable references.
  Personal reference-skill paths are not required runtime dependencies.
- Start the dashboard under macOS service management with an exact service
  identity, logs, readiness checks, and targeted restart/stop actions. Database
  lifecycle stays with Beads; it does not require a second service installer.
- Enroll the user-created Archon and Night Watchman and configure one patrol
  automation. Setup never manufactures substitute persistent human tasks.

The inspected CLI was `codex-cli 0.153.4`. It advertised an app-server proxy,
but its default control socket was absent. Treat this as evidence that runtime
observation needs capability detection, not as proof that creating a new
unrelated app-server will reveal existing desktop tasks.

The dashboard adapter may connect to a configured compatible existing app-server
through its supported local transport and use read-only methods. If it cannot
observe the registered desktop tasks, it reports unavailable runtime visibility.
Never scrape private Codex databases as an undocumented fallback or start a new
agent merely to populate a status card.

Model defaults use verified runtime identifiers. Upgrade policy applies to
delegated assignments, not silent changes to human-created task preferences.
Record user authorization separately from agent recommendations for Astra.

### Database lifecycle and recovery

Use Beads' local server mode for concurrent access to the single brain.
Embedded mode needs no separate process but permits only one writer; changing
to it would require reconsidering the fleet's concurrent access contract.
Server mode still runs entirely on the owner's Mac.

Let Beads start its server when needed. Inspect health with the installed
version's supported status/connectivity commands. For explicit maintenance or
recovery, use `bd --directory <brain> dolt stop` and
`bd --directory <brain> dolt start`, then verify connectivity and the intended
database. Coordinate a maintenance boundary before intentionally stopping a
database shared by active tasks. An Executor's ordinary worktree cleanup does
not stop the shared brain database.

Setup must exercise concurrent clients, automatic startup after a stopped or
failed server, and repeated startup without duplicate processes. Verify actual
behavior before claiming transparent recovery. If the installed version fails
these checks, diagnose its configuration/version through supported Beads tools;
report the failure rather than silently adding another lifecycle manager.
Fulcrum retains a concise health observation or recovery report, not a competing
source of truth for server PID files and ports.

### Self-updates and schema changes

Fulcrum's own changes pass through the same pair and Tollgate process. Restart
the dashboard from a certified version after successful promotion, preserving
the brain and local agent state. A dashboard update does not restart the shared
database. Report the served version explicitly.

- Validate local records on read and report incompatible formats clearly.
  A format change should preserve active assignments; use a small explicit
  conversion with a local backup when needed.
- Changed hook definitions may require renewed Codex trust. Verify coverage
  after updates and report skipped hooks; do not bypass the trust flow.
- Beads updates and migrations follow its supported version and backup process,
  using Beads' lifecycle commands when maintenance requires stopping/starting
  the server. Verify database identity and data afterward; do not concurrently
  migrate the shared database from several agents.
- Reload skills for newly dispatched work while recording the skill version
  used by existing runs. Material workflow-contract changes require explicit
  reconciliation before an active run adopts them.
- Tollgate self-updates follow its repository's installation policy, including
  verification that the installed service matches the certified version.

## Validation Policy

Fulcrum's Tollgate checks cover formatting, types, the dashboard build, and a
small number of meaningful behavior checks. Keep the suite proportionate.

- Python uses Black and Pyre.
- New dashboard TypeScript and retained upstream checked JavaScript pass type
  checks, Prettier, ESLint, and the Vite production build.
- Focus tests on consequential boundaries: reading real Beads data, excluding
  dashboard mutations, preserving complete JSON reads during replacement, and
  distinguishing unavailable runtime observations from idle tasks. Include the
  [hook checks](hooks.md#performance-and-compatibility) covering reminders,
  legitimate waits, Plan mode, and the cost of correction turns.
- Retain useful upstream tests without reproducing every wrapper or adding
  unit tests for straightforward documentation and state plumbing.

Integration fixtures use an isolated brain and disposable software project.
They do not dispatch production work or modify the human's real registry.
