# Operations

`fulcrum serve` is the sole operational writer. CLI mutations travel as one
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
