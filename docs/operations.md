# Operations

`fulcrum serve` is the sole operational writer. CLI mutations travel as one
newline-delimited JSON request over the configured Unix socket. SQLite uses WAL,
foreign keys, full synchronous commits, explicit transactions, and database
constraints for unique ownership and reservations. Transactions never cover
Codex, Tollgate, Git, or Beads waits.

The controller records external intent before every mutation. A lost response is
uncertain, retains its reservation, and receives one bounded reconciliation pass;
it is never blindly repeated. Runtime starts require a terminal last turn, idle
thread state, terminal native helpers, and no unresolved start intent.

Relevant events advance work immediately. Due timers cover recurring specialists,
interviews, liveness checks, and retryable Python operations. A non-overlapping
fallback reconciliation runs every 30 seconds. Agent turns are inspected after
the configured 1,800 seconds by default; the threshold creates one evidence-backed
possible-stall condition and never interrupts automatically.

Use `fulcrum status --json` for tasks, assignments, stages, runtime activity,
capacity, holds, queues, operations, obligations, occurrences, and concise recent
events. `fulcrum doctor --json` checks the assembled installation.

Fleet replacement is explicit:

- `reboot --soft` drains active managed turns before replacing conversations.
- `reboot --hard` interrupts and confirms managed turn/helper inactivity first.
- `reboot --reset` additionally discards verified owned worktrees and operational
  state while preserving source repositories, service definitions, endpoint, and
  model configuration.

Reboots never stop the shared app-server or desktop. A recovery record beside
configuration survives reset until the operation completes.
