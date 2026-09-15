# Validation

Validation reports are evidence, not workflow state. Public scripts drive the
installed CLI and public fixture/provider controls; they do not add an acceptance
work kind, retry engine, or hidden state injector.

## Repository checks

```sh
scripts/check
```

This builds a clean check environment from `requirements-dev.lock`, installs the
package, checks formatting and types, and discovers the complete unit/integration
test suite.

## Deterministic installed CLI

```sh
scripts/validate-fulcrum2-cli --report /absolute/report.json
```

The suite uses isolated real Beads state and deterministic runtime/delivery adapters.
It covers normal delivery plus invalid input, actor denial, duplicate/conflicting
requests, lost responses, restart/reconciliation, dependency and review paths,
publication/analytics, recovery, reset boundaries, and exact fixture cleanup.

## Live eight-role workflow

```sh
scripts/validate-fulcrum2-live \
  --model gpt-5.6-luna --effort low --timeout 3000 \
  --report /absolute/live-report.json
```

This starts real Luna work for Vizier, Marshal, Weaver, Executor, Warden, Sage,
Mason, and Justiciar and proves an actual Tollgate/Git delivery. The report records
native task/turn IDs, prompts, tool evidence, ownership, source identities,
provider facts, usage coverage, cleanup, and observation gaps. The acceptance
budget is 50 minutes.

## Thirty-task concurrency

```sh
scripts/validate-fulcrum2-concurrency \
  --workers 30 --model gpt-5.6-luna --effort low --timeout 600 \
  --report /absolute/concurrency-report.json
```

The smoke pauses admission while recording 30 normal authorizations, unpauses for
controller reconciliation, and requires 30 distinct native task and turn IDs active
together at a public barrier. Every worker must call the barrier tool and later
finish with evidence. Status must remain responsive, the runtime must report no
supported overload signal, and every subscription/task/provider/fixture owned by the
run must be released or removed. Work creation/admission, ledger subprocesses,
external runtime calls, and IPC all remain explicitly bounded.

The ten-minute budget includes fixture installation and cleanup. A report remains
failed if only the overlap assertions pass but terminal closeout or cleanup is
incomplete. To avoid repeating a known-good long setup while diagnosing a later
phase, retain and inspect the per-phase report and use focused unit/integration
tests; do not relabel partial evidence as a pass. A reusable live fixture may be
added only if it preserves exact ownership, source identity, and cleanup semantics.

## Evidence rules

Every report records its invocation, installed source commit, fixture/runtime and
delivery-provider facts, assertions, explicit gaps, cleanup results, and JSON plus
Markdown paths. Keep successful reports; do not overwrite them with later attempts.
Any source change that can affect the exercised path requires proportionate retest.
Documentation-only changes and deletion of unreachable replacement code do not
invalidate already-recorded native execution evidence, but the final repository
checks and deterministic installed-CLI suite still run after cleanup.

Actual replacement runs are listed in
[validation-results.md](fulcrum2/validation-results.md).
