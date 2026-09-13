# Technical design

The product design is
[`plans/fulcrum-python-runtime.md`](plans/fulcrum-python-runtime.md). The
[reliability architecture](reliability-architecture.md) is authoritative for
failure semantics and runtime invariants. The runtime has five narrow boundaries:

1. `kernel.py` atomically admits leases and defines retry/progress invariants.
2. `controller.py` is the supervised single asyncio writer and scheduler.
3. `store.py` contains current SQLite state, enforcement triggers, operation
   attempts, transition history, worker health, and diagnostic events.
4. `runtime.py`, `tollgate.py`, and `beads.py` return observed native facts without
   granting workflow authority.
5. `cli.py` parses local commands and forwards mutations over a Unix socket.

Python creates and names managed Codex tasks. Archon owns scheduling judgment;
Executor implements; Overseer independently reviews; Weaver authors intake; Sage
and Inquisitor produce evidence-based reports. Only one execution-pair member may
run at a time. Native helpers remain children of their parent and retain its slot.
Before every managed turn, Python selects Codex's built-in Default collaboration
mode and concise reasoning summaries through app-server thread settings. The
server owns the mode instructions; Fulcrum does not copy or version their text.

SQLite stores native task/turn IDs, normalized per-turn token-usage snapshots,
separate role counters, approved scope
snapshots, pair bindings, assignments, actions/outcomes, reservations, composed
holds, external operation intent and attempts, frozen message batches, specialist
occurrences, interviews, publication/delivery obligations, worker heartbeats,
audited transitions, and concise events. There is no
parallel JSON state store, role plugin framework, event-replay architecture, or
agent-owned operational record.

## Usage and API-equivalent cost accounting

The app-server's `thread/tokenUsage/updated` notification is retained as one
coalesced cumulative row per native thread and turn, then associated with the
generic Fulcrum action. This covers every role and action kind without a
role-specific schema. A row keeps the configured model and reasoning effort, the
latest-response breakdown when supplied, the full cumulative breakdown, context
window, observation times, terminal time, and an explicit coverage state. Missing
values remain unknown; they are never converted to zero.

`totalTokens` is the authoritative raw aggregate. `cachedInputTokens` is already a
subset of input and `reasoningOutputTokens` is already a subset of output, so
callers must not add every field together. Total processed tokens are also not a
measure of unique prompt text: repeated context and cache reads remain processed
input. Fulcrum also freezes an estimate of equivalent public OpenAI API charges.
This is not actual ChatGPT subscription consumption, credits, an invoice, internal
cost, or marginal cost.

Pricing is response-specific. For each `last` response boundary, ordinary input is
`input - cached input - cache-write input`; its contribution charges those three
input categories at their separate rates and all output at the output rate.
Reasoning output is informational within output and is never charged again.
Negative quantities, invalid subset relationships, and reasoning output above
output make the contribution invalid instead of being coerced. Decimal text retains
exact arithmetic; display normally rounds to cents, with positive sub-cent values
shown as `<$0.01`.

The dated rate card retains provider, effective and configured models, effective
processing tier, currency, unit rates and multipliers, rules, official model URL,
and captured/effective timestamps. The initial card was captured 2026-09-13 from
the official Sol, Terra, Luna, and Astra model pages. Prompts above 272,000 input
tokens apply 2x input/cache and 1.5x output rates to that response only. Batch/Flex
use 0.5x and Fast/priority 2x when observed. A reroute uses its effective model.
The controller retains the protocol-native `model/rerouted` tuple (`threadId`,
`turnId`, `fromModel`, `toModel`, `reason`) and associates it with the following
response boundary. Once associated, that reroute is not eligible for another
response; later responses use their own effective-model fact or the configured
model assumption. Value-identical delivery is idempotent while that occurrence is
pending; after consumption, the same tuple represents a new observable occurrence
and can be associated with a later response. A reroute with no following boundary
is reported as partial instead of being guessed onto an earlier response. Missing
model, tier, rate, or response boundaries produce explicit partial coverage.
Frozen contributions retain their rates, so later card changes never recompute
history.

Direct usage answers what the managed agent thread used. Attributed usage includes
that direct usage plus native helper turns linked by app-server collaboration
identities, including nested and late helpers. Unrelated project threads are never
inferred to be helpers. Helper ownership is append-stable per collaboration item,
including its parent native turn; the helper's `turn/started` lifecycle identity
binds that invocation to one native helper turn. Repeated calls to one helper within
the same parent turn therefore remain distinct, while replay of an item cannot
rewrite an earlier turn. If a lifecycle gap leaves multiple possible owners, the
observed turn remains unassociated with partial coverage instead of being assigned
to the newest relationship. Action summaries
roll up to task/agent, assignment, run,
role, and project by summing each turn's final cumulative snapshot—not repeated
streaming samples. A disconnect, restart, or missing notification leaves observed
counts partial or unavailable with a gap reason; workflow completion never waits
on telemetry.

`fulcrum cost` returns frozen token/tool components, response/action counts,
provenance, assumptions, and coverage by action, task, role, assignment, run,
project, or causal workflow. Direct cost covers the managed action; attributed
cost also includes observed nested native helpers. Priced tools are separate generic
contributions with quantity, unit, rate, source, and amount. An observed tool with
no authoritative public rate is excluded and makes coverage partial; unobservable
calls are never inferred as zero. A completed App Server `webSearch` item is one
observable call and resolves against the dated official tool card captured
2026-09-13 at $10 per 1,000 calls. Started/replayed items do not create additional
contributions, and late completed items retain native turn identity for direct or
helper attribution.

A causal identity begins at Weaver intake and follows its beads through Archon
proposal/approval, all Executor/Overseer correction and recovery actions, helpers,
and delivery. Specialist workflows use occurrence identity. Explicit joins exclude
unrelated concurrent work and survive retries and Archon succession. The controller
acknowledges judgment-free completion updates without starting an Archon turn,
emits a concise completion event, and freezes the workflow total. Conditional state
transitions and unique source identities make replay and restart idempotent.

Controller-bound Archon actions use a layered contract: action/update identities;
bead and project with a bounded scope summary; capacity and active work;
dependencies, holds, and conflicts; then allowed outcomes and required decisions.
An aggregate action budget always retains frozen update facts and finish guidance;
active-work, conflict, hold, and uncertain-operation rows share the remaining space
round-robin, with exact shown and controller-retained counts for omitted rows. The
complete dispatched prompt has a separate deterministic ceiling. Batch admission
freezes the largest ordered update prefix whose mandatory layers fit; overflow
updates remain retained for later exact batches instead of failing advancement.
The full proposal scope is retained in a durable `scope_references` row keyed to the
frozen update. Archon returns that stable identity, never copied scope text. Outcome
application rejects missing, malformed, cross-update, or stale references before
resolving the exact retained scope into an assignment. Review-failure escalations
identify only the current failing review as unresolved, label older findings as
history without inferring their resolution, and carry bounded candidate revision,
intervening change evidence, and current reviewer recommendation. Complete evidence
stays in handoffs.

The installed `$sage` human entry skill registers the current native task against
one exact retained Bead. Registration resolves that Bead's single causal workflow
and most recent applicable Executor/Overseer assignment before retaining or
renaming anything. The controller then adopts the current turn as one
`human-skill` Sage occurrence, freezes a bounded causal evidence snapshot, and
requires exactly one interview round with that assignment's Executor and
Overseer. Queued reviews remain available separately as `fulcrum sage request`;
direct registration creates no policy or cadence.
