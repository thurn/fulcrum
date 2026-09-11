# Fulcrum Data and Agent Contracts

Fulcrum keeps tasks in Beads, plans and high-level memory in Markdown, and
runtime coordination in local JSON. Each has a clear owner. Ordinary Git and
Beads operations save durable project knowledge; filesystem operations save
local agent state. Readers may observe these sources at slightly different
moments.

## Related Information

- [Main design](technical-design.md): purpose, roles, and project boundaries.
- [Operations](operations.md): scheduling, handoffs, and certified promotion.
- [Dashboard](dashboard.md): presentation of current data and source freshness.
- [Hooks](hooks.md): compaction refresh and bounded handoff reminders.
- [Beads synchronization][sync]: Git files and Dolt issue history.
- [Beads backend documentation][backend]: local database operating modes.

[sync]: https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md
[backend]: https://github.com/gastownhall/beads

## Data Ownership

The brain at `~/brain` is a private repository. Markdown and Beads history are
version controlled and pushed to its private remote. Local coordination JSON
is excluded from Git, whether stored alongside the brain or in Fulcrum's local
application data. Database working files, credentials, caches, and logs are
also excluded from ordinary Git commits.

| Information | Source of truth | Writer |
| --- | --- | --- |
| Plans and high-level decisions | Markdown | Weaver or human |
| Current summaries and NEWS | Markdown | Archon or human |
| Concise role/project lessons | Markdown | Relevant role or human |
| Issue scope, priority, dependencies, status | Beads | Responsible agent |
| Projects, active Archon, holds, run registry | Local JSON | Current Archon |
| Current pair assignment and review decision | Local JSON | Pair's Overseer |
| Executor phase, candidate, cleanup obligations | Local JSON | That Executor |
| Other agents' progress and expected handoffs | Local JSON | That agent |
| Active interviews and prior archival state | Local JSON | Sage conducting the interview |
| Optional hook diagnostics | Local logs | Hook helper |
| Actual task execution and conversation | Codex | Codex |
| Candidate, CI, certification, promotion | Tollgate | Tollgate |

Beads remains the task tracker. JSON adds details such as “waiting for review
from this Overseer”; it does not keep a separate editable copy of bead status.
The dashboard derives its cards from these sources. Hooks may add small
diagnostic log entries, but do not maintain delivery receipts or change
agent-owned state, issue status, or the Archon's registry.

## Projects and Task Identity

A registered project identifies its Git checkout, saved Codex Project, and
Tollgate registration. Host-local paths are paired with a host ID. V1 supports
one Mac; a path alone is not assumed to identify a future remote checkout.

An illustrative project record is:

```json
{
  "project_id": "battlement",
  "host_id": "local",
  "repo_path": "/Users/dthurn/battlement",
  "codex_project_id": "<actual saved project ID>",
  "tollgate_repo_id": "<actual registered repository ID>",
  "enabled": true
}
```

Use actual Codex task IDs for communication and lookup. Keep descriptive titles
for humans. Archon-created tasks also receive numbered tags such as `[sage-3]`
and `[inquisitor-2]`. Overseers and Executors share a pair number, such as
`[overseer-3]` and `[executor-3]`. The Archon chooses the next unused number
from existing registrations; numbers are not recycled.

- The Archon records role, project, host, actual task ID, title, model, and run.
- Human-created Weavers keep ordinary descriptive titles. They do not use
  numbered tags or defer a rename until after Plan mode.
- A Weaver in Plan mode reads available identity but writes no files until
  approved. It then writes its own role/progress record and reports its identity
  and completed work to the Archon, who updates the registry. Until then, hooks
  do not persist inferred role information during the planning interview.
- Use the real task ID returned by creation when available. If creation is
  pending, resolve it through supported task listing or completion results.
  A temporary `clientThreadId` must not be used as a routable task ID.
- A unique role tag is a fallback for discovery. If a create call has an
  uncertain outcome, inspect matching tasks before trying to create another.

Named implementation tasks start in the saved project's local context. Their
Executors create worktrees through `$wt`; Codex task creation must not also
create an unrelated worktree. A Sage may use Fulcrum's project context for
fleet analysis; an Inquisitor uses its reviewed project's context.

## Beads and Dependencies

Use one brain-wide Beads database with a required project label on each bead.
This supports cross-project dependencies without a second task graph. A plan
label groups work, and a relative Markdown link supplies its design context.
Use Beads' existing issue types, priorities, dependencies, and lifecycle.
For example, labels `project:fulcrum` and `plan:review-handoffs` identify a
bead's project and optional plan.

Each implementation bead contains:

- A concrete problem, intended outcome, and bounded scope.
- Project and plan references, when a plan exists.
- Constraints, dependencies, and relevant code-repository documentation.
- Acceptance criteria and proportionate validation expectations.
- Explicit human model overrides when applicable.

