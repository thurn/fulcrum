# Live iteration

Fulcrum is developed using Fulcrum. Local master must become available to the
next operation without interrupting the work that produced it. This is an
architectural constraint, not an optional development optimization.

## Local master is authoritative

Local master in ~/fulcrum is the sole source of application behavior, formulas,
role instructions, and skills. A commit is sufficient: no push, remote fetch,
installation, manual activation, or restart is required for the next operation.
Agents make changes, run the repository check, commit, and push as instructed by
AGENTS.md. They must not add an update ritual after ordinary code changes.

Application operations execute an immutable snapshot of the local master commit
observed at launch. This internal cache is not a separately managed installation.
selected.json is a cache of prepared source, never the authority for freshness.
Every fresh command checks local master before using it. A new background worker
does the same before touching events or durable state. Running operations keep
their source lease, so delayed imports and asset reads cannot mix commits.
Uncommitted application edits and other branches do not change application
behavior. Preparation failure fails new operations visibly with the commit and
reason; it never silently runs older application behavior.

Skills deliberately have a different read boundary: all owned Codex skill links,
including the unprefixed `weaver` skill, point directly into ~/fulcrum/skills. They
never follow an instance-owned pointer, packaged data directory, or snapshot.
Subsequent skill reads see edits immediately. Instructions already delivered to an
agent remain part of that existing turn.

## Boundaries

The stdlib launcher resolves local master and acquires a lease on one concrete
source directory before importing application code. On a cache miss it prepares
source automatically. Imports and assets use that directory throughout execution.
Never put a moving symlink on application sys.path: delayed imports could
otherwise mix two commits. Controller and recovery launchers enter this same
selection mechanism; setup never builds a private installed copy of Fulcrum.

CLI mutations execute in detached Python processes. Client timeout returns the
operation locator and does not kill the process. Output goes to temporary files,
so a departed client cannot block a completed effect by leaving a pipe unread.
Reads execute directly. Beads-only commands need no resident host.

The resident owns the initialized native WebSocket, pending server requests,
subscriptions, event buffer, and bounded job scheduling. It imports only transport
and bootstrap mechanisms. Business policy, typed runtime adapters, reconciliation,
role instructions, and delivery live in fresh processes. Closing a CLI client
must never close the shared native connection or release its subscriptions.

Background jobs independently resolve local master just like commands. Reconciliation passes
cannot overlap. Wakeups coalesce, and publication detection runs independently
of reconciliation. Pending native requests remain with the transport until
resolved; application event batches are acknowledged after processing. Overflow
is observable and requires reconciliation, never an assumption of success.

Reconciliation is event-driven and also runs at the configured bounded interval
(15 seconds by default). The resident does not spawn a fresh worker every second.
Every pass records its identifier, start and completion or failure, duration,
action counts, pressure, gaps, and associated work IDs in the diagnostic journal.
This preserves prompt event handling while making idle operation inexpensive and
the cause of repeated scheduling directly inspectable.

The standing Steward keeps one blocking instruction wait alive. A one-minute
thread heartbeat is the bounded continuity fallback when its prior model turn has
ended. Each heartbeat processes at most sixteen authorized native actions, returns
to the blocking wait after every result, and ends on the first idle deadline or
protocol stop. It does not make policy decisions or replace event-driven wakeups.
This bounds turn context without letting scheduler cadence or background archival
work strand foreground delivery.

## Coordination

Operation locks exclude duplicate execution across CLI processes and background
reconciliation. Resource locks exclude concurrent effects on the same target.
One reentrant process-shared state lock protects read/authorize/reserve/write
sections, including global admission capacity. External runtime and delivery
calls release that state lock while retaining operation and resource ownership.

Read-modify-write updates merge changed fields against fresh state. Conflicting
field or ownership changes are rejected rather than overwriting another writer.
Never carry an unchecked whole-record replacement across an external call.

Lock files are not a database and must not be unlinked: replacing their inode
would let two processes believe they held the same lock. A crash releases kernel
locks, but durable operation receipts still require postcondition inspection.
Beads remains the source of truth; multi-record changes are recoverable sequences,
not atomic transactions.

## Ordinary source preparation

