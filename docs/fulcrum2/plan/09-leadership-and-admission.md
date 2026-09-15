# 09 — Leadership and admission

Status: implemented and validated.

Dependencies: [07](07-role-context-and-entry.md), [08](08-controller-supervision.md)

Normative reading: [Marshal](../design.md#5-marshal-context-and-dispatch), [decision briefs/current context](../contracts.md#marshal-decision-briefs-and-current-context), [freshness](../contracts.md#ownership-operation-and-decision-freshness). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and source boundary

Implement persistent Vizier/Marshal identities, selective judgment and mechanical
admission. Keep Marshal focused on explicit choices, supplied author evidence and
current decisions rather than activity history. Replace Archon/scheduling assumptions in `scheduling.py`, `controller.py`
and `store.py`. Configuration remains YAML; decisions/reservations remain Beads.

## Public commands

`leader show`, `marshal brief`, `marshal request`, `marshal decide`, `backlog list`,
`context --role marshal`, `dispatch --bead ID [--human|--authorize]`, `policy show/set` and later leader replacement share
the contracts. Both brief/request accept `--kind auto|groom|dispatch|recover`.
`marshal request --bead ID --json` returns a decision receipt and selected work
when judgment is needed; otherwise it returns an explicit no-decision result
without starting a model turn; `marshal brief` alone is read-only. Decisions reference that receipt
and include expected ownership/phase plus action-specific fields. Return per-row
applied/conflict results without rolling back valid independent decisions.

## Implementation and durable state

1. Bootstrap standing task identities from recorded creation intents, without a
   Vizier model turn. New raw intake is effectively Marshal-owned; if leadership
   cannot be established, retain truthful HUMAN accountability.
2. Implement one brief selector with `groom`, `dispatch` and `recover` purposes.
   Every row states the choice required, why now, unknowns and relevant evidence.
   Group related choices; do not mix urgent recovery with speculative proposals.
   Select at most 12 rows by urgency, existing priority and age, with counts by
   purpose and continuation commands for omitted work. Prefer fewer complete rows
   over stripping uncertainty to fit. Bound the whole serialized prompt, including
   current-context/memory excerpts and JSON, to 6,000 characters; 2,000 tokens is
   a target. Keep full comparison facts on the existing decision receipt.
3. Consume author-supplied `intake.benefit/uncertainties` plus existing outcome,
   acceptance, dependencies, size and evidence. Missing detail is an unknown,
   never a registration failure or an invented positive readiness signal. Marshal
   may read full context for a quick question. For substantial investigation, its
   `clarify` decision transfers the unstarted bead to Weaver with a concrete
   question/expected result, using ordinary ownership and admission. Continue
   other work; reconsider that bead only on relevant new findings/scope. Splitting
   uses Weaver's existing graph/refinement path. No interviews or extra gate.
4. Coalesce compatible judgment events for up to two seconds, keeping one
   outstanding Marshal turn. Completed Executor work, ordinary freed capacity and
   unchanged failures do not generate status-only model turns. Select newly urgent
   recovery at the next safe boundary without interrupting a productive turn.
   Apply dispatch/defer/clarify/duplicate/reject/recover/human decisions. Compare
   relevant current scope, intake facts, dependencies, policy and source against
   retained values even if owner/phase did not change. Reject only stale rows;
   never regenerate their judgment automatically from an old answer.
5. Persist current decision rationale and reconsideration conditions in existing
   dispatch/waiting/disposition fields, with their `decision_operation` reference.
   Replacing a decision leaves prior evidence on its receipt, not in the next
   prompt. Build `context --role marshal` from those facts, pending decisions and
   YAML. Share this projection with normal turns, compaction and replacement;
   omit closed work and superseded incidents except a directly relevant linked
   fact. Do not maintain a second backlog/summary store or replay transcripts.
   This read projection works before task 14's curated-memory implementation;
   task 07 supplies the context/hook integration point.
6. Persist dispatch authorization on work. Reserve starts under the same short
   lock as ledger writes. Default global/project limits are four, with no reserve.
   Count each managed active task or in-flight start once, including leadership and
   independent reviews. Idle leaders do not count. Keep unknown managed activity
   reserved until resolved. Human starts bypass policy and are visibly counted.
7. Inspect dependencies/waits at actual start. Ordinary freed capacity executes an
   existing authorization without another Marshal turn. Serialization of overlap
   tags is a judgment recorded with dispatch; provider integration remains serialized
   by delivery. Use `control-plane` for Fulcrum runtime/install edits. Grooming
   may authorize newly clear work in its answer; brief kinds must not create a
   mandatory second approval turn when no additional judgment is needed.
8. Typed deferral triggers react only to relevant changes. Future plans require a
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


Add compact public-CLI fixtures for incomplete/overlapping proposals, competing
priorities, a dependency chain and recovery arriving during a grooming turn.
Assert the required decision/evidence is present, kinds stay separate, omitted
work is visible, unknown intake remains accepted, and a clarification transfers
the same bead while other work proceeds. Replay routine completion/idle events
and assert no unnecessary decision turn. Rebuild context after compaction/restart
and assert current rationale/triggers survive without superseded incidents.
These deterministic cases prove the information/transition contract, not model
judgment quality. Task 23 inspects real Marshal choices within its existing live
workflow/budget; no additional live-role gate or stress run is added.
