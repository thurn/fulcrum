# Operations

`fulcrum serve` is the normal operational writer. CLI mutations travel as one
newline-delimited JSON request over the configured Unix socket. SQLite uses WAL,
foreign keys, full synchronous commits, explicit transactions, and database
constraints for unique ownership and reservations. Transactions never cover
Codex, Tollgate, Git, or Beads waits.

The controller records external intent and an attempt before every mutation. A
lost response is uncertain and is resolved by an exact native-object observation;
it is never interpreted as deterministic failure or blindly repeated. Runtime
starts require a terminal last turn, idle thread state, terminal native helpers,
and no unresolved start intent. Confirmed-unsent starts and due pending actions are
retried with bounded backoff; exhausted retries become explicit holds.

Relevant events advance work immediately. Due timers cover recurring specialists,
interviews, liveness checks, and retryable Python operations. A non-overlapping
fallback reconciliation runs every 30 seconds alongside a supervised advancement
worker. By default, agent turns are inspected after 1,800 seconds; the threshold creates one evidence-backed
possible-stall condition and never interrupts automatically.

Command responses are isolated from later advancement. A successful intake returns
after its own mutation commits; an unrelated Archon dispatch failure is reported as
a later workflow condition. Structured records are retained in SQLite and appended
to `logs/workflow.jsonl` with correlation IDs, durations, redaction, operation
attempts, before/after transitions, worker failures, and reconciliation boundaries.

Use `fulcrum status --json` for tasks, assignments, stages, runtime activity,
capacity, holds, queues, operations, obligations, occurrences, and concise recent
events. `fulcrum doctor --json` checks the assembled installation.

A managed agent may run `fulcrum context` when compaction has removed an exact
current-action fact. The command is read-only, derives identity solely from the
calling Codex task, and accepts no selector for another task or an older action.
Routine action messages remain self-contained; this is an optional recovery path.

Transient structured finish inputs live under `control/handoffs`, partitioned by
the SQLite state generation and action. Invalid, malformed, stale, or otherwise
rejected files are retained for diagnosis. After durable acceptance, the
controller atomically moves the expected name into a fresh private quarantine,
verifies the moved regular file's accepted identity through a no-follow
descriptor, removes only that entry, and then attempts to remove its empty action
directories; it never sweeps the handoff tree. A swapped entry remains in its
quarantine. If cleanup fails, the accepted outcome and cleanup result remain
committed and the response and event log contain a bounded diagnostic. The parsed
outcome remains durable in SQLite after successful file cleanup, including for
lost-response retries.
Authored `--evidence` documents and `fulcrum intake --input` sources keep their
existing caller-owned durability and are not moved into or removed through this
handoff lifecycle.

Fleet replacement is explicit:

- `reboot --soft` drains active managed turns before replacing conversations.
- `reboot --hard` interrupts and confirms managed turn/helper inactivity first.
- `reboot --reset` checkpoint-cancels active candidates, confirms owned worktrees
  absent, archives independent threads, and then discards operational state while
  preserving source repositories, service definitions, endpoint, and model
  configuration. Archive exceptions are quarantined and reported separately from
  destructive completion and replacement readiness.

Reboots never stop the shared app-server or desktop. A recovery record beside
configuration survives reset until the operation completes. The controller runs
from an atomic snapshot under the control root, not from the checkout its fleet
edits; source changes request a refresh at a recorded quiescent boundary.

## Emergency Operative takeover

`$operative` is the only activation path. A human-created unmanaged task registers
from an absolute private JSON file. Setup installs
`control/recovery/current/bin/operative-recovery` with mode 0700 and a private
Python/package dependency closure. The launcher runs isolated from configured
source, its editable install, and its `.venv`. `fulcrum operative probe` is
read-only and uses that launcher when the ordinary path is unavailable.

Fallback acquisition uses `controller.lock`. If it is held, recovery verifies the
configured launchd controller, boots out that exact service, and waits for the
same lock before writing. The fsynced `operative.json` fence precedes authority.
With missing/corrupt SQLite or unverifiable App Server identity, state remains
`acquiring` and only filesystem, Git, recovery-artifact, and service checks are
authorized. `reconcile` must verify the exact caller by `thread/read` before agent
control or durable workflow mutation.

All mutating controls take an absolute JSON `--input`, stable operation key, and
exact IDs: `wind-down`, `reconcile`, `worktree`, `reinstall`, `quarantine`,
`store-repair`, `finish`, `abort`, and `recover`. `repair-check` and
`service-check` are read-only. Active wind-down steers a no-follow-up notice onto
the exact expected turn before interrupting it, then targeted reads must prove the
parent and every helper terminal before archive. Idle notification turns bind no
action and grant no normal authority. Late outcomes remain quarantined evidence.

Corrupt SQLite is copied with its WAL and SHM into a private timestamped quarantine
before replacement is considered, and exact retry reuses the retained copy. Direct
store repair refuses corrupt input, verifies a readable backup first, permits only
explicit INSERT/UPDATE/DELETE in `BEGIN IMMEDIATE`, records before/after SELECT
results, and rolls back unless quick-check and foreign-key invariants pass. It is
unavailable while caller verification is provisional: the exact App Server caller
and active Operative task/action binding are reverified immediately before repair.
The journal fsyncs the exact repair intent before SQLite, marks it sent before the
transaction, and reconciles a sent/uncertain retry by observation without reissuing
its statements. Dirty
source/worktree adoption records its starting HEAD, branch, and changes and never
cleans unrelated state.

Finish cannot release early. The controller retains its closeout contract, waits
for the Operative and helpers to become terminal, rechecks dispositions, recovery
launcher, store, services, Git, and readiness, then archives the Operative and
restores only the prior dispatch intent. A fallback finish must transfer the held
controller lock to a detached recovery-artifact finalizer and receive its exact-thread
readiness acknowledgement before reporting acceptance. The finalizer survives the
invoking command and owns `closing -> closed`; a failed handoff reverts the takeover
to active and rejects finish. Human-only abort reports unresolved
failure, keeps dispatch disabled, and preserves the same takeover for explicit
exact-ID recovery or succession.
