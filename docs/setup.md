# Setup

Fulcrum setup is rerunnable and Beads-only. It never imports an earlier workflow
database or replay journal.

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

The input document supplies any non-default configuration and initial projects.
`brain.root` is the directory containing the authoritative `fulcrum.yaml`. Runtime
and delivery provider identities are explicit; projects include absolute roots,
validation commands, exact provider IDs when already registered, and whether
source synchronization is required. Use `fulcrum config validate --json` to inspect
the effective document and missing prerequisites.

Setup performs these bounded operations:

1. Creates only declared instance and brain paths and installs locked dependencies.
2. Builds an installed controller environment separate from the development `.venv`.
3. Installs an independent recovery environment and `fulcrum-recover` launcher.
4. Installs uniquely named controller, Dolt, optional runtime, and updater services.
5. Initializes one externally served `fulcrum` Beads database, then receipts all
   subsequent external effects.
6. Reconciles the nine human-invoked skills and the read-only compaction hook without
   replacing real user directories or unrelated hooks.
7. Validates runtime models/efforts and exact project/delivery registrations.
8. Creates or reuses standing Vizier and Marshal identities without model turns.

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

Launch Codex Desktop against the configured shared runtime with:

```sh
fulcrum runtime launch-desktop
```

The command reads the selected runtime endpoint and starts Desktop with the
required `CODEX_APP_SERVER_WS_URL` environment variable. The retained-checkout
shortcut `scripts/launch_codex.sh` invokes the same command.

After changing `pyproject.toml` or `requirements-dev.lock`, reinstall both locked
requirements and the editable package in `.venv`; `scripts/setup` does this.

## Published source

Configure `source.repository`, `source.remote`, and `source.branch`; production
uses only committed published source. The default remote and branch are `origin`
and `master`. Setup provisions the independent runtime once. Ordinary activation
reuses it. See [live iteration](architecture/live-iteration.md) for the required
execution and update boundaries.
