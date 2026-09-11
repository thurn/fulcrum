# Setup, verification, and removal

## Bootstrap boundary

The compatibility report was committed and pushed at
`6bec7288b39403d085d62da1417118ac08cc0c2c`. That clean source state was
registered with Tollgate as repository
`01a09277-71b8-7c93-9d80-04b8d8dbd072`, using `scripts/check` as its sole
voting step. Tollgate candidate `01a09278-b9c2-7831-b361-8e403cea9bf2`
correctly terminated as `baseline-failing`: the check script did not exist at
the anchor. This is retained bootstrap evidence, not a passing certificate.

Task 02's package scaffold is the first ordinary code candidate. It must pass
the same configured command from a clean Tollgate worktree before the task is
treated as certified. Registration by itself says nothing about code health.

## Local installation

1. Install CPython 3.12.
2. Install the pinned Dolt version listed in `docs/compatibility.md`; do not
   substitute the newest release without repeating the compatibility probes.
3. Clone the repository and verify that `origin` identifies the intended
   private GitHub repository.
4. Create a virtual environment and install `requirements-dev.lock`, followed
   by the editable package as shown in the README.
5. Run `scripts/check`, `fulcrum --help`, and `fulcrum version`.

The application resolves configuration in this order: explicit
`--brain-root`/`--state-root`, `FULCRUM_BRAIN_ROOT`/`FULCRUM_STATE_ROOT`, then
the installation record selected by `FULCRUM_CONFIG` (defaulting to
`~/Library/Application Support/Fulcrum/config.json`). The brain defaults to
`~/brain`; local state defaults to `~/Library/Application Support/Fulcrum`.
Paths are expanded once and derived record identifiers may not contain path
separators or traversal. Do not put credentials, Beads working files, logs, or
real operational records in the source checkout.

State files are split by the ownership table in `schemas/README.md` and are
written through `fulcrum state write --input <file>`. The helper validates the
schema and declared writer before using a same-directory temporary file,
`fsync`, and atomic replacement. `logs/` and `observations/` are reserved
separately from the registry; readers and hooks do not mutate another role's
state.

## Shared brain server

The brain uses Beads' managed local Dolt server. Fulcrum does not install a
LaunchAgent, retain a PID, select a port, or restart the process. Every client
must point to the one configured brain rather than running `bd init` in a
software repository.

Install Dolt 2.2.0 on `PATH`, then verify an existing brain without changing
it:

```sh
fulcrum --brain-root /absolute/brain brain status \
  --expected-remote git@github.com:owner/private-brain.git
bd --directory /absolute/brain where --json
bd --directory /absolute/brain dolt status --json
bd --directory /absolute/brain dolt test --json
```

`fulcrum brain init` has the same required `--expected-remote`. It initializes
only when `.beads` is absent, requests server mode on `127.0.0.1`, and otherwise
validates the existing store. It refuses remote mismatches and non-loopback
configuration. Its status output identifies the active Beads directory,
database path, database name, PID, selected port, and connectivity result.

Beads owns lifecycle operations. Coordinate maintenance with other clients
before explicitly stopping the shared server:

```sh
bd --directory /absolute/brain dolt status --json
bd --directory /absolute/brain dolt stop
bd --directory /absolute/brain dolt start
```

### Moving to a new machine

A Git clone contains the brain's Markdown and Beads configuration, but ordinary
Git history does not contain the Dolt database. On a newly cloned machine,
verify the configured action before hydrating the database from its private
remote:

```sh
chmod 700 /absolute/brain/.beads
bd --directory /absolute/brain bootstrap --dry-run
bd --directory /absolute/brain bootstrap --yes
bd --directory /absolute/brain dolt status --json
bd --directory /absolute/brain dolt test --json
```

The dry run must identify the expected private remote and database. Do not use
`bd init` merely because the cloned server initially reports that its database
is missing; doing so can create empty state instead of restoring shared issue
history.

After a crash, run `bd --directory /absolute/brain dolt test`. A normal client
read should automatically start the loopback server; inspect `dolt status` and
`.beads/dolt-server.log` if it does not. Diagnose the configured mode, host,
database, and Beads/Dolt versions rather than adding another supervisor.

### Migration evidence and rollback

Before the 2026-09-11 migration, `bd backup sync` captured a readable
full-history backup at
`/Users/dthurn/Library/Application Support/Fulcrum/backups/brain-pre-server-20260911`.
The original embedded `.beads` directory is separately preserved at
`/Users/dthurn/Library/Application Support/Fulcrum/backups/brain-embedded-beads-20260911`.
The full-history backup was restored into a disposable embedded database before
the live store changed. The migrated brain and its Dolt remote were then
committed and pushed independently; ordinary Git history does not contain the
database history.

For rollback, first coordinate downtime and stop the brain with `bd dolt stop`.
Move the current `.beads` directory to a new dated quarantine path, restore the
preserved embedded directory, and verify `bd where`, `bd status`, issue counts,
and dependency counts before resuming clients. Alternatively, initialize a
fresh disposable destination and use `bd backup restore` there first. Never
restore over the live database, run two migrations concurrently, or stop the
brain as part of worktree cleanup.

## Tollgate verification

On a machine where Fulcrum is not yet registered, initialize it with the same
gate used by the repository:

```sh
tg init /absolute/fulcrum --run scripts/check --json --no-launch
```

If the cloned tip is a merge commit, Tollgate cannot use it as a source
candidate. Register that already-pushed state with `--no-bootstrap`, enable the
documented `origin`/`master` remote policy in `.tollgate/config.toml`, apply the
configuration, and submit the next single-parent substantive commit normally.
Do not rewrite the shared history merely to manufacture a bootstrap candidate.

```sh
tg repo list --json --no-launch
tg --repository 01a09277-71b8-7c93-9d80-04b8d8dbd072 config explain --json --no-launch
tg --repository 01a09277-71b8-7c93-9d80-04b8d8dbd072 status --json --no-launch
```

The repository identity must resolve to this checkout, the integration ref
must be `refs/heads/release`, and the voting step must run `scripts/check`.
Tollgate owns release advancement and candidate cleanup. When automatic source
push is disabled, finish a promoted change with `tg --repository <id>
--no-launch push` and verify the configured remote tip before closing work.

## Removal

Remove only the installation being decommissioned:

- Uninstall the `fulcrum` package from its virtual environment or delete that
  dedicated environment.
- Remove Fulcrum from Tollgate with `tg repo remove` only when intentionally
  discarding its validation history and configuration.
- Preserve the brain and local application state unless they are separately
  backed up and explicitly selected for removal. Package removal never implies
  database deletion.
