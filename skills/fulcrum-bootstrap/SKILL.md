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

An existing task does not acquire MCP tools added after that task started. If the
initial response reports `mcp.changed: true`, or the MCP block is already present
but the current non-continuation task lacks Fulcrum MCP tools, stop before its
returned native setup actions and create exactly one continuation task so it
receives the current tool catalog. Invocation of this skill authorizes that single
bounded task. A task whose prompt marks it as the sole MCP-refresh continuation
must never create another; it stops and reports the missing tools instead.

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

**You must approve fulcrum hooks in Codex Settings**

- Open **Settings**.
- Select **Hooks**.
- Click **Trust** for every Fulcrum hook.
- Click **Enable** for every Fulcrum hook.

Do not report hook setup as complete until the user has approved and enabled all
six Fulcrum hooks. Resume bootstrap with a new request UUID after approval so it
can observe the new postconditions.

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
six trusted hooks, broker socket, supported models/tools, transcript and
accounting checks, exact Marshal heartbeat, and focused acceptance are recorded.
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
  and schedule targets;
- `schedule_overlap`: after activation, one real heartbeat overlaps a controlled
  Marshal prompt without duplicate effects, lost targeting, or silent failure.

First pass true values for the five checks other than `schedule_overlap` after
they actually succeed. Bootstrap then returns the distinct action that activates
the already-paused heartbeat while admission remains paused. After the real
overlap check succeeds, rerun bootstrap with all six true values; only then may
admission open. Never activate the schedule directly, infer acceptance from unit
tests, or mark an unexercised check true. Keep admission paused and report exact
gaps otherwise.

Explain that Desktop Stop does not durably pause Fulcrum. The initial MCP
configuration change requires the one fresh continuation task described above;
ordinary source edits never require a new task, restart, activation, installation,
or remote publication.
