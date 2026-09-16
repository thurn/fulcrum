# Setup

Fulcrum setup is rerunnable and Beads-only. It never imports an earlier workflow
database or replay journal.

## Non-installation invariant

Local master in ~/fulcrum is authoritative. Committing an ordinary application
change is sufficient for the next command or background operation to use it.
Launch automatically prepares an immutable snapshot when necessary; agents never
run installation, activation, or restart steps to expose ordinary behavior.
Dependency environments provide interpreters and dependencies, not independently
installed copies of the application. Running operations retain their source.

Every owned Codex skill is an absolute symlink from
`~/.codex/skills/fulcrum-*` directly to `~/fulcrum/skills/fulcrum-*`. A link
through an instance directory, `skills-current`, a packaged asset directory, or a
selected source snapshot is invalid and reconciliation repairs it automatically.

## Prerequisites

- macOS and Python 3.12
- authenticated Codex Desktop/CLI with an available app-server
- Git, `bd`, and `dolt`
- `tg` when using the Tollgate delivery adapter
- a retained Fulcrum checkout and a Git-backed brain destination

Run setup from the retained checkout:

```sh
scripts/setup --input setup.json --non-interactive --json
```

If you pass `--instance`, it must be an absolute path. Setup writes source-following
launchers to `<instance>/bin`. Production setup also installs
`~/.local/bin/fulcrum` and `~/.local/bin/fulcrum-recover` as collision-safe links
to those launchers; it refuses to replace unrelated links or real files. Ensure
`~/.local/bin` is on `PATH`. Explicit `--instance` setups remain isolated and use
`<instance>/bin/fulcrum` directly.

A minimal `setup.json` is:

```json
{
  "brain": {"root": "/absolute/path/to/brain"},
  "runtime": {"endpoint": "ws://127.0.0.1:4500"}
}
```

Setup discovers `bd`, `codex`, and `tg` on `PATH` when their executable fields
are omitted. The brain must be a Git worktree with its configured remote
(`origin` by default), and that remote must already contain at least one branch
and commit. For a brand-new remote, create and push its initial commit before
running setup; setup does not create the remote's first branch.

The input is a partial configuration object. Accepted top-level maps are
`brain`, `beads`, `runtime`, `delivery`, `models`, `projects`, `knowledge`,
`source`, `timing`, `diagnostics`, `resources`, and `policy`. Omitted values use
the defaults written to `fulcrum.yaml`. The principal fields are:

- `brain`: `root`, `remote`, `branch`, `push_interval_seconds`
- `beads`: `executable`, loopback `host`, `port`, `database`
- `runtime`: `kind` (`codex`), `endpoint`, `executable`
- `delivery`: `kind` (`tollgate`), `executable`
- `models`: per-role `model` and `effort`
- `projects`: a map keyed by project ID; each entry accepts `root`,
  `codex_project_id`, `delivery`, `integration_branch`, `prepare_argv`,
  `validate_argv`, `source_remote`, `require_source_sync`, `models`, and
  `enabled`
- `knowledge`: `root`, `remote`, `branch`, `require_remote_sync`
- `source`: `repository`, `remote`, `branch`
- `policy`: capacity, pause, suspension, and rationale fields
- `timing`, `diagnostics`, and `resources`: operational overrides

All roots and executable paths must be absolute. Use distinct loopback runtime
and Beads ports for an isolated test instance. Production setup owns and starts
its configured runtime service. For an explicit isolated instance, start the
documented runtime prerequisite yourself before setup, for example:

```sh
codex app-server --listen ws://127.0.0.1:4500
```

The command must remain running at the endpoint in `setup.json` while setup
validates capabilities. If required capability validation fails, setup retains
its configuration, service definitions, and failure receipt for the documented
rerun, but stops any runtime or Dolt service that this failed attempt started.

The input document supplies any non-default configuration and initial projects.
`brain.root` is the directory containing the authoritative `fulcrum.yaml`. Runtime
and delivery provider identities are explicit; projects include absolute roots,
validation commands, exact provider IDs when already registered, and whether
source synchronization is required. Use `fulcrum config validate --json` to inspect
the effective document and missing prerequisites.

Setup is a one-time/rerunnable bootstrap and performs these bounded operations:

1. Creates only declared instance and brain paths and provisions locked dependencies.
2. Keeps entry points bound to the editable master checkout; it never copies Fulcrum.
3. Provides `fulcrum` and `fulcrum-recover` launchers from the same master source
   on the production user's `PATH`.
4. Writes uniquely named controller, Dolt, optional runtime, and updater services.
5. Initializes one externally served `fulcrum` Beads database, then receipts all
   subsequent external effects.
6. Reconciles the nine human-invoked skills and the read-only compaction hook without
   replacing real user directories or unrelated hooks.
7. Validates runtime models/efforts and exact project/delivery registrations.
8. Creates or reuses standing Vizier and Marshal identities and sends one bounded
   readiness turn to each new task so it is durable and visible in Codex Desktop.

Automatic capacity defaults to four globally and four per project. The owned shared
runtime service uses an FD soft limit of 4096. Setup reports actual capability facts;
a protocol handshake does not by itself claim Desktop attachment.

Rerun the same command after repairing any named prerequisite. Existing valid YAML,
leadership IDs, service identity, provider registrations, and completed operation
receipts are reused. Test fixtures use isolated roots/services and never rewrite
production links or configuration.

Useful checks:

```sh
fulcrum service status --instance INSTANCE --json
fulcrum runtime capabilities --instance INSTANCE --json
fulcrum project list --instance INSTANCE --json
fulcrum skills reconcile --instance INSTANCE --json
fulcrum doctor --instance INSTANCE --json
```

For a disposable test instance, archive or delete its exact managed tasks before
running `service stop`; task commands use the resident connection and are
unavailable after it stops. `service stop` deliberately leaves the dedicated Dolt
service running. Fulcrum has no one-command instance disposal operation, and hard
reset is not a disposal substitute because it bootstraps clean leadership and
services again.

Launch Codex Desktop against the configured shared runtime with:

```sh
fulcrum runtime launch-desktop
```

The command reads the selected runtime endpoint and starts Desktop with the
required `CODEX_APP_SERVER_WS_URL` environment variable. The retained-checkout
shortcut `scripts/launch_codex.sh` invokes the same command.

After changing `pyproject.toml` or `requirements-dev.lock`, refresh the dependency
environment and editable entry-point metadata in `.venv`; `scripts/setup` does
this. This dependency refresh is not a Fulcrum installation and is never needed
for ordinary application, documentation, role, or skill changes.

## Local master

Production launches observe refs/heads/master in ~/fulcrum without waiting for
origin/master. The source.repository setting describes the canonical repository;
remote/branch settings do not gate execution. Uncommitted application edits do not
change new operation behavior; commit them first. Skills remain direct live links.
See [live iteration](architecture/live-iteration.md) for source consistency,
diagnostics, and exceptional maintenance.
