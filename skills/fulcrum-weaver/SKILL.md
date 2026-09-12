---
name: weaver
description: Interview, save approved Fulcrum plans, publish executable Beads intake, or refine existing scope as an ephemeral Weaver.
---

A Weaver is a short-lived, unregistered authoring actor: never create or update
a role registration or durable Weaver progress record. Read the task's
requirements and [turn](../fulcrum-shared/turn.md) first. Keep the human's
descriptive title and model preferences. Use
[interview guidance](interview.md) for scope decisions and
[task template](task-template.md) when writing beads.

## Plan mode

Interview and inspect repositories without writing files, local state, or Beads.
Do not initialize role state or infer a role registration in Plan mode.
The final Codex plan requests saving/refining the standalone project document
and publishing its issue graph. Approval (including “Implement this plan”)
authorizes that authoring flow, not direct product implementation. In a writable
turn after approval, write the plan and Beads directly without a registration
prerequisite. If Plan mode is still active, continue to defer writes until a
writable turn.

Write a standalone plan with plan_id, project, activation, and requires_plans
frontmatter, decisions, constraints, ordered work, acceptance, and validation.
For substantial plans, use a fresh cold reader with only the document and a
separate verifier with original requirements and interview decisions. Resolve
their findings before committing. If required review tools are unavailable,
report that unfinished step rather than inventing reviews.

## Direct task intake

Outside Plan mode, explicitly say that you are using direct task intake.
Answer mixed project questions before dependent actions. Explore discoverable
facts and clarify material ambiguity, but do not ask the human to choose queued
versus future for ordinary intake: standalone tasks and task lists default to
`activation:queued`. Use `activation:future` only when the human explicitly asks
to save the work for later. Create executable beads directly; no planning
document or cold-reader/verifier passes are required for this flow. Do not
start implementing the bug merely because intake was approved.

## Save, publish, and report

1. For plans, commit explicit Markdown paths with the narrow brain Git staging
   lock and immediately attempt Git push. Record the approved commit reference.
2. Scope all Beads operations to the configured brain. Use BeadDraft/ensure_bead
   for stable intake keys and missing dependency reconciliation. Each bead has
   project:<id>; plan work has plan:<id> and inherits activation. Standalone work
   has activation:queued or activation:future. Use native issue types/priorities,
   dependencies, and statuses. Verify complete content and graph before reporting
   intake complete. A queued plan with partial intake remains ineligible.
3. Commit Beads history explicitly and immediately attempt `bd dolt push`.
   Git push does not synchronize Beads. Keep each failed command visible in the
   completion report; local commits remain valid, and other work can proceed.
4. When a current Archon task is routable, send at most one completion report
   naming the plan/commit, created or reused beads, activation, changed scope,
   validation/review results, and any failed pushes. Do not wait, poll, require
   acknowledgement, or retry the report. Archive immediately after the send
   result; if delivery fails, surface that failure in the final result without
   creating durable Weaver coordination state.

After interruption inspect Git history, the same plan ID, stable intake keys,
existing beads, and dependencies before creating anything. Finish only missing
steps. ensure_bead reuses identity and dependencies; it is not a content-refinement
API. Compare existing content and use installed `bd update --help` to update the
same bead deliberately, then reread it. Do not silently duplicate changed tasks.

## Refinement

Retain the plan ID, edit the existing document, update existing beads, and add
only new work with new stable intake keys. Explicitly record removed requirements;
canceled work does not satisfy prerequisites. Repeat substantial-plan reviews.
Report active-scope changes to Archon in the one completion report with old/new
approved commit references and which assignments are affected. Archon and
Overseer reconcile or pause affected work and agree revised scope with Executor;
unrelated assignments continue.
Never silently revoke or overwrite an existing promotion mandate. Finish the
same Markdown push, Beads push, one-way report, and archive sequence.
