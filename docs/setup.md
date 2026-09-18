# Setup

## Fresh installation

1. Install macOS prerequisites: Python 3.12, Git, `bd`, `dolt`, `tg`, and an
   authenticated Codex Desktop/CLI profile.
2. Clone Fulcrum at the canonical path `~/fulcrum` with `master` checked out.
3. Open `~/fulcrum` as a project in Codex Desktop.
4. Invoke `$fulcrum-bootstrap` in that project.

The repository exposes bootstrap and uninstall skills through `.agents/skills`,
so both are available immediately from a fresh clone. The bootstrap skill
discovers the selected instance, Codex root, native task tools, supported models,
and saved projects. It runs `~/fulcrum/scripts/setup` for first-use dependency
provisioning and then resumes deterministic bootstrap receipts until every
prerequisite or native result is settled.

Do not pre-create `~/brain`, edit Codex configuration, link skills, or run a
separate package installer. Setup creates `.venv`, installs an editable launcher at
`~/.local/bin/fulcrum`, and atomically writes the initial authoritative
`~/brain/fulcrum.yaml`. It refuses to replace a foreign launcher or an existing
invalid configuration. A clone outside `~/fulcrum` is rejected because local
`master` at that path is the runtime authority.

The setup script is an agent entry point, not a shell-only installer: bootstrap
returns native Codex task and automation actions that must be claimed, invoked
once, and reported. Running it manually can prepare local resources and display
the remaining actions, but it cannot create or verify Codex tasks by itself.

## Prerequisites

- macOS, Python 3.12, Codex Desktop/CLI, Git, `bd`, `dolt`, and `tg`
- the authoritative local checkout at `~/fulcrum`
- an authenticated Codex profile
- exact saved Codex project IDs only for projects enrolled during setup

Configuration top-level maps are `brain`, `beads`, `delivery`, `projects`,
`models`, `policy`, `knowledge`, `source`, `timing`, and `diagnostics`. It has no
runtime/App Server endpoint. Project roots and executable paths are absolute.
Project enrollment requires an observed `codex_project_id`; Fulcrum never invents
project creation after an uncertain native result.
Agents should use the bounded [project enrollment procedure](project-enrollment.md)
instead of inspecting implementation source or manually configuring Beads and
Tollgate.

The four stock timing defaults are:

- Steward instruction wait: 3,600 seconds
- Warden CI wait: 1,800 seconds
- MCP tool timeout: 3,900 seconds
- Marshal heartbeat: 900 seconds

## Deterministic bootstrap

On the first invocation, attach JSON to stdin before running
`scripts/setup --input - --request-id UUID --json`; `--input` accepts a file path
or `-`, not inline JSON. Generate new request IDs with `uuidgen`. The command
provisions the environment and enters bootstrap. Later invocations use
`fulcrum bootstrap --input - --request-id UUID --json` directly. Input contains
the observed `codex_root`, `native_tools`, and `model_support`; it may include a
`configuration` mapping only for initial configuration. Stock defaults create an
empty project map, so projects can be enrolled after installation without making
an invented saved-project choice.

Bootstrap preserves unrelated Codex configuration and installs an owned
`[mcp_servers.fulcrum]` block pointing at the source-following `fulcrum-mcp` entry
point. It links owned skills directly to `~/fulcrum/skills`, installs the six
scoped command hooks, and returns exact native actions for the standing tasks and
heartbeat.

Claim each action before invoking it and report its actual result using the
attempt ID returned by the claim. New MCP calls may omit request, attempt, loop,
and turn IDs where the schema says Fulcrum generates them. Never edit a returned
prompt, target, model, effort, schedule, or native result; never retry an
uncertain effect. New standing tasks register with the action marker before work.
One bounded native wait lets their initial turns settle; bootstrap reruns inspect
retained IDs and postconditions instead of recreating tasks.

The fixed identities are `🧰 STEWARD 🧰` on `gpt-5.6-luna` and `🧭 MARSHAL 🧭`
plus `🔮 VIZIER 🔮` on `gpt-5.6-sol`. One heartbeat targets the registered Marshal
every 15 minutes and stays quiet on healthy no-op runs. There is no Steward
heartbeat or hourly recovery task. Short-lived Weaver, Executor, and Warden work
defaults to Luna with low reasoning effort; project or work-specific configuration
may override that choice.

Admission opens only after the broker answers, required native tools and model
efforts are observed, all three tasks register, the schedule result is retained,
and focused acceptance is explicitly recorded. Acceptance contains exactly five
checks: `workspace_access`, `hook_identity`, `transcript_lifecycle`,
`usage_accounting`, and `task_targeting`. Each check is an object with `passed` and
a nonempty `evidence` array; bare booleans are rejected. Once those five checks
pass, bootstrap creates the Marshal heartbeat active while admission stays paused.
Admission opens after the exact active schedule result is retained. Subsequent
delivery health is monitored asynchronously. A ready socket or caller assertion
alone is not setup.
An existing task does not acquire MCP tools added after it started. When the
initial MCP configuration changes, the bootstrap skill creates exactly one new
task in the saved `~/fulcrum` project, using the retained checkout directly rather
than a worktree. That continuation invokes `$fulcrum-bootstrap`, explicitly names
`~/fulcrum/skills/fulcrum-bootstrap/SKILL.md`, uses a new request ID, receives the
current MCP tool catalog, and rebinds retained pending actions to itself. The
current Desktop UI lists servers under Settings > Plugins > MCPs and does not
expose the former Restart control; do not toggle the server or quit the app.
Retained action IDs and registered task identities make the continuation
resumable rather than duplicative. Ordinary committed edits never require a new
task or restart.

## Services and source

Owned launch services are only Dolt and the thin broker. Codex Desktop owns its
runtime. `service stop` pauses admission and refuses to hand off a broker with
pending responses unless interruption was explicitly requested; it never stops
Desktop or Dolt. Skills read local master directly, and fresh CLI evaluations make
operation-boundary hot reload automatic.

After changing `pyproject.toml` or `requirements-dev.lock`, refresh requirements
and the editable package in `.venv`. This dependency refresh is exceptional
packaging maintenance, not the ordinary editing workflow.

If broker transport code changes, use `fulcrum service update --maintenance` after
pending responses settle. That explicit handoff is not part of ordinary policy or
skill iteration.

## Complete uninstall

Invoke `$fulcrum-uninstall` for a complete removal. It uses Codex native tools to
delete the Marshal heartbeat and archive only the three Fulcrum role tasks, then
runs `~/fulcrum/scripts/uninstall`. The script previews by default and requires
`--yes` before changing anything. A full uninstall also passes `--remove-source`;
omit that flag only when retaining the clone for a clean reinstall.

The script stops the owned broker and Dolt launch jobs and any Fulcrum MCP
processes. It removes the instance, brain, incidents, logs, caches, temporary
state, virtual environment, source-following launchers, owned skill links, and
legacy Fulcrum paths. It removes only Fulcrum's marked MCP block, source-project
trust entry, and hook handlers from Codex configuration, preserving unrelated
settings and hooks. Foreign real files at owned link names are reported and left
untouched. Existing tasks may retain the tool catalog they started with; fresh
tasks read the updated MCP configuration. Uninstall does not restart, quit, or
toggle Codex Desktop.
