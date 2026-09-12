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

### Brain commits and synchronization

Use one `project:<id>` label on every bead. Plan work carries one `plan:<id>`
label and inherits activation from that plan. Standalone work may carry one
`activation:queued` or `activation:future` label; no activation label means
future work. The checked-in [bead template](../templates/brain/bead.md) includes
the implementation contract expected at intake.

Server-mode writes use an explicit batch boundary: pass
`--dolt-auto-commit batch` while creating or updating related issues, then run
`bd --directory <brain> dolt commit -m <message>`. Immediately follow the Dolt
commit with `bd --directory <brain> dolt push`. This is separate from the Git
commit and push that save Markdown; a successful `git push` does not save issue
history.

For shared Markdown edits, serialize only staging and committing. Refuse a
commit when unrelated staged content already exists, stage explicit paths or
hunks, then release the short commit lock before `git push` or any Beads
operation. A failed Git or Dolt push leaves the corresponding local commit in
place and becomes a `push_obligations` entry in the responsible role's progress
record. Normal work or patrol retries that exact push; there is no sync daemon
or cross-store transaction journal.

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

## Repository-linked install and update workflow

Every Fulcrum user must retain a Git checkout. Wheel-only and copied-skill
installations are unsupported. Run installation from the repository root after
Tollgate has promoted and synchronized the exact revision. The installer
rejects nested and disposable `.worktrees` paths and requires `HEAD`,
`refs/heads/release`, and `refs/remotes/origin/master` to equal
`--certified-revision`.

```sh
revision=$(git -C /absolute/retained/fulcrum rev-parse HEAD)
python3.12 -m venv /absolute/retained/fulcrum/.venv
/absolute/retained/fulcrum/.venv/bin/pip install \
  --requirement /absolute/retained/fulcrum/requirements-dev.lock
/absolute/retained/fulcrum/.venv/bin/pip install --no-deps \
  --editable /absolute/retained/fulcrum
/absolute/retained/fulcrum/.venv/bin/fulcrum \
  --brain-root /absolute/brain --state-root /absolute/state install \
  --source-root /absolute/retained/fulcrum \
  --certified-revision "$revision" \
  --skills-root /absolute/codex/skills \
  --hooks-config /absolute/codex/hooks.json \
  --expected-brain-remote git@github.com:owner/private-brain.git \
  --sage-anchor 2026-09-12T16:00:00Z \
  --codex-projects-verified-at 2026-09-11T19:45:00Z
```

The command creates `fulcrum-*` symlinks under the selected Codex skills
directory and a `hooks/fulcrum` symlink under the same Codex home. Every link
points directly into the retained checkout. It also creates
`<codex-home>/bin/fulcrum` pointing to the checkout's editable `.venv` command.
It merges two small handler entries into the user-level hooks file while
preserving unrelated hooks. Running the command twice is a no-op for links and
does not create roles, schedules, services, or project registrations. It never
restarts Beads.

Edits to a linked skill or hook script are visible immediately. The editable
Python install likewise uses the checkout's current `src/fulcrum` code on the
next command or hook process. There are no copied skill manifests, content
hashes, per-run skill snapshots, or stale-content reconciliation. Re-run
installation only when establishing links or changing the hook definition, not
after ordinary repository edits. If Codex does not surface a skill edit, restart
Codex to refresh discovery.

The editable import must resolve to `<retained-source>/src/fulcrum`, and record
schemas load only from `<retained-source>/schemas`; neither has an installed-copy
fallback. Invoke `<codex-home>/bin/fulcrum` by absolute path unless that bin
directory is known to be on `PATH`. Fulcrum does not pull Git, synchronize
dependencies on each invocation, or hot-reload an already running process.

The user-level `hooks.json` remains a stable merge point because it may contain
unrelated hooks. Fulcrum owns only its two marked definitions there; all mutable
behavior is reached through the linked `hooks/fulcrum` directory. Never replace
or symlink the whole hooks file.

