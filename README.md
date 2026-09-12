# Fulcrum

Fulcrum is local coordination infrastructure for durable multi-agent workflows.
The initial package provides the foundation for typed operational records,
small context readers, role entry points, and diagnostics described in
[`docs/implementation-plan.md`](docs/implementation-plan.md).

## Install

Fulcrum currently targets CPython 3.12. Keep a Git clone as the permanent
runtime source; copied skills and wheel-only installations are unsupported.
Create a clean environment inside that clone and install the package with its
exactly pinned development tools:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --requirement requirements-dev.lock
.venv/bin/python -m pip install --no-deps --editable .
```

The equivalent convenience extra is `pip install --editable '.[dev]'`; use the
lock file for reproducible validation.

## Use and validate

```sh
.venv/bin/fulcrum --help
.venv/bin/fulcrum version
scripts/check
```

Run the repository-linked installation workflow in
[`docs/setup.md`](docs/setup.md) to point the Codex skill and hook directories
at this checkout and create the stable `~/.codex/bin/fulcrum` link. Changes in
the checkout are then live without reinstalling. Use that command by absolute
path unless `~/.codex/bin` is known to be on `PATH`.

State commands accept global `--brain-root` and `--state-root` overrides before
the subcommand. They emit JSON on stdout and diagnostics on stderr:

```sh
fulcrum --state-root /absolute/state state read --kind progress --id task-123
fulcrum --state-root /absolute/state state write --input progress.json
fulcrum --brain-root /absolute/brain --state-root /absolute/state \
  context --task task-123
fulcrum --brain-root /absolute/brain --state-root /absolute/state \
  plans list --project fulcrum
fulcrum --brain-root /absolute/brain brain status \
  --expected-remote git@github.com:owner/private-brain.git
```

`fulcrum brain init` initializes only a missing `.beads` store and then performs
the same verification. It refuses a different Git or Dolt remote, a non-server
backend, and a non-loopback endpoint. Beads remains responsible for automatic
startup, PID and port selection, logs, and recovery.

`fulcrum plans list` reads safe YAML frontmatter from
`plans/<project>/<plan_id>.md`, validates registered project IDs, activation,
duplicate IDs, and dependency cycles, and returns JSON without modifying or
dispatching the plan. Task context reads bounded global, role, and per-project
memory from `memory/`; NEWS and plan fixtures define the same deterministic
Markdown contract used by later dashboard adapters.

`fulcrum version` includes the source Git revision when it can resolve the
installed checkout, or when packaging supplies `FULCRUM_BUILD_REVISION`.
`scripts/check` creates an ignored `.venv-check`, installs the pinned tools, and
runs Black, Pyre, and the focused unit tests. The same command is the Tollgate
gate; Dashboard checks are intentionally deferred until Dashboard code exists.

Operational setup and the registration bootstrap boundary are documented in
[`docs/setup.md`](docs/setup.md).

After installing a certified release, invoke `$fulcrum-setup` in the
human-created task that should become the first Archon. The skill verifies the
installation and three project integrations, guides creation of the persistent
Watchman, installs its single hourly heartbeat, records hook evidence, evaluates
the live readiness gate, and then transitions the task to `$archon`.
