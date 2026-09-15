# Fulcrum

Fulcrum is a local, CLI-operated coordination system for durable Codex work. Its
workflow source of truth is one stock Beads ledger in a Git-backed brain. It does
not require a private workflow database, replay journal, or model-powered
housekeeping process.

The normative product definition is [Fulcrum2 design](docs/fulcrum2/design.md),
[contracts](docs/fulcrum2/contracts.md), and the
[implementation index](docs/fulcrum2/plan/README.md). Operational guidance lives
in [setup](docs/setup.md), [operations](docs/operations.md),
[hooks](docs/hooks.md), and [validation](docs/validation.md).

## Roles

Fulcrum has eight roles. Every role receives its full cooked instructions directly
when a native turn starts; the Beads description retains the same context.

| Role | Responsibility |
| --- | --- |
| Vizier | Human-directed policy, durable preferences, and future-scope authority |
| Marshal | Decision-focused backlog judgment, dispatch, deferral, and recovery priority |
| Weaver | Intake, clarification, investigation, and substantial plan authorship |
| Executor | Implementation of exact authorized work in its assigned workspace |
| Warden | Independent source review, bounded fixes, approval, and delivery |
| Sage | Bounded workflow/evidence investigation |
| Mason | Bounded architecture and test investigation |
| Justiciar | Explicitly scoped recovery and reconciliation |

Leadership is long-lived but idle leadership consumes no execution capacity.
Automatic work defaults to four active tasks globally and four per project. All
four slots are available to ordinary work. The runtime and IPC layers are bounded
but explicitly support a separately validated 30-task concurrency workload.

## Install

Requirements are macOS, Python 3.12, Codex Desktop/CLI, Git, `bd`, `dolt`, and the
configured delivery provider (`tg` for Tollgate). From the retained checkout:

```sh
scripts/setup --input setup.json --non-interactive --json
```

Setup installs locked dependencies and this package into `.venv`, writes the
authoritative YAML configuration beside the brain, builds an independent installed
controller and recovery environment, initializes the Beads/Dolt ledger, installs
uniquely owned services, reconciles the nine human-invoked skills and read-only
compaction hook, validates provider identities, and creates the standing Vizier and
Marshal without model turns. Rerunning the same input repairs owned artifacts and
does not duplicate leaders, providers, or services.

The configuration file is authoritative. Humans may change any allowed field;
Vizier may change policy/model/memory-owned fields. Other roles cannot rewrite
configuration. Defaults include four automatic slots, five-minute dirty-only ledger
publication, and FD soft limit 4096 for an owned shared runtime. Project enrollment
records exact Codex and delivery-provider IDs; source synchronization is opt-in and
requires a configured remote.

Use `fulcrum --help` and `fulcrum COMMAND --help` for the complete installed command
surface. All commands accept `--json`; mutating retries use the same `--request-id`
and exact input. `--offline` runs the same application operation while holding the
canonical brain writer lock. It is not a compatibility or fallback state store.

## Everyday operation

```sh
fulcrum status --json
fulcrum doctor --json
fulcrum project list --json
fulcrum backlog list --json
fulcrum work create --input work.json --json
fulcrum dispatch --bead WORK_ID --authorize --json
fulcrum task wait TASK_ID --until terminal --json
fulcrum trace WORK_ID --json
```

Humans can enter a role directly with a literal request:

```sh
fulcrum enter weaver --description "Investigate and plan the requested change" --json
fulcrum enter justiciar --description "Inspect this exact failed operation" --json
```

`enter` may also take an exact `--bead`, caller `--thread-id`, and explicit
`--model`/`--effort`. It returns registration state, work/task IDs, the acquisition
operation, workspace, and full instructions. A degraded entry never invents
ownership.

Plans use explicit draft, independent review, approval, publication, refinement,
activation and completion commands. Future scope can be activated only by a human
or Vizier. Validation work is proportionate to risk; Fulcrum does not create a
mandatory validation task for every small plan or run the full live role suite for
ordinary delivery.

Runtime control, provider delivery, operation recovery, memory/publication,
analytics, fleet continuity, fixture controls, and every other command family are
listed in the [CLI coverage map](docs/fulcrum2/plan/README.md#complete-cli-and-application-map).

## Recovery and cutover

The independently packaged `fulcrum-recover` entry point exposes only
`inspect`, `takeover`, `repair`, and `release`. Start with read-only inspection:

```sh
fulcrum-recover inspect --instance INSTANCE --json
```

Recovery authority is explicitly scoped and retained in Beads. It does not create
a second journal. A production hard reset is separate, destructive, and requires
an exact preview followed by explicit human confirmation:

```sh
fulcrum recover inspect --instance INSTANCE --json
fulcrum reset --hard --yes --instance INSTANCE --json
scripts/setup --input setup.json --non-interactive --json
fulcrum service status --instance INSTANCE --json
```

Hard reset removes enumerated Fulcrum-owned workflow resources and replaces the
dedicated remote ledger history. It preserves ordinary brain Git documents/history,
configuration, unrelated native tasks/projects/services, and provider resources not
proven owned. It makes no claim about a hosting provider's physical retention.
Never run production reset from documentation alone; it requires a separate,
explicit cutover instruction.

## Validation

Repository checks and public installed-CLI validation are:

```sh
scripts/check
scripts/validate-fulcrum2-cli
scripts/validate-fulcrum2-live --model gpt-5.6-luna --effort low --timeout 3000
scripts/validate-fulcrum2-concurrency --workers 30 --model gpt-5.6-luna --effort low --timeout 600
```

The deterministic suite covers success, denial, conflicts, uncertain effects,
restarts, cutover boundaries, and exact fixture cleanup. Live validation exercises
all eight roles plus real Tollgate/Git delivery. The concurrency smoke requires 30
distinct native tasks and turns active together, observed barrier tool calls,
responsive status, no supported overload signal, and exact subscription/provider
cleanup. Reports contain the invocation, installed source commit, provider facts,
assertions, gaps, cleanup, and evidence paths; a partial or timed-out run remains a
failure.

See [recorded replacement evidence](docs/fulcrum2/validation-results.md) for the
actual acceptance runs and any still-open validation gap.
