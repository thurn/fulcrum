# Fulcrum Implementation Plan

Status: implementation-ready sequence; no work is activated by this document.

Build Fulcrum's coordination infrastructure first. Once that infrastructure
passes Task 20, build the Dashboard as the first complete project carried
through Fulcrum's own lifecycle. The Dashboard is a plan within the registered
Fulcrum software project, not a second repository or project registration.

This document specifies implementation outcomes, dependencies, and acceptance
criteria. References to Archon, Weaver, and other roles describe the product
being implemented and the required Dashboard acceptance exercise.

## Sources and scope

Read the relevant source sections before each task:

- [Technical design](technical-design.md): system boundaries and role behavior.
- [Data and agent contracts](contracts.md): ownership, identity, plans, and state.
- [Operations and recovery](operations.md): scheduling, resources, promotion,
  recurring work, setup, and updates.
- [Lifecycle hooks](hooks.md): refreshers, bounded reminders, and desktop checks.
- [Dashboard specification](dashboard.md): routes, presentation, security, and
  serving.
- [Original prompt](original-prompt.md): original requirements, audited below.

The technical design and its appendices control where they refine the original
prompt. This plan fills in delivery order and implementation details without
reopening those decisions. In particular, retain lit-html and Node/Express from
beads-ui, use Python for workflow helpers, and keep operational JSON untracked.

Use server mode for concurrent access to the shared brain, with Beads owning
the local database process. Reuse its automatic startup and existing lifecycle
commands. Fulcrum adds health checks and recovery guidance, not a database
LaunchAgent or a second process supervisor. Server mode runs on this Mac and
does not require cloud hosting.

The initial targets are Fulcrum, Tollgate, and Battlement on one Mac. The brain
is private data, not a managed software project. Multi-host dispatch, a separate
scheduling daemon, dashboard mutations, broad policy hooks, and a new issue
tracker are outside V1.

## Starting point and execution conventions

Read-only inspection on September 11, 2026 found:

| Area | Observed state | Implementation consequence |
| --- | --- | --- |
| Fulcrum repository | LICENSE, design documents, and Obsidian files; no application code or build configuration. Documents and Obsidian files were untracked. | Preserve these inputs; establish the application and checks without sweeping unrelated files into changes. |
| Source remote | `git@github.com:thurn/fulcrum.git` | Verify remote and branch state during setup. |
| Brain | Existing Git checkout with `.beads/embeddeddolt`; minimal Markdown | Back up and inspect existing data before migrating storage. |
| Installed tools | Beads 1.2.2, Python 3.12.14, Node 24.19.0; `tg` available; no `dolt` executable found on PATH | These are observations, not the final compatible-version selection. |
| Codex projects | Saved local projects exist for Fulcrum, Tollgate, and Battlement | Resolve their actual IDs at setup; do not create duplicates. |
| Tollgate projects | Tollgate and Battlement registered; Fulcrum absent. Battlement reported `configuration-pending`. | Register Fulcrum and verify configuration health before enabling implementation for each project. |

These facts can change. Task 01 rechecks them. Fulcrum is not registered with
Tollgate at the start of this sequence; registration happens in Task 02. A
registered repository is not automatically healthy, and an installed CLI is
not proof of desktop integration.

Execute numbered tasks in order. Task 01 is intentionally a pre-registration,
documentation-only prerequisite: inspect the environment, review the resulting
compatibility report, and commit and push it without claiming Tollgate
certification. That is the final direct bootstrap change. Task 02 begins by
registering the resulting repository state with Tollgate, then delivers the
first normally certified code candidate. Every later task depends on the
preceding task unless explicitly described as conditional.

Task headings carry a primary-work tag: `[skills]` for Markdown, agent-skill,
runbook, or workflow-evidence work, and `[code]` for product, helper, hook,
service, or test implementation. Mixed tasks use the tag for their dominant
deliverable.

The listed files are proposed new paths; preserve useful upstream structure
when importing the Dashboard. Each task must leave its existing checks passing
and produce the evidence named in its acceptance criteria. Later tasks extend
the checks rather than making earlier stages depend on nonexistent frontend
code.

### Marking tasks done

Each numbered task starts with `Status: pending`. When its implementation,
required review, and acceptance criteria are complete, change that task's
status in this document to **`done`**. Do not leave completed tasks marked
pending. Use `in_progress` or `blocked` while work remains, with a concise
reason or evidence reference where useful.

If the task has a corresponding Beads issue, complete that issue through the
installed Beads version's supported completion command/status and promptly
commit/push its history. Use its native terminal state rather than introducing
a parallel Beads status; the implementation plan's completion label is `done`.
For code tasks, review alone is insufficient: certification, required source
push, and cleanup must finish before marking the task done. For non-code
tasks, retain the required review/validation result and save the deliverable.
Record a short completion-evidence reference with the status update.

Task 31 can be done once its required support code and checks are complete even
when optional remote activation remains unconfigured; explicitly note that
optional setup as pending. Do not use that exception for required work in
other tasks. Update the corresponding task status in the brain's Dashboard
plan as well once that plan exists.

Keep code-behavior documentation and sanitized fixtures in this repository.
Keep real plans, NEWS, and high-level memory in the brain. Keep real task IDs,
credentials, host configuration, raw evidence, and operational state local.
Evidence in the brain should normally be a concise result and a reference to
the retained source, candidate, or log, not a copied transcript or source patch.

### Proposed repository layout

```text
pyproject.toml                 Python package, Black, pinned development tools
.pyre_configuration           Pyre source and environment configuration
src/fulcrum/                  CLI, records, readers, observations, dashboard service
skills/fulcrum-<role>/SKILL.md Seven portable role entry points
skills/fulcrum-shared/         Small shared workflow/reference documents
hooks/                        One installable hook definition source
templates/brain/               Plan, memory, and NEWS examples
schemas/                      Versioned record and read-model contracts
tests/                        Focused behavior and integration checks
tests/fixtures/                Synthetic brain/state examples
scripts/check                 Repository validation entry point
docs/setup.md                 Install, verify, recover, and remove
docs/compatibility.md          Tested versions and capability limitations
docs/validation.md             Reproducible acceptance exercises
dashboard/                    beads-ui fork; introduced only after Task 21
```

Use Python's standard library for ordinary filesystem and process work. Add
small dependencies where justified, such as safe YAML parsing, rather than
creating a framework. Expose a `fulcrum` CLI and a repository-owned hook wrapper.
Commands below define interfaces to implement, not commands assumed to exist.

Configuration precedence is explicit CLI path override, then a named Fulcrum
environment override, then per-user configuration. Default the brain to
`~/brain`; default local state to `~/Library/Application Support/Fulcrum`.
Tests supply isolated roots. Expand paths once, validate them, and use argument
arrays when invoking subprocesses. Do not require `/Users/dthurn` paths in
installed code or skills.

## Infrastructure tasks

### Task 01 [skills] — Verify integration contracts and record prerequisites

**Status:** done — compatibility and prerequisite evidence is recorded in
`docs/compatibility.md` (2026-09-11).

**Outcome:** implementation begins from supported interfaces and known setup
gaps, with no guessed task APIs, database commands, or version compatibility.

**Work:**

1. Recheck repository state, saved Codex projects, Tollgate registrations, brain
   storage, tool versions, and relevant repository instructions.
2. Inspect installed Beads help for server configuration, migration, commits,
   dependency operations, remote synchronization, and restoration. Inspect
   Tollgate help for configuration, candidates, worktrees, pause, and diagnosis.
   Verify Beads' automatic startup and `bd dolt start`, `status`, and `stop`
   contracts, including database identity, port selection, logs, and recovery.
   Use brain-scoped commands; do not plan a separate Fulcrum database supervisor.
3. Record Codex capabilities separately: creation, ID resolution, messaging,
   listing, reading, archival, model selection, scheduled wakes, hook delivery,
   and optional observation of existing desktop tasks.
4. Establish compatible Python/Pyre, Beads/Dolt, and Node combinations. Verify
   upstream beads-ui's Beads compatibility before selecting dependency versions;
   do not begin frontend implementation yet.
