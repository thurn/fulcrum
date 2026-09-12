# Fulcrum

Fulcrum is a local Python controller for coordinated Codex work. It connects to
the same app-server as the desktop, stores operational truth in SQLite, and owns
task creation, naming, scheduling, message delivery, recovery, and archival.
Agents retain implementation, review, planning, and strategic judgment.

The complete design is [`docs/plans/fulcrum-python-runtime.md`](docs/plans/fulcrum-python-runtime.md).

## Install

On macOS, retain this Git checkout and run:

```sh
./scripts/setup
```

The guided first run installs the checkout's Python environment, saves ordinary
configuration, prepares or restores the private brain, enrolls selected projects,
links the human entry skills and context hook, installs separate LaunchAgents for
Codex app-server and `fulcrum serve`, and creates a desktop launch wrapper. It
then creates Archon and remains incomplete until Archon records capacity and
recurring policies. Re-running the command inspects and reuses prior work.

For unattended setup:

```sh
./scripts/setup --config /absolute/setup.json --non-interactive
```

## Operate

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

Managed agents finish their current controller-bound action with the exact
`fulcrum finish ...` form included in their brief. They never pass task, turn,
assignment, or dispatch IDs and cannot write operational state directly.

`scripts/check` formats, type-checks, and tests the package. Python and prompt
edits in the retained clone are live; the controller safely re-execs on Python
changes. Dependency metadata changes require reinstalling the lock file and
editable package.
