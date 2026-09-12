# Dashboard read contract

There is no dashboard server in this repository. A future dashboard consumes the
machine-readable `fulcrum status --json` view. It should display native task links
and canonical names, project/run/bead/stage, thread and helper activity, slot use,
unfinished assignment count, holds, pending decisions, review and candidate
identity, delivery/publication/archive obligations, last reconciliation, possible
stall evidence, and the concrete reason queued work cannot start.

Unknown observations render as unavailable. Completed work with a failed archive
operation remains completed with archive pending. No UI component may infer
authority from a title or write controller tables.
