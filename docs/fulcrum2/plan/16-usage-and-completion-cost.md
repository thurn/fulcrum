# 16 — Usage and completion cost

Status: implemented and validated.

Dependencies: [06](06-codex-runtime-adapter.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [13](13-plans-and-root-completion.md)

Normative reading: [usage/cost](../contracts.md#usage-and-cost-commands), [root annotation](../contracts.md#automatic-cost-metadata-on-top-level-beads). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and existing code

Retain API-equivalent cost reporting with accurate scope and explicit gaps. Inspect
pricing/usage calculations near the end of `src/fulcrum/store.py` and old CLI reports;
reuse correct decimal/counter logic without SQLite rows, lineage IDs, or separate
native-subagent accounting. Prices are documented inputs, not frozen assumptions
copied from old code.

## CLI and stored facts

Implement `usage`, `cost`, `rates list/show/add`, and `usage reconcile`. Support the
intersection filters and groupings in contracts §9. `cost --workflow ROOT --json`
returns currency, priced subtotal, nullable complete total, coverage, included native
turns, direct/review/coordination components and rate provenance. `work show ROOT`
exposes the same latest summary at `fc.completion_cost`.

## Implementation sequence

1. Persist one analytics record per unique managed native task/turn, selected through
   exact external_ref and preselected receipt IDs. Terminal counters replace repeated
   cumulative samples; never sum streaming samples. Retain response-level pricing
   facts and split beyond 64 response entries into linked ordinal blocks.
2. Attribute ordinary work/review tasks to their recorded workflow root; new unrelated
   reports create their own root. Allocate shared Marshal turn cost equally across
   distinct roots in its recorded decision input. Weights sum to one; unattributed
   leadership is overhead. Do not infer native-subagent contributions or double-count
   usage already included in native totals.
3. Retain configured/effective models, reroute events and service tiers. Deduplicate
   pending reroute observations and consume at the next response only. Missing model/
   response evidence makes pricing partial, not zero or assumed configured-model cost.
4. Seed immutable documented USD rate cards verified from official sources during
   implementation, with effective/retrieval time. Use Decimal strings and documented
   cached/cache-write/output/long-context/tier/tool rules. Reasoning tokens are part
   of output already. Unsupported model/tier/tool prices stay unpriced.
5. Finalize on root closure without a model turn. First persist the summary with its
   included native IDs and exclusions; then write completion_cost on the original
   root through its completion receipt. Pending terminal observations are visible;
   unreachable usage can finalize explicit partial coverage rather than block work.
6. Recover missed annotations from the same receipt after restart. Late observations
   or pricing create a correction referencing the prior frozen result, then update
   the root reference. Reopened intervals sum unique lifetime turns, not prior
   rollups plus those same turns. While open, last completion remains historical.

## Acceptance

Use hand-calculable fixtures for cached input, reroutes, repeated cumulative events,
shared Marshal attribution, independent reviews, unknown prices and reopened roots.
Assert no double counting across children/root, exactly one annotation after duplicate
completion/restart, preserved frozen totals and explicit late corrections. Query
with controller stopped and after log pruning. Missing telemetry cannot hold delivery
open or produce a fake zero. Live task 23 checks observable coverage and provenance;
it need not require telemetry the runtime cannot supply or invent actual subscription
spend from this estimate.
