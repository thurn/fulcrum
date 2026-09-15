# 25 — Replacement completion

Status: implementation and final audit in progress.

Dependencies: [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [04](04-work-and-ownership.md), [05](05-observability-and-inspection.md), [06](06-codex-runtime-adapter.md), [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md), [13](13-plans-and-root-completion.md), [14](14-memory-and-document-publication.md), [15](15-brain-publication.md), [16](16-usage-and-completion-cost.md), [17](17-recovery-and-human-resolution.md), [18](18-task-continuity-and-fleet.md), [19](19-installation-and-service.md), [20](20-source-refresh-and-recovery-launcher.md), [21](21-hard-reset-and-cutover.md), [22](22-deterministic-cli-validation.md), [23](23-luna-end-to-end-validation.md), [24](24-thirty-task-concurrency.md)

Normative reading: [implementation index](README.md), [requirement audit](../audit.md). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and code boundary

Complete the replacement as one coherent installed product after every capability
and its evidence exist. Remove obsolete responsibilities as their replacements
land; this task checks for leftovers rather than postponing all cleanup to the end.
Inspect old `store.py`, `controller.py`, `operative.py`, `kernel.py`, role assets,
CLI aliases, setup hooks and documentation. Preserve useful code only if it serves
the replacement contracts without the old state model.

## Required implementation work

1. Verify every command in the index's coverage map is installed, discoverable through
   help, callable through normal/offline paths where specified, and returns its exact
   JSON/error contract. Include review/draft/approval/dependency/output/fixture tools
   rather than declaring CLI completeness from a happy delivery alone.
2. Remove Fulcrum SQLite/journals, runs/lineages/occurrences, old role/retry engines,
   recurring model jobs, fixed bootstrap prompts and random claim tokens. Remove
   stale models/default limits and obsolete skill names/implicit invocation paths.
   Old-store reading is permitted only for explicit cutover deletion inventory,
   never migration or runtime fallback. Do not add compatibility aliases/versions.
3. Keep package metadata required by packaging tools without introducing a new
   release-version scheme. Ensure formulas/static fallbacks and independent recovery
   packaging ship correctly. Dependency edits require updating the lock and reinstalling
   requirements/editable package in the repository `.venv` per AGENTS.md.
4. Update README, setup/operations/hook docs and microskill instructions with current
   commands and role names. Every documented action has a tested CLI path. Document
   configuration authority, direct prompts, acquisition receipts, four default slots,
   explicit 30-task support, proportionate plan validation and remote reset boundaries.
5. Run appropriate project checks (format/type/behavior) after final source changes,
   the deterministic installed-CLI suite, then explicit real Luna functional and
   concurrency validation. Record command invocations, installed source commit,
   fixture/runtime/provider facts, result/evidence paths and known limitations.
6. Audit actual delivered behavior against the requirement map. Missing required
   behavior/evidence leaves replacement readiness incomplete. Partial telemetry is
   allowed only with the promised explicit coverage, not fabricated zero cost.
   A docs statement or a task marked complete cannot substitute for native evidence.
7. Prepare the cutover runbook using `recover inspect`, `reset --hard` and setup/
   service commands. Production destructive execution requires the user's explicit
   cutover instruction; the implementation plan alone does not trigger it. Keep
   pre-cutover code/installation repair possible without importing historical work.

## Final acceptance

A clean installed replacement can be bootstrapped and completely operated from its
CLI; ordinary native Beads intake reaches observed delivery; ownership and external
effects survive the required failure checks; all eight roles have real Luna evidence;
30 simultaneous native tasks meet the bounded smoke; and no old workflow store is
required. Reports distinguish tests actually passed, unsupported capabilities,
partial observations and outstanding issues. No routine per-task full live suite,
mandatory small-plan validation bead, native-subagent system or model-powered
housekeeping is introduced. Commit/publish through the current authorized repository
workflow, with Conventional Commits and required checks.
