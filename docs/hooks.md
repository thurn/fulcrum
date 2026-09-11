# Fulcrum Lifecycle Hooks

Fulcrum uses two small Codex hook behaviors: a short refresher after context
compaction and a bounded reminder when an Executor or Overseer may have
forgotten a handoff. The Sage uses existing logs to assess whether these help.

Hooks support the role skills. They do not verify the entire workflow, inspect
every tool call, or make scheduling and promotion decisions.

## Related Information

- [Main design](technical-design.md): roles and system boundaries.
- [Data contracts](contracts.md): ordinary messages and agent-owned state.
- [Operations](operations.md): escalation, patrols, and Tollgate.
- [Dashboard](dashboard.md): workflow improvements and operational visibility.
- [Codex hooks][hooks]: event behavior and configuration.

[hooks]: https://learn.chatgpt.com/docs/hooks

## Default Scope

Enable only the hooks that address demonstrated problems:

| Hook | Purpose | When it runs |
| --- | --- | --- |
| `SessionStart`, matching `compact` | Restore important role instructions and current context. | After compaction. |
| `Stop` | Remind an implementation pair to communicate before stopping. | Normal stops; intervenes only in active Executor/Overseer runs. |

Initial role context comes from invoking the role skill. There is no routine
per-prompt refresher, per-tool policy check, or extra helper-start prompt.
The installed stop handler also receives unrelated root-task stops, but
returns immediately without a reminder for those tasks and Plan-mode
interviews. Codex does not provide a role matcher for `Stop`.

Use a small Python helper that reads Codex's event JSON and the task's existing
Fulcrum registration and progress. It makes no model calls, network requests,
Beads queries, or transcript scans. Missing information is not a reason to
invent task state or block the fleet.

## Refresh After Compaction

For a registered role, emit a short reminder of its responsibilities, current
assignment, and where to read current project memory. Role-specific reminders
include the Archon's delegation boundary, the Executor's reporting obligation,
and the Inquisitor's whole-codebase scope.

The Archon keeps a short briefing in its existing Markdown memory: current
priorities, reasons behind unusual scheduling decisions, and unresolved
questions. Update it when decisions change, prune stale material, and commit
and push normally. The refresher can include a brief excerpt and a link.

An Executor refresher could say:

```text
You are Executor 3, working with Overseer 3 on fc-91.
Send your Overseer a message when ready for review, complete, or blocked.
A final response in your own task does not notify the Overseer.
Use your owned wt worktree and obtain the required promotion mandate.
Read current assignment state and holds before continuing.
```

Aim for roughly 500 tokens, with a 1,000-token output limit. Use existing local
facts and links; do not inject complete plans, source files, or conversation
history. If registration is missing, emit no role-specific context. A Weaver
first invoked in Plan mode relies on its skill until it can register normally.

Codex's `SessionStart` event with source `compact` delivers the refresher before
the next model request, including after automatic compaction during a turn.
[Codex event behavior][hooks]

Illustrative configuration for the installed helper:

```toml
[[hooks.SessionStart]]
matcher = "^compact$"
[[hooks.SessionStart.hooks]]
type = "command"
command = "/absolute/path/to/fulcrum-hook"
timeout = 2
additionalContextLimit = 1000
```

The Archon remains a persistent user-created task. Fresh-task handover remains
available when drift or runtime problems warrant it; there is no automatic
weekly replacement.

## Bounded Handoff Reminder

The stop hook is a reminder, not proof that a message was delivered. It uses
existing progress state to avoid bothering an agent already in a healthy wait
or finished after reporting its outcome.

The agent records whether it has sent its current handoff in its own progress
record, updating that indication when a new handoff is needed. It marks the
handoff sent only after the Codex messaging tool succeeds. The hook trusts
this report; it does not maintain independent receipts or parse conversations.

At a normal stop:

1. Return without intervention for unrelated roles, inactive runs, Plan mode,
   or an already reported outcome or healthy wait.
2. If `stop_hook_active` says Codex already continued this stop sequence,
   allow stopping. Never repeat the correction indefinitely.
3. Otherwise, if an active Executor or Overseer may still owe a handoff, issue
   one short reminder to check and send it if needed.

The reminder should explicitly avoid duplicate messages:

```json
{
  "decision": "block",
  "reason": "Before stopping, check whether your current handoff was sent. If not, message the responsible agent with the outcome or blocker. If already sent, do not resend. Update your progress and stop when appropriate."
}
```