The Archon and Night Watchman task IDs must come from human-created Codex tasks
and use human model authorization in the role registry. After the Watchman task
exists, create or update its one hourly heartbeat through Codex's supported
automation UI/API, then repeat install with `--watchman-schedule-id <actual-id>`.
Do not manufacture substitute tasks or specialist schedules. Record exactly
three initial projects with their actual Git root, Codex project ID, host, and
Tollgate repository ID. Keep an ineligible project registered with
`ineligibility_reason`; a true scope exclusion additionally needs a written
`scope_decision`, and disabled initial integrations still fail readiness until
repaired.

After every install or update, use the CLI before any Dashboard is available:

```sh
/absolute/codex/bin/fulcrum doctor \
  --expected-brain-remote git@github.com:owner/private-brain.git \
  --skills-root /absolute/codex/skills \
  --hooks-config /absolute/codex/hooks.json
```

`doctor` reports required capability failures, optional desktop/runtime
observation gaps, and retained Git/Dolt push failures separately. Database
health comes from `bd where`, `bd dolt status`, and `bd dolt test` through the
Beads adapter; Fulcrum does not keep a PID or offer a generic database service
manager.

When installation changes the user-level hook definition, it marks hook trust
as requiring review. Use `/hooks` in Codex to inspect and trust that definition,
then perform the compact/Stop desktop exercise. Re-running an unchanged install
does not invalidate already recorded trust, and editing the linked hook
implementation does not require reinstalling it. Never use a trust bypass as
readiness evidence.

## Guided fleet bootstrap

Invoke `$fulcrum-setup` in the human-created task that should become the first
Archon. The skill performs the repeatable preflight, discovers current Codex and
Tollgate identities, and uses `fulcrum setup bootstrap --input <file>` to create
the initial owned role and project registries. It cannot create either
persistent role: the invoking task supplies the human-created Archon identity,
and the user creates the distinct Watchman task when prompted.

The skill inspects existing automations, applies the idempotent Watchman plan
through Codex's supported automation API, and records the returned schedule ID.
Hook configuration is machine-verifiable, but trust and desktop delivery remain
a human checkpoint in `/hooks`. After the exact hook hash has been trusted and
exercised, `fulcrum setup record-evidence` records the optional timestamp and
human evidence without claiming that CLI execution proves desktop delivery.
When that exercise is unavailable, the same command records project and
schedule evidence while leaving the documented hook fallback visible.

After the first real Watchman patrol, rerun the evidence command with
`--first-patrol-observed-at <UTC-time>` and
`--first-patrol-evidence <concise-result>`. A quiet patrol is valid successful
evidence and does not need a manufactured alert. Scheduling alone is not proof
of a patrol, so Doctor keeps readiness closed until this durable, current-
Watchman-owned evidence is recorded.

The checked-in readiness matrix retains its historical evidence. To evaluate
the current machine, save `fulcrum doctor` JSON outside the repository and
overlay only its four runtime-dependent rows:

```sh
fulcrum readiness \
  --matrix /absolute/retained/fulcrum/docs/readiness-evidence.json \
  --doctor-report /absolute/temporary/doctor.json
```

Required doctor failures remain failures; optional desktop/runtime observations
may remain unsupported only with their documented fallbacks. A ready result lets
the invoking task load `$archon` and continue as the first coordinator.

Invalid or unknown record schemas remain unchanged and produce an error; Fulcrum
does not carry record-format conversion machinery. For a package rollback,
install a previously certified revision from its retained checkout and rerun the
same command; do not restore the whole state directory over active work.
Database backup and restoration use `bd backup sync` and `bd backup restore`
into a disposable destination first, with `bd dolt stop/start` only during
coordinated database maintenance.

Uninstall by removing the Fulcrum skill symlinks and `hooks/fulcrum` symlink only
after disabling/removing the two marked Fulcrum handlers. Preserve their Git
checkout, config, brain, state, role registry, and project registry unless the
user separately selects those data for deletion.