For example, a bead for reducing Fulcrum review overhead explains which
handoffs are redundant, which review guarantees must remain, and what evidence
will demonstrate fewer messages or lower elapsed time. “Improve efficiency”
alone is not an implementation-ready task.

The Weaver creates task dependencies. The Archon handles plan-level ordering
and temporary holds. A plan prerequisite belongs in its Markdown metadata;
a condition such as “pause until the benchmark finishes” belongs in the
Archon's local holds. Check both when scheduling. Report dependency cycles
instead of repeatedly waiting for a cycle to resolve itself.

A bead belonging to a plan inherits that plan's activation. A bead without a
plan uses an `activation:queued` or `activation:future` label; absence defaults
to future work. The Weaver confirms the intended activation during direct
intake. Sage and Inquisitor findings start as future work; the Archon queues
them when appropriate. Beads' normal readiness and dependencies still apply.

The responsible Executor updates implementation status; the Overseer records
assignment and review decisions. The Archon coordinates reassignment before
another Executor writes the same bead. This is cooperative ownership, not a
new access-control system layered on Beads.

## Plans and Activation

A plan is a Markdown document with small frontmatter sufficient for discovery
and scheduling. Fulcrum discovers plans by reading this metadata, including
plans saved directly by the human. Discovery alone does not start work.

```yaml
---
plan_id: battlement-startup
project: battlement
activation: future
requires_plans: []
---
```

`activation` is `queued` or `future`. The Weaver asks which is intended during
planning. Codex's “Implement this plan” approves saving the project document
and creating its beads; the chosen activation determines whether that work
should enter the queue. It does not directly start product implementation.

A prerequisite plan is complete when all implementation beads currently linked
to it have completed their required work. Code beads require certified
promotion, source push, and cleanup before closure. Canceled work does not
satisfy a prerequisite: an explicit scope decision recorded in the plan must
remove that requirement. An empty plan is not treated as completed work.

Refinement may add required beads. Undispatched dependent plans use the updated
requirements. If a dependent run has already started, the Archon checks whether
the added work affects its contract and coordinates a pause or revised scope
when needed; the edit does not silently revoke existing mandates.

The normal completion sequence is:

1. Write the approved Markdown and complete the required document reviews.
2. Commit the Markdown and immediately attempt its Git push.
3. Create or update the related beads, commit their Beads history, and
   immediately attempt their Beads push.
4. Tell the Archon which plan and tasks are ready, then archive the Weaver.

The Archon checks readiness before assigning work, including whether task
creation has finished. A newly discovered queued plan with incomplete beads
waits for the author to finish; it does not require a publication manifest.
Lightweight task intake follows the same flow without a planning document.

A refinement edits the same document and updates the existing beads. For an
active assignment, the Weaver tells the Archon what changed. The Archon and
Overseer pause affected work if necessary and agree the revised scope with the
Executor. Record the plan's Git commit in the assignment so everyone can
identify the reviewed version. Other assignments may continue unchanged.

## Ordinary Git and Beads Operations

Agents and humans may edit and commit Markdown using normal tools. There is no
mandatory `fulcrum sync` command, special editor-save state, or publication
service. Agent skills require committing completed document edits and promptly
attempting the corresponding push. Direct human edits remain visible to the
dashboard, while execution follows the approved assignment scope.

Beads currently stores its issue history in Dolt. Normal Git pushes carry
Markdown; Beads' supported commit and `bd dolt push` operations carry issue
history to the same private repository. Beads uses a separate Dolt ref for that
history, so ordinary `git push` alone does not save database edits remotely.
Use the installed Beads version's supported commands and verify its commit
behavior. [Beads documents this storage distinction][sync].

V1 uses one **local Dolt server**, a database process running on the Mac. It
requires no remote database host. It lets independent Beads clients work
concurrently without repeatedly opening an embedded database. Beads/Dolt
handles database concurrency; Fulcrum adds no global edit lock around it.
Performance benefits should be measured in actual use, not assumed.

Keep file coordination equally narrow:

- Each agent edits its own documents or coordinates a shared edit with the
  document's owner. Do not overwrite another author's concurrent changes.
- In the shared checkout, serialize only Git staging and committing, using a
  short local lock if needed. Stage explicit files or hunks belonging to the
  edit. Do not include someone else's staged changes in a commit.
- Do not hold that lock while planning, editing, calling Beads, or pushing.
- Let ordinary Git and Beads conflict handling resolve remote divergence.
  Preserve changes and involve their author if intent is ambiguous.

There is no transaction joining Markdown, Beads, and JSON. A dashboard may
briefly show a new plan before its tasks exist, or task completion before the
Archon writes its NEWS update. These are normal intermediate states.

After interruption, inspect the document, Git history, and related beads, then
finish whichever steps remain. Existing plan labels and task content are
enough to identify already-created work; inspect before duplicating it.
A failed push leaves local work intact. Report which push failed, retry on the
next relevant action or patrol, and allow other work to continue. The Weaver
may archive after reporting that pending push to the Archon.

