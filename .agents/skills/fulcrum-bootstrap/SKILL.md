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

On first use, run `~/fulcrum/scripts/setup` with a stable request UUID and JSON
input containing `codex_root`, `native_tools`, and `model_support`. Use setup when
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
the actual result. Do not edit its arguments, invent a retry ID, or retry an
uncertain native effect. A newly created standing task must call
`register_standing` with its retained action marker before other activity.
Preserve the fixed titles `🧰 STEWARD 🧰`, `🧭 MARSHAL 🧭`, and
`🔮 VIZIER 🔮`; correct a normalized title only through a separately retained
native action.

Setup is complete only when all three identities, the source-following MCP server,
six trusted hooks, broker socket, supported models/tools, transcript and
accounting checks, exact Marshal heartbeat, and focused acceptance are recorded.
Pass an `acceptance` object with true values for `workspace_access`,
`hook_identity`, `transcript_lifecycle`, `usage_accounting`, `task_targeting`, and
`schedule_overlap` only after each check actually succeeds. Bootstrap first
creates the heartbeat paused, then returns a distinct activation action after
acceptance; admission opens only after that activation result succeeds. Keep
admission paused and report exact gaps otherwise.

Explain that Desktop Stop does not durably pause Fulcrum. The initial MCP
configuration change requires the one fresh continuation task described above;
ordinary source edits never require a new task, restart, activation, installation,
or remote publication.
