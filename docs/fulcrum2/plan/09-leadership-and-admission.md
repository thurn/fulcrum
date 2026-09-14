# 09 — Leadership and admission

Status: not implemented.

Dependencies: [07](07-role-context-and-entry.md), [08](08-controller-supervision.md)

Normative reading: [Marshal](../design.md#5-marshal-context-and-dispatch), [freshness](../contracts.md#ownership-operation-and-decision-freshness). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and source boundary

Implement persistent Vizier/Marshal identities, selective judgment and mechanical
admission. Replace Archon/scheduling assumptions in `scheduling.py`, `controller.py`
and `store.py`. Configuration remains YAML; decisions/reservations remain Beads.

## Public commands

`leader show`, `marshal brief`, `marshal request`, `marshal decide`, `backlog list`,
`dispatch --bead ID [--human|--authorize]`, `policy show/set` and later leader replacement share
the contracts. `marshal request --bead ID --json` returns a decision receipt and
selected work; `marshal brief` alone is read-only. Decisions reference that receipt
and include expected ownership/phase plus action-specific fields. Return per-row
applied/conflict results without rolling back valid independent decisions.

## Implementation and durable state

1. Bootstrap standing task identities from recorded creation intents, without a
   Vizier model turn. New raw intake is effectively Marshal-owned; if leadership
   cannot be established, retain truthful HUMAN accountability.
2. Select at most 12 candidate/changed rows by urgency and priority, with missing-
   summary markers and continuation commands. Bound the complete serialized brief
   including JSON to 6,000 characters; 2,000 tokens is a target, not an asserted
   conversion. Keep full comparison facts on the decision receipt.
3. Coalesce judgment events for up to two seconds, keeping one outstanding Marshal
   turn. Apply dispatch/defer/clarify/duplicate/reject/recover/human decisions.
   Compare relevant current scope, dependencies, policy and source against retained
   values even if owner/phase did not change. Conflicting rows are stale; do not
   regenerate their judgment automatically from an old answer.
4. Persist dispatch authorization on work. Reserve starts under the same short
   lock as ledger writes. Default global/project limits are four, with no reserve.
   Count each managed active task or in-flight start once, including leadership and
   independent reviews. Idle leaders do not count. Keep unknown managed activity
   reserved until resolved. Human starts bypass policy and are visibly counted.
5. Inspect dependencies/waits at actual start. Ordinary freed capacity executes an
   existing authorization without another Marshal turn. Serialization of overlap
   tags is a judgment recorded with dispatch; provider integration remains serialized
   by delivery. Use `control-plane` for Fulcrum runtime/install edits.
6. Typed deferral triggers react only to relevant changes. Future plans require a
   human/Vizier activation receipt; Marshal cannot invent one. One resolved waiting
   reason does not erase others. Duplicates preserve evidence and canonical links.

## Acceptance

Test four simultaneous automatic reservations and fifth refusal, then freeing one
without a new decision turn; human bypass and unknown-task accounting are distinct.
At full capacity, prove repair can use existing Marshal/terminal paths. Test changed
scope with unchanged owner/phase, mixed stale/valid decision rows, coalescing, ignored
idle ticks and future-plan activation rejection. Vizier receives no automatic
message during bootstrap/replacement/ordinary completion. Explicit policy writes
remain human/Vizier-only. Native leadership behavior is exercised in task 23.
