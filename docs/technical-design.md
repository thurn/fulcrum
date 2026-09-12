# Fulcrum Technical Design

Status: approved design direction; implementation specification.

**Fulcrum** coordinates software development performed by local Codex tasks.
It gives those tasks durable assignments, explicit authority, shared project
memory, and a human-readable view of the fleet. Its primary product is reliable
coordination: turning approved intent into completed, certified changes while
keeping work moving when tooling, dependencies, or resource contention fail.

**Tollgate** is the local CI and promotion system required by every managed
software project. **Beads** is the issue tracker that owns individual tasks and
their dependencies. The **brain** is the private repository at `~/brain` holding
Beads history and high-level Markdown documents. Remaining JSON coordination
state stays local and untracked. Fulcrum's implementation and this
specification belong to the public repository at `~/fulcrum`.

V1 operates on one Mac for one human owner. It uses agent skills, Python
workflow scripts and Codex lifecycle hooks, a Beads-managed local Dolt server,
and a read-only dashboard forked from beads-ui using lit-html and its
existing Node backend.
Agents make decisions; small scripts assist with repetitive operations.
There is no additional background service making scheduling decisions.

Dashboard implementation starts from the screen and design-system mockups in
[`docs/mockups/`](mockups/) as concept art. They establish the intended visual
direction, information hierarchy, composition, responsive treatment, and
component language. They are not pixel-accurate interface contracts; the
[dashboard specification](dashboard.md), live product data, responsive behavior,
and accessibility requirements govern whenever a mockup is incomplete or
conflicts with the written design.

## Related Information

These documents form one specification. The main document explains ownership
and expected behavior; the appendices define the corresponding contracts.

- [Data and agent contracts](contracts.md): identity, persistence, messages,
  lifecycle transitions, and interface examples.
- [Operations and recovery](operations.md): scheduling, resources, Tollgate,
  escalation, periodic work, and compatibility.
- [Lifecycle hooks](hooks.md): compaction refresh, bounded handoff reminders,
  and selective diagnostics for the Sage.
- [Dashboard and design system](dashboard.md): views, information hierarchy,
  visual tokens, motion, data access, and authenticated remote viewing.
- Dashboard concept art is the starting point for the implementation's screen
  composition and visual system:
  - [Status screen, desktop](mockups/landing-desktop.png) and
    [Status screen, mobile](mockups/landing-iphone.png): agent-fleet hierarchy,
    navigation, responsive card treatment, and featured improvements.
  - [Projects screen](mockups/projects-desktop.png): project summaries, work
    counts, future direction, constraints, and recent improvements.
  - [Newsfeed screen](mockups/newsfeed-desktop.png): featured updates, filters,
    search, and work-item cards.
  - [Design-system primitives](mockups/design-system-primitives.png): surfaces,
    semantic colors, typography, spacing, focus treatment, and role emblems.
  - [Design-system components](mockups/design-system-components.png):
    navigation, filters, status indicators, cards, and the
    featured-improvement banner.
- [Beads][beads] and its [sync documentation][beads-sync]: authoritative issue
  storage and the distinction between Git and Dolt synchronization.
- [Codex app-server documentation][codex-server] and
  [scheduled tasks][codex-schedules]: runtime observation and scheduled wakes.
- The local [wt skill][wt] and [implement-plan skill][implement-plan]: existing
  worktree, review, messaging, and promotion contracts adapted by Fulcrum.
