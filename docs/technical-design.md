# Technical design

The authoritative design is
[`plans/fulcrum-python-runtime.md`](plans/fulcrum-python-runtime.md). The runtime
has four narrow boundaries:

1. `controller.py` is the single asyncio writer and scheduler.
2. `store.py` contains current SQLite state, constraints, and diagnostic events.
3. `runtime.py`, `tollgate.py`, and `beads.py` return observed native facts without
   granting workflow authority.
4. `cli.py` parses local commands and forwards mutations over a Unix socket.

Python creates and names managed Codex tasks. Archon owns scheduling judgment;
Executor implements; Overseer independently reviews; Weaver authors intake; Sage
and Inquisitor produce evidence-based reports. Only one execution-pair member may
run at a time. Native helpers remain children of their parent and retain its slot.

SQLite stores native task/turn IDs, separate role counters, approved scope
snapshots, pair bindings, assignments, actions/outcomes, reservations, composed
holds, external operation intent, frozen message batches, specialist occurrences,
interviews, publication/delivery obligations, and concise events. There is no
parallel JSON state store, role plugin framework, event-replay architecture, or
agent-owned operational record.