At operation launch, read refs/heads/master locally. Reuse a matching prepared
source, otherwise serialize preparation, archive that exact commit, reuse
unchanged dependencies, and check imports, configuration, and required assets.
Select source and interpreter together, then recheck master before launch.
Concurrent callers share the prepared result. Rapid commits supersede candidates
rather than permitting older preparation to overwrite newer code.

Self-publication wakes the resident; its periodic probe also reads local master.
Neither wakeups nor polling are correctness requirements for commands: launch
itself performs the freshness check. The resident continues owning its connection,
pending approvals, event buffer, active turns, and terminals throughout.

The service update command is optional diagnosis/preparation, not a required
workflow step. Service status and service update deliberately remain available
from retained code when master cannot prepare. Use service update --retry after
repairing an environmental/configuration rejection; a new commit is retried
automatically. Status includes observed and selected commits, rejected preparation,
resident maintenance, active source identities, and stage timings.

Dependency changes provision a separate environment and may take longer. Ordinary
application edits never install packages. Recovery has a source-following launcher
independent of the resident, and source preparation for recovery does not depend on
valid workflow configuration. It does not maintain a second installed application.

## Exceptional maintenance

Resident implementation changes are reported for an explicit safe handoff.
Application source can advance while the existing connection owner retains its
loaded implementation; this is the deliberate exception to immediate behavior. They
are not ordinary updates. Pending native requests and active turns must settle
before replacing the connection; a busy service refuses a non-interrupting stop.
Do not force this boundary to satisfy the normal activation latency target.

Breaking state changes supply an idempotent `fulcrum.state_upgrade.migrate(config)`
function. An explicit `service update --maintenance` fences admission and acquires
exclusive maintenance ownership. Failure retains the fence for repair. Successful
migration selects the candidate before reopening admission and rejects commands
started from retired source. There is no schema-version chain or compatibility
mode. Recovery shares the maintenance boundary and remains independently runnable.

## Preventing regression

When adding behavior, put it in a command handler or bounded background job unless
it literally requires connection/process continuity. Do not import application
handlers into the resident. Do not add remote discovery or manual activation gates to local commits. Do not rebuild dependencies for application edits. Do not hold the state
lock across network calls or waits. Preserve source leases during launch and cleanup.

Tests enforce the import boundary, source/asset consistency, approval retention,
event acknowledgment, cross-process locking, and rejected preparation behavior. A local Git/process fixture commits a change
without a remote, immediately runs its new behavior, retains an older process
across delayed imports/assets, and verifies visible failure for a broken commit.
The normal repository check remains independent of external providers. Measure
preparation and command startup separately; the local target is p95 below one
second for unchanged dependencies and state contracts, not a guarantee about
network latency or migrations.

## Local-master acceptance measurement

On 2026-09-15, 20 disposable-repository trials measured **0.890 seconds p95**
and 0.731 seconds median from a completed local commit to its new behavior in
a fresh command. No remote was configured. Source preparation averaged 0.040
seconds and real application/configuration preflight averaged 0.245 seconds.
Launch and source-resolution overhead averaged 0.415 seconds. One simulated
WebSocket connection and pending approval remained live throughout all trials.

Reproduce with .venv/bin/python scripts/measure-live-iteration.py. The experiment
uses real imports/preflight and a small behavior/asset probe; it measures startup,
not completion of an arbitrary workflow action. Timing is machine-dependent.

## Measured initial acceptance

These historical measurements used the previous published-source mechanism;
they do not measure the new launch path.

On 2026-09-15, 20 isolated activations of committed source measured p95 **0.475 s**
and maximum **0.488 s**, including Git archive materialization, actual import and
configuration preflight, atomic selection, and a fresh CLI configuration query.
The same local simulated native WebSocket and pending approval survived all 20
activations. This measures the local activation path; it excludes remote fetch
latency and does not establish a hard real-time guarantee. The source commit
measured was `1501a12`.

The initial production handoff retained the two leadership task identities and
reused the existing Codex runtime and Dolt processes. Ordinary source selection
is separately checked without restarting the resident.

## Accepted update contract

- Ordinary application changes must not restart the connection owner.
- Local master is checked at launch; remote publication is not an execution gate.
- New operations use local master; executing operations retain consistent code.
- Waiting on external work must not block unrelated state transitions.
- Preparation must not interrupt agents or discard pending approvals.
- Skills point directly to master and never depend on source-cache selection.
- A preparation failure is visible; using old behavior is not a silent fallback.
