# Fulcrum

Fulcrum is a Beads-backed coordination layer for Codex Desktop. Python
processes own workflow policy, Tollgate owns worktrees and delivery, and Codex's
native task and automation tools own task creation, messaging, inspection, naming,
and archival. Fulcrum does not connect to the Codex App Server or maintain a second
workflow queue.

The normative architecture is the [Codex Desktop integration](docs/architecture/desktop-integration.md).
Operational guidance is in [setup](docs/setup.md), [operations](docs/operations.md),
[performance tracing](docs/performance.md), [hooks](docs/hooks.md), and
[validation](docs/validation.md).

## Workflow control

Three visible standing tasks are retained:

- `🧰 STEWARD 🧰` on Luna blocks in `wait_for_instructions`, executes only exact
  claimed native actions, and selects ready work without a Marshal gate.
- `🧭 MARSHAL 🧭` on Sol receives one 15-minute heartbeat for bounded health,
  curation, and recovery work.
- `🔮 VIZIER 🔮` on Sol presents exact decisions requiring human authority.

Workers register against an exact assignment token and workspace. Native effects
move through `pending`, `issuing`, and a recorded terminal or `uncertain` result.
Instruction and CI calls remain pending through the thin broker; every evaluation
runs current committed Python policy in a fresh process.

## Live iteration

The local `master` branch in `~/fulcrum` is authoritative. Commit ordinary code,
formula, documentation, or skill changes and the next operation observes them.
Fulcrum keeps active operations on their original source.
Pending broker connections also remain intact. Do not install, activate, restart,
or wait for a remote push for ordinary edits. See [live iteration](docs/architecture/live-iteration.md).

## Bootstrap

For a fresh installation, clone the repository at `~/fulcrum`, open that checkout
in Codex Desktop, and invoke `$fulcrum-bootstrap`. The bootstrap skill is exposed
directly by the checkout, so it is available before Fulcrum has installed any
global skills or configuration.

The skill runs the checked-in setup script, which creates the Python environment,
installs a source-following `fulcrum` launcher, and atomically initializes
`~/brain/fulcrum.yaml` with discovered executable paths and stock defaults. It then
drives deterministic `fulcrum bootstrap` receipts and executes only returned
native actions. Do not create the brain, configuration, skill links, or MCP entry
by hand. See [setup](docs/setup.md) for prerequisites and the exact first-run
sequence.

Bootstrap installs the source-following MCP entry, five scoped hooks, the
broker service, the three standing tasks, and one Marshal heartbeat. Admission
remains paused until focused acceptance is recorded. An existing task retains its
original MCP catalog, so the bootstrap skill creates exactly one fresh
continuation task after the initial MCP configuration change. Current Desktop
builds list the server under Settings > Plugins > MCPs; do not look for a removed
Restart control, toggle the server, or quit the app. Retained setup resumes in the
fresh task rather than creating duplicate resources.

Configuration contains Beads, Tollgate, saved-project IDs, role models, capacity,
wait budgets, source selection, and diagnostics. It contains no App Server endpoint
or runtime selector. Saved project creation is a user prerequisite when no exact
native project ID exists.

## Everyday operation

```sh
fulcrum status --json
fulcrum doctor --json
fulcrum work create --input work.json --json
fulcrum trace --bead WORK_ID --json
fulcrum logs --limit 100 --json
```

Use `$weaver` for a new request and `$fulcrum-bead` for one small incidental
follow-up. Exact mutating retries reuse the same request UUID and unchanged input.
A timeout means the detached operation or native effect may still complete; inspect
its durable locator before doing anything else.

Plan drafting, approval, publication, refinement, future activation, and mechanical
parent completion remain. Weaver investigation always stays in the invoking task;
Fulcrum never creates another Weaver task. Manual `ledger sync` and `config sync`
remain; automatic publication and curated memory do not.

## Validation

```sh
scripts/prepare-check   # initially and after dependency/packaging changes
scripts/check
```

The complete local check formats, type-checks, and runs focused tests without
provider calls, model turns, CI waits, or scheduled delays. Keep it under the
30-second normal budget and 55-second hard deadline.
