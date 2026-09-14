# 22 — Deterministic CLI validation

Status: not implemented.

Dependencies: [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [04](04-work-and-ownership.md), [05](05-observability-and-inspection.md), [06](06-codex-runtime-adapter.md), [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md), [13](13-plans-and-root-completion.md), [14](14-memory-and-document-publication.md), [15](15-brain-publication.md), [16](16-usage-and-completion-cost.md), [17](17-recovery-and-human-resolution.md), [18](18-task-continuity-and-fleet.md), [19](19-installation-and-service.md), [20](20-source-refresh-and-recovery-launcher.md), [21](21-hard-reset-and-cutover.md)

Normative reading: [CLI validation](../contracts.md#7-cli-driven-validation), [fixture controls](../contracts.md#disposable-fixtures-and-test-controls). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and development order

Build checked-in executable regression scripts invoking the installed CLI against
real isolated stock Beads. This is a cross-cutting verification task: introduce its
minimal fixture/provider slice alongside task 02, add cases as features land, and
complete its full dependency set after task 21. Earlier tasks own their initial
behavioral checks; they do not wait for this task to finish. Tests are not Fulcrum
work kinds or controller acceptance workflows.

## Public tools and implementation

1. Implement `fixture create/show/cleanup` and the test-only
   `scenario emit/advance/fault/crash` utilities as contracts §10 specifies.
   Fixtures own disposable repositories, remotes, ports, provider registrations and
   task inventories; creation uses normal setup/project operations. No test defaults
   may resolve to production brain, integration branch or controller service.
2. Provide deterministic Runtime/Delivery implementations matching production fact
   types. Their separate persisted provider state survives controller restart so an
   applied-but-lost mutation can be observed. It is external fake-provider state,
   never a Fulcrum ownership/replay store. Fake delivery performs real fixture Git
   operations so delivered OIDs/ancestry can be asserted, not invented.
3. Events affect provider facts only. Faults select provider/method/occurrence and
   applied/not-applied outcome; they cannot directly set work phase/owner. An injected
   clock drives supervision and timers. Named crash boundaries target the controller
   after real persisted steps and are unavailable in native/production instances.
4. `scripts/validate-fulcrum2-cli` is an external subprocess driver with case selection,
   bounded waits and JSON/Markdown reports. Read IDs from public result envelopes;
   do not import controller internals, patch transport calls, or query private SQL.
   Direct fixture setup/assertion Git reads can support test oracles, but every
   workflow transition and intervention must be possible through Fulcrum commands.
5. Exercise both a foreground served instance and explicit offline operations. Test
   cancellation/client timeout separately from killing a controller at a recorded
   boundary. Preserve failed evidence before explicit fixture cleanup.

## Required behavioral cases

- Creation/retry/graph partial-write recovery; native intake from two project actors;
  scope conflicts and stale ownership after same-task reacquisition.
- Marshal mixed stale/valid decisions, changed acceptance with unchanged phase,
  four-slot admission, direct human bypass, independent waits and future activation.
- Lost task create/start, pending native requests, full prompt delivery, interruption,
  sealed finish, handoff crashes, Warden fixes and uncertain successful promotion.
- Publication native writes/cadence/idle suppression, concurrent control edits, remote
  divergence, late source sync, and preservation of unrelated staged Git changes.
- Draft/review/refinement/approval, small-plan completion without validation child,
  root cancellation blocker, memory/fleet context and archive-once/manual unarchive.
- Config authority/outage, retry exhaustion, no-slot recovery, independent launcher,
  source refresh, reset authority/remote history and essential degraded inspection.
- Unique usage and completion metadata across duplicate events/restart/log pruning.

## Completion evidence

Reports contain case names, pass/fail assertions, public command/result references,
bead/task/operation/source IDs, gaps and cleanup outcome. Failures remain failures;
ordinary application recovery may still complete the requested workflow. No hidden
whole-suite rerun or pass manufactured by state mutation. Standard project checks
run appropriate deterministic cases without native model calls. Large prompt or
module-layout snapshots are not acceptance evidence.
