# Setup

The supported entry point is `./scripts/setup`. It creates or reuses `.venv`,
installs `requirements-dev.lock` and the editable checkout, then runs the Python
installer. First use asks only for missing brain, project, validation, and Archon
model choices. `--config <absolute-json> --non-interactive` supplies the same
fields unattended.

Setup owns both background services; users do not start the app-server or controller manually.

Static configuration is stored at `~/Library/Application Support/Fulcrum/config.json`
unless `FULCRUM_CONFIG` selects another environment. It contains source, brain,
state, Codex, desktop, endpoint, model, and enrolled-project connection facts.
Reset preserves this file and the adjacent `control/` directory.

Setup installs two independent user LaunchAgents:

- `dev.fulcrum.codex-app-server` runs the installed Codex binary with
  `app-server --listen ws://127.0.0.1:4500`.
- `dev.fulcrum.controller` runs the checkout's editable Python environment with
  `-m fulcrum.cli serve`.

Both definitions set a deterministic executable `PATH` rather than inheriting
the shell that ran setup. It includes `~/.local/bin` and `~/bin` for the
configured macOS user, the standard Apple Silicon and Intel Homebrew locations,
and the macOS system executable directories. This lets the controller find
user-installed dependencies such as `bd` and `tg` after a clean login. When a
rerun changes an installed definition, setup unloads and bootstraps that service
so the running job receives the repaired environment; unchanged jobs are reused.

After setup completes, launch Codex desktop from the retained checkout with:

```sh
./scripts/launch_codex.sh
```

This is the user-facing desktop command. It honors `FULCRUM_CONFIG` and
`FULCRUM_CONTROL_ROOT`, then delegates to the configuration-aware launcher that
setup installed in the selected control directory. The installed launcher sets
`CODEX_APP_SERVER_WS_URL` before starting the configured desktop executable; its
path and contents are internal implementation details. Existing private-runtime
work must be drained and the desktop relaunched with `./scripts/launch_codex.sh`;
setup never terminates or silently migrates it.

Setup checks `/readyz`, the protocol handshake, configured models, native Codex
project and Tollgate identities, Git/Beads connectivity, linked assets, SQLite,
and initial Archon policies. It prints `setup incomplete` with the exact remaining
condition and exits nonzero until all required checks pass.

For service diagnostics, run `fulcrum doctor --json`, inspect the installed and
loaded controller definitions, and read its error log:

```sh
plutil -p "$HOME/Library/LaunchAgents/dev.fulcrum.controller.plist"
launchctl print "gui/$(id -u)/dev.fulcrum.controller"
tail -n 100 "$HOME/Library/Application Support/Fulcrum/logs/controller-error.log"
```

If the plist or loaded job has a missing or stale `PATH`, rerun
`./scripts/setup`; do not repair the session with `launchctl setenv`, because
that change is transient and is not part of the installed service definition.
