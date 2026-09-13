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

SQLite stores native task/turn IDs, separate role counters, approved scope
snapshots, pair bindings, assignments, actions/outcomes, reservations, composed
holds, external operation intent and attempts, frozen message batches, specialist
occurrences, interviews, publication/delivery obligations, worker heartbeats,
audited transitions, and concise events. There is no
parallel JSON state store, role plugin framework, event-replay architecture, or
agent-owned operational record.
