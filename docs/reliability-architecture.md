# Reliability architecture

This document turns the findings in the
[first Python runtime postmortem](postmortems/2026-09-12-python-runtime-first-test.md)
into runtime invariants. The design assumes SQLite, Codex, Tollgate, Beads, Git,
the filesystem, and launchd can never share one transaction. Every boundary may
complete and lose its reply, stop after a partial effect, or disagree temporarily
with Fulcrum.

## Workflow kernel

`kernel.py` is the common mechanism beneath assignments, specialists, interviews,
publication, delivery, and archival. Policy selects work; the kernel determines
whether that work can be started safely.

An external mutation follows one protocol:

1. Commit an immutable operation intent and correlation ID.
2. Commit an attempt row and mark the operation sent.
3. Perform the exact external command.
4. Retain its duration, bounded structured result, stdout, stderr, native ID, and
   error classification.
5. Observe the exact native object after any ambiguous result.
6. Commit confirmed success, confirmed failure, a retry deadline, or an explicit
   operator hold.

The operation and every attempt are append-oriented evidence. A controller restart
classifies a retained `sent` operation as uncertain before doing anything else.
A retained `intent` is known not to have crossed the effect boundary and is released
for safe replay. A missing response never proves failure.

## Enforced invariants

SQLite rejects or repairs the following states:

- a reservation whose action is not runnable;
- a reservation above global or per-project capacity;
- simultaneous reservations with the same conflict key;
- a recovering assignment without either a retry deadline or an operator hold;
- two current actions for one native task;
- two reservations for one execution pair; and
- two unfinished assignments for one Bead.

Moving an action to processed, failed, or canceled automatically releases its
reservation in the same transaction. Existing databases are repaired on open:
legacy recovery rows receive explicit operator holds and terminal-action leases are
removed before enforcement triggers are installed.

The controller admits one assignment, commits its lease, and only then calculates
capacity for the next assignment. A ready-list snapshot never authorizes a batch.
Conflict keys may be supplied in Bead context as `conflict:<resource>`, allowing
policy to serialize work on shared resources such as service definitions, schema,
release integration, or brain publication even when numerical slots remain.

## Ownership boundaries

Agents own judgment and artifacts. Python owns mechanics.

- Executor edits, validates, commits, and reports evidence. It does not create a
  Tollgate candidate.
- The controller creates and captures the immutable candidate for the exact
  assignment worktree.
- Overseer judges that retained candidate and reports approval or concrete changes.
- The controller approves, observes promotion/synchronization/cleanup, closes the
  Bead, releases capacity, and archives the pair.
- Weaver and specialists author content. The controller owns Beads publication,
  idempotency identities, retries, scheduling, and archival.

Controller-owned brain Git publication fetches before push. If local and remote
history diverge, it creates a normal merge that preserves both histories, then
verifies the remote ref equals the published revision. A merge conflict is aborted
with the local commit retained for explicit recovery; publication never force-pushes,
resets, or silently discards either side.

Every worktree receives the retained validation environment at `.venv`, so prompts
and repository checks do not depend on an environment that only exists in the
source checkout.

## Recovery and progress

Pending actions are runnable durable work, not passive records. Both the event loop
and a periodic advancement worker retry due actions. Failed starts use bounded
exponential backoff; exhaustion becomes an urgent operator hold and releases the
capacity lease. Runtime-turn failures and missing outcomes also release capacity and
give the assignment a concrete retry deadline.

Reconciliation is action-driven rather than task-state-driven. It inspects every
current starting, active, terminal, or uncertain action, so an accepted outcome
cannot be skipped merely because a task row was stale or a notification was lost.
A corrupt native thread is marked uncertain and logged with a traceback, but the
pass continues to later independent tasks.

Ambiguous Tollgate approval is decided from the exact candidate's observed state.
A promoted candidate with required remote synchronization, cleanup, and certificate
is completed even when `tg approve` returned malformed output. A confirmed terminal
failure returns to correction. A still-ambiguous candidate creates visible operator
attention and disables new dispatch.

Status exposes, for each nonterminal assignment and action, what it is waiting for,
what will try next, the retry deadline, and any operator hold. A nonterminal entity
that cannot answer those questions is an invariant violation.

