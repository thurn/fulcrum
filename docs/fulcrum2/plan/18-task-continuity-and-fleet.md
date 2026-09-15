# 18 — Task continuity and fleet

Status: implemented and validated.

Dependencies: [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [13](13-plans-and-root-completion.md), [17](17-recovery-and-human-resolution.md)

Normative reading: [archive once](../design.md#archive-once), [continuity](../contracts.md#installation-recovery-and-continuity-commands). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and source boundary

Implement same-work continuity, archive-once behavior and explicit fleet replacement.
Inspect old lineage reuse/archive/fleet code in `lifecycle.py` and `controller.py`;
retain intent and useful native inspection, not unrelated conversation worker pools
or repeated archive timers.

## Commands and records

Complete `leader replace ROLE --reason`, `fleet replace --mode drain|interrupt
[--project ID] --reason`, and archive/unarchive controls. Return exact selected
managed IDs, old-to-new map, pending ownership transfers, stop evidence and receipt.
`task show` exposes replacement links, associated work, archive state/deadline and
current subscription observation. No task is considered absent merely because it
is unloaded or archived.

## Implementation steps

1. Reuse an idle task only for the same bead/role with compatible scope, config and
   current acquisition. Start from retained context and current facts, not transcript
   replay or a closed unrelated task. Restart locates it without creating a successor.
2. Release subscriptions as soon as no immediate turn is required. Archival is
   independent: default ten-minute eligibility requires idle status, no owned open
   work, and no associated active/pending implementation. Deferred future work alone
   does not retain a finished author. Standing leaders are never auto-archived.
3. Persist pending deadline, then `archive_state=requested` and request operation
   before the one lifetime automatic send. After uncertain response, inspect rather
   than resend. Mark done only from observed archive state. A native/manual unarchive
   after that request becomes suppressed and never restarts the timer.
4. Fleet replacement snapshots exact selected tasks and records replacement IDs before
   creation. Pause selected admission. Drain waits for turns to stop; timeout never
   silently switches it to interrupt. Interrupt mode explicitly interrupts and observes
   termination before transferring acquisitions/workspaces.
5. Preserve memory/policy, worktree/source evidence, current phase and causal costs.
   Task mapping reconciles partial replacement without duplicate starts. Replace each
   standing leader once; a Vizier successor receives no unsolicited model turn.
   Optional explicit archival retains a successor link and old evidence.
   A Marshal successor rebuilds the task 09 current-context projection, including
   pending decisions and current rationale/triggers, before its next decision.
   Do not load completed incidents or replay the predecessor's notification log.
6. Project-scoped replacement cannot unexpectedly replace shared standing leadership;
   it replaces selected project work/review tasks, with leaders retained. Instance-wide
   replacement includes both leaders. Neither restarts the shared native runtime nor
   interrupts unrelated human tasks. Associated review results survive replacement.

## Acceptance and failures

Advance a fake clock through handoff, active child work, future-only deferral,
completion, archive and manual unarchive. Assert one automatic archive send across
restart and no rearchive after manual unarchive. A lost archive response must not
cause another request. Interrupt/drain replacement survives a crash at each mapping/
transfer boundary with one current owner. Verify preserved dirty work, context and
cost attribution. Runtime outage leaves replacement pending and scoped admission
paused; it never becomes permission to delete the fleet or invoke hard reset.
