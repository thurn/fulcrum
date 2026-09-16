# Live iteration

Fulcrum is developed using Fulcrum. Published master must become available to the
next operation without interrupting the work that produced it. This is an
architectural constraint, not an optional development optimization.

## Boundaries

The stable launcher selects one immutable source directory and interpreter before
importing application code. It acquires a source lease under the activation lock.
Imports and assets use that concrete directory throughout execution. Never put a
moving symlink on `sys.path`: delayed imports could otherwise mix two commits.

CLI mutations execute in detached Python processes. Client timeout returns the
operation locator and does not kill the process. Output goes to temporary files,
so a departed client cannot block a completed effect by leaving a pipe unread.
Reads execute directly. Beads-only commands need no resident host.

The resident owns the initialized native WebSocket, pending server requests,
subscriptions, event buffer, and bounded job scheduling. It imports only transport
and bootstrap mechanisms. Business policy, typed runtime adapters, reconciliation,
role instructions, and delivery live in fresh processes. Closing a CLI client
must never close the shared native connection or release its subscriptions.

Background jobs use the same selected source as commands. Reconciliation passes
cannot overlap. Wakeups coalesce, and publication detection runs independently
of reconciliation. Pending native requests remain with the transport until
resolved; application event batches are acknowledged after processing. Overflow
is observable and requires reconciliation, never an assumption of success.

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

## Ordinary activation

Only the configured published integration branch is eligible (here,
`origin/master`). Working-directory edits and local commits are not live input.
Self-publication wakes the updater; remote polling every five seconds covers
other publishers, with backoff after failure.

The updater materializes exact committed source, reuses unchanged dependencies,
checks imports/configuration/assets, and atomically selects source and interpreter
together. It then wakes background processing. Executing commands retain their
source; later commands, including those from existing agent tasks, use new code.
An active agent turn retains its prompt until a later turn is created.

A failed preflight keeps the old selection. Failures after activation do not
cause automatic rollback or blind effect replay. Service status exposes selected
and observed commits, rejected/pending activation, active operation sources,
resident health, and stage timings. `service update --retry` retries a rejected
candidate after repairing its environment.

Normal activation must never install dependencies, restart the connection owner,
interrupt an agent, or discard a pending approval. Dependency changes provision a
separate environment; no running interpreter's environment is modified.

## Exceptional maintenance

Resident implementation changes are staged for an explicit safe handoff. They
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
handlers into the resident. Do not add filesystem watchers that deploy dirty
source. Do not rebuild dependencies for application edits. Do not hold the state
lock across network calls or waits. Preserve source leases during launch and cleanup.

Tests enforce the import boundary, source/asset consistency, approval retention,
event acknowledgment, cross-process locking, and rejected activation behavior.
The normal repository check remains independent of external providers. Measure
activation separately from remote discovery; the local target is p95 below one
second for unchanged dependencies and state contracts, not a guarantee about
network latency or migrations.
