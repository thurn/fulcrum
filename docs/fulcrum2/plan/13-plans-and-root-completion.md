# 13 — Plans and root completion

Status: not implemented.

Dependencies: [04](04-work-and-ownership.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md)

Normative reading: [plans](../contracts.md#planning-memory-and-publication-commands), [completed plan lifecycle](../contracts.md#plan-authoring-reviews-and-closure). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and existing code

Implement complete plan authoring/refinement and automatic root completion using
ordinary Beads epics/children. Inspect old `intake.py`, Weaver prompt and publication
code for retained capabilities, not old plan occurrences or frozen-scope tables.
No workflow-step beads or acceptance-test engine are introduced.

## CLI and records

Implement `plan draft/approve/publish/refine/show/activate/complete` and connect
review operations from task 10. Schemas are contracts §§9–10. Example:

```sh
fulcrum plan draft --bead ROOT --input draft.json --json
fulcrum plan approve --bead ROOT --actor human --input approval.json --json
fulcrum plan activate ROOT --authorization ACTIVATION_OPERATION --json
```

The last command references a retained activation authorization for that scope,
not merely any plan approval receipt. `plan show` returns canonical draft/approved
scope, stable child map, review/approval evidence, activation, publication facts
and unsatisfied completion obligations. Preserve full user requirements.

## Ordered implementation

1. Save unpublished drafts without graph activation or Git publication. Use stable
   caller keys, typed validation/check coverage and candidate publication settings.
   Review tasks snapshot this input; changed relevant scope makes old results stale.
2. Accept human/Vizier approval only against actual retained scope and review
   resolution/waiver evidence. Substantial plans require cold-reader and requirements
   perspectives or authorized waiver; small plans/question answering do not acquire
   this whole process by default. Explicit plan approval does not require adding a
   validation child to a small plan.
3. Publish/refine records planned child IDs/edge changes before effects. Reuse IDs
   for surviving keys and preserve delivered children. Deleted unfinished children
   require explicit disposition and stop-before-change. Substantive changes to
   active work are surfaced and invalidate affected approval rather than silently
   replacing an executing task's scope.
4. Distinguish graph creation, local document publication and required remote
   publication. Task 14 supplies publication mechanics. Before that adapter exists,
   ledger-only publication remains usable and remote-required activation remains
   explicitly blocked. No false success or circular implementation dependency.
5. Future roots remain open/deferred after Weaver `finish planned`. Human/Vizier
   authorizes their current approved scope; Marshal only executes that receipt.
   Required remote sync must settle before implementation starts. No timer activates
   future work and freeing capacity does not remove future deferral.
6. Root completion runs after child/publication settlement or `plan complete`.
   Re-read current approved obligations; close only when satisfied. Cancelled,
   rejected or reduced-scope children cause a scope decision, not success merely
   because they are closed. Authorized refinement can remove/replace obligations.
7. Keep root owner Marshal during children; finishing authoring does not close it.
   Separate cross-task validation is chosen only when warranted. Existing child
   checks can prove the whole outcome. Closure queues cost annotation independently.

## Acceptance

Prove small-plan completion without a validation child, future nonactivation,
explicit authorization, stale review/approval rejection, interrupted graph refinement,
active-child scope changes, retained delivered IDs, cancellation blocking and automatic
closure without a new model turn. Cover root reopen and historical costs. A large
fixture includes a deliberately justified assembled-system check; this must not
become a mandatory template child for every plan.
