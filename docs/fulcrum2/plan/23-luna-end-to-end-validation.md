# 23 — Luna end-to-end validation

Status: not implemented.

Dependencies: [05](05-observability-and-inspection.md), [10](10-task-control-and-reviews.md), [12](12-executor-warden-delivery.md), [13](13-plans-and-root-completion.md), [14](14-memory-and-document-publication.md), [15](15-brain-publication.md), [16](16-usage-and-completion-cost.md), [17](17-recovery-and-human-resolution.md), [18](18-task-continuity-and-fleet.md), [19](19-installation-and-service.md), [20](20-source-refresh-and-recovery-launcher.md), [21](21-hard-reset-and-cutover.md), [22](22-deterministic-cli-validation.md)

Normative reading: [replacement validation](../contracts.md#7-cli-driven-validation), [fixture controls](../contracts.md#disposable-fixtures-and-test-controls). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and boundary

Write `scripts/validate-fulcrum2-live` to demonstrate a fully assembled system with
real `gpt-5.6-luna`/low tasks, real Beads and real Tollgate/Git delivery. It is an
external CLI client/report generator, not a new workflow class or controller state
machine. Required replacement evidence covers all eight roles. Ordinary task
promotions do not run this suite.

## Setup and execution limits

Use `fixture create` with explicit disposable root/brain/remotes and the configured
shared native runtime. Enroll only toy repositories through Fulcrum project commands;
record the native/provider ownership inventory. Keep production configuration and
unrelated native tasks unchanged. Call `service start` after fixture configuration and before the role workflows.
Set fixture role defaults and review overrides to
Luna/low, source synchronization required, and capacity four. Probe advertised
capabilities before models start; unavailable Luna/Tollgate is a reported unmet
requirement, never silent deterministic fallback.

The functional script has a 3,000-second wall-clock budget including setup and a
bounded cleanup attempt. Reserve its final 120 seconds for evidence/interruption/
cleanup; stop requesting new fixture work then and invoke normal scoped stopping
for any remaining active work. Record native turn-start counts and do not silently
rerun whole cases. No production turn-budget mechanism or currency cap is added.
Report observed API-equivalent cost with coverage, never guessed subscription spend.

## Synthetic workflows and actual assertions

1. Invoke Vizier with a retained explicit human request to change one harmless fixture
   policy value and curate a memory. Verify YAML/Beads effects through CLI and prove
   a subsequent decision uses them. Restore desired fixture settings explicitly.
2. Invoke Weaver with a small ordering/aggregation library request and two concrete
   deliverables. Request substantial-plan reviews to exercise cold-reader and
   requirements tasks; collect full native results. The script records human approval
   of this fixed synthetic scope through `plan approve` after checking draft bounds.
   It must not approve arbitrary unexpected scope or fabricate review findings.
3. Let Marshal make and finish a real dispatch decision. Observe an Executor change
   actual code in its prepared workspace, run checks, commit and finish; observe
   Warden independently review/fix as needed and promote. Verify fixture integration
   content, provider source mapping, required remote ancestry, cleanup and root state.
4. Use a separate small plan whose checks are covered by existing work to prove no
   validation child is inserted by default. Exercise future deferral then explicit
   activation; no timer/capacity event may start it prematurely.
5. Invoke Sage on retained workflow evidence and Mason on a toy duplicated component.
   Assert concrete findings/report beads and stable investigated identity; do not
   rely on prose saying the roles ran. Findings do not mutate production or require
   interviewing other workers. Explicitly bound/hold follow-up work in the fixture.
6. Present a separate synthetic blocked delivery with an intentionally infeasible
   optional criterion and an explicit scoped repair request. Observe Justiciar inspect,
   take over, record its chosen tradeoff and complete/retain residual work truthfully.
   Do not inject fake native completion or waive a real production criterion.
7. Restart the fixture controller through service CLI while work is retained, then
   inspect ownership/context and continued progress. Read task outputs, pending input,
   trace, delivery, costs and doctor entirely through Fulcrum commands. A small
   deterministic test covers fault timings that cannot reliably be produced live.

## Report and failure handling

Each asserted role has native task/turn IDs, actual input/output, operation and bead
links, and observed effects. Check default-capacity behavior, no stale-owner writes,
no automatic Vizier messages, root closure and completion-cost coverage. Missing
telemetry can be explicit partial coverage; missing workflow evidence cannot pass.
Normal bounded Fulcrum recovery is allowed. Exhausted limits/unresolved assertions
produce a failed report with retained evidence and exact CLI next actions, not a
new product acceptance state. Interrupt only fixture tasks on timeout and preserve
failed-run evidence before cleanup. An explicit rerun is a separate test invocation.
