# Fulcrum

Fulcrum is a local orchestration system for coordinating Codex
software-development tasks across Git repositories that you enroll. You describe
the outcome once; Fulcrum turns it into durable work, sends implementation and
review to separate Codex conversations, and carries an approved change through
CI and repository integration. This avoids making a human manually coordinate
agent handoffs, worktrees, retries, and delivery.

The division of responsibility is deliberate: Python owns the reliable workflow
mechanics, while agents own the judgment. A local Python controller records state
and performs dispatch, recovery, and delivery. Codex agents decide what the work
means, how to implement it, and whether the result is correct.

## The vocabulary

Fulcrum uses a few terms throughout its status output and conversations:

- A **Codex task**, also called a **thread**, is a managed conversation in Codex
  desktop. It has a runtime thread ID. It is **not** a task in the Beads issue
  tracker.
- A **project** is an enrolled Git repository together with its Codex and
  Tollgate identities, validation command, and delivery configuration.
- **Beads** is the issue tracker. One **Beads issue**, or **bead**, is a durable
  unit of proposed work, including its described scope and dependencies.
- The **private brain** is a private Git repository containing plans, shared
  memory, and Beads history. It is separate from Fulcrum's SQLite operational
  state.
- The **controller** is Fulcrum's local Python process and sole owner of durable
  workflow changes. It creates and messages managed Codex tasks, records state,
  dispatches work, retries recoverable operations, publishes results, delivers
  code, and archives finished conversations after they have remained idle for 10
  minutes and their Weaver lineage has no pending or active work.
- **Tollgate** is the source-delivery and continuous-integration system. A
  **candidate** is its retained, immutable source submission. Tollgate certifies
  the candidate's checks and **promotes** it into the repository's configured
  integration branch, then performs configured source synchronization and
  worktree cleanup.
- A **run** is an Archon-approved, project-scoped, ordered group of beads. Runs
  can proceed concurrently when capacity and conflicts allow.
- An **assignment** binds one approved bead and its exact scope to an isolated
  worktree and an **Executor/Overseer pair**: one agent implements, and a
  different agent reviews. Only one member of the pair runs at a time.

## How work moves through Fulcrum

1. **The human submits intent through Weaver.** The human creates a Codex task in
   an enrolled project and registers it as Weaver. Weaver clarifies the request
   and files one bead or a dependency graph. Filing makes pending work eligible
   for consideration; it does not approve implementation.
2. **Archon decides what should run.** The controller presents pending work and
   relevant constraints to Archon. Archon alone approves, prioritizes, groups,
   and schedules pending implementation work. Approval creates one or more runs.
3. **The controller prepares isolated execution.** When an approved run is
   eligible, Python reserves capacity, asks Tollgate for a fresh worktree, and
   creates or reuses the Weaver lineage's managed Executor Codex task. Humans
   create neither Executor nor Overseer tasks.
4. **Executor implements.** The controller gives Executor the approved scope and
   isolated worktree. Executor investigates, edits, validates, commits, and
   submits evidence. The controller captures the exact commit as an immutable
   Tollgate candidate.
5. **Overseer reviews independently.** When the retained candidate and evidence
   are ready for review, the controller creates or reuses the lineage's Overseer
   and gives it the exact candidate, scope, and evidence. Overseer reads the source
   without editing the Executor worktree. It approves candidates that may ship
   unchanged, optionally
   records nonblocking minor fixes, requests blocking changes, or identifies missing
   evidence. The controller routes outcomes and any required correction.
6. **The controller and Tollgate deliver.** After approval, the controller asks
   Tollgate to certify and promote the candidate. Tollgate runs the configured
   checks, integrates the code into the repository's main/integration branch,
   synchronizes it as configured, and cleans up. The controller verifies those
   results, closes the bead, advances the run, and schedules the pair for archival
   once the whole lineage has no pending or active work.

Each registered Weaver defines a durable numeric lineage. Its primary trio uses
the same number in canonical names—for example `WVR0032`, `EXE0032`, and
`OVR0032`. Direct intake and every member of graph intake retain that Weaver
identity through Archon approval, runs, and assignments. Later assignments and
follow-up turns reuse the original Executor and Overseer native conversations when
they remain safely idle and compatible, including after controller restarts and
prior run completion. New lineage work cancels pending completion archival.
After the final pending or active lineage assignment closes, Fulcrum re-arms those
obligations for every eligible conversation in the lineage and applies the normal
idle delay.