- [Tollgate's design][tollgate-design]: exact-candidate certification, resource
  scheduling, and promotion semantics.
- [grill-me][grill] and [technical-design-docs][design-skill]: Weaver interview
  and document-comprehension practices.
- [qq][qq]: answering mixed project questions before dependent task actions.
- [Thermo-nuclear code-quality review][inquisitor-reference]: inspiration for
  ambitious architectural simplification and type-boundary review.
- [beads-ui][beads-ui]: the dashboard foundation for issues, epics, and
  dependencies.
- [The Shape of Things to Come][shape], [Fences, not Sandboxes][fences], and
  [Welcome to Gas Town][gastown]: coordination inspiration, not dependencies or
  normative implementation contracts.

[beads]: https://github.com/gastownhall/beads
[beads-sync]: https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md
[codex-server]: https://learn.chatgpt.com/docs/app-server
[codex-schedules]: https://learn.chatgpt.com/docs/automations?surface=app
[wt]: /Users/dthurn/.llms/skills/wt/SKILL.md
[implement-plan]: /Users/dthurn/.llms/skills/implement-plan/SKILL.md
[tollgate-design]: /Users/dthurn/tollgate/docs/technical-design.md
[grill]: /Users/dthurn/.llms/skills/grill-me/SKILL.md
[design-skill]: /Users/dthurn/.llms/skills/technical-design-docs/SKILL.md
[qq]: /Users/dthurn/.llms/skills/qq/SKILL.md
[inquisitor-reference]: https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md
[beads-ui]: https://github.com/mantoni/beads-ui
[shape]: https://yegge.ai/listings/the-shape-of-things-to-come
[fences]: https://yegge.ai/listings/fences-not-sandboxes
[gastown]: https://yegge.ai/listings/welcome-to-gas-town

The local links identify the reference environment. The specification below
states the required adaptations explicitly; installed Fulcrum skills must not
depend on those personal absolute paths being present on another machine.

## System Boundaries

A **project** is a registered software repository connected to both Tollgate
and a saved Codex Project. Fulcrum, Tollgate, and Battlement are the initial
projects. Fulcrum manages itself through the same normal execution process.

- Registration records stable project and host identities, the host-local
  repository path, Codex Project identity, and Tollgate repository identity.
- A project cannot receive normal implementation assignments until both
  integrations are available and their identities match.
- The brain is a data repository, not a managed software project. Its immediate
  changes use ordinary Git and Beads commits and pushes, not software CI.
- V1 uses one shared brain checkout and its Beads-managed local Dolt server.
  Host-qualified paths and task identities leave room for multiple hosts without
  pretending that cross-host dispatch or replica reconciliation already exists.

A **task** or **agent** in this specification means a local Codex task with
persistent history, corresponding to the app's thread APIs. A **subagent** is a
bounded helper inside a task; it does not replace a named fleet task.

The execution boundaries are deliberately small:

- Codex skills define roles and invoke the app's project/task tools.
- Python scripts help read context, update owned local state, inspect resources,
  and manage the dashboard service. Database lifecycle uses Beads' own commands.
- Codex hooks refresh context after compaction and remind implementation pairs
  about handoffs. Scheduling and promotion stay with the agents and Tollgate.
- Beads owns issue content, priority, dependencies, and task status.
- Tollgate owns CI execution and certified Git promotion.
- The forked beads-ui backend reads Beads, Markdown, local JSON, and available
  runtime evidence. It makes no scheduling decisions.
- Beads starts and manages the brain's local Dolt server. Fulcrum verifies its
  health and uses supported Beads commands for recovery; it does not add a
  database supervisor. The server makes no fleet decisions.
- An optional Cloudflare connector exposes only the authenticated dashboard.

## Agent Roles

Roles are responsibilities attached to identifiable Codex tasks, but not every
role has a durable registration. Persistent and implementation roles start by
reconciling their identity and reading the small amount of project memory
relevant to their assignment. Weavers are short-lived, unregistered authoring
actors and never create role or progress records. Registered roles receive
concise refreshers after compaction. Active Executors and Overseers also receive
a bounded handoff reminder when needed at a normal stop. The [hooks appendix](hooks.md)
defines this limited scope.

| Role | Responsibility | Creation and lifetime |
| --- | --- | --- |
| Archon | Fleet strategy, scheduling, recovery, human contact | Human-created; persistent role |
| Weaver | Interviews, plans, task intake, refinement | Ephemeral; archives after one-way completion report |
| Overseer | Assignments, code review, promotion authority | Archon-created; one execution run |
| Executor | Implementation, validation, CI repair, cleanup | Archon-created; paired execution run |
| Night Watchman | Patrols and due recurring-work reports | Human-created; persistent |
| Sage | Workflow postmortems and improvement beads | Archon-created; one postmortem run |
| Inquisitor | Project-wide architectural review beads | Archon-created; one project review |

### Archon

The **Archon** is the strategic coordinator and default human point of contact.
Invoking `$archon` registers the current task as the Archon and transfers the
role if another task previously held it. Exactly one task has current authority.
See [Archon handover](contracts.md#archon-handover).

- Read project plans, summaries, constraints, and concise role memory. Keep a
  short Markdown briefing of current priorities, scheduling rationale, and
  unresolved decisions; update it when decisions change.
- Delegate source-code investigation to a bounded specialist or appropriate
  project agent. The Archon does not write code or execute builds.
- Coordinate work across projects, including conflicts, priorities, maintenance,
  resource pressure, and dependencies that are not obvious from one bead.
- Create Overseer/Executor pairs and scheduled specialist tasks using actual
  Codex Project scope.
- Maintain `NEWS.md` and current project summaries after meaningful changes,
  prominently reporting workflow improvements and architectural refactoring.
- Adjudicate escalations rather than relay an agent's interpretation blindly.
- Be the only fleet role that decides blocked execution needs human input.
  The Weaver's intentional planning interviews are a separate authoring flow.

For example, a performance measurement requiring a quiet machine causes the
Archon to checkpoint conflicting work, verify resource drainage, dispatch the
measurement, and subsequently release the hold. A startup crash may outrank
ordinary feature work even when that feature work has already started.

### Weaver

The **Weaver** converts human intent into implementation-ready plans and beads.
Invoking `$weaver` starts an ephemeral authoring flow. Weaver tasks keep their
ordinary descriptive titles, do not receive numbered role tags, and do not
create or update a role registration or durable progress record.

In Codex Plan mode, the Weaver interviews one material decision at a time and
uses repository exploration to answer discoverable questions. It records the
plan's explicit activation choice, while standalone direct intake defaults to
queued unless the human explicitly asks to save the work for later.

- No brain files or beads are modified during Plan mode. For a refinement, the
  currently approved revision remains effective throughout the interview.
- The approved Codex plan describes writing or refining the project plan and
  creating its beads. “Implement this plan” authorizes saving that work.
- The resulting project plan stands alone, explains the implementation
  contract, and gives each task concrete completion criteria.
- For substantial plans, a fresh cold reader receives only the document. A
  separate verifier compares the document with the original requirements and
  interview decisions. Both checks finish before the document is committed.
- Beads include enough scope, dependencies, context, and acceptance criteria for
  a weaker model to execute without reconstructing the planning conversation.
- The Weaver commits and pushes the Markdown, then commits and pushes the
  related Beads changes. It sends at most one completion report to the current
  Archon when routable, does not wait for acknowledgement, and archives after
  the send result. A failed push or report is surfaced without creating durable
  Weaver coordination state.

Outside Plan mode, the Weaver explicitly states that it is using task intake
and does not intend to create a project planning document. It answers project
questions, clarifies task scope, and creates beads directly. Standalone tasks
and task lists are queued by default; only an explicit human request saves them
for later. This resembles the existing `$qq` discussion pattern without
requiring large-plan reviews. Cold-reader and requirements-verifier passes are
skipped for this flow.

Refinement updates the existing plan identity and related beads. Approval of a
revision that affects an active assignment triggers a targeted pause and
contract reconciliation; it does not silently rewrite the Executor's scope.
See [plans and activation](contracts.md#plans-and-activation).

### Overseer and Executor

An **Overseer** and its **Executor** form one pair for an approved plan or task
collection. The pair remains together across sequential bead assignments and
is archived after its run finishes. Independent runs may proceed concurrently.

- The Overseer assigns one bead at a time, reviews exact submitted code and
  evidence, and grants explicit scope-bound promotion authority.
- The Executor owns its worktrees, implementation, local checks, candidate
  submission, merge-conflict repairs, in-scope CI repairs, and cleanup.
- Normal worktrees are created through `$wt` from certified `release`.
- The Overseer may inspect its Executor's worktree read-only. It does not edit
  or run builds, tests, or Tollgate mutations there.
- After three unsuccessful substantive reviews, the Overseer escalates to the
  Archon. There is no third-review automatic promotion rule.
- Candidate submission, review, certification, promotion, remote push, and
  resource cleanup remain distinct, observable events.

The pair uses Codex messages and records expected waits before ending turns.
An Executor waiting for a named Overseer's review is healthy; one that stops
without a completion or recorded handoff is not. A stop hook can remind the
agent to check its handoff once, trusting its existing progress report. It does
not independently verify delivery. A final response in the Executor's own task
does not notify the Overseer; the agent must use the messaging tool.

### Night Watchman, Sage, and Inquisitor

The **Night Watchman** is the hourly scheduled patrol task. It compares brain
metadata with available Codex and Tollgate evidence, reports anomalies to the
Archon, and reports when other recurring jobs are due. It does not become a
second scheduler or silently repair product code. Its patrol also covers missed
reports, unexpected archives, and unavailable hooks; a hook event alone is not
a continuous liveness signal.

The **Sage** performs a fleet-wide workflow postmortem every 24 hours. She reads
handoff reports and available logs, temporarily reopens completed agents for
interviews, and rearchives them afterward. Findings must name actionable
improvements, evidence, affected projects, and expected benefit. Improving
Fulcrum itself is an explicit central responsibility: she examines its skills,
scripts, agent workflows, coordination overhead, and resource use, proposing
simpler and more efficient ways of working. She files or updates beads for
these changes; the Archon decides when to execute them and highlights their
outcomes in `NEWS.md` and the dashboard. Hook latency, unnecessary reminder
turns, and excessive context injection are part of that review, using existing
logs before adding narrowly scoped diagnostics.

The **Inquisitor** reviews each enabled project every 24 hours, twelve hours
offset from the Sage. Each project gets a fresh task. The review scope is the
entire codebase: recent commits and recently written code receive no special
emphasis. The Inquisitor seeks the biggest architectural problems, including
overloaded files, duplicated responsibilities, brittle boundaries, and invalid
states that the type system could eliminate. Findings become deduplicated
beads, not unsolicited direct edits. Architectural improvements are prominent
content in `NEWS.md` and the dashboard.

## Models and Fresh Context

Model policy keeps routine work economical while allowing deliberate upgrades.
The actual model and reasoning setting are recorded for each assignment.

| Role | Default model | Reasoning |
| --- | --- | --- |
| Archon, Night Watchman | Human-selected | Human-selected |
| Overseer, Sage, Inquisitor | `gpt-5.6-sol` | `high` |
| Executor | `gpt-5.6-luna` | `xhigh` |

Weaver task settings remain with the human-created task and are not copied into
Fulcrum registration or assignment records.

- Explicit assignment instructions override role defaults. For example,
  “Use Astra High for this bead” authorizes that exact scope.
- The Archon may autonomously upgrade delegated work to Sol. Astra requires
  explicit human authorization; an agent-written bead cannot manufacture it.
- An unavailable model causes escalation or an explicitly authorized policy
  change. Never silently substitute a different model.
- Named fleet roles are real local Codex tasks. Short research helpers and
  document readers may be subagents, with enough bounded context for their job.
- Executors and Overseers persist for their run, while each new run, Sage
  postmortem, or project Inquisitor review starts with fresh context.

### Context continuity

Persistent roles use a short post-compaction refresher assembled from current
role instructions, memory, and assignment state. This restores important rules
and current priorities without reinjecting the entire history. Consequential
decisions still require checking current records.

The Archon remains a persistent user-created task. A fresh task can take over
through the existing handover if drift or runtime problems persist. There is
no scheduled rotation by default. The Sage reviews repeated investigations,
forgotten constraints, and obsolete assumptions as workflow problems.

## Scheduling and Forward Progress

The Archon considers the whole fleet whenever work becomes available or its
constraints change. It need not be prompted to think beyond the current bead.

Eligibility combines several independent facts:

- The work is ready and queued through its plan or a standalone bead's
  activation label, rather than saved as future work. Missing standalone
  activation resolves to queued; direct intake uses future only when explicitly
  requested.
- Its Beads prerequisites and applicable plan-level prerequisites are complete.
- No human or Archon hold prevents execution in its scope.
- Its project integrations are available and an Executor can own the work.
- Its expected resource use is compatible with current reservations and any
  exclusive operation.

Natural-language constraints become durable, inspectable records. For example:

```text
Hold Battlement implementation until the startup refactor is promoted.
Reason: reduce conflicting edits while the entry-point contract changes.
Release condition: all required refactor beads complete certified promotion.
```

The Archon prioritizes useful throughput. It considers local CPU and memory
pressure, Tollgate queues, heavy Executor-local commands, recent task durations,
and failures caused by contention. It learns general resource characteristics
in concise project memory, with measurements retained separately from lessons.

“Pause all work” means stop dispatch and checkpoint promptly. The pause is not
complete until agents and relevant processes acknowledge a safe boundary.
Overdue or unclear behavior causes escalation and investigation, not invented
success. [Operations](operations.md) specifies drainage and recovery.

## Persistence and Project Memory

The three data forms have distinct responsibilities:

- Beads owns issue descriptions, task priority, dependencies, and open/closed
  status. A single brain-wide issue graph supports cross-project dependencies.
- Markdown owns project plans, high-level decisions, concise role memory, and
  human-facing `NEWS.md` updates.
- Local untracked JSON records agent identity, assignments, expected handoffs,
  scheduling holds, and other operational state. Each file has one writer and
  is updated by atomic filesystem replacement. Optional hook diagnostics stay
  in local logs; hooks never rewrite agent-owned assignment state.
- Runtime caches and raw build logs are not project memory. Retain pointers to
  existing evidence rather than copying source code or large tool output into
  the brain.

Use ordinary Git to commit and push Markdown. Beads uses Dolt for issue history
and its supported commands to commit and push that history to the same private
GitHub repository. A normal Git push alone does not include database edits.
[Beads documents this distinction][beads-sync]. Server mode supports concurrent
Beads clients on the same Mac. Embedded mode runs Dolt inside each `bd` process
and permits only one writer; the fleet uses server mode so independent agents
can access the shared brain concurrently. Neither mode requires cloud hosting.

Use Beads' automatic server startup and its `bd dolt start`, `status`, and
`stop` commands in the brain directory. Verify that behavior with the selected
version. Fulcrum does not add a database LaunchAgent, process supervisor, or
global lock around Beads edits. Database backup, migration, and synchronization
remain Beads/Dolt operations.

Commit Markdown changes, then commit related Beads changes. Immediately attempt
each corresponding push. On a network failure, retain the local changes,
report the failed push, and retry while other work continues. Human edits and
commits use ordinary tools and are visible without a special Fulcrum command.
There is no cross-store transaction or publication manifest. The dashboard
reads current data and accepts brief differences between sources.
See [ordinary operations](contracts.md#ordinary-git-and-beads-operations).

Memory stays short and useful:

- Give named roles concise global and per-project memory views. The Archon and
  Weaver maintain their applicable views with ordinary Markdown edits.
- Store conventions such as “panic rather than return Result at this project
  boundary,” with context explaining when they apply.
- Store general lessons, not inventories of filenames or rapidly changing
  implementation details.
- Replace superseded guidance and merge repeated lessons; preserve decision
  history in Git rather than forcing every agent to read it.
- Keep code-behavior guidance with its code, often as short discoverability
  skills. For example, Battlement's Reactant skill points agents to the public
  UI authoring interfaces and examples rather than copying their behavior into
  central project memory.
- Prune closed issues only after postmortem needs and live references have been
  resolved. Keep enough retained identifiers to recognize delayed messages.

## Dashboard and Daily Operation

The dashboard is a persistent localhost view operated by the Archon. The Archon
can start, inspect, and request repair of its managed processes through scripts;
it delegates code and asset builds to Executors or setup tooling.

- **Status** shows active, waiting, scheduled, and recently completed agents,
  including missing handoffs and hook health in their operational details.
- **Projects** shows concise current summaries and future plans from structured
  `NEWS.md` content, linked to live work counts.
- **Newsfeed** shows bead-centered cards for running, completed, ready, held,
  and future work, including project-specific feeds. Workflow improvements and
  code refactoring receive prominent coverage alongside task progress.
- Desktop navigation uses a left sidebar and two-column card layouts. Mobile
  uses one column and compact navigation.
- The science-fantasy visual system uses violet and gold, role emblems,
  intentional whitespace, readable text, and status motion.
- V1 permits viewing, filtering, searching, and navigation, but no workflow
  mutations through the dashboard API.

The dashboard forks beads-ui, retaining its lit-html components, issue data
access, and Node/Express backend. Vite provides frontend development and builds;
the persistent Node backend serves built assets and read-only data. Python
remains the language for agent workflow helpers.
Optional Cloudflare Tunnel and Access provide authenticated remote viewing.
See the [dashboard specification](dashboard.md) for exact behavior and tokens.

## Implementation Constraints and Compatibility

Fulcrum must make integration limitations observable instead of converting
missing evidence into a claim that an agent is healthy or a task is complete.

- Use Black and Pyre for Python. Use TypeScript for new dashboard code, with
  Prettier and ESLint. Keep upstream checked JavaScript where it works; do not
  require a wholesale conversion to adopt the fork.
- Register Fulcrum's checks with Tollgate. Keep unit tests focused and small,
  with meaningful integration checks at Beads and runtime boundaries.
- Prefer current Codex task IDs returned by creation tools or exposed to the
  task itself. Archon-created roles use numbered tags as discovery aids;
  Weavers retain ordinary descriptive titles.
- Persist real task/host IDs only after creation resolves. A temporary
  `clientThreadId` is not a routable task ID.
- Verify compaction refresh and bounded stop reminders in the desktop runtime,
  including their latency and extra model turns. Native permissions, role
  skills, and Tollgate still apply when hooks are unavailable.
- Feature-detect read-only Codex runtime observation. The inspected environment
  had task-management tools but no shared app-server control socket at the
  CLI's default location. Creation-tool availability does not establish that
  the dashboard can observe live runtime state.
- Display reported workflow state separately from runtime observations and
  their timestamps. A disconnected runtime adapter reports unavailable status.
- Normal repairs use Tollgate worktrees. If Tollgate cannot create one and
  operational recovery fails, the Archon may authorize a recorded emergency
  ordinary Git worktree for repairing Tollgate. Certification remains required
  before the resulting source lands.

The implementation must include capability checks, compatible-version records,
and the [operational compatibility behavior][compatibility].
These documents describe the required product, not a delivery schedule.

[compatibility]: operations.md#compatibility-and-setup
