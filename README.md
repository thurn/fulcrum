# Fulcrum

Fulcrum is a Beads-backed coordination layer for stock Codex Desktop. Python
processes own workflow policy, Tollgate owns worktrees and delivery, and Codex's
native task and automation tools own task creation, messaging, inspection, naming,
and archival. Fulcrum does not connect to the Codex App Server or maintain a second
workflow queue.

The normative architecture is [stock Codex Desktop](docs/architecture/stock-codex-desktop.md).
Operational guidance is in [setup](docs/setup.md), [operations](docs/operations.md),
[hooks](docs/hooks.md), and [validation](docs/validation.md).

## Control plane

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
Existing operations keep their pinned source and pending broker connections remain
intact. Do not install, activate, restart, or wait for a remote push for ordinary
edits. See [live iteration](docs/architecture/live-iteration.md).

## Bootstrap

After preparing the dependency environment, invoke `$fulcrum-bootstrap` in Codex
Desktop. The skill drives deterministic `fulcrum bootstrap` receipts and executes
only returned native actions. Bootstrap installs the source-following MCP entry,
six scoped trusted hooks, the broker service, the three standing tasks, and one
Marshal heartbeat. Admission remains paused until focused acceptance is recorded.

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
parent completion remain. A substantial plan is reviewed by a native subagent in
the Weaver turn, not by a separate Fulcrum review subsystem. Manual `ledger sync`
and `config sync` remain; automatic publication and curated memory do not.

## Validation

```sh
scripts/prepare-check   # initially and after dependency/packaging changes
scripts/check
```

The complete local check formats, type-checks, and runs focused tests without
provider calls, model turns, CI waits, or scheduled delays. Keep it under the
30-second normal budget and 55-second hard deadline.
