---
name: fulcrum-bootstrap
description: Install, bootstrap, or resume the Fulcrum control plane in Codex Desktop from a fresh ~/fulcrum clone.
---

Use `~/fulcrum` on local `master` as the retained checkout. Resolve setup inputs
from observed state rather than asking the user to transcribe them:

- Use an explicit instance/config selection when supplied, otherwise
  `FULCRUM_INSTANCE`, otherwise `~/Library/Application Support/Fulcrum`; the
  unselected authoritative configuration defaults to `~/brain/fulcrum.yaml`.
- Use `CODEX_HOME` when set and `~/.codex` otherwise.
- Read the available native task-tool names from the current tool catalog and the
  supported model/effort matrix from the native task creation schema. Pass short
  tool names such as `create_thread`, not MCP-qualified names.
- Call `list_projects` and retain exact project IDs. The saved project whose path
  is `~/fulcrum` is the only valid target for an MCP-refresh continuation task.

On first use, run `~/fulcrum/scripts/setup` with a stable request UUID from
`uuidgen` and JSON input containing `codex_root`, `native_tools`, and
`model_support`. Pass JSON through an already-attached stdin stream with
`--input -`; never pass inline JSON as the `--input` argument or start the command
before its stdin content is attached. Use setup when
the source-following launcher has not yet been provisioned; after the first
invocation use `fulcrum bootstrap` directly. Setup provisions Python 3.12
dependencies, installs the source-following command launcher, and atomically
creates the authoritative YAML configuration when absent. Preserve any existing
authoritative configuration. Do not ask the user to hand-create configuration or
skill links. If required executables, credentials, or an explicitly requested
saved project are unavailable, report that exact prerequisite. Never install or
use an App Server fallback.

In JSON mode, provisioning progress may precede the result on stderr. Read the
final JSON response rather than treating those logs as malformed output. The
outer envelope state `completed` means only that the command invocation finished;
bootstrap status comes from `result.state`, `result.admission`, pending actions,
and prerequisites. Reuse a request UUID only to resume the exact same command
after a client timeout. Use a new request UUID after returned actions or
prerequisites settle so bootstrap can observe the new postconditions.

**Approve hooks before refreshing the tool catalog.** If bootstrap reports
`hooks.operator_confirmation_required: true` or
`hooks.operational_state: confirmation_required`, ask the user to approve the
hooks and end the current turn. Do not create the MCP-refresh continuation,
invoke any returned setup action, or poll while approval is pending. A configured
hook command is not evidence of approval.

- Open **Settings**.
- Select **Hooks**.
- Click **Trust** for the five required Fulcrum hooks: SessionStart,
  UserPromptSubmit, PreToolUse, PostToolUse, and Stop.
- Click **Enable** for those five required hooks.

After the user explicitly confirms approval, rerun bootstrap with a new request
UUID so it can observe the postconditions. Do not create the continuation until
that response confirms all five hooks are trusted and enabled and no longer
requires operator confirmation. If approval is still incomplete, report the
exact gap and stop again.

Only after hook approval has been observed, apply the MCP catalog boundary. An
existing task does not acquire MCP tools added after that task started. If the
response reports `mcp.changed: true`, or the MCP block is already present but the
current non-continuation task lacks Fulcrum MCP tools, stop before its returned
native setup actions and create exactly one continuation task so it receives the
current tool catalog. Invocation of this skill authorizes that single bounded
task only after the hook gate has passed. A task whose prompt marks it as the sole
MCP-refresh continuation must never create another; it stops and reports the
missing tools instead.

- Call native `create_thread` once with title `Resume Fulcrum bootstrap`, omit
  model/effort overrides, and use target
  `{"type":"project","projectId":"<saved fulcrum ID>","environment":{"type":"local"}}`.
  Never use a worktree or projectless target. If that saved project is absent,
  report it as the exact prerequisite instead of creating a substitute.
- Use this prompt, preserving its meaning and the explicit recursion fence:

  ```text
  $fulcrum-bootstrap
  Read and follow `~/fulcrum/skills/fulcrum-bootstrap/SKILL.md` from the retained
  local-master checkout.
  Resume the retained initial bootstrap with a new request UUID. This is the sole
  MCP-refresh continuation task. Do not create another continuation task. If
  Fulcrum MCP tools are unavailable here, report that exact failure and leave
  admission paused.
  ```

- Do not retry an uncertain task-creation result. After a successful dispatch,
  stop in the original task without invoking pending setup actions or polling the
  continuation.

