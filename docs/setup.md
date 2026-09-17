# Setup

Use the `$fulcrum-bootstrap` skill in Codex Desktop. It discovers the retained
checkout, selected instance, saved projects, native task tools, and supported
models, then resumes one deterministic bootstrap receipt until every prerequisite
or native result is settled.

## Prerequisites

- macOS, Python 3.12, Codex Desktop/CLI, Git, `bd`, `dolt`, and `tg`
- the authoritative local checkout at `~/fulcrum`
- an authenticated Codex profile and exact saved Codex project IDs
- a Beads brain and authoritative YAML configuration

Configuration top-level maps are `brain`, `beads`, `delivery`, `projects`,
`models`, `policy`, `knowledge`, `source`, `timing`, and `diagnostics`. It has no
runtime/App Server endpoint. Project roots and executable paths are absolute.
Project enrollment requires an observed `codex_project_id`; Fulcrum never invents
project creation after an uncertain native result.

The four stock timing defaults are:

- Steward instruction wait: 3,600 seconds
- Warden CI wait: 1,800 seconds
- MCP tool timeout: 3,900 seconds
- Marshal heartbeat: 900 seconds

## Deterministic bootstrap

`fulcrum bootstrap --input - --request-id UUID --json` preserves unrelated Codex
configuration and installs an owned `[mcp_servers.fulcrum]` block pointing at the
source-following `fulcrum-mcp` entry point. It links owned skills directly to
`~/fulcrum/skills`, installs the six scoped command hooks, and returns exact native
actions for the standing tasks and heartbeat.

Claim each action before invoking it and report its actual result. Never edit a
returned prompt, target, model, effort, or schedule; never retry an uncertain
effect. New standing tasks register with the action marker before work. Bootstrap
reruns inspect retained IDs and postconditions instead of recreating tasks.

The fixed identities are `🧰 STEWARD 🧰` on `gpt-5.6-luna` and `🧭 MARSHAL 🧭`
plus `🔮 VIZIER 🔮` on `gpt-5.6-sol`. One heartbeat targets the registered Marshal
every 15 minutes and stays quiet on healthy no-op runs. There is no Steward
heartbeat or hourly recovery task.

Admission opens only after the broker answers, required native tools and model
efforts are observed, all three tasks register, the schedule result is retained,
and focused acceptance is explicitly recorded. Acceptance is an evidence map with
`workspace_access`, `hook_identity`, `transcript_lifecycle`, `usage_accounting`,
`task_targeting`, and `schedule_overlap`; each value must be true. Bootstrap creates
the heartbeat paused and returns a separate activation action only after that
evidence is supplied. A ready socket alone is not setup.
The initial MCP configuration may require one exceptional Desktop reconnect;
ordinary committed edits never do.

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
