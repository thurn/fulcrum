# 17 — Recovery and HUMAN resolution

Status: not implemented.

Dependencies: [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md)

Normative reading: [repair commands](../contracts.md#operations-diagnostics-and-emergency-repair), [Justiciar](../design.md#justiciar). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and existing code

Implement scoped takeover, truthful repair and human resolution through the normal
application/adapter contracts. Replace old Operative authority/journal mechanisms
in `operative.py`/`recovery.py`; retain useful exact-target inspection and independent
repair ideas, not private workflow stores or healthy-world prerequisites.

## Public operations

Implement `recover inspect/takeover/repair/release --scope SCOPE`, `human list/resolve`
and Justiciar `finish repaired`. Scopes are exact bead sets, project or instance.
Repair payloads use the enumerated action/target/arguments/reason schema from §2;
unknown actions fail before effects. `recover inspect --scope bead:ID --json` must
work best-effort even when the controller or runtime is unavailable.

## State and implementation steps

1. Persist takeover intent and exact scope/inventory, mark a fence for affected
   work/admission, and stop/observe competing managed turns and owned tools. Only
   then assign Justiciar ownership using that receipt's acquisition ID. A fence
   is Beads control/work state, not an independent journal.
2. At four occupied slots, release owned idle resources and transition the existing
   Marshal task to Justiciar when feasible. Pause ordinary automatic dispatch while
   leadership is unavailable, then restore it through explicit recorded operations.
   If models cannot run, the terminal repair path uses the same adapters and lock.
3. Implement typed actions: interrupt/release subscription/terminate owned terminal,
   adopt owner/replace thread, cancel/reconcile delivery, remove worktree, set
   disposition, restore leadership, repair service/reinstall/quarantine, exact
   stock Beads update and literal Git argv on recorded scoped roots.
4. Before each effect record arguments and observed state; inspect after uncertain
   responses. Preserve dirty/unsent work or quarantine corrupt artifacts. Failed
   repair never automatically removes fences while writers/effects remain unknown.
5. Justiciar may reduce scope or waive checks within scope, retaining defects and
   real delivery evidence. It cannot modify authoritative YAML, silently change
   configured defaults or declare an unobserved promotion successful.
6. Beads outage permits read-only inspection and explicit resource-release/essential
   dependency repair with a truthful no-receipt degraded result. No destructive
   multi-resource reset begins without the separate durable reset authority.
   Reconcile external facts into Beads before restoring normal authority.
7. HUMAN ownership is for irreducible external intervention. Retain exact question,
   required action and intended continuation. Resolve one reason through an explicit
   answer/scope-change; route to Marshal, preserving other blockers and source facts.

## Acceptance

Exercise stale takeover caller, conflicting active writer, full-slot recovery,
uncertain cancellation, repair failure retaining fence, YAML edit denial, reduced-scope
closure and human resolution with multiple waiting reasons. A broken main import
is covered by task 20's independent launcher. No model, healthy ledger, or Desktop
UI is required merely to inspect a failure. Receipt absence must be explicit when
persistence is unavailable. Ordinary cleanup preserves work; destructive wiping
belongs exclusively to task 21.
