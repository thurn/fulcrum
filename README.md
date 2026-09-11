# Fulcrum

Fulcrum is local coordination infrastructure for durable multi-agent workflows.
The initial package provides the foundation for typed operational records,
small context readers, role entry points, and diagnostics described in
[`docs/implementation-plan.md`](docs/implementation-plan.md).

## Install

Fulcrum currently targets CPython 3.12. Create a clean environment and install
the package with its exactly pinned development tools:

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

State commands accept global `--brain-root` and `--state-root` overrides before
the subcommand. They emit JSON on stdout and diagnostics on stderr:

```sh
fulcrum --state-root /absolute/state state read --kind progress --id task-123
fulcrum --state-root /absolute/state state write --input progress.json
fulcrum --brain-root /absolute/brain --state-root /absolute/state \
  context --task task-123
```

`fulcrum version` includes the source Git revision when it can resolve the
installed checkout, or when packaging supplies `FULCRUM_BUILD_REVISION`.
`scripts/check` creates an ignored `.venv-check`, installs the pinned tools, and
runs Black, Pyre, and the focused unit tests. The same command is the Tollgate
gate; Dashboard checks are intentionally deferred until Dashboard code exists.

Operational setup and the registration bootstrap boundary are documented in
[`docs/setup.md`](docs/setup.md).