5. Write `docs/compatibility.md` with versions, exact supported commands or tool
   fields, evidence date, and unsupported capabilities. Add a setup checklist
   identifying missing configuration, not secret values.

**Acceptance:** the checklist distinguishes required dispatch capabilities from
optional runtime observation, hooks with unverified desktop coverage, and remote
viewing. Each unresolved technical compatibility issue has a concrete probe and
an owning later task. No production records, schedules, or databases are changed
merely to inspect them.

### Task 02 [code] — Establish packaging, checks, and Fulcrum's Tollgate registration

**Status:** done — package scaffold, checks, and Tollgate bootstrap evidence are
documented in `README.md` and `docs/setup.md`.

**Outcome:** a small installable Python foundation with an executable validation
entry point and registered software CI.

**Work:**

1. After Task 01's documentation is reviewed, committed, and pushed, verify that
   the checkout is clean and record its repository/ref state. Register Fulcrum
   through Tollgate's supported initialization with `scripts/check` as the
   intended gate command. Because that script does not exist at the registration
   anchor, record the bootstrap baseline as failing or unvalidated; registration
   is not a passing certificate.
2. Create the first normal Tollgate worktree from that anchor. Add the package
   layout, CLI entry points, dependency lock or equivalent
   reproducible pins, Black configuration, and Pyre configuration. Use the
   compatible Python selected in Task 01; do not silently replace Pyre.
3. Implement `fulcrum --help` and `fulcrum version`. Include installed source
   revision in version output when available.
4. Add `scripts/check` for Black, Pyre, and the focused tests that exist at this
   stage. Document clean-environment installation and invocation in README.
5. Add ignores for environments, application state, credentials, database files,
   logs, caches, and generated builds. Preserve the user's existing documents.
6. Verify the repository identity, remote mapping, configured checks, and
   Tollgate-owned release behavior. Submit the scaffolding from its Tollgate
   worktree and obtain normal passing evidence before describing it as certified.
   Record the bootstrap boundary in `docs/setup.md`. Do not change other
   projects' policies as a side effect.

**Acceptance:** a clean checkout installs the package and runs the Python checks;
Tollgate can evaluate that same baseline and reports Fulcrum's identity and
configuration. A deliberately failing check in a disposable probe cannot
produce passing evidence. Dashboard checks are added when that code exists.

### Task 03 [code] — Define configuration and owned local records

**Status:** done — versioned typed records, JSON Schema, ownership, examples,
and malformed-input checks are implemented in `src/fulcrum/records.py`,
`schemas/`, and `tests/fixtures/records/`.

**Outcome:** small, documented records that separate durable task truth from
operational coordination.

**Work:** create typed Python models, JSON examples, and schema validation for:

| Record | Minimum information | Writer |
| --- | --- | --- |
| Installation configuration | Brain/state roots, host ID, configured services and observations | Setup/human |
| Project registry | Stable project ID, repository path, host, Codex project ID, Tollgate ID, enabled state | Current Archon |
| Role/run registry | Current Archon, registered roles, actual task/host IDs, role numbers, run/pair IDs, title, and selected model/reasoning | Current Archon |
| Holds and recurring jobs | Scope, reason, release condition, permitted exceptions; cadence anchor, next due and active run | Current Archon |
| Assignment | Bead/plan IDs, approved plan commit, Executor/Overseer IDs, scope reference, review history, exact mandate | Pair's Overseer |
| Progress | Phase, phase start, expected next actor/action, handoff needed/sent, delivery error, timestamps, owned resources | That role task |
| Executor evidence | Worktree/base, source/candidate/tested identities, queue revision, push and cleanup obligations, suspended investigation references | That Executor |
| Interview | Sage/run/task IDs, prior archival state, reminder and completion state | Interviewing Sage |

Use `schema_version`, validated UTC timestamps, and explicit optional fields.
Do not put mutable bead status into these records. Keep model authorization
provenance separate from an agent's recommendation. Represent unavailable
observations distinctly from successful empty results.

**Acceptance:** examples cover a healthy review wait, unresolved task identity,
source push pending after promotion, paused work, and an active Sage interview.
Malformed fields and unsupported schema versions produce actionable errors.
Every mutable file has one documented owner.

### Task 04 [code] — Implement atomic state I/O and concise context readers

**Status:** done — configuration, owned atomic state I/O, CLI readers/writers,
and partial task context are implemented with focused failure and concurrency
coverage.

**Outcome:** records remain readable during writes, and roles can obtain a
small, relevant view without loading the entire brain.

**Work:**

1. Implement configuration/path resolution and record readers under
   `src/fulcrum/`. Reject path traversal and unknown record kinds.
2. Implement validated temporary-file writes in the target directory, flush,
   appropriate `fsync`, and `os.replace`. Require the declared writer identity
   to match the record's cooperative ownership contract. This is not a new
   security boundary against arbitrary filesystem access.
3. Expose `fulcrum state read`, `fulcrum state write --input <file>`, and
   `fulcrum context --task <actual-id>`. Keep stdout machine-readable where
   requested and diagnostics on stderr.
4. Context readers assemble role identity, assignment references, holds, and
   applicable short global/project memory. Return partial data with explicit
   read errors; do not turn corrupt registry data into an empty fleet.
5. Reserve separate paths for optional logs and observations. Hooks and readers
   must not mutate the registry or another role's progress.

**Acceptance:** a meaningful concurrent read/write check observes complete old
or new JSON; a failed write preserves the previous record. An unknown task gets
no inferred private role context. Missing memory does not hide a valid
assignment, and invalid state remains visible as an error.

### Task 05 [code] — Provision the brain with Beads-managed local server mode

**Status:** done — the existing brain was backed up and migrated to loopback
Beads-managed Dolt server mode; diagnostics and idempotent initialization are
implemented and recovery was exercised in disposable fixtures.

**Outcome:** independent Beads clients can use one local database without data
loss or accidental exposure.

**Work:**

1. Inspect and back up the existing embedded Beads store through its supported
   mechanism. Verify the backup can be read before migration. Preserve Git
   state, ignored configuration, and remote settings separately as needed.
2. Install the compatible Dolt version and configure the brain's Beads store
   for local server mode using Task 01's verified interfaces. Use loopback TCP
   with Beads' automatic startup; Unix-socket operation does not support that
   automatic startup. Keep credentials and database working files outside
   ordinary Git tracking. Inspect Beads' selected port and report conflicts
   without commandeering an unrelated listener.
3. Reuse Beads' automatic startup and its existing lifecycle commands, scoped
   to the brain: `bd --directory <brain> dolt status`, `start`, and `stop`.
   Beads owns process identity, port selection, and logs. Add only the thin
   status/connectivity calls needed for Fulcrum diagnostics. Do not implement
   a database LaunchAgent, PID registry, restart loop, or lifecycle framework.
4. Implement idempotent brain initialization that preserves existing data and
   verifies the intended private remote. Do not create a second issue database
   in each software repository.
5. Document migration rollback, how to identify the active database, and
   recovery through Beads commands. Coordinate maintenance before explicitly
   stopping the shared database. Never concurrently migrate through multiple
   clients; worktree cleanup must not stop the brain's database.

**Acceptance:** two independent Beads clients see each other's committed issue
changes and existing issues/dependencies survive migration. In disposable
server-mode fixtures, stop the server, verify subsequent client access starts
it through Beads, and exercise recovery after controlled process failure.
Concurrent startup and repeated setup create no duplicate server/database.
If recovery fails, diagnose Beads configuration/version and report the failure;
do not add a second supervisor to conceal it. Database endpoints are not
remotely exposed. Restore into a disposable location works without touching
the live brain.

### Task 06 [code] — Integrate Beads ownership, dependencies, and synchronization

**Status:** done — Beads conventions, idempotent intake, narrow Git commit
serialization, independent synchronization, and disposable restore evidence
are implemented in `src/fulcrum/beads.py`, `templates/brain/bead.md`, and
`docs/validation.md`.

**Outcome:** one brain-wide issue graph supports project work and is durably
recoverable through both Git and Dolt synchronization.

**Work:**