## Supervision and health

Events, periodic fallback reconciliation, asynchronous advancement, and source
watching are named critical workers. Each publishes durable state and a heartbeat.
An unexpected return or exception records the complete traceback, marks the worker
degraded, disables dispatch, and restarts the worker after a bounded delay.

Readiness combines configuration readiness with progress readiness. It fails when:

- reconciliation has never completed or is older than 90 seconds;
- a critical worker is absent or degraded;
- capacity is exceeded;
- a lease lacks a runnable owner;
- recovery lacks a retry or hold;
- an external operation remains uncertain after targeted observation;
- a project integration is unavailable; or
- the runtime connection is down.

Socket response and process existence are diagnostics, never workflow-health proof.
`fulcrum doctor` also requires both configured launchd jobs to be stably running
with a PID and the exact installed arguments, working directory, and environment.
A listener on the app-server port is reported as unmanaged and cannot satisfy
topology checks.

## Command isolation

A client command commits and returns its own result. Fleet advancement is signaled
to an asynchronous worker after the response mutation commits. A later Archon or
runtime start failure is recorded as a separate condition and cannot turn successful
intake into a failed command.

Every request receives a correlation ID and durable received/succeeded/failed event
with duration. Events are bounded and redact common credential fields. The same
records are appended as JSON lines to `logs/workflow.jsonl`; SQLite remains
authoritative if the secondary file sink is unavailable. Database triggers retain
every workflow state change with before/after values. External attempts preserve
structured result or error evidence and bounded stdout/stderr.

## Control-plane isolation and refresh

Setup copies the Python package into an owned deployment directory under the
control root and atomically switches a `current` symlink. launchd runs this isolated
snapshot with Python isolated mode. The managed source checkout can therefore be
promoted without replacing code beneath an in-flight controller call.

Source watching records a refresh request and disables starts. Refresh waits until
no external operation is in `sent`, installs a new snapshot atomically, closes the
runtime and socket, and execs the isolated launcher. Reconciliation runs before
dispatch is re-enabled after restart.

## Reset and setup convergence

Reset checkpoints exact completed targets in the retained reboot record. For each
assignment it:

1. reads the exact candidate;
2. cancels an active candidate and confirms it is no longer active;
3. accepts an already-absent worktree as success;
4. otherwise removes the worktree and confirms the path is absent; and
5. clears the stored worktree path immediately.

Thread archival continues independently. Missing or corrupt native rollout lineage
is returned as an archive exception and preserved in the new database rather than
blocking later threads. Material candidate/worktree cleanup failures stop destructive
state removal with the checkpoint intact. A completed reset returns success even
while the replacement Archon is still establishing policies; destructive completion
and replacement readiness are separate response fields.

Setup never treats an unmanaged app-server listener as the configured service. It
checks endpoint ownership before changing installation state, installs current
definitions, reloads stale PATHs, observes a failed bootstrap, retries once only
when the job is confirmed absent, and requires each job to remain running across
multiple observations. Error messages retain the launchctl domain, plist, first
stdout/stderr, observed state, and retry stdout/stderr.

## Validation strategy

The automated gate covers both domain behavior and interruption boundaries:

- transactional capacity and conflict-key contention;
- database-level rejection of capacity bypass and progress-free recovery;
- automatic lease release on terminal actions;
- bounded action retry and operator-hold exhaustion;
- controller-owned candidate creation;
- successful approval with malformed command output;
- same-key intake recovery superseding stale failure records;
- runtime disconnect detection and reconnectability;
- crash adoption of sent operations and replay of confirmed-unsent starts;
- continuation past a corrupt native thread;
- idempotent reset of an already-missing worktree;
- ordered candidate cancellation and worktree removal;
- independent archive quarantine;
- transient launchctl bootstrap failure; and
- atomic control-plane snapshot replacement.

Mocks prove deterministic failure semantics, not native readiness. Before another
live fleet test, the assembled-product gate in `validation.md` remains mandatory and
must run in isolated state against real Codex, Tollgate, Beads, Git, worktrees, and
launchd.
