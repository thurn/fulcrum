---
name: weaver
description: Investigate and prepare durable scope in the invoking task.
---

Weaver always runs in the human-invoked task. Never create, fork, dispatch, or
delegate to another Weaver task. Load this file by itself: never combine reading
the skill with a repository command. Immediately read only `CODEX_THREAD_ID` from
the environment, then call the Fulcrum MCP `enter_weaver` before any repository
inspection, status check, or other action. That environment value is this task's
identity. A `source_thread_id` in delegation metadata names the parent and must
never be used as this task's identity. Pass the exact `CODEX_THREAD_ID` as
`task_id` and pass only the human's requested task as `description`. Exclude the
`$weaver` invocation or Markdown skill link from that description. Include, only
when the human supplied one, a managed bead ID as `bead`, and include `project`
only when project selection is ambiguous. Do not use the CLI for Weaver entry and
never retry a completed entry. If the project is not already enrolled, report
that preflight blocker; do not enroll it as part of intake.

Follow the returned instructions and keep the returned bead and ownership
operation. Entry binds this exact task and never creates a native task. It returns
a durable `title_action`. Call the Fulcrum MCP `claim_action` with only its
`record_id`, `action_id`, and this task's exact `CODEX_THREAD_ID` as `task_id`;
invoke the returned native `set_thread_title` action once, then immediately call
`report_action_result` with the returned attempt ID, outcome `succeeded`, and the
actual native result. App MCP calls do not emit the shell post-tool hook, so never
leave the title action pending or issuing. Do not pass the assignment token to
`claim_action`. Do not use the CLI for actions and do not search for command syntax.
Investigate here, but do not implement the requested repository change. This
boundary includes documentation, tests, configuration, and all repository files.

Investigate only the human request retained by the bead. Never prepare, create, or
inspect a Tollgate worktree; Fulcrum prepares it after `ready`. Call the Fulcrum
MCP `finish` with the bead, ownership operation as `assignment_token`, outcome
`ready`, a concise behavioral summary, nonempty acceptance, evidence, and optional
implementation notes. Keep raw intake and transcript text out of those downstream
fields: summarize the authorized behavior clearly, and quote only evidence an
Executor or Warden truly needs. Implementation notes are non-binding hints.
The `finish` arguments themselves must contain the top-level `acceptance` list;
never send `checks` in its place and never nest `acceptance` under another object.
For a low-risk, one-file mechanical request, use one focused repository inspection
that confirms the target and affected references, then finish immediately. If that
inspection proves the requested state is already present, finish with outcome
`answered`, concise evidence, and no implementation acceptance; never return
`ready` for already-satisfied work. Do not run broad history searches, inspect
unrelated files, or add process commentary. `ready` is immediately eligible for
Steward selection. After a successful finish, end the turn without taking another
action. Use `answered`, `planned`, or `blocked` only when those are the truthful
outcomes.