1. Define and validate `project:<id>`, `plan:<id>`, and standalone
   `activation:queued|future` conventions. Require project identity; absence of
   standalone activation means future work.
2. Add a bead template containing problem, outcome, bounded scope, context,
   dependencies, acceptance criteria, validation, and authorized model overrides.
   Use existing Beads types, priorities, statuses, and dependency commands.
3. Document the supported commit behavior for server-mode writes. Require an
   immediate push attempt after the corresponding Markdown or Beads commit.
   An ordinary Git push does not carry issue history; use the verified Beads
   remote operations. [Beads synchronization documentation](https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md).
4. Keep conveniences thin: explicit working directory and arguments to `bd`,
   bounded command timeouts, useful errors, and no new task database.
5. Support narrow serialization of Git staging/committing in the shared brain.
   Stage only the intended paths/hunks, detect unrelated staged content, and
   release any commit lock before network pushes or Beads operations.
6. On failed pushes, retain local work and record the outstanding push in the
   responsible role's progress/report. Retry through normal work or patrol;
   do not require a global sync daemon or transaction journal.

**Acceptance:** a disposable graph contains two projects and a cross-project
dependency. Separate Git and Dolt pushes restore Markdown and issue history
into a clean checkout. Network failure preserves local changes and exposes a
retry obligation. Retrying interrupted intake does not duplicate existing beads.

### Task 07 [code] — Implement Markdown plans, memory, and NEWS readers

**Status:** done — safe plan and NEWS parsing, concise memory views, CLI plan
listing, templates, and sanitized parity fixtures are implemented in
`src/fulcrum/documents.py`, `templates/brain/`, and `tests/fixtures/brain/`.

**Outcome:** people can maintain ordinary Markdown while Fulcrum discovers
plans and supplies useful role/project context.

**Work:**

1. Add templates under `templates/brain/` for plans, concise global/per-project
   memory, and NEWS. Use `plans/<project>/<plan_id>.md` and
   `memory/<role>/...` as the default organization, with relative links.
2. Parse safe YAML frontmatter for `plan_id`, `project`, `activation`, and
   `requires_plans`. Detect duplicate IDs, unknown projects, invalid activation,
   and dependency cycles. Discovery does not dispatch work.
3. Parse NEWS project summaries and dated entries with Projects, Category, and
   Beads fields, following the existing appendix. Preserve body Markdown and
   dates. Categories are workflow, architecture, and progress.
4. Expose `fulcrum plans list` and concise context output. Define sanitized
   parser fixtures for later Node parity checks without requiring a backend
   or a model to summarize documents.
5. Define brief pruning guidance: replace stale lessons, preserve useful
   evidence references, retain active incidents, and keep code-behavior
   documentation with source. Bead pruning must respect postmortems and links.

**Acceptance:** a manually saved valid plan is discoverable; future activation
stays future; a malformed document produces a file-specific diagnostic.
Multi-project NEWS entries and future-plan links preserve their scope. No
Markdown reader invokes a model or rewrites human text.

### Task 08 [code] — Implement eligibility facts and plan revision reconciliation

**Status:** done — read-only eligibility, completion evidence, interrupted
preparation detection, and targeted plan-revision reconciliation are
implemented in `src/fulcrum/eligibility.py` with scenario fixtures.

**Outcome:** roles can explain why work may start without relying on a second
automated scheduler.

**Work:**

1. Implement a read-only eligibility summary combining Beads readiness, plan or
   standalone activation, plan prerequisites, holds, project integration health,
   assignment ownership, and resource observations.
2. Return reasons independently: ready in Beads may still mean held, future,
   integration unavailable, already assigned, or awaiting plan preparation.
   Unknown data must not grant eligibility.
3. Define completion as all required work done. Code beads require certified
   promotion, source synchronization, and cleanup; canceled work and empty plans
   do not satisfy prerequisites. Removing a requirement needs a recorded scope
   decision. Use retained evidence to expose contradictory closures.
4. Include approved plan commit in assignments. Specify how a changed plan or
   newly added prerequisite is compared with active scope and routed for
   targeted pause/reconciliation. Preserve unaffected assignments and mandates.
5. Use normal plan labels, author progress, and existing bead content to detect
   interrupted preparation. Do not add a publication manifest or cross-store
   transaction protocol.

**Acceptance:** fixtures demonstrate queued versus future, composed holds,
cross-project prerequisites, a cycle, partial intake, canceled prerequisites,
an empty plan, and refinement of an active assignment. The helper explains
facts and never mutates task inventory, changes activation, or issues a promotion.

### Task 09 [skills] — Implement task identity and portable shared role contracts

**Status:** done — identity/handoff checks and portable wheel verified; commit
`17ccfb0` certified, pushed, and cleaned up through Tollgate.

**Outcome:** roles use actual Codex identities, reliable handoffs, and installed
instructions independent of personal skill directories.

**Work:**

1. Write shared skill references for registration, starting a turn, handoffs,
   archive obligations, model policy, and reading current assignment/holds.
2. Specify immediate and pending identity-resolution flows. Store a real task/host ID only
   after resolution; never send messages to `clientThreadId`. Reconcile unique
   role tags and existing tasks before retrying uncertain provisioning.
3. Allocate nonrecycled role numbers; pair numbers match. Weaver titles stay
   descriptive. Human-created roles preserve selected model preferences.
4. Implement record helpers for role/progress initialization. A Weaver may
   write its own record after approval and report to the Archon; only the
   Archon updates its registry.
5. Define the handoff sequence: record intended actor/action, send through the
   supported Codex tool, then record success/wait. Uncertain delivery requires
   inspection before retry. A final answer is not a message to another task.
6. Record defaults: Overseer/Sage/Inquisitor `gpt-5.6-sol` high; Executor
   `gpt-5.6-luna` xhigh. Respect explicit overrides, allow Archon upgrades to
   Sol, and require human provenance for Astra. Unavailable models surface a
   capability problem rather than silent substitution.

**Acceptance:** disposable tool exercises resolve pending IDs, detect duplicate
provisioning attempts, and deliver a real handoff to the intended task. A clean
installation outside the developer's home paths resolves all shared references.
Plan-mode role activation causes no brain or state writes.

### Task 10 [skills] — Implement the Archon role and project enrollment

**Status:** done — cooperative transfer and enrollment scenarios pass; commit
`794a499` certified, source synchronized, and worktree cleaned by Tollgate.

**Outcome:** one current strategic coordinator can register projects and
reconcile the fleet without writing code or running builds.

**Work:**

1. Add `skills/fulcrum-archon/SKILL.md` with identity checks at each turn, compact
   briefing/memory, project registration, summaries, and NEWS ownership.
2. Implement cooperative handover: contact the previous Archon, establish
   relinquished writes or verify inactivity, then replace registration and
   reconcile holds, assignments, and outstanding reports. A resumed old Archon
   yields. Existing mandates are preserved.
3. Verify actual repository/Codex/Tollgate identity before enabling a project.
   Reconcile the existing Fulcrum, Tollgate, and Battlement records; report
   configuration problems instead of marking registration alone as readiness.
4. Describe strategic prioritization, investigation delegation, and explicit
   initialization of project-scoped runs. Named implementation tasks begin in the
   saved local project context; their product workflow owns worktree creation.
5. Require scheduling rationale and meaningful project/news updates. Fulcrum
   workflow improvements and architectural changes receive prominent coverage.

**Acceptance:** a scripted scenario transfers the Archon role without two
writers, preserves a pending mandate, and prevents a mismatched or unhealthy
project from receiving implementation. The role delegates source investigation
and builds and keeps its briefing short enough for the context reader.

### Task 11 [skills] — Implement Weaver planning, intake, and refinement

**Status:** done — Weaver scenario and skill checks pass; commit `8041896`
certified, source synchronized, and worktree cleaned by Tollgate.

**Outcome:** approved human intent becomes standalone plans and executable
beads without starting work simply because a plan was saved.

**Work:**

1. Add `skills/fulcrum-weaver/SKILL.md`, a task-writing template, and portable interview
   guidance: one material question at a time, recommended answer, repository
   exploration for discoverable facts.