The current Desktop UI lists MCPs under Settings > Plugins > MCPs and may notice
configuration changes without an app restart. Do not instruct the user to find a
removed Restart control, toggle the server, or quit the app. A fresh task—not a
process restart—is the required catalog boundary; this follows the current
[OpenAI Plugins guidance](https://learn.chatgpt.com/docs/plugins) that newly added
tools become available in new chats.

For every returned setup action in the continuation task, call `action claim`
before invoking the exact named native tool once, then call `action result` with
the actual result and the `attempt_id` returned by the claim. Omit `request_id`,
`attempt_id`, `loop_id`, and `turn_id` on new MCP calls when the tool schema says
Fulcrum generates them. Supply retained IDs only when resuming the exact timed-out
call. Do not edit action arguments, augment a native result, invent a retry ID, or
retry an uncertain native effect. A newly created standing task must call
`register_standing` with its retained action marker before other activity.
Report a successful `create_thread` result immediately; when the native creation
path does not emit an initial prompt callback, the matching settled `threadId`
provides the registration evidence instead.
After all standing creations are reported, make one bounded `wait_threads` call
for the still-running standing tasks, with a maximum wait of 120 seconds, so their
initial registration turns can settle. Do not poll unchanged tasks. Rerun
bootstrap with a new request UUID after that bounded wait; a still-missing
registration is an exact prerequisite, not authority to recreate the task.
Preserve the fixed titles `🧰 STEWARD 🧰`, `🧭 MARSHAL 🧭`, and
`🔮 VIZIER 🔮`; correct a normalized title only through a separately retained
native action.

Setup is complete only when all three identities, the source-following MCP server,
five trusted required hooks, broker socket, supported models/tools, transcript and
accounting checks, an active Marshal heartbeat targeted at the retained Marshal,
and focused acceptance are recorded.
Record acceptance only from direct evidence:

- `workspace_access`: each retained standing task has its reported workspace and
  can read the expected local resources;
- `hook_identity`: callbacks bind the exact retained task/session identity and
  unrelated tasks remain unbound;
- `transcript_lifecycle`: registered task turns expose prompt, tool, completion or
  pending-wait lifecycle with no collection gap;
- `usage_accounting`: exercised managed turns have attributed token/cost rows or
  an explicit supported zero-cost observation, with no missing reason;
- `task_targeting`: exact titles and retained task IDs match creation, diagnostic,
  and schedule targets.

For each check, pass `{"passed": true, "evidence": ["specific retained or native evidence"]}`
only after it actually succeeds. Bare booleans and empty evidence are rejected.
Bootstrap then returns one action that creates the heartbeat already active. Claim
and invoke that exact action once, report its actual result, and rerun bootstrap
with a new request UUID. A returned automation identity and `ACTIVE` status prove
configuration, not delivery; they are sufficient to finish bootstrap because
scheduled delivery is monitored asynchronously.

After bootstrap returns `state: ready` and `admission: running`, enroll the
canonical `~/fulcrum` checkout as project `fulcrum` when possible. Read and
follow `~/fulcrum/docs/project-enrollment.md`. Invocation of this skill authorizes
only that canonical enrollment. Reuse the exact saved Codex project ID already
observed for path `~/fulcrum`; the fixed integration branch is `master`, prepare
argv is `["scripts/prepare-check"]`, validate argv is `["scripts/check"]`, and
the project is enabled.

If `fulcrum project list --json` already shows the exact enrollment, do nothing.
If it is absent, send one exact enrollment request to the retained Vizier because
the bootstrap continuation is not configuration authority, make one bounded
`wait_threads` call of at most 120 seconds for that turn, then verify with
`fulcrum project show fulcrum --json`. Do not retry an uncertain native message or
enrollment. A missing or ambiguous saved project, conflicting existing enrollment,
or unverified Vizier result is an enrollment prerequisite to report; it does not
reopen an otherwise ready bootstrap.

At the end, if native `list_projects` contains other saved local projects not in
`fulcrum project list`, ask once whether the user wants any of them enrolled.
List only their labels and paths. Do not enroll them until the user selects them,
and then follow the same enrollment procedure without inventing project-specific
branches or commands.

Do not wait for a heartbeat, keep Marshal busy across a guessed schedule boundary,
or manufacture an overlap test. Native task turns are serialized, so a controlled
Marshal turn can defer the heartbeat it is intended to observe. The first genuine
heartbeat calls `marshal_check` with `input.trigger` set to `heartbeat`, settles the
returned bounded brief, and completes it with `marshal_decide` using the returned
turn and decision identifiers; Fulcrum records the exact observed native turn for
that delivery. The Marshal must not inspect implementation source or invent an
identifier to discover this call shape.
`fulcrum doctor` reports the loop as `initializing` until
the first delivery, `running` while a delivered cycle is still within its bounded
completion window, `healthy` only after a recent completed cycle, and `degraded`
after either deadline. A single missed documented deadline is sufficient evidence of
a scheduler incident: stop immediately rather than waiting through another
interval or testing alternate creation-, activation-, or completion-time anchors.
Report that incident without reopening bootstrap or retrying an uncertain
automation mutation.

Mention stop semantics only when the user asks about stopping or pausing:
Desktop Stop does not durably pause Fulcrum. The initial MCP configuration change
requires the one fresh continuation task described above; ordinary source edits
never require a new task, restart, activation, installation, or remote publication.
