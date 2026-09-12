# Model selection

Defaults for delegated work: Overseer, Sage, Inquisitor use gpt-5.6-sol/high;
Executor uses gpt-5.6-luna/xhigh. Preserve explicit human/approved-plan overrides
and human-created task preferences. Archon may upgrade a delegated Executor to
Sol with recorded rationale. Astra requires explicit human authorization with
its source reference, separate from any agent recommendation. Never treat an
agent recommendation as that authorization. Check runtime model/reasoning
capability before creation; unavailable choices are a capability problem to
report to Archon, never permission for silent substitution.

Named fleet roles are Codex tasks, not subagents. Initialize implementation
roles with create_thread target project, actual saved projectId, and explicit
environment type local; the Executor owns subsequent Tollgate worktree creation.
Retain returned task/host IDs, chosen model/reasoning, and run/pair identity.