When a same-role conversation cannot safely be reused because it is archived,
retired, unavailable, active in incompatible work, nonterminal, or configured for
different project/model authority, Fulcrum creates a stable overflow identity in
the same lineage: `EXE0032B`, then `EXE0032C` (and likewise for Overseer). The
unsuffixed primary is never renamed, different Weavers never share workers, and
persisted provisioning intent makes retries and reconciliation reuse the same
chosen suffix instead of creating duplicates.
Existing pre-lineage canonical names, including archived tasks, also reserve their
visible identity so mixed lineage and unlineaged provisioning remains collision-free.

An Overseer approval is not itself a merge. Code reaches the main/integration
branch only after Tollgate certifies and promotes the exact approved candidate.
If CI fails, the controller returns the retained diagnosis to Executor for a
real fix rather than blindly rerunning the same source.

## Who has authority

The human-facing entry points are **Weaver** for submitting work, **Sage** for a
read-only investigation of one retained work item, and **Archon** for strategic
coordination. Archon is the only role that approves, prioritizes, or schedules
pending implementation work. A queued Sage or Inquisitor request separately
authorizes one analysis run, but it neither approves the resulting findings for
implementation nor bypasses normal capacity controls.

| Role | Judgment it owns | How it is invoked | What it does not do |
| --- | --- | --- | --- |
| **Archon** | Chooses which pending outcomes to approve, their priority, run grouping, capacity, holds, and responses to unresolved exceptions. | Setup creates the current Archon. The controller wakes it with pending proposals or material updates; a human can locate its long-lived conversation for strategic direction. | Does not implement code, run builds, dispatch agents, or write operational records. |
| **Weaver** | Clarifies human intent and authors an implementation-ready bead, task graph, or substantial plan. | A human creates a Codex task and registers it as Weaver; the returned instructions guide direct intake or Plan Mode. | Does not approve or schedule the bead, implement it, or manage publication, retries, or archival. |
| **Executor** | Decides how to implement the exact approved scope, which proportionate checks to run, and how to make bounded in-scope corrections. | The controller reuses the Weaver lineage's safely idle Executor, or creates its next deterministic overflow identity, when an approved assignment is eligible. | Does not choose its own scope, create or promote a Tollgate candidate, push its worktree branch, review itself, or write Fulcrum's operational state. |
| **Overseer** | Independently decides whether the exact candidate satisfies scope; it owns blocking findings, nonblocking minor fixes, approval, and any narrow repair permission. | The controller reuses the lineage's safely idle Overseer, or creates its next deterministic overflow identity, after Executor finishes and the immutable candidate and evidence are available. | Does not edit source, build in Executor's worktree, contact Executor directly, certify or promote code, or perform delivery. |
| **Sage** | Reviews **workflow effectiveness**: failures, wasted effort, handoff friction, and evidence-backed process improvements. | A human can invoke `$sage` for one retained work item; the controller also runs queued and recurring reviews. | Does not implement findings, approve them for implementation, set its own cadence, schedule interviews, or publish its own report and issues. |
| **Inquisitor** | Reviews **project architecture** across the selected codebase and proposes evidence-backed structural improvements. | The controller runs it from an Archon-approved recurring policy, an Archon request, or an explicit one-off human request. | Does not edit product source, authorize implementation or promotion, or publish its own report and issues. |

The controller, not an agent, owns SQLite state, Beads publication, dispatch,
handoffs, bounded retries, specialist report publication, code delivery, and
archival. Tollgate, not Fulcrum's agents, owns candidate CI, certification,
promotion, integration, configured source synchronization, and worktree cleanup.
Executor edits and validates only in its assigned isolated worktree; Overseer is
an independent read-only reviewer.

## Install on macOS

Keep this Git checkout in its permanent location and run:

```sh
./scripts/setup
```

On first use, the guided setup asks only for choices it cannot infer. It saves
ordinary local configuration, creates or reuses `.venv`, installs the locked
dependencies and editable package, prepares or restores the private brain, and
installs the CLI, human-entry skills, and instruction-refresh hook. It enrolls
selected repositories, configures their validation and delivery integration,
and initializes SQLite. It also creates Archon, waits for Archon's initial
capacity and recurring policies, and runs readiness checks. Re-running the same
command repairs or resumes the retained installation rather than creating a
second fleet.

Setup installs two separate per-user `launchd` services:

- the Codex app-server; and
- the Fulcrum controller, which connects to that app-server.

Codex desktop is another client of the **same** app-server. Do not start the
app-server or controller manually. To join the shared runtime, launch the desktop
from the retained checkout after setup completes:

```sh
./scripts/launch_codex.sh
```

A normal Dock launch or an environment variable added to `.zshrc` does not
reliably connect the desktop to the shared listener. If desktop is already using
its private runtime, finish or drain those conversations and relaunch it through
this command; setup will not silently terminate or migrate them. The script honors
the supported `FULCRUM_CONFIG` and `FULCRUM_CONTROL_ROOT` overrides and delegates
to the configuration-aware launcher installed in the control directory. That
installed launcher is an internal implementation detail and should not be invoked
directly.

