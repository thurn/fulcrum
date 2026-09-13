# Fulcrum

Fulcrum is a local Python controller for coordinated Codex work. It connects to
the same app-server as the desktop, stores operational truth in SQLite, and owns
task creation, naming, scheduling, message delivery, recovery, and archival.
Agents retain implementation, review, planning, and strategic judgment.

The product design is
[`docs/plans/fulcrum-python-runtime.md`](docs/plans/fulcrum-python-runtime.md);
failure semantics and enforced invariants are specified in
[`docs/reliability-architecture.md`](docs/reliability-architecture.md).

## Install

On macOS, retain this Git checkout and run:

```sh
./scripts/setup
```

The guided first run installs the checkout's Python environment, saves ordinary
configuration, prepares or restores the private brain, enrolls selected projects,
links the human entry skills and context hook, and creates a desktop launch
wrapper. Setup starts and manages the Codex app-server and Fulcrum controller as
background services; do not launch the app-server manually. Setup succeeds only
after Archon has completed a turn and is visible through the shared runtime.
Re-running the command inspects and reuses prior work.

For unattended setup:

```sh
./scripts/setup --config /absolute/setup.json --non-interactive
```

## Operate

Human users create and register Weaver tasks; Fulcrum creates Archon, Executor, and Overseer tasks.

```sh
fulcrum status --json
fulcrum doctor --json
fulcrum archon
fulcrum intake --project fulcrum --title "Fix empty results" \
  --description "Show the empty state when search has no matches and test both paths."
fulcrum sage
fulcrum inquisitor --project fulcrum
fulcrum reboot --soft
```

Managed agents receive role guidance and finish-command examples at creation.
Subsequent messages contain the actual request and relevant facts directly;
compaction adds only a short reminder. No instruction-fetching command is required.
Agents submit `fulcrum finish ...` for their controller-bound action without task,
turn, assignment, or dispatch identity arguments, and cannot write operational
state directly.

`scripts/check` formats, type-checks, and tests the package. Python and prompt
edits in the retained clone request an atomic control-plane snapshot refresh at a
quiescent boundary. The controller never executes from the checkout its fleet can
promote into. Dependency metadata changes require reinstalling the lock file and
editable package.
