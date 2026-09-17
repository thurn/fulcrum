---
name: fulcrum-bootstrap
description: Install, bootstrap, or resume the Fulcrum control plane in Codex Desktop from a fresh ~/fulcrum clone.
---

Use `~/fulcrum` on local `master` as the retained checkout. Discover the selected
instance, Codex root, available native task tools, supported model/effort matrix,
and saved Codex projects. On first use, run `~/fulcrum/scripts/setup` with a stable
request UUID and JSON input containing `codex_root`, `native_tools`, and
`model_support`. The script provisions Python 3.12 dependencies, installs the
source-following command launcher, and atomically creates the authoritative YAML
configuration when it is absent. Do not ask the user to hand-create configuration
or skill links. If required executables, credentials, or an explicitly requested
saved project are unavailable, report that exact prerequisite and resume after it
is fixed. Never install or use an App Server fallback.

After the first invocation, use `fulcrum bootstrap` directly. Reuse a request UUID
only to resume the exact same command after a client timeout. Use a new request UUID
after returned actions or prerequisites settle so bootstrap can observe the new
postconditions. Preserve any existing authoritative configuration. If the initial
MCP block changed, stop before native actions and tell the user to open Desktop
Settings, select MCP servers, and select Restart, as required by the
[official Codex MCP setup](https://developers.openai.com/codex/mcp). Then have them
invoke this skill again; the fresh bootstrap call rebinds its retained pending
actions to the new task. Do not require a full macOS app quit when the in-app
Restart control is available, and do not treat this exceptional initial restart
as an ordinary source-update step.

For every returned action, call `action claim` before invoking the exact named
native tool once, then call `action result` with the actual result. Do not edit its
arguments, invent a retry ID, or retry an uncertain native effect. A newly created
standing task must call `register_standing` with its retained action marker before
other activity. Preserve the fixed titles `🧰 STEWARD 🧰`, `🧭 MARSHAL 🧭`, and
`🔮 VIZIER 🔮`; correct a normalized title only through a separately retained
native action.

Setup is complete only when all three identities, the source-following MCP server,
six trusted hooks, broker socket, supported models/tools, transcript and accounting
checks, exact Marshal heartbeat, and focused acceptance are recorded.
Pass an `acceptance` object with true values for `workspace_access`,
`hook_identity`, `transcript_lifecycle`, `usage_accounting`, `task_targeting`, and
`schedule_overlap` only after each check actually succeeds. Bootstrap first creates
the heartbeat paused, then returns a distinct activation action after acceptance;
admission opens only after that activation result succeeds. Keep
admission paused and report exact gaps otherwise. Explain that Desktop Stop does
not durably pause Fulcrum and that an initial MCP configuration change requires
the one in-app MCP Restart described above; ordinary source edits never require
restart, activation, installation, or remote publication.