For unattended setup, supply the same configuration as JSON:

```sh
./scripts/setup --config /absolute/setup.json --non-interactive
```

The command exits nonzero and names any missing choice, credential, consent, or
integration capability. See the detailed [setup guide](docs/setup.md) for paths,
services, and diagnostics.

## Common workflows

### Submit work with Weaver

Open a new Codex task rooted in an enrolled repository and invoke the installed
`$weaver` skill. It immediately runs the following registration command; you can
also ask the task to run it explicitly:

```sh
fulcrum weaver register --project fulcrum --description 'Fix empty search results'
```

Registration binds and names the current conversation and returns its authoring
instructions. The description is required and becomes the task-specific suffix in
the conversation name. Describe the desired outcome in that conversation. Weaver
will ask only material questions, inspect the repository as needed, and file the
result.
For a small request, the underlying intake is equivalent to:

```sh
fulcrum intake --input - <<'JSON'
{
  "project": "fulcrum",
  "title": "Fix empty search results",
  "description": "Show an empty state when search has no matches and test matching and empty results.",
  "activation": "pending"
}
JSON
```

Use `"activation": "future"` only for deliberately deferred work. Normally the
Weaver agent runs `intake` and finishes its authoring action for you. The human
creates and registers Weaver; Fulcrum creates and manages all Executor and
Overseer tasks after Archon approval.

### Report an incidental follow-up

Use the installed `$bead` skill for one small, understood, implementation-ready
problem discovered incidentally during a session, such as a pre-existing defect,
tooling failure, or workflow friction. It gathers concise evidence and submits one
JSON object through `fulcrum report --input -`. Run `fulcrum report --help` for the
complete schema and stdin example. File independent problems separately; use
`$weaver` when the work needs substantial planning, dependent tasks, or material
clarification.

`fulcrum intake` remains Weaver's interface for authoring a requested task or task
graph. `fulcrum report` is the lightweight follow-up interface and does not
register, approve, schedule, reopen, or delay managed work. An ordinary
unregistered Codex task may report directly. A managed Fulcrum task may report a
session follow-up only after its current action has an accepted finish outcome.

### Find Archon

```sh
fulcrum archon
```

This read-only lookup prints the current Archon's thread ID and title. The
installed `$archon` skill runs the lookup and presents the native task link; it
does not turn the current conversation into Archon. Use that conversation for
priorities, tradeoffs, holds, and other strategic coordination—not routine
manual dispatch.

### Request a workflow or architecture review

```sh
fulcrum sage register --item brain-123 --description 'Review failed handoff workflow'
fulcrum sage request --scope "Investigate why recent review handoffs have been slow"
fulcrum inquisitor --project fulcrum --scope "Review the indexing architecture"
```

`sage register` is the command used by the installed `$sage` skill. It adopts the
calling task and returns the complete instructions and exact retained evidence for
the named retained item. For non-Weaver work without a causal workflow boundary,
it uses the exact Bead assignment and retained Executor/Overseer pair. `sage request`
retains the existing queued fleet or
project review behavior. `inquisitor` reviews all enabled projects by default.
`--project <id>` limits a queued request, and `--scope <prompt>` supplies a focus.
A queued command returns its request ID without waiting for the report. Direct-item
Sage findings are always pending work for Archon's consideration. A queued
specialist may explicitly defer a finding as future work, and a match may update an
existing bead instead of creating one. No analysis command authorizes
implementation.

### Inspect work and health

```sh
fulcrum status --json
fulcrum status --queue
fulcrum status --capabilities
fulcrum status --run 3 --events 20
fulcrum doctor --json
fulcrum usage --assignment 7 --group-by assignment
fulcrum cost --workflow weaver-action:12 --group-by workflow
```

`status` reports managed tasks, runs, assignments, stages, runtime activity,
capacity, holds, pending decisions, delivery obligations, and recent events.
Use `--queue` for waiting work, `--capabilities` for dispatch readiness and
project capacity, and `--run <integer>` to focus on one run. `doctor` checks the
assembled installation and reports concrete service, runtime, project, brain,
and dependency failures.