Codex uses this response to continue the agent. That continuation consumes a
model turn, so avoiding unnecessary reminders matters more than optimizing a
few local file reads. [Stop-hook behavior][hooks]

A review wait, CI wait, or escalation wait can stop normally after the needed
communication. The hook grants no new authority and must not restart work
against a pause, cancellation, or newer user instruction. Interview-only wakes
of completed agents are outside active implementation runs.

If a send fails, the agent retains the error and follows normal escalation.
If it still cannot report after the reminder, its unresolved progress remains
available to the Night Watchman. The hook cannot guarantee communication after
crashes, direct archival, missing hooks, or an incorrect progress report.
Those remain responsibilities of the skills and patrol process.

## Diagnostics for the Sage

Start with the logs and timing information Codex and Tollgate already retain.
The useful questions are whether handoffs are still being forgotten, whether
reminders help, and whether injected context or corrections cost too much.

- Inspect compaction/refresher frequency, hook failures, and stop reminders.
- Use agent interviews and actual follow-up messages to assess effectiveness;
  counting reminders alone does not prove that a missed handoff was fixed.
- Measure helper execution time and the extra model turns caused by reminders.
- If existing logs omit a useful fact, the two existing handlers may write a
  small best-effort diagnostic entry with task, event, time, and outcome.
  Keep it local and untracked, separate from agent-owned progress.

Diagnostic loss must not change workflow behavior. No receipt store,
per-invocation file collection, retention-dependent handoff checks, or log
processing service is required. Do not copy complete tool outputs or message
bodies into a new log.

A narrowly matched `PostToolUse` logger is an option when a concrete Sage
investigation lacks a particular result or failure signal. It is not part of
the default installation. Reuse duration fields when available; do not add a
matching pre-tool hook merely to time every command. Remove instrumentation
when it stops providing useful evidence.

## Opportunities Considered

These are possible responses to future observed problems, not requirements to
implement or enable by default:

| Opportunity | Why it is omitted |
| --- | --- |
| Broad `PreToolUse` policies | Frequent execution, brittle command interpretation, and overlap with skills and Tollgate. A specific recurring mistake could justify a narrow check later. |
| `UserPromptSubmit` refresher | Role activation and compaction refresh supply context without repeating instructions every turn. |
| `SubagentStart` or `SubagentStop` policy | The parent supplies the assignment; native completion returns the result. |
| Messaging-result `PostToolUse` receipts | Extra matching and state machinery exceeds what a reminder needs. |
| Stop checks for every role and self-archive guards | Adds distinct completion checklists and tool interception; normal role skills and patrols retain these duties. |
| `PreCompact` and `PostCompact` logging | The existing refresher already provides a useful compaction observation. |
| `Interrupt` and `SessionEnd` handlers | Existing task status and patrols cover recovery; these hooks cannot guarantee completion or cleanup. |
| `PermissionRequest` automation | Fulcrum does not add automatic permission grants. |

Any added hook should identify the observed failure, the narrow event matcher,
and a way to assess its benefit and cost. Audit coverage does not imply that
every lifecycle event needs instrumentation.

## Performance and Compatibility

Install one Fulcrum hook source through Codex's supported review/trust flow.
Keep existing unrelated hooks intact. Resolve real task identity during setup;
do not apply private role context based only on a repository path or title.

The helpers run synchronously with short explicit timeouts, normally two
seconds as a ceiling rather than a performance target. Keep them local and
quiet. Do not write Fulcrum files in Plan mode, alter another agent's state,
start services, run builds, or commit and push from a hook.

The inspected CLI reports hooks as stable and enabled, but actual desktop
loading and event delivery still need verification. Unknown or failed hook
coverage must not be presented as working protection. Role skills, Codex's
normal permissions, and Tollgate remain in effect when hooks are unavailable.

Keep validation focused: verify a real desktop compaction receives the brief
refresher; a disposable Executor receives at most one handoff reminder; an
already reported wait, inactive run, and Plan-mode task stop without one.
Measure the added latency and correction turns in that exercise. Adjust or
remove a noisy reminder before extending its scope.

The dashboard may show recent reminders and hook errors in operational details,
without new receipt states or a stream of routine hook events. The Archon
highlights useful improvements in NEWS; the Sage proposes changes based on
observed workflow benefit.