2. In Plan mode, interview without writing files or beads. Ask queued versus
   future. Approval authorizes saving the plan and publishing its Beads work graph, with
   execution eligibility determined separately by activation.
3. For substantial plans, specify a fresh cold reader given only the document
   and a separate requirements verifier given original inputs and decisions.
   Resolve findings before committing the document.
4. Outside Plan mode, explicitly state direct task intake; answer mixed
   questions before dependent actions, clarify scope, and create beads without
   inventing a planning-document requirement or those two review passes.
5. On refinement, retain plan identity and update existing beads. Notify the
   Archon about changed active scope. Follow Markdown commit/push, Beads
   commit/push, report, then archive. Failed pushes remain retry obligations.

**Acceptance:** exercises cover future-plan approval, queued-plan approval,
direct bug intake, interrupted bead creation, and active-plan refinement.
Generated beads contain enough scope and criteria to execute without the
conversation. Saving a future plan does not dispatch implementation.

### Task 12 [skills] — Implement assignment, review, and certified completion roles

**Status:** done — delivery roles, review accounting, replacement mandates,
and disposable Git/Tollgate exercises verified; commit `4a6771f` certified,
source synchronized, and owned worktree cleanup completed. See `docs/validation.md`.

**Outcome:** Overseer and Executor skills express the complete code-delivery
contract, including failures and cleanup.

**Work:**

1. Add `skills/fulcrum-overseer/SKILL.md` and `skills/fulcrum-executor/SKILL.md`. Adapt the
   normative worktree and promotion behavior into portable references; do not
   depend on the author's absolute `$wt` or `$implement-plan` paths.
2. Cover one active bead per pair; a fresh owned Tollgate worktree from captured
   certified release; implementation/checks; clean immutable candidate
   submission without authority; immediate review handoff; and expected waits.
3. Preserve distinct source OID, tested OID, candidate ID, base identity, and
   queue revision. Include the specified encoded worktree review link and
   visual evidence for UI changes. Review may inspect read-only, not edit or
   run builds in the Executor's tree.
4. Count substantive reviews of new submissions. Three unsuccessful reviews
   escalate to Archon; missing evidence and duplicate messages do not increment
   the count. Never import the reference skill's automatic third-review
   promotion rule.
5. Record exact scope-bound mandates and allowed replacements. The Executor
   drives candidate authorization, CI diagnosis, in-scope repair, certification,
   promotion, configured push, and owned runtime/worktree cleanup.
6. Close the bead only after all completion obligations are verified; report
   before archival. A promoted candidate awaiting source push remains recovery
   work, not an implementation redo.

**Acceptance:** a disposable repository exercise covers accepted review, one
rejected revision, third-rejection escalation, a replacement candidate, and
push/cleanup pending after promotion. Certification remains with Tollgate and
there are no direct writes to release or unauthorized source-branch pushes.

### Task 13 [code] — Add resource observations, holds, and verified pause/resume

**Status:** done — bounded local/Tollgate observations, composable hold
lifecycles, and verified checkpoint, quiet, and resume helpers are implemented
with controlled-process coverage.

**Outcome:** the Archon has enough facts to coordinate capacity and quiet
intervals; no helper schedules independently.

**Work:**

1. Add `fulcrum resources` for timestamped CPU, memory pressure, relevant
   processes, and compact Tollgate queue/resource evidence. Mark missing
   measurements unavailable and bound subprocess durations/output.
2. Implement hold records for fleet, host, project, plan, and assignment scope.
   Compose holds; human indefinite holds require explicit release. Exceptions
   name recovery scope and permitted resources.
3. Extend role guidance to check eligibility before dispatch and before the
   next bead, prioritize incidents/unblocking, consider aging and resource
   pressure, and record reasons for departures from priority order.
4. Specify heavy-command expectations and escalation before exceeding them.
   Record owned process identities; do not create per-command permits or a
   competing queue supervisor.
5. Implement pause/checkpoint reporting: inventory within five minutes,
   preserve intended changes within ten, reconcile candidates, stop owned
   processes, and observe Tollgate drainage before declaring a host quiet.
6. Resume only after the applicable holds are released and worktree, contract,
   candidate, and base identities are rechecked.

**Acceptance:** an exercise with controlled processes never reports quiet while
a relevant process lives. Overlapping holds survive partial release; an
unvalidated checkpoint cannot be treated as a candidate. Raw measurements stay
local while concise evidence-linked lessons can enter project memory.

### Task 14 [skills] — Implement escalation and recovery runbooks

**Status:** done — portable escalation/recovery runbooks, durable failure and
suspension evidence, bounded CI decisions, and the narrow Tollgate emergency
path are implemented with tabletop coverage.

**Outcome:** tooling failures lead to bounded diagnosis and preserved work.

**Work:**

1. Add shared runbooks and state support for Executor → Overseer → Archon
   escalation, with exact boundary, error/evidence, attempts, retained work,
   untried recovery, and requested decision. Only Archon decides blocked work
   requires human input; Weaver planning interviews remain separate.
2. Specify `tg diagnose` first for candidate failures, at most one unchanged
   retry with a stated hypothesis, and fifteen further minutes of focused
   diagnosis before repair, rollback, or evidence-bearing escalation.
3. Model same-project investigations as a suspension stack with a fresh owned
   worktree; cross-project fixes use the affected project's scope. Preserve
   original worktree ownership, review history, and failure history on resume.
4. Cover unavailable tasks: resume original first; replacement uses a fresh
   worktree, retained commits/evidence, and explicit disposition of dirty work.
   Reconcile already-promoted candidates before reconstructing changes.
5. Implement the documented Tollgate emergency path: operational recovery
   first, recorded narrow exception, reviewed provisional repair, then normal
   certification and installed-version reconciliation. No release rewrites,
   disabled voting checks, or fabricated certificates.

**Acceptance:** tabletop and disposable-boundary exercises cover send failure,
repeated CI failure, lost task, source push failure, nested investigation, and
Tollgate worktree outage. Each ends in recovery or a named expected next actor
with preserved evidence. Do not break the production Tollgate service to test it.

### Task 15 [code] — Implement Night Watchman patrol and recurring-work bookkeeping

**Status:** done — the portable Watchman skill, read-only evidence patrol,
condition-aware deduplication, fake-clock recurring bookkeeping, and idempotent
hourly automation planning are implemented.

**Outcome:** one hourly patrol detects anomalies and reports due work without
becoming a second scheduler.

**Work:**

1. Add `skills/fulcrum-night-watchman/SKILL.md` and read-only patrol helpers. Compare
   registry/progress with supported Codex and Tollgate evidence, pending role
   provisioning, expected waits, archived tasks, and known failed pushes.
2. Distinguish a healthy idle review wait from an unexplained stop. Respect
   deadlines recorded with handoffs; elapsed time alone does not prove a hung
   task. Report uncertainty when observation is unavailable.
3. Define deduplication using stable anomaly identity plus current condition;
   report meaningful changes and resolutions without repeated identical noise.
4. Implement due-time calculations with injectable time: Sage every 24 hours,
   each enabled project's Inquisitor twelve hours offset. Store UTC anchors,
   next due, and active task per scope. The Archon owns dispatch/registry writes.
5. Prevent overlapping occurrences. After downtime request one catch-up per
   scope and advance to the next future due time. Disabled projects get no new
   reviews; incidents/holds may leave analysis visibly due.
6. Provide idempotent installation of one hourly Codex scheduled wake attached
   to the human-created Watchman, using supported automation tools.

**Acceptance:** fake-clock checks cover overlap, downtime, disabled projects,
and twelve-hour offset. A disposable patrol reports a real anomaly to the
registered Archon while a quiet patrol creates no user-facing noise. Schedule
setup reruns without duplicating the hourly wake.

### Task 16 [skills] — Implement Sage postmortems and bounded interviews

**Status:** done — bounded interviews, restored archival state, future-finding
deduplication, and live disposable probes verified; commit `b803b1d` certified,
pushed, and cleaned up through Tollgate. See `docs/validation.md`.

**Outcome:** workflow problems become actionable, deduplicated future work.

**Work:**

