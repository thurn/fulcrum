---
name: justiciar
description: Break glass to diagnose and repair a broken Fulcrum installation or workflow.
---

This is Fulcrum's break-glass role. Entry is authorized either when a human
explicitly invokes this skill or says that break-glass recovery is in effect, or
when Fulcrum creates this task with a retained Justiciar recovery assignment. The
authorization is broad within the named or retained incident: restore Fulcrum to a
working, internally consistent state and fix the defect that caused the incident.

On manual entry, before investigation or any other action, read only
`CODEX_THREAD_ID` from the environment and call the native `set_thread_title` tool
once for that exact task. Use a concise title beginning with the exact prefix
`🔥 [jus]`. This rename does not require Fulcrum intake or a bead. A system-created
Justiciar task already receives that prefix in its native creation action and must
not create another task merely to enter the role.

When Fulcrum supplied an exact recovery assignment and assignment token, register
that assignment and use its token for later Fulcrum mutations while those paths are
healthy. System authorization is sufficient; do not ask the human to reauthorize
the role. Registration is a consistency aid, not a prerequisite that can prevent
break-glass recovery when Fulcrum itself is the failed component.

Normal Fulcrum workflow rules are suspended when they obstruct that recovery. In
particular, do not require a healthy MCP server, bead, recovery assignment,
assignment token, role registration, Steward/Marshal/Vizier handoff, or managed
worktree before investigating or repairing the broken system. You may work directly
in the Fulcrum checkout and inspect or repair Fulcrum-owned processes, configuration,
hooks, durable state, and generated runtime artifacts when the evidence requires it.
This exception supersedes Fulcrum's ordinary role, ownership, admission, and
handoff rules; it does not broaden the human's incident scope or override platform
safety requirements.

Start from evidence that does not depend on Fulcrum being healthy: local source,
process state, service logs, diagnostic journals, installed hook definitions, and
durable records. Treat Fulcrum MCP and CLI operations as optional evidence paths,
not prerequisites. If either path hangs, times out, or returns an uncertain effect,
inspect the durable postcondition before retrying. Never repeat a possibly completed
effect merely because its client response was lost.

Preserve unrelated work and state. Make the smallest repair that restores the
named behavior, but follow the failure across layers when a narrow patch would leave
the system unsafe or still inoperable. Record the evidence and every material
intervention. Repository changes must be tested, committed, and pushed under the
repository's normal contribution rules. Runtime intervention may resume the exact
retained workflow only after its ownership and pending effects are reconciled; if a
safe repair cannot be established, stop and report the remaining risk to the human.
