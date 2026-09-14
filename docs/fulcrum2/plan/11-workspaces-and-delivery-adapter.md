# 11 — Workspaces and delivery adapter

Status: not implemented.

Dependencies: [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [04](04-work-and-ownership.md)

Normative reading: [delivery adapter](../contracts.md#delivery-provider), [source publication](../design.md#executor-and-warden). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and existing code

Implement the narrow `Delivery` protocol and initial Tollgate adapter. Inspect
`src/fulcrum/tollgate.py`, `brain.py`, `install.py` and retained Tollgate JSONL fixtures.
Reuse observed provider-response normalization and environment checks; remove the
assumption that streamed `approve --wait` output is one final JSON object.

## Commands and facts

Back `worktree prepare/inspect/cleanup`, `validation start/show`, `promotion start/
show`, and `source sync`. Warden approval authority is task 12's application rule,
not adapter policy. `validation start --bead ID --source OID --json` returns an
operation, exact source and opaque provider handle; long CI returns pending.
Workspace/delivery result fields follow contracts §5, including separate source,
integration OID, validation, promotion, synchronization and cleanup.

## Implementation sequence

1. Record intended branch, exact project/provider identity and locator before
   worktree creation. Tollgate may choose a repository-local `.worktrees` path;
   persist its actual absolute path and verify Git ownership. Never infer ownership
   solely from a pathname under the instance.
2. Run configured preparation/validation argv with explicit cwd/environment and
   bounded output/time. No generated shell interpolation. Repeated preparation
   inspects the same workspace and preserves dirty evidence.
3. Submit immutable source OID through `tg candidate`, retaining handle and source
   mapping. If response is lost, inspect queue/history by exact source/worktree;
   ambiguity is uncertain, not permission to create a new unrelated candidate.
4. Use nonblocking approval/promotion calls, then inspect native status/Git. Normalize
   typed rejection, transient, uncertain, unavailable and unsupported failures.
   A command exit or a green check alone is not proof of integration.
5. Preserve submitted and actual integration source identities when Tollgate performs
   regeneration, together with provider evidence connecting them. Do not match a
   later unrelated candidate just because its branch is current.
6. Synchronize the configured source remote by observing provider publication first;
   otherwise push the recorded promoted integration commit and inspect remote
   ancestry. A newer remote containing it satisfies synchronization. Never promote
   again to retry publication. `not_required` requires explicit project config.
7. Clean only settled managed worktrees/branches after synchronization requirements
   and dirty-evidence checks. Inspect absence after response loss. Cleanup failure
   preserves actual promotion and an actionable remaining obligation.

## Acceptance and exclusions

Run normalization checks against retained real Tollgate responses, including malformed
and multi-object streams. Use disposable Git projects to prove exact-source mapping,
changed integration base, dirty cleanup refusal, lost submit/promotion responses and
remote ancestry inspection. Preserve provider-side merge serialization. Task 22's
deterministic adapter implements the same facts; task 23 proves real Tollgate delivery.
Do not add CI-only stubs that call validation a promotion, force-push source, or
repair unrelated provider history. Live integration capability failure is explicit
and blocks the relevant shipping evidence, not general investigation.