1. Add `skills/fulcrum-sage/SKILL.md` with fleet-wide scope since the previous
   postmortem. Examine Fulcrum's scripts, skills, context, handoffs, resource
   use, hook latency/correction turns, and Tollgate friction explicitly.
2. Read existing reports/logs first. Distinguish measured timing/token evidence
   from unavailable data and expected benefits.
3. Implement interview records before unarchiving. Send debrief-only requests;
   do not reactivate implementation or promotion. Re-archive only tasks the
   Sage reopened, using their saved prior state.
4. Allow one reminder on a later patrol, then finish with missing-response
   evidence. Retain interrupted interview state so patrol/recovery can finish
   archival obligations.
5. Deduplicate findings by underlying problem. File/update project-labeled
   future beads with evidence, impact, proposed change, and acceptance criteria;
   report to Archon and archive after completion.

**Acceptance:** an interview exercise restores archival state, a missed reply
cannot stall forever, and repeated findings update one issue. A sample finding
about Fulcrum itself explains expected benefit without claiming an unmeasured
speedup. It does not silently expand an active assignment.

### Task 17 [skills] — Implement Inquisitor architectural review

**Status:** done — portable whole-codebase review and synthetic repository
exercise verified; commit `7137050` certified, source synchronized, and worktree
cleanup completed through Tollgate. See `docs/validation.md`.

**Outcome:** each enabled project can receive fresh whole-codebase review that
produces useful future tasks.

**Work:**

1. Add `skills/fulcrum-inquisitor/SKILL.md` and a findings template, using the design's
   whole-codebase scope and project-specific context.
2. Guide analysis toward architectural importance: responsibilities, brittle
   boundaries, duplicated logic, type modeling, and opportunities to remove
   complexity. Recent commits receive no special emphasis.
3. Require evidence, a credible behavior-preserving direction, affected
   interfaces, and validation expectations. File size alone does not justify
   arbitrary splitting.
4. Deduplicate existing open findings; file new ones as future work. Report to
   Archon and archive. No findings is a valid result; the role makes no direct
   product edits and grants no implementation authority.

**Acceptance:** a small synthetic repository exercise yields a concrete
architectural finding or an evidence-based no-findings result, honors project
scope, and does not prioritize a trivial recent commit over an older major
boundary problem.

### Task 18 [code] — Implement and verify the two lifecycle hooks

**Status:** done — local hook behavior and idempotent source merging pass focused
checks; this non-interactive run could not perform the required desktop trust and
event-delivery exercise, so skill/patrol fallback remains explicitly active.

**Outcome:** registered roles receive short compaction context and active pairs
receive at most one reminder about a potentially missing handoff.

**Work:**

1. Implement the repository-owned hook wrapper using only local event JSON and
   existing readers.
   Install one hook source preserving unrelated hooks. Resolve actual task
   identity, not a repository/title guess.
2. Handle `SessionStart` with source `compact`: relevant role, assignment,
   short memory, and links; target about 500 tokens with a 1,000-token cap.
3. Handle `Stop`: intervene only for an active Executor/Overseer that may owe
   a handoff. Allow reported waits/outcomes, inactive runs, unrelated tasks,
   Plan-mode work, and debrief-only interviews. Respect `stop_hook_active`.
