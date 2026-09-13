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

## Token-usage accounting

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
input. Fulcrum reports usage, not estimated dollar cost or ChatGPT subscription
billing.

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
