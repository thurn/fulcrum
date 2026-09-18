# 03 — Configuration and projects

Status: implemented and validated.

Dependencies: [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md)

Normative reading: [design §3](../design.md#3-ledger-project-context-and-task-context), [configuration contract](../contracts.md#authoritative-system-configuration). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and code boundary

Implement `config`, `policy`, and `project` commands with one authoritative
`<brain.root>/fulcrum.yaml`. Replace the JSON configuration paths in
`src/fulcrum/config.py` and old policy-seeding behavior in `setup.py`/`install.py`.
Retain useful executable/path and service-environment validation.

## Public behavior

`config show/validate/set/sync`, `policy show/set`, and
`project add/list/show/enable/disable/remove` use the schemas in contracts §§2, 9–10.
For example, `config set --input - --actor human --json` with
`{"policy":{"automatic_capacity":4}}` merges the supplied mapping. Arrays replace;
unknown keys, duplicate YAML keys and unsafe tags fail. Defaults are four global
and four per-project slots; no reserve/helper-limit fields remain. Standing
leadership defaults to the configured Sol/Luna choices, while short-lived Weaver,
Executor, and Warden work defaults to Luna/low; explicit test/task overrides do
not edit production defaults.

## Implementation and durable boundaries

1. Use safe, comment-preserving YAML editing (a maintained round-trip YAML library).
   Add its dependency through the normal package/lock workflow and reinstall the
   editable `.venv` if those files change. Validate before writing a temporary
   sibling and atomic replacement. Compare original bytes immediately before
   replacement; retain unexpected concurrent edits and return `CONFIG_CONFLICT`.
   Document that same-user manual filesystem races are not an OS security boundary.
2. Permit writes only for human or the verifiable current Vizier. Marshal and
   Justiciar may read/propose but not edit. Record initiating authority and changed
   fields in Beads; the receipt is not a second configuration source.
3. Verify canonical brain/instance/backend bindings. Never replace an unrelated
   `.beads` workspace. Enroll project-local connection/actor settings and local
   Git excludes without changing Git author identity or setting global BEADS_DIR.
4. Resolve project from explicit input, admitted metadata, one label, then exact
   actor format; contradictory evidence becomes a scope blocker. Provider IDs can
   be discovered/created from exact roots through adapters as §10 specifies.
   Before those adapters land, expose their unavailable capability honestly.
5. Disabling a project stops automatic new work while allowing active finish and
   human repair. Removal rejects open work or unsettled managed worktrees. Model/
   capacity changes affect new starts; endpoint/backend changes require controlled
   restart. Invalid YAML pauses admission, preserving available inspection.
6. `config sync` uses task 14/15's selected-path publication; implement its adapter
   boundary now, with no false remote-success result before publication exists.

## Acceptance and failures

Test human/Vizier success and Marshal/Justiciar rejection, comments/unrelated keys
preserved, concurrent edit conflict, malformed/missing selected config, default
four-slot selection, project actor isolation and disallowed production fallback.
A human offline config repair works when Beads is down, reports actual changed
fields and `degraded` with no receipt. A Vizier cannot use that exception without
verifiable leadership. Rerun setup without mutation and assert identical YAML.
Capability outage marks model validation unverified rather than inventing support.
