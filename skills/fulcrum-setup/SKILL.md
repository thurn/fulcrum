---
name: fulcrum-setup
description: Install, verify, and bootstrap a Fulcrum fleet from a human-created Codex task, including Watchman guidance, project enrollment, hourly scheduling, hook evidence, the readiness gate, and transition to the first Archon. Use for initial setup or repair, not routine fleet coordination.
---

# Fulcrum Setup

Complete setup until the fleet is ready or one exact human-only action remains.
This invoking task is enrolled as the bootstrap Archon so it can own the initial
registries, but it begins routine coordination only after the gate passes. It is
human-created because the user started it; never create a substitute Archon or
Watchman. Do not activate in Plan mode.

Read [turn](../fulcrum-shared/turn.md), [identity](../fulcrum-shared/identity.md), and
[handoffs](../fulcrum-shared/handoffs.md) before writing live state. Use the installed
`archon` skill after the final transition.

## Resume safely

Treat every invocation as resumable. Inspect the installation, registries,
Codex tasks, automations, projects, hooks, Git, Beads/Dolt, and Tollgate before
changing anything. Reuse matching state and schedules. A missing observation is
unknown, not permission to invent an ID, task, trust decision, or health result.

If another current Archon is registered, do not seize authority. Complete only
read-only diagnosis or follow the Archon skill's cooperative handover procedure.
If this task's actual ID cannot be resolved uniquely with supported Codex tools,
stop with one precise identity instruction. Titles are discovery aids, not IDs.

## Automated preflight

1. Resolve the installed brain, state, source, skills, and hooks paths from the
   installation record. Fulcrum requires a retained Git checkout; wheel-only or
   copied-skill installations are invalid. Verify `HEAD`, `release`, and
   `origin/master` agree before installing.
2. Run `scripts/check` when source verification or installation changed. Run
   `fulcrum doctor`, `fulcrum brain status`, `bd where`, `bd dolt status`, and
   `bd dolt test`. Inspect `tg repo list`, configuration, and status through its
   supported CLI. Do not start a second database service.
3. Use supported Codex project listing to resolve the actual Git root, project
   ID, host, and Git-project flag for exactly Fulcrum, Tollgate, and Battlement.
   Match each canonical root to one active Tollgate repository with remote push
   enabled and no block reason.
4. Verify every `~/.codex/skills/fulcrum-*` entry is a symlink to the matching
   directory under the retained checkout's `skills/`, and
   `~/.codex/hooks/fulcrum` is a symlink to its `hooks/`. Verify the user-level
   hooks file contains exactly one marked Fulcrum `SessionStart` compact handler
   and one marked Fulcrum `Stop` handler using the linked `fulcrum-hook`. Do not
   duplicate hooks per project.

Repair safe, in-scope installation drift with the documented idempotent
`fulcrum install` command from retained certified source. Preserve the brain,
active state, unrelated skills, and unrelated hooks. Report rather than bypass
remote mismatches, uncertified source, unknown schemas, or unhealthy
integrations.

The links are the runtime contract. Edits under the retained checkout's
`skills/`, `hooks/`, and editable `src/fulcrum/` tree take effect on the next
read or process invocation; do not copy files, compute content hashes, record
per-run skill snapshots, or reinstall after an edit. Restart Codex only if its
skill discovery cache does not show a changed skill.

## Human task checkpoint

Use this current task as the proposed first Archon. Preserve its selected model
and reasoning as human-authorized. Locate a distinct, persistent,
human-created Watchman through supported task listing and history.

If none exists, pause and tell the user exactly this:

1. Create a new local task in the saved Fulcrum project.
2. Name it `Fulcrum Night Watchman`.
3. Start it with: `Use $night-watchman. Act as the persistent Night Watchman for this Fulcrum fleet.`
4. Leave it unarchived and return here.

Do not call task creation for either persistent role. If multiple Watchman
candidates exist, reconcile their actual IDs with the user instead of guessing.

## Bootstrap roles and projects

After both actual task IDs and human-selected model settings are known, create a
temporary JSON input using [the bootstrap input contract](references/bootstrap-input.md),
then run `fulcrum setup bootstrap --input <file>`. Mark each role
`human_created: true`, include a concrete human authorization reference, and
include exactly three projects. For every project include both its intended
mapping and fresh observations named `git_root`, `codex_id`, `codex_path`,
`codex_host`, `codex_is_git`, `tollgate_id`, `tollgate_path`, and
`tollgate_healthy`.

The command initializes or reconciles the role registry, makes this task the
current Archon, initializes its progress, validates each project with
`fulcrum.coordination.enroll_project`, and writes the project registry through
the ownership-safe writer. It is idempotent. An unhealthy project is retained
disabled with its exact reasons and keeps the gate closed; repair it rather
than excluding an initial project merely to pass.

## Install one Watchman heartbeat

Inspect existing Codex automations before writing, using the supported automation
view operation for known IDs and the local Codex automation records for
discovery. Convert them to the observations accepted by
`fulcrum.watchman.watchman_automation_plan` and follow that plan:

- `create`: create one active hourly heartbeat attached to the resolved
  Watchman task.
- `update`: update that exact automation in place.
- `none`: make no change.
- More than one match: reconcile duplicates; never add another.

Use the plan's exact name and patrol prompt with the supported Codex automation
tool. Do not create standalone Sage or Inquisitor schedules. View the resulting
automation and retain its actual ID as evidence.

## Verify hooks and record evidence

Configuration inspection is automatic; trust and desktop delivery are human
evidence. If the user-level hook definition changed, instruct the user to open
`/hooks`, inspect it, and trust it. Editing the linked hook implementation does
not require reinstalling Fulcrum. Never use a trust bypass. Ask the user to
confirm one real compact refresh and one normal Stop exercise. If Codex cannot
compel compaction, retain the documented unsupported fallback instead of
claiming delivery.

After viewing the active hourly automation, run:

```text
fulcrum setup record-evidence \
  --archon-task-id <this-actual-task-id> \
  --watchman-schedule-id <actual-automation-id> \
  --codex-projects-verified-at <observed-UTC-time>
```

When the user also confirmed the desktop exercise, append:

```text
  --hooks-verified-at <observed-UTC-time> \
  --hooks-evidence <concise-human-confirmation>
```

The two hook arguments must be supplied together. Without them, scheduling and
project evidence are recorded while desktop delivery remains an explicit
optional fallback. The command refuses a non-current Archon or a missing human
Watchman. Never record schedule or hook success from intent alone.

## Clear the gate and become Archon

Run `fulcrum doctor` again and save its JSON outside the source checkout. Then
run `fulcrum readiness --matrix <retained-source>/docs/readiness-evidence.json
--doctor-report <doctor-json>`. This preserves the checked-in historical
baseline while overlaying current runtime evidence. Required failures keep the
gate closed; optional unsupported desktop/runtime visibility is allowed only
with its documented fallback.

When and only when both commands report ready:

1. Announce the exact Archon and Watchman task IDs, automation ID, enrolled
   projects, hook result, and remaining optional limitations.
2. Read the installed `archon` skill and its referenced shared guidance.
3. Continue in this same task as the persistent first Archon. Reconcile holds,
   assignments, runs, pushes, and due work before dispatch. Do not archive this
   task on routine completion.
