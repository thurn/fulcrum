# Operations

The CLI is the complete operator surface. Use `--json` for stable
envelopes and `--request-id UUID` for a mutating request that may need an exact
retry. A timeout means observation is incomplete; inspect the retained operation
before retrying.

## Observe first

```sh
fulcrum status --json
fulcrum doctor --json
fulcrum backlog list --json
fulcrum human list --json
fulcrum operation list --json
fulcrum logs --limit 100 --json
```

`status` reports configuration, services, loops, admission, runtime pressure, and
publication state. `trace ID` ties work, ownership, tasks, delivery, and operations
together. `wait --bead ID --until VALUE` and `operation wait ID` are bounded
observation commands, not escalation.

## Work and roles

Create/update work with JSON input, explicit dependencies, and concrete acceptance.
Ownership is the acquisition or transfer operation ID; the acting task and that
exact operation must match. Progress changes `last_transition`, never ownership.

```sh
fulcrum work create --input work.json --json
fulcrum work dependencies WORK_ID --input dependencies.json --json
fulcrum dispatch --bead WORK_ID --authorize --json
fulcrum enter weaver --description "Literal human request" --json
fulcrum context --bead WORK_ID --json
```

Marshal receives separate grooming, dispatch, and recovery decision batches.
Operator authorization queues ordinary admission; `dispatch --human` is an explicit
human bypass and remains observable. Vizier gets no unsolicited turns. Future plan
activation requires a human or Vizier authorization. Small plans do not
automatically acquire a validation child; select checks proportionate to risk.

Use `task list/show/output/wait/requests/terminals` for inspection and the matching
typed commands for send/respond/interrupt/terminal stop/release/archive. Targeted
terminal stop never broadens into stopping unrelated resources. Subscription
release is explicit and independently observable.

## Services and source refresh

```sh
fulcrum service status --json
fulcrum service stop --json
fulcrum service start --json
fulcrum service restart --json
fulcrum service update --json
```

Ordinary local-master commits are available automatically at the next command or
background operation, with no service update, network discovery, installation,
or restart. Client timeout leaves detached operations running on their original
source. A failed preparation fails new commands visibly instead of silently
using old code. Service status and service update --retry remain available
from retained code for diagnosis and repair. Skills link directly to master;
subsequent skill reads need no commit or update operation.

Service stop/restart is exceptional maintenance: without explicit `--interrupt`,
active native turns or pending requests prevent replacement. A state migration
requires `service update --maintenance`; failed migration leaves admission fenced.
See the [live-iteration architecture](architecture/live-iteration.md) for source
selection, concurrency, maintenance, and recovery rules.

## Publication, delivery, and analytics

Ledger publication is native Beads/Dolt Git transport and runs only when real native
or document/config changes are dirty, at the configured five-minute cadence. Its own
receipt fields do not create a publication loop. Selected plans, memory, knowledge,
and configuration publish through their explicit commands.

Delivery facts come from the configured provider and Git source identities. Warden
approval binds exact source; promotion and optional source synchronization are
separate observed operations. `usage`, `cost`, and `rates` report source attribution
and coverage. Unknown pricing remains an explicit gap, never fabricated zero cost.

## Recovery

Begin with read-only evidence:

```sh
fulcrum-recover inspect --instance INSTANCE --scope SCOPE --json
```

The independent launcher remains usable with a broken main package or stopped
controller. `takeover`, typed `repair`, and `release` require an exact scope and
retain their fence/operation in Beads. Justiciar may act only within explicit human
scope. Repairs never create a side journal or silently steal active ownership.

## Cutover runbook

Production reset is not implied by installing or reading this runbook. Obtain a
separate explicit human instruction, preserve the `recover inspect` inventory, then:

```sh
fulcrum recover inspect --instance INSTANCE --scope reset --json
fulcrum reset --hard --yes --instance INSTANCE --json
scripts/setup --input setup.json --non-interactive --json
fulcrum service status --instance INSTANCE --json
fulcrum doctor --instance INSTANCE --json
```

The hard reset is resumable and removes only enumerated owned tasks, provider
registrations/workspaces, services, ledger state, and obsolete installation
artifacts. It replaces dedicated remote ledger history and verifies fresh-clone
absence while preserving ordinary brain Git history/documents/configuration and
unrelated external resources. Hosting-provider physical retention is outside the
claim boundary.
