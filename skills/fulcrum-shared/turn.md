# Starting a turn and ending a run

Resolve actual identity first; read `fulcrum context --task <actual-id>` and
current assignment/holds using `fulcrum state read`. Read only relevant short
role/project/global memory. Treat read failures as unknown, never an empty fleet.
Verify registry ownership each turn, especially after compaction. A former
Archon yields when the current ID differs. Read the assignment's approved plan
commit, bead, mandate, and current holds before acting; a working plan edit does
not silently replace an approved contract.

Write only your owned progress, evidence, or assignment record. Set phase,
phase_started_at, expected_next_actor/action and owned resources truthfully.
Report blocked work to the next responsible role: Executor to Overseer to
Archon. An identified review/CI/decision wait is healthy; do not run endlessly
or infer completion from idle runtime. Record delivery errors for patrol.

Before archival, send the result through the shared handoff procedure and verify
owned resource cleanup. Preserve outstanding push/recovery obligations with an
identified owner. Weaver may archive after reporting failed Git/Beads pushes to
Archon; an Executor cannot close code work with source push or cleanup pending.
Persistent human-created Archon/Watchman do not archive on routine completion.
