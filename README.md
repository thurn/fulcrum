# Fulcrum

Fulcrum is a local, CLI-operated coordination system for durable Codex work. Its
workflow source of truth is one stock Beads ledger in a Git-backed brain. It does
not require a private workflow database, replay journal, or model-powered
housekeeping process.

The normative product definition is in the
[Fulcrum2 contracts](docs/fulcrum2/contracts.md) and the
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

## Live iteration

Fulcrum runs local-master application changes in fresh processes while a small
resident host preserves native connections and pending requests. Read the
[live-iteration architecture](docs/architecture/live-iteration.md) before changing
these boundaries.

**Commit to local master; the next operation uses it automatically.** The master
checkout at ~/fulcrum is authoritative. Operations retain immutable source
snapshots for consistent imports and assets, but no installation, explicit update,
push, or restart makes ordinary behavior changes live. Skills link directly into
~/fulcrum/skills and subsequent reads see edits immediately.

## Bootstrap

Requirements are macOS, Python 3.12, Codex Desktop/CLI, Git, `bd`, `dolt`, and the
configured delivery provider (`tg` for Tollgate). From the retained checkout:

```sh
scripts/setup --input setup.json --non-interactive --json
```

Setup provisions locked dependencies and an editable `.venv`, writes the
authoritative YAML configuration beside the brain, initializes the Beads/Dolt
ledger, writes uniquely owned service definitions, reconciles the nine
human-invoked skill links and read-only compaction hook, validates provider
identities, and creates the standing Vizier and Marshal without model turns.
This provisions infrastructure: launchers automatically resolve local master,
and ordinary code changes need only a commit. Running operations retain their
source and existing agent turns continue. Rerunning the same input repairs owned
operational artifacts and does not duplicate leaders, providers, or services.

The configuration file is authoritative. Humans may change any allowed field;
Vizier may change policy/model/memory-owned fields. Other roles cannot rewrite
configuration. Defaults include four automatic slots, five-minute dirty-only ledger
publication, and FD soft limit 4096 for an owned shared runtime. Project enrollment
records exact Codex and delivery-provider IDs; source synchronization is opt-in and
requires a configured remote.

Use `fulcrum --help` and `fulcrum COMMAND --help` for the complete command
surface. All commands accept `--json`; mutating retries use the same `--request-id`
and exact input. Commands run selected source directly. Ledger-only operations do not require
the resident host; native runtime operations share its persistent connection.

## Everyday operation

```sh
fulcrum status --json
fulcrum doctor --json
fulcrum project list --json
fulcrum backlog list --json
fulcrum work create --input work.json --json
fulcrum dispatch --bead WORK_ID --authorize --json
fulcrum task wait TASK_ID --until terminal --json
fulcrum trace --bead WORK_ID --json
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
analytics, fleet continuity, and every other command family are
listed in the [CLI coverage map](docs/fulcrum2/plan/README.md#complete-cli-and-application-map).

## Recovery and cutover

The independently packaged `fulcrum-recover` entry point exposes only
`inspect`, `takeover`, `repair`, and `release`. Start with read-only inspection:

```sh
fulcrum-recover inspect --instance INSTANCE --scope instance --json
```

Recovery authority is explicitly scoped and retained in Beads. It does not create
a second journal. A production hard reset is separate, destructive, and requires
an exact preview followed by explicit human confirmation:

```sh
fulcrum recover inspect --instance INSTANCE --scope instance --json
fulcrum service stop --instance INSTANCE --json
fulcrum reset --hard --yes --actor human --instance INSTANCE --json
scripts/setup --input setup.json --non-interactive --json
fulcrum service status --instance INSTANCE --json
```

Hard reset removes enumerated Fulcrum-owned workflow resources and replaces the
dedicated remote ledger history. It preserves ordinary brain Git documents/history,
configuration, unrelated native tasks/projects/services, and provider resources not
proven owned. It then bootstraps clean leadership and services; reset is replacement,
not instance disposal. It makes no claim about a hosting provider's physical retention.
Never run production reset from documentation alone; it requires a separate,
explicit cutover instruction.

## Validation

Prepare dependencies initially and when they change, then run the complete check:

```sh
scripts/prepare-check
scripts/check
```

The check runs formatting, full strict type checking, and all tests in a prepared
Python environment with a 55-second deadline. Tests use in-memory records and
mocked providers, with small local file/socket checks. Expensive integration and
live validation harnesses have been permanently removed.

See [validation instructions](docs/validation.md) for coverage and timing policy.