Restoration retrieves both Git files and Beads history through their supported
tools. A JSONL export can aid interchange, but does not replace a Dolt backup.
Rebuild local agent state from Codex conversations and Tollgate evidence after
loss; confirm live assignments and review mandates before resuming them.

## Local JSON Writes

Each file has one authorized writer at a time. Split state by ownership rather
than putting all agents into one shared writable document. The Archon owns the
registry and holds; each pair and agent writes its own details. Other roles
send a message when they need the owner to change something.

A small Python helper validates the record, writes a temporary file in the
same directory, flushes it, and replaces the target with `os.replace`. Use
`fsync` where crash durability is needed. Atomic replacement gives readers a
complete old or new file. There is no need for a global lock or revision-based
compare-and-swap when a file has one writer.

For example, an Executor's local record can contain:

```json
{
  "task_id": "<actual Codex task ID>",
  "bead_id": "fc-91",
  "phase": "reviewing",
  "waiting_on": "<actual Overseer task ID>",
  "next_action": "Review submitted candidate",
  "updated_at": "2026-09-11T18:00:00Z"
}
```

Add worktree path, candidate ID, and cleanup obligations when relevant. Record
whether the current handoff has been sent, updating that indication when a new
handoff is needed and only marking it sent after the messaging tool succeeds.
This is the agent's own report, not independent verification by a hook.
Keep large logs and conversations in their existing tools. Malformed JSON
produces a visible read error; it must not silently become an empty fleet.

## Messages and Expected Progress

Agents communicate through ordinary Codex task messages. A message names the
current bead or run, requested action, relevant candidate or document, and
supporting evidence. No machine-parsed report format is required.

```text
Executor 3 to Overseer 3: fc-91 is ready for review.
Candidate: <actual candidate ID>; source: <full commit OID>.
Local checks passed. Review link and evidence are attached.
Next action: review this candidate and grant a mandate or request fixes.
```

Before a handoff, the sender records its phase and intended next actor. It
sends the message through the normal Codex tool and, after success, records
the resulting wait or completion. A final response in its own task is not a
sent report.

If delivery is uncertain, inspect the tool result or destination conversation
before retrying. A repeated message is not a new assignment or promotion
mandate. Recipients check the current bead and candidate before acting. Codex
accepting a send does not mean the receiving agent has acted on it.

The [stop reminder](hooks.md#bounded-handoff-reminder) trusts the agent's
progress state and can ask it to check for a forgotten handoff once. It does
not maintain a parallel receipt store, match report identifiers, or intercept
self-archival. The skills retain responsibility for reporting before archival.

Useful phases include queued, implementing, reviewing, fixing, promoting,
investigating, completed, and canceled. A wait also records its reason and
expected next action, such as an Overseer review, CI result, or Archon decision.
There is no requirement to keep a task continuously running during a handoff.

Completion records the outcome and remaining cleanup, if any. Archive only
after the next responsible role has been informed. An idle task with no
terminal result or expected handoff is a patrol anomaly. An idle task waiting
for an identified reviewer is healthy, unless that review is itself overdue.
An Executor or Overseer stop reminder allows legitimate reported waits and
makes at most one corrective continuation per stop sequence. If reporting
still fails, the agent leaves its unresolved progress for normal patrol and
escalation. Hooks cannot detect every incorrect progress report.

## Archon Handover

Invoking `$archon` in a new task makes it the current Archon. Read the previous
task ID, tell that task to stop coordinating, and confirm it has relinquished
writes before replacing the current Archon ID. Then read the existing registry,
holds, assignments, and outstanding reports and continue from them.

If the previous task is already idle or unavailable, verify that it is not
still acting and replace its registration. Archon skills check the current ID
at the start of each turn; an old Archon that resumes yields to the new one.
If both are active, resolve that overlap before issuing new assignments.

Existing Executors, reviews, and promotion mandates continue to apply. There
is no need to invalidate them or attach an authority epoch to every message.
This is a cooperative handover on one machine, not leader election across
independent servers. Apply the same single-owner rule when replacing a
persistent Night Watchman. Compaction refresh can remind the Archon of current
ownership, but the skill retains responsibility for checking it. A new task is
a recovery option, not a mandatory periodic replacement.

## Python Helpers

Python scripts support repetitive operations: reading role context, validating
and atomically writing an owned record, collecting resource observations,
listing discovered plans, and starting or inspecting managed services.
Lifecycle handlers reuse these readers for short refreshers and local checks;
they do not invoke models, push repositories, or make scheduling decisions.

Use ordinary Git, Beads, Tollgate, and Codex interfaces for their own work.
Helpers may make common tasks convenient, but must not require an operation
journal or custom transaction protocol for every edit. Return useful errors
with the affected file, task, or command so the responsible agent can recover.
