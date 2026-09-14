# 19 — Installation and service

Status: not implemented.

Dependencies: [03](03-configuration-and-projects.md), [06](06-codex-runtime-adapter.md), [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [17](17-recovery-and-human-resolution.md)

Normative reading: [installation](../design.md#8-installation-reset-and-implementation-sequence), [continuity commands](../contracts.md#installation-recovery-and-continuity-commands). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and existing code

Provide a rerunnable installed system and complete service CLI. Rework `setup.py`,
`install.py`, `config.py` and startup wiring around Beads-only state. Retain absolute
executable paths, deterministic service environments and conservative skill-link
repair. Do not retain SQLite readiness or initial recurring policy jobs.

## Public commands and setup input

Implement `setup [--input FILE] [--non-interactive]`, `skills reconcile`,
`service start/stop/restart/status`, and `runtime launch-desktop`. Setup uses the
configuration schema in contracts §2; interactive mode asks only for missing values.
Return installed assets/services, selected config, leadership IDs and per-capability
results. Errors name missing fields or unavailable operations. Reruns preserve
configuration unless human/Vizier explicitly requested edits.

## Implementation sequence

1. Resolve bootstrap paths, acquire the brain-root writer lock and create only
   declared owned directories/configuration. Follow §10's pre-ledger bootstrap
   exception; once Beads works, receipt every subsequent external operation.
   Initialize one externally served Dolt database `fulcrum` under the brain.
2. Install the controller in a separate non-editable environment and package formulas,
   static fallbacks, skill metadata and entry points. Preserve user directories and
   unrelated skills; only repair owned links. Development `.venv` is separate.
3. Install uniquely identified owned service definitions with absolute executables,
   explicit PATH/environment, loopback endpoints and the configured shared runtime.
   Set the shared runtime soft FD limit to 4096 when installing its owned service;
   validate actual configuration instead of assuming workers-to-FD ratios.
4. Detect occupied ports/service ownership conflicts and report them; never kill
   unrelated processes. Repair changed definitions, reuse unchanged running services.
   Test instances use isolated Beads/service identities and do not rewrite production
   runtime definitions, skill links or configuration.
5. Validate Beads, native protocol, models/efforts, project/provider IDs and installed
   assets. Create leadership identities by recorded native intents without Vizier
   turns. Some optional unavailable facilities can produce warnings, but required
   capability absence must be visible and disable the affected operation.
6. `service stop` stops new admission and drains by default; `--interrupt` explicitly
   interrupts managed turns and observes stop. Client timeout is not escalation.
   Restart retains Beads identities and reconciles before admission. Source-refresh
   installation is completed separately in task 20.
7. Launch Desktop using the configured shared endpoint. Report actual attachment
   evidence when available or unknown; a successful protocol handshake does not
   prove attachment. Never terminate a separate runtime to force the app onto this one.

## Acceptance

Install into disposable roots, rerun and prove no duplicate leaders/services or YAML
rewrite. Test missing prerequisites, wrong provider ID/model, occupied port, broken
owned link and user-directory collision. `service status` must work independently
of the controller socket. Prove active tasks survive a controller restart and
unrelated native tasks/services stay untouched. Packaging must include all CLI/
formula/fallback assets; neither setup nor reset requires a model turn to establish
mechanical defaults.
