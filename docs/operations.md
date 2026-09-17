# Operations

Use `--json` and a stable `--request-id UUID` for mutations. Equal retries replay
the retained result; changed input conflicts. A timeout or disconnect never proves
that an operation or native effect failed.

## Observe

```sh
fulcrum status --json
fulcrum doctor --json
fulcrum operation list --json
fulcrum trace --bead WORK_ID --json
fulcrum logs --limit 100 --json
fulcrum service status --json
```

Status separates work, capacity, delivery, publication, standing identities,
outstanding actions/waits, schedule state, and diagnostic health. Missing native
or transcript evidence is an explicit gap, never a successful completion.

## Work

Use `$weaver` for new requests and `$fulcrum-bead` for one small incidental report.
Weaver `ready` and deterministically validated implementation-ready reports become
eligible for Steward selection immediately, subject to pause, dependencies,
capacity, overlap, project enablement, and an exact Tollgate workspace. Marshal
curation updates those facts but is not a per-bead gate.

Workers register before substantive work. Executor commits exact assigned source;
Warden submits one candidate and blocks in `wait_for_ci_results` instead of polling.
Failed CI returns to the same Warden task. Justiciar uses one separate recovery slot
only after three ordinary repair failures and may intervene once.

An accepted finish does not itself release capacity. The transcript collector must
observe terminal lifecycle evidence for the exact registered task and turn first.
For Warden, that boundary also authorizes workspace cleanup; promotion and source
sync remain separate retained facts, and failed or uncertain cleanup stays visible
after the worker slot is released.

Plans retain stable keys, approved scope, publication/refinement, future activation,
dependencies, and mechanical parent completion. Substantial plan review occurs as
a native Weaver subagent and is addressed in the authored plan; there are no review
commands or review receipts.

## Pause, Stop, and recovery

`fulcrum pause` is durable and prevents new assignments, continuations, provider
submissions, promotion, sync, cleanup, and recovery creation. Issued effects may
settle and already active assignments may report or finish. `fulcrum resume`
revalidates current facts before new work. The Desktop Stop button only interrupts
one turn and does not create this durable pause.

Marshal's heartbeat coalesces incidents and can resume the same positively stopped
Steward only after reconciling outstanding effects. Unknown/active state never
creates a second relay. Missing tools, approvals, credentials, or scope reach Vizier
with exact evidence.

## Services and publication

Ordinary commits require no `service update`, installation, activation, restart, or
remote publication. Broker restart is exceptional transport maintenance and must
wait for pending responses or retain their uncertainty. Explicit `ledger sync`,
`ledger status`, and `config sync` remain available; no automatic publication loop
exists. Curated memory, fleet replacement, direct native task controls, and App
Server operations are intentionally unavailable.

## Destructive cutover

A fresh cutover requires a separate explicit human instruction. Inventory exact
owned resources, pause admission, disable old schedules, settle writers/effects,
and stop the owned broker/Dolt services before deleting the enumerated old Beads
state and obsolete Fulcrum-owned configuration. Preserve unrelated Desktop tasks,
projects, repositories, delivered source, and shared infrastructure. Native
resources without a supported delete tool require explicit manual cleanup or a
recorded retention exception; archival is not deletion. Bootstrap fresh state and
pass acceptance before reopening admission. Never migrate old workflow receipts or
standing IDs.

The authorized command is `fulcrum reset --hard --yes --request-id UUID --json`.
It refuses a running broker or unsettled admission/assignment/action/operation,
retains `maintenance-fence.json` outside Beads, removes only its enumerated owned
paths, initializes a new empty Beads database, and leaves admission fenced until
bootstrap acceptance succeeds. A rerun with the same request ID resumes a partial
post-deletion initialization; a different request conflicts. Unsupported native
task and automation deletion is recorded as an exact retention exception.