4. Return the runtime's documented JSON response for Stop, including valid
   nonintervening output. The reminder asks the role to avoid duplicate sends.
   Documented event contracts still require a real desktop test.
   [Official hook documentation](https://learn.chatgpt.com/docs/hooks).
5. Use a two-second ceiling, local reads, and no network/model/Beads/transcript
   work. Diagnostics are best-effort local logs; they never alter coordination
   state. Do not add broad pre-tool policies or receipt tracking.
6. Test actual desktop compaction and stops through the supported trust flow.
   Record unavailable/skipped coverage honestly; do not bypass trust or claim
   CLI support proves desktop delivery.

**Acceptance:** focused hook checks cover all exclusions and one-correction
behavior. Desktop evidence records added latency, context size, and correction
turns. A failed/missing helper does not manufacture authority or mutate state.
If desktop coverage is unavailable, the documented skill/patrol fallback is
verified and the limitation remains visible.

### Task 19 [code] — Complete installation, diagnostics, and safe self-updates

**Status:** blocked — repeatable certified-source installation,
version/skill/hook diagnostics, schema backup conversion, and safe update checks
pass, but the absent human Archon/Watchman enrollment and Watchman schedule are
required Task 19 outcomes that cannot be manufactured by setup.

**Outcome:** the infrastructure can be installed repeatedly and maintained
without losing active work.

**Work:**

1. Finish `fulcrum doctor`, versioned installation, skill installation, and
   Beads-backed database diagnostics. Report required capability failures,
   optional observation gaps, hooks, source versions, and failed pushes
   separately. Read database health through Beads' supported commands rather
   than maintaining Fulcrum-owned database process metadata.
2. Enroll the actual human-created Archon and Watchman; do not manufacture
   substitutes. Collect the initial Sage cadence time during setup. Resolve
   and verify all three initial project integrations.
3. If an existing project's configuration cannot yet support execution, retain
   its registration with explicit ineligibility and resolve that integration
   problem before the readiness gate. Do not invent health or start unrelated
   production work as a setup side effect. Excluding an initial target requires
   an explicit scope decision, not silently disabling it to pass setup.
4. Install from a retained certified Git checkout, not a disposable worktree or
   wheel-only environment. Symlink Codex's Fulcrum skill and hook directories to
   that checkout so repository edits apply immediately without revision
   tracking or copied-file manifests.
5. Reject unsupported record schemas without rewriting them. Document
   Beads/Dolt backup, migration, lifecycle commands and version checks, hook
   review, targeted rollback, and uninstall preserving user data. A Fulcrum
   package update does not restart the database.
6. Provide usable CLI diagnostics before the Dashboard exists. Dashboard
   service installation is implemented in Task 29; do not build a generic
   database service manager as a prerequisite.

**Acceptance:** install twice, recover the database through Beads, update the
package, and re-read an active assignment without losing identity or creating duplicate
roles/services/schedules. Doctor exposes actual health and configured version.
An incompatible record is preserved and diagnosed, not reset to empty state.

### Task 20 [skills] — Pass the infrastructure readiness gate

**Status:** blocked — the evidence matrix and fail-closed evaluator are complete;
human Archon/Watchman enrollment, the hourly Watchman schedule, and the
Archon-owned three-project registry remain required failures, so Task 21 is not
admitted.

**Outcome:** all non-Dashboard infrastructure exists and can support the first
real Fulcrum project.

**Work:** consolidate `docs/validation.md` with commands, setup, expected
results, and evidence locations for the preceding boundary checks. Use an
isolated brain, disposable repository, and explicitly test-scoped tasks for
destructive/error exercises. Do not run a separate production pilot project.

**Required gate evidence:**

- Python package/checks and Fulcrum Tollgate integration work from clean state.
- Beads-managed startup, concurrent access, recovery, Git/Dolt synchronization,
  backup, and restore work without a second database supervisor.
- All seven roles are installed; real identity, messaging, Plan-mode exclusions,
  ownership, and archival obligations have been exercised.
- Plans, activation, prerequisites, holds, review, promotion, source push,
  cleanup, pause, and escalation produce the expected state/evidence.
- Patrol cadence, catch-up, interviews, deduplication, and meaningful NEWS
  generation work without a second scheduler.
- Hooks are verified in desktop or explicitly unavailable with the specified
  fallback exercised. Runtime visibility is verified or explicitly unavailable.
- Human-created Archon/Watchman enrollment and hourly schedule are ready.
- All three initial project mappings and integrations are verified and healthy.
  A remaining configuration problem must be repaired or explicitly excluded
  by a scope decision before claiming this gate passed.

**Acceptance:** write a concise pass/fail/unsupported matrix with references to
actual evidence. Required failures prevent Task 21; optional capabilities use
only the fallbacks already allowed by the design. No Dashboard implementation,
UI fork, or production Dashboard beads have begun before this gate.

## Dashboard as the first complete Fulcrum project

The following tasks specify both the application and its acceptance as a real
Fulcrum project. Complete all required infrastructure first. The Dashboard
must not merely display seeded records claiming that its own development ran
through Fulcrum.

### Task 21 [skills] — Admit the Dashboard through the real project lifecycle

**Status:** pending

**Outcome:** Dashboard work exists as an approved plan and genuine brain-wide
Beads tasks managed by the installed Fulcrum system.

**Work:**

1. Use Weaver's implemented plan flow to save a standalone Dashboard plan at
   `~/brain/plans/fulcrum/fulcrum-dashboard.md`, with plan ID
   `fulcrum-dashboard`, project `fulcrum`, and the chosen activation. Carry
   Tasks 22–32 and their criteria into that document; include Task 33 as the
   final system acceptance activity. Do not require a new product-design
   interview for decisions already fixed in the approved documents.
2. Run the required document comprehension and requirements checks. Commit and
   attempt the Markdown push, then create sequential implementation beads,
   commit their Beads history, and attempt the Beads push.
3. Record the infrastructure gate as the already-satisfied prerequisite with
   evidence; do not invent completed infrastructure beads solely to populate a
   graph. Keep optional remote activation distinguishable from required code.
4. Let normal discovery, eligibility, and Archon coordination produce the
   project run and actual assignments. Capture plan commit and real task IDs.
5. Establish evidence collection for each Dashboard bead: assignment, candidate,
   review, certification, promotion, source push, cleanup, and completion.

   Use `queued` when admitting the already-authorized Dashboard implementation
   to run; if the owner chooses `future`, save the work but keep Task 22 pending
   until activation changes. This operational admission does not approve a new
   product scope or automatically activate this public planning document.

**Acceptance:** the real brain contains the approved Dashboard plan and
dependency-linked beads; the Archon discovers and starts eligible work through
implemented behavior. No handwritten JSON claims substitute for actual task
messages, candidates, or completion. The application remains part of the
Fulcrum repository and its Tollgate registration.

### Task 22 [code] — Import beads-ui and establish a read-only application foundation

**Status:** pending

**Outcome:** a licensed, reproducible fork provides the retained issue UI and
backend foundation without exposing upstream editing features.

**Work:**

1. Import a specific upstream revision into `dashboard/`; record repository,
   commit, license notices, upstream structure, and update procedure. Preserve
   useful issue, epic, dependency, router, CLI adapter, and subscription code.
   Inspect the imported version rather than assuming file names from the
   upstream README. [beads-ui upstream](https://github.com/mantoni/beads-ui).
2. Retain lit-html and Node/Express. Add Vite development/build integration,
   TypeScript for new code, Prettier, ESLint, and existing checked-JavaScript
   validation. Keep useful upstream tests without wholesale conversion.
3. Inventory every HTTP and WebSocket handler. Remove editing controls and
   reject all mutation operations server-side. Allow only configured brain
   workspace selection and necessary reads/subscriptions. Unknown operations
   fail closed; HTTP POST alone is not proof of mutation if upstream uses it
   for a read query.
4. Extend `scripts/check` and Tollgate checks with dependency installation,
   frontend/backend type checks, lint/format checks, tests, and production build.

**Acceptance:** a clean install builds retained issue views. An API test calls
every inventoried mutation family through HTTP and WebSocket and verifies both
rejection and unchanged Beads data. No raw path, command, SQL, or arbitrary
workspace selection is accepted. The foundation is not deployed with writable
upstream handlers while awaiting a later security task.

### Task 23 [code] — Add the read-only Fulcrum data API and runtime observation

**Status:** pending

**Outcome:** the Node backend reads real Fulcrum state with explicit freshness
and source failures.

**Work:**

1. Define an allowlisted read-model contract for projects, plans, NEWS,
   registrations, assignments, progress, holds, scheduling facts, and safe
   evidence references. Include schema version and per-source availability,
   update/observation time, and sanitized error information.
   Expose it through `GET /api/fulcrum`; retain upstream read endpoints for
   Beads. Define the response in `schemas/dashboard-read-model-v1.schema.json`
   and use that same contract for Node validation and frontend types.
2. Implement Node readers of the registered Markdown/local JSON paths. Match
   the shared parser fixtures and schema semantics from Tasks 03 and 07. Keep
   the existing Beads command adapter; do not replace it with Python or launch
   Python once per UI card. Let Beads handle its database lifecycle; the backend
   does not launch a separate Dolt server or run a database restart loop.
3. Add a bounded read-only runtime adapter only for a configured compatible
   existing Codex transport. Demonstrate it observes the registered desktop
   tasks; otherwise return unavailable. Never scrape private Codex databases
   or launch an unrelated app-server to manufacture observations.
4. Join facts by actual task/host, project, plan, and bead IDs. Keep reported
   workflow separate from observed runtime. Curate evidence links; expose no
   raw application directory, credentials, unneeded transcripts, or file browser.
5. Parse NEWS without executing embedded HTML. On invalid structure preserve
   the previous valid in-memory view with an error; an initial failure is an
   error rather than a successful empty project list.

**Acceptance:** real state reaches the API with timestamps; corrupt/missing
sources affect only their portion and remain visible. Disconnected runtime is
unavailable, not idle or running. A successful empty source remains distinct
from failure, and traversal/symlink escape attempts cannot read unregistered
files.

### Task 24 [code] — Implement the design system, navigation, and responsive shell

**Status:** pending

**Outcome:** all retained/new views share the specified science-fantasy visual
language before feature pages multiply.

**Work:**

1. Implement the exact dark/light colors, typography, spacing, radii, focus
   treatment, and motion tokens from `dashboard.md`. Follow OS theme changes.
   Verify foreground contrast instead of assuming white on accent fills.
2. Create shared cards, status labels, metadata, evidence links, filters,
   empty/error/stale states, and vector role emblems. Apply them to retained
   issue/dependency components as well as new pages.
3. Implement all specified hash routes and root redirect to Status. Keep
   filter/search state in route query parameters and preserve browser history.
4. Implement the 224px desktop sidebar, two-column grid, maximum content width,
   compact tablet/mobile navigation, and one-column narrow layouts. Use DOM row
   order, not masonry. Touch targets are at least 44px.
5. Implement motion primitives and reduced-motion behavior. Running animation
   is reserved for observed execution; waits are static. Pause offscreen and
   background-tab activity indicators.

**Acceptance:** a component/demo route using synthetic fixtures shows both
themes and every major state without becoming production fake data. Inspect
320px width, desktop, 200% zoom, keyboard focus, long titles, and reduced motion.
The style guide documents reusable tokens and components in the code repository.

### Task 25 [code] — Implement Status and agent details

**Status:** pending

**Outcome:** the owner can see who is responsible, what happens next, and where
progress needs attention.

**Work:**

1. Build summary counts and agent cards for action-required/inconsistent,
   working, healthy waits, scheduled jobs, and recent completions. Use priority
   and stable assignment start time within groups.
2. Show role/emblem, actual title/tag, project, assignment, reported activity,
   next actor/action, phase duration, runtime observation/time/availability,
   and relevant CI/resource facts.
3. Implement `/#/agents/:agent_id` with assignment/evidence links, handoff errors,
   pauses, cleanup obligations, and optional hook diagnostics. Distinguish
   recent hook events from current runtime or proven message delivery.
4. Add prominent recent workflow/architecture improvements from NEWS and compact
   resource/synchronization summaries linked to read-only diagnostics.
5. Keep animation and ordering stable across telemetry refreshes; an unverified
   working report has a clear label and cannot look like verified execution.

**Acceptance:** show real Dashboard development assignments as soon as available.
Fixtures cover waiting-on-review, missing handoff, runtime disconnected,
paused, scheduled, completed, and promoted-with-cleanup-pending. No row falsely
implies an intervention occurred, and ordinary refresh does not reshuffle
unchanged cards.

### Task 26 [code] — Implement Projects and approved plan details

**Status:** pending

**Outcome:** project direction and live delivery facts are visible together.

**Work:**

1. Implement project cards with integration health, enabled state, current/next
   narrative from NEWS, separate narrative and live-data timestamps, and counts
   for active, ready, blocked, held, future, and recently completed work.
2. Define count semantics explicitly: counts that overlap, such as ready in
   Beads but held in Fulcrum, must be labeled rather than implying a partition.
3. Show queued/future plans and exact ineligibility reasons, including incomplete
   intake, unsatisfied dependencies, holds, and unavailable integrations.
4. Implement `/#/plans/:plan_id`: current Markdown, approved assignment revision
   references, related beads, and prerequisite information. Do not present a
   working-file edit as the revision already assigned to an Executor.
5. Link project feeds and feature scoped workflow/architecture improvements.
   Unknown identities get missing/retained-history states, not fuzzy matches.

**Acceptance:** all three initial project registrations render with their real
health. A stale narrative, future plan, active plan revision change, and empty
project are distinguishable. No page-load model inference fills missing text.

### Task 27 [code] — Implement Newsfeed, bead details, search, and dependencies

**Status:** pending

**Outcome:** the full task contract and its current delivery evidence are
available in fleet and project views.

**Work:**

1. Implement fleet `/#/newsfeed` and fixed-project
   `/#/projects/:project_id/newsfeed` using shared components. Default to
   nonclosed work plus work closed within seven days; offer older history.
2. Add status, project, plan, role/assignee, priority, and text filters. Match
   current assignment, or last assignment for completed work, using actual IDs.
3. Sort active/action-required first, then ready, held/future, and recent
   completion; use meaningful change time and bead ID for deterministic ties.
4. Implement bead details with complete scope, approved plan reference,
   prerequisites, eligibility reason, assigned pair, handoff/evidence links,
   candidate/source identities, and push/cleanup obligations.
5. Retain useful upstream epic/dependency presentation and apply the common
   design system. Feature workflow/architecture NEWS above task cards, scoped
   appropriately for project feeds.

**Acceptance:** route links reproduce searches/filters; opening details and
back preserves position. Dependency blockers, canceled work, future work,
retained completion, and unavailable evidence all read correctly. An empty
project feed shows no invented example tasks.

### Task 28 [code] — Complete live refresh, caching, and partial-failure behavior

**Status:** pending

**Outcome:** dashboard changes normally appear within ten seconds without
each browser tab multiplying CLI work.

**Work:**

1. Test the retained watcher/subscription behavior against an independent
   server-mode Beads client. If it misses changes, refresh subscribed queries
   on one shared five-second backend timer.
2. Coalesce identical requests with a bounded shared cache and in-flight
   deduplication. Expire unused subscriptions and ensure request errors do not
   permanently poison the cache.
3. Refresh visible Fulcrum data every five seconds, pause frontend polling in
   background tabs, and refresh on return. Preserve filters, expansion, focus,
   and scroll while applying changes.
4. Keep previous valid data on transient source failure with age/error visible.
   Distinguish incomplete plan preparation, unknown links, empty query results,
   pending pushes, and unavailable runtime.
5. Record lightweight measurements of refresh latency and Beads subprocess
   volume under several tabs. Do not add a replication/export pipeline or
   persistent versioned snapshot store.

**Acceptance:** independent Beads, NEWS, and atomic state edits appear within
ten seconds under the tested normal load. Several tabs share backend work;
database failure and recovery preserve state and show accurate freshness.
Background tabs stop polling and animating.

### Task 29 [code] — Install persistent serving and certified Dashboard updates

**Status:** pending

**Outcome:** the built Dashboard survives task completion and reports the
version actually running.

**Work:**

1. Make the Node backend serve Vite production assets, HTTP reads, and
   WebSocket subscriptions from one loopback origin. Vite dev/preview servers
   are not the persistent deployment.
2. Implement the service helpers with a named Dashboard LaunchAgent, configured
   port, log paths, health/readiness, exact-process restart/stop, and served
   source/build revision. Health exposes availability without private content.
   Use `GET /health` for process readiness and served version; source failures
   appear separately so a live backend is not mistaken for a healthy database.
3. Build retained deployment artifacts from certified source, then switch the
   installed service to the new version and verify readiness. Keep a known
   working version for targeted rollback; never serve from a disposable
   implementation worktree.
4. Make Archon service operations invoke management scripts and delegate code
   repair/builds. Retain database and state through backend restart/update;
   do not restart the Beads-managed database with the Dashboard.
5. Document install/start/status/update/rollback/stop in `docs/setup.md` and
   add a minimal operational troubleshooting page or CLI output.

**Acceptance:** backend remains available after the implementation task ends,
recovers from controlled process exit, and shows the certified served version.
An unsuccessful update preserves recoverable previous service artifacts and
does not erase brain/state. No development server remains as an owned-process
cleanup leak.

### Task 30 [code] — Validate the complete local Dashboard

**Status:** pending

**Outcome:** the local application meets functional, visual, and read-only
requirements before optional external exposure.

**Work:**

1. Consolidate the focused automated checks: schema/parser parity, real Beads
   reads, HTTP/WebSocket mutation rejection, runtime unavailable versus idle,
   freshness/cache behavior, and route/filter behavior.
2. Walk every specified route with live development data and controlled
   fixtures for rare states. Verify evidence links, missing identities,
   approved/current plan distinction, and useful dependency presentation.
3. Inspect light/dark and OS switching, desktop/tablet/320px mobile, 200% zoom,
   keyboard navigation, focus return, contrast, long content, reduced motion,
   and background/offscreen animation behavior. Retain focused screenshots.
4. Verify malformed NEWS, malformed JSON, database outage, stale observations,
   browser reconnect, and initial empty state without leaking raw errors or
   private paths unnecessarily.
5. Measure normal foreground/background CPU impact and tool-call volume; fix
   obvious polling/animation churn rather than claiming a budget without data.

**Acceptance:** Black/Pyre, frontend/backend type checks, Prettier/ESLint, build,
retained useful upstream tests, and focused integration checks pass. The UI
walkthrough finds no unmet specified state or major responsive/readability
problem. Fix findings within their affected tasks or explicit repair beads.

### Task 31 [code] — Implement optional authenticated remote viewing

**Status:** pending

**Outcome:** remote viewing is supported but remains disabled until the owner
supplies the required host and access configuration.

**Work:**

1. Implement explicit allowed hosts/origins and verification of Cloudflare
   Access tokens for the configured remote hostname: issuer, audience,
   signature, expiry, and failures fetching verification keys. Use a maintained
   verifier rather than implementing cryptography.
2. Apply the same protection to HTML, APIs, authenticated documents, and
   WebSocket upgrades. Remote requests must not obtain localhost bypass through
   forged Host/forwarding headers; reject unknown hosts/origins.
3. Supply configuration/runbook support for a named outbound Tunnel preserving
   the protected hostname. Expose only the loopback Dashboard backend, never
   Dolt, Codex transports, development servers, or administration commands.
4. Keep credentials outside tracked files. Use private/no-store responses for
   API/private documents, allowing immutable caching only for suitable versioned
   frontend assets.
5. Test missing, invalid, expired, and wrong-audience tokens, HTTP/WebSocket
   bypass attempts, and valid authenticated reads using a controlled verifier
   fixture. Record deployment prerequisites in setup documentation.

**Acceptance:** support code and denial tests pass even without a configured
Cloudflare account. Actual tunnel activation is conditional on hostname,
owner identity, credentials, and Access policy; leave it disabled if absent and
record activation as pending optional setup. When configured, test the actual
remote URL and direct-origin denial. Never report remote viewing as verified
from mocks alone. Missing remote configuration does not block local completion.

### Task 32 [skills] — Prove Dashboard delivery through Fulcrum end to end

**Status:** pending

**Outcome:** the first complete Fulcrum project is delivered with real system
evidence, including its own live monitoring view.

**Work:**

1. Reconcile the preceding Dashboard implementation beads (Tasks 22–31) against
   the actual approved plan revision, assignment, review decision, candidate,
   certification, promoted source, configured remote tip, and cleanup result.
   Check the evidence accumulated since Task 21; do not reconstruct a fictional
   history. This acceptance task closes after its own evidence/report is saved;
   it must not depend on its own bead already being closed.
2. Verify normal Archon scheduling and sequential dependencies were used;
   installed roles exchanged genuine handoffs; the Watchman observed real
   activity/waits; NEWS and summaries were updated through their intended owner.
3. Exercise a controlled scoped pause/resume and a restart/reconciliation
   boundary during this project. Rare dangerous failures may rely on earlier
   disposable checks; do not intentionally damage production to fill an audit.
4. Any real tooling problem uses the implemented escalation/recovery path.
   Record additional infrastructure repairs as bounded, linked work with their
   own evidence. Infrastructure repairs are not permission to bypass delivery
   controls or quietly alter the Dashboard contract.
5. Demonstrate Status, Projects, and project Newsfeed displaying the actual
   Dashboard run, its completed beads, remaining obligations if any, and the
   real served version. Verify shutdown/archival cleanup and retained history.

**Acceptance:** all required Dashboard code is certified, source-synchronized,
and cleaned up; its persistent local service is usable. The evidence establishes
that Fulcrum carried the project rather than merely showing it afterward.
Conditional remote activation is reported separately, and incomplete required
work is not relabeled as future scope to claim completion.

### Task 33 [skills] — Complete the first postmortem and operational acceptance

**Status:** pending

**Outcome:** the first real project also exercises Fulcrum's improvement loop.

**Work:**

1. Run a real Sage postmortem on Dashboard delivery, including available
   handoff/CI/hook evidence and bounded interviews with completed agents.
   Restore archival state and publish concrete findings or a justified
   no-findings report.
2. Run the Fulcrum Inquisitor against the now-complete codebase, applying the
   whole-codebase scope. Verify both roles deduplicate findings and create
   future work for Archon consideration, not unapproved implementation.
3. Exercise due-job reporting through Watchman and Archon, then preserve the
   configured daily cadence and twelve-hour offset. An explicit acceptance
   invocation must not create duplicate recurring schedules or false due runs.
4. Update NEWS and project summaries with measured outcomes, recoveries,
   improvements, and any open limitations. Keep raw measurements/evidence local
   and useful lessons brief.
5. Re-run installation health/version checks and reconcile initial project
   readiness, pending source/brain pushes, open interviews, owned processes,
   and schedules. Identify remaining optional setup separately from failures.

**Acceptance:** the Dashboard project has a completed operational acceptance
record; daily analysis and hourly patrol are configured; genuine findings are
tracked and visible. The owner can use the local Dashboard to understand fleet
state and future work without reconstructing this implementation conversation.
No unresolved required completion obligation is hidden behind archived tasks.

## Requirement audit against the original prompt

This audit treats the approved design's refinements as intentional. A task
mapping is a coverage commitment, not a claim that the feature already exists.

| Original requirement | Tasks | Coverage or explicit refinement |
| --- | --- | --- |
| Fulcrum coordinates local Codex tasks across software projects | 01, 09–10, 19–21, 32 | Actual host/task IDs and saved project scope; no substitute chat or scheduling daemon. |
| Every project uses Git, Tollgate, and a Codex Project; Fulcrum manages itself | 02, 10, 19–21, 32 | Initial three projects reconciled; Dashboard is Fulcrum's first complete self-managed project. |
| Private brain with Beads, Markdown, JSON; immediate pushes | 03–07, 19 | Markdown/Git and Beads/Dolt each push immediately after commit. Design refines JSON to local/untracked and allows visible retries after failed pushes. |
| Concurrent fleet access to the brain with simple local operation | 01, 05, 19–20, 23 | Server mode retained; Beads owns automatic startup and lifecycle. Fulcrum does not duplicate its database process management. |
| Archon is strategic, delegates source investigation, maintains NEWS | 10, 13–15, 25–27 | Includes priorities, project summaries, cross-project incidents, and concise briefing. |
| Weaver interviews, plans, approval, discovery, task intake, refinement | 07–08, 11, 21 | Plan-mode writes deferred; queued/future distinct; cold-reader and verifier behavior implemented. |
| Clear tasks executable by a weaker model | 06, 11, 21 | Scope, references, dependencies, constraints, acceptance, and validation in each bead. |
| Paired review/implementation, sequential work, worktrees and promotion | 09, 12, 14, 32 | Explicit authority and exact candidate evidence; three failed reviews escalate under the approved design. |
| Plan dependencies, holds, priorities, throughput, resource learning | 08, 10, 13 | Includes cross-project edges, indefinite holds, quiet intervals, and short evidence-linked lessons. |
| Never silently stop when blocked; repair tooling | 09, 12–15, 18 | Expected waits, escalation chain, bounded diagnosis, owned recovery work, and patrol fallback. |
| Short high-level project memory; code behavior stays with code | 04, 07, 10–11, 16, 33 | Context selection, pruning, retained evidence references, and correct repository boundaries. |
| Hourly persistent Watchman | 15, 19–20, 33 | One human-provisioned role and one idempotent schedule; no duplicate specialist schedules. |
| Daily Sage, interviews, workflow/tooling improvements | 16, 33 | Includes Fulcrum's own efficiency and actionable deduplicated future beads. |
| Daily Inquisitor, twelve-hour offset, major architectural issues | 15, 17, 33 | Separate project-scoped runs and whole-codebase review, not recent-change review. |
| Role lifetimes, task tags, paired numbers, fresh runs | 09–12, 15–17 | Design refines Weaver to ordinary titles and uses real returned task IDs; tags are discovery aids. |
| Default models and deliberate overrides | 01, 03, 09 | Sol high/Luna xhigh, preserved human preferences, explicit Astra authorization provenance. |
| Status, Projects, fleet/project Newsfeed | 24–27 | All specified routes, details, filters, dependencies, summaries, and useful empty states. |
| Coherent futuristic visual system, whitespace, contrast, themes, mobile, animation | 24–25, 28, 30 | Exact design tokens, role emblems, OS themes, accessible focus, reduced/background motion. |
| Read-only Dashboard | 22–23, 30–31 | Server rejects mutations on HTTP and WebSocket; controls removed. |
| React/Vite dashboard, possibly based on beads-ui | 22–24, 29 | Approved design chooses beads-ui's lit-html plus Node/Express; Vite builds/dev only. No React rewrite. |
| Persistent secure remote endpoint, if possible | 31 | Support included; actual Cloudflare activation conditional on owner configuration, disabled otherwise. |
| Black, Pyre, TypeScript, Prettier, ESLint, minimal unit tests, Tollgate CI | 02, 22, 30 | Small meaningful boundary checks plus production build; retain useful upstream checks. |
| Continuous workflow and architecture improvement visible to the user | 10, 16–17, 25–27, 33 | Prominent NEWS content and real first-project postmortem, not only diagnostic logs. |
| Infrastructure before Dashboard; Dashboard as fully realized project test | 01–20, 21–33 | Explicit readiness gate, genuine lifecycle evidence, production operation, and improvement loop. |

Additional safeguards introduced by the approved design are also covered:
single-owner atomic JSON (03–04), separate reported/runtime status (01, 23, 25),
desktop hook verification (18–20), Git/Dolt restoration (05–06), lost-agent
recovery (14), schema/skill updates (19, 29), partial-data UI behavior (23, 28),
and authenticated WebSocket access (31).

## Remaining setup inputs and limits

No additional product decision is needed to start Task 01. Do not turn these
setup inputs into invented defaults or blockers for unrelated tasks:

- Actual host/project/task/Tollgate IDs: discover during enrollment; do not
  hard-code this planning session's observations.
- Existing brain contents and remote access: inspect and preserve before
  migration; obtain missing credentials through normal setup if necessary.
- Human-created Archon and Watchman identities, and initial Sage time: collect
  during Task 19; user-provisioned persistent roles are part of the product contract.
- Desktop hook coverage and access to existing runtime observations: prove
  capability or retain the documented unavailable state. Do not broaden scope
  into private database scraping or a replacement runtime.
- Remote hostname, owner identity, and Cloudflare configuration: needed only
  for actual optional activation in Task 31.

The supplied design already resolves the meaningful original-prompt differences
listed above. If implementation discovers a contradiction that changes product
behavior rather than a missing setup value, record the exact conflict and ask
one focused question with a recommended answer before implementing that change.