`usage` reports durable raw token telemetry. `cost` reports a frozen estimate of
equivalent public OpenAI API charges with direct versus helper-attributed totals,
token/tool components, rate provenance, counts, assumptions, exclusions, and
coverage. It is not actual ChatGPT subscription usage, credits, billing, internal
cost, or marginal cost. Ordinary input is total input minus cached and cache-write
input; reasoning output remains within output. Long-context pricing is applied per
response, never to an action aggregate. App Server `model/rerouted` events are
retained against the following response boundary so the actual routed model is
priced. A consumed reroute cannot affect later responses; they use their own model
facts or the documented configured-model assumption. Identical delivery coalesces
only while an occurrence is pending, so the same reroute observed after consumption
is retained for another response. A reroute without a following boundary remains
partial. Completed `webSearch`
items use the dated official $0.01-per-call rate, while an observed tool without a
matching authoritative card remains an explicit partial exclusion. Historical
contributions keep their dated rates when the public card changes.

Workflow totals follow explicit causality from Weaver intake through Archon,
Executor/helpers, Overseer correction/recovery cycles, specialists, delivery, and
completion acknowledgement, excluding unrelated concurrent work. Judgment-free
completion updates are acknowledged directly by the controller without starting an
Archon turn. The controller emits one concise completion event with any available
frozen action-cost estimate, then freezes the eligible all-in workflow value. The
processed update makes this restart- and replay-idempotent.

### Replace the managed fleet

```sh
fulcrum reboot --soft
fulcrum reboot --hard
fulcrum reboot --reset
```

- `--soft` stops new dispatch, lets managed turns finish, and replaces the
  conversations while retaining work and operational state.
- `--hard` interrupts managed turns first, then replaces them while retaining
  work and operational state.
- `--reset` disposes of pending owned work and resets operational state while
  preserving enrolled source repositories and service, connection, and model
  configuration.

All modes leave the shared app-server and desktop running. Prefer `--soft` unless
you intentionally need interruption or destructive reset behavior. Progress and
remaining conditions are visible in `fulcrum status --json`.

## What is automatic, and what is not

**Humans do:** install and launch the shared desktop correctly; enroll projects
during setup; create/register a Weaver conversation and describe work; use Archon
for strategy; optionally request specialist reviews; and inspect or recover the
system with `status`, `doctor`, and `reboot`.

**Fulcrum does automatically:** publish intake to Beads; notify Archon; start only
Archon-approved implementation; enforce capacity and holds; reuse or provision
lineage Executor and Overseer tasks and create isolated worktrees; route
implementation, review, and bounded repairs; retain and retry external operations;
publish specialist reports and findings; drive Tollgate delivery; close beads; and
archive completed managed conversations after 10 continuous minutes of confirmed
native-thread idleness when their lineage has no pending or active work.

The controller also protects the shared app-server's file-descriptor budget. It
records the supervised app-server PID, descriptor/type counts, direct-child count,
and soft limit in status. On the supported 256-descriptor soft limit it keeps 64
descriptors free, plus an eight-descriptor start allowance. Admission pauses with
a bounded actionable condition before crossing that line. At that limit Fulcrum
supports at most four simultaneous active role conversations and keeps at most four
safe idle worker conversations resident. That maximum mix is covered by the stress
regression, including 100 consecutive process starts while the reserve is held.

Safely idle Executor, Overseer, Sage, and Inquisitor conversations are natively
archived after ten minutes (and immediately under descriptor pressure), releasing
and unsubscribed so their helper children, pipes, sockets, session files, and
writer locks are unloaded. Native retirement has a ten-second reclamation bound:
the opt-in disposable app-server regression warms global pools, exercises four
active plus four idle conversations and 100 native commands at a 256-descriptor
limit, then requires child and descriptor/type counts to return exactly to that
warmed baseline. Run it with `FULCRUM_NATIVE_APP_SERVER_TEST=1 python -m unittest
tests.test_resources_native`. This is a reversible resource park: Fulcrum restores
the same conversation if later lineage work needs it. Restart reconciliation
confirms parked, retired, and archived native threads; if a supposedly parked
thread is active, Fulcrum durably tracks it without interruption until terminal
cleanup. Archon and human-driven Weaver conversations are never resource-parked.

Fulcrum does not automatically invent approval. Pending work waits for Archon,
and a reviewed candidate waits for successful Tollgate certification and
promotion before it counts as delivered.

## Further reading

You do not need these documents to operate Fulcrum, but they describe the current
contracts and implementation in more detail:

- [Python runtime product design](docs/plans/fulcrum-python-runtime.md)
- [Reliability architecture and invariants](docs/reliability-architecture.md)
- [Installation and service setup](docs/setup.md)
- [Operations and recovery](docs/operations.md)
- [Controller data and lifecycle contracts](docs/contracts.md)
- [Supported integration observations](docs/compatibility.md)
- [Codex desktop/shared-runtime integration evidence](docs/codex-desktop-python-control.md)

For repository development, `scripts/check` creates an isolated check
environment, verifies formatting and types, and runs the test suite.
