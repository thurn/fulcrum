# Setup

The supported entry point is `./scripts/setup`. It creates or reuses `.venv`,
installs `requirements-dev.lock` and the editable checkout, then runs the Python
installer. First use asks only for missing brain, project, validation, and Archon
model choices. `--config <absolute-json> --non-interactive` supplies the same
fields unattended.

Static configuration is stored at `~/Library/Application Support/Fulcrum/config.json`
unless `FULCRUM_CONFIG` selects another environment. It contains source, brain,
state, Codex, desktop, endpoint, model, and enrolled-project connection facts.
Reset preserves this file and the adjacent `control/` directory.

Setup installs two independent user LaunchAgents:

- `dev.fulcrum.codex-app-server` runs the installed Codex binary with
  `app-server --listen ws://127.0.0.1:4500`.
- `dev.fulcrum.controller` runs the checkout's editable Python environment with
  `-m fulcrum.cli serve`.

The desktop launcher in the control directory sets
`CODEX_APP_SERVER_WS_URL` before starting the configured desktop executable.
Existing private-runtime work must be drained and the desktop relaunched through
this wrapper; setup never terminates or silently migrates it.

Setup checks `/readyz`, the protocol handshake, configured models, native Codex
project and Tollgate identities, Git/Beads connectivity, linked assets, SQLite,
and initial Archon policies. It prints `setup incomplete` with the exact remaining
condition and exits nonzero until all required checks pass.
