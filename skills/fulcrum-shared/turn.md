# Starting a turn and ending a run

Persistent and implementation roles resolve actual identity first; read
`fulcrum context --task <actual-id>` and current assignment/holds using
`fulcrum state read`. Read only relevant short role/project/global memory. Treat
read failures as unknown, never an empty fleet. Verify registry ownership each
turn, especially after compaction. A former Archon yields when the current ID
differs. Read the assignment's approved plan commit, bead, mandate, and current
holds before acting; a working plan edit does not silently replace an approved
contract. An ephemeral Weaver has no registry, assignment, or durable progress
record to resolve.

Registered roles write only their owned progress, evidence, or assignment
record. Set phase, phase_started_at, expected_next_actor/action and owned
resources truthfully. Report blocked work to the next responsible role:
Executor to Overseer to Archon. An identified review/CI/decision wait is
healthy; do not run endlessly or infer completion from idle runtime. Record
delivery errors for patrol. A Weaver reports completion once to the current
Archon when routable, surfaces a send failure, and does not wait, poll, or create
durable coordination state.

Every Fulcrum role must not call Codex `wait_threads`, including the local MCP
alias `mcp__codex_app__wait_threads`, nested code-mode calls, zero-timeout
snapshots, babysitting, uncertain handoffs, or current-turn-result waits. Use
direct handoffs and one-shot inspection instead. This hook is a practical
guardrail, not an absolute security boundary, so the shared instruction remains
authoritative when hook delivery is unavailable.

Before archival, registered roles send the result through the shared handoff
procedure and verify owned resource cleanup. Preserve outstanding push/recovery
obligations with an identified owner. A Weaver sends its one-way completion
report directly to Archon when routable, surfaces failed Git/Beads pushes in
that report, and may archive without a handoff record. An Executor cannot close
code work with source push or cleanup pending.
Persistent human-created Archon/Watchman do not archive on routine completion.
