# Fulcrum2 implementation plan

Status: documentation complete; implementation and replacement validation pending.

This is a ground-up replacement plan, not evidence that the current executable
implements the specified commands. Read [design.md](../design.md) for architecture,
[contracts.md](../contracts.md) for normative interfaces and records,
[audit.md](../audit.md) for scope/review findings, and
[failure-analysis.md](../failure-analysis.md) for historical failure evidence.
There is no compatibility layer, data migration, new version scheme, or parallel
workflow store. Task numbers order implementation work; they are not versions.

## Settled design decisions

- Beads formulas supply full role/task prompts, passed directly at turn start and
  retained with the sent input. Context/compaction reads remain available.
- The acquisition/transfer operation ID identifies ownership. Check it and native
  task ID; ordinary progress does not change it. No random claim tokens or hashes.
- Default global and per-project capacity is four active managed tasks, with no
  reserved recovery slot. Idle leaders do not count; thirty is a tested capacity goal.
- Independent reviews are ordinary Codex tasks linked in Beads. Fulcrum has no
  native-subagent orchestration, discovery, or special accounting requirements.
- Marshal receives decision-focused grooming/dispatch/recovery batches. Authors
  provide useful intake; substantial clarification returns to Weaver. Current
  decisions/rationale are rebuilt from Beads after compaction, not message history.
- Only human/Vizier authorizes future-plan activation. Root completion is mechanical
  over approved obligations; cancelled children do not imply success. A separate
  validation child is a judgment call, never a default for small plans.
- Replacement evidence requires deterministic installed-CLI checks and live Luna/low
  work across all eight roles with real delivery (50 minutes), plus thirty-task
  concurrency (10 minutes). Ordinary promotions use proportionate checks.
- Validation scripts own assertions/reports. Test utilities expose fixture/provider
  controls but create no acceptance work kind, workflow engine, or retry subsystem.
- Hard reset replaces dedicated remote ledger history, preserving ordinary brain
  history/documents/configuration and unrelated resources. It does not promise
  physical erasure from hosting-provider retention.

## Worker contract

Each numbered file is one implementation task. Read its dependencies and referenced
contracts before editing. Each supplies outcome/code boundaries, public inputs and
results, ordered steps, authoritative facts, failure behavior, and observable checks.
The public CLI calls the same application operations as the controller. Ship useful
CLI increments; never return invented success for an unfinished dependency.

General mutation invariants apply to every task: record intent before external
workflow effects; retain exact IDs/inputs; inspect uncertain results before replay;
compare request values directly; check current ownership; and reconcile partial
writes. Bootstrap, unavailable-ledger essential repair and temporary-Beads reset
are the only expressly documented authority exceptions. No new disk replay journal.
Native Git source identifiers are legitimate evidence, not newly calculated hashes.

Use existing transport, provider parsing, decimal arithmetic and service mechanics
where they fit; remove obsolete responsibility when replaced. The current runtime
already uses `websockets`; do not invent a rewrite of nonexistent framing code.
Tests must prove behavior, not internal module layout or prompt spelling. Add CLI
behavior checks with each task, consolidating fixtures/cases in task 22. Tests that
need a later adapter must expose the dependency honestly until it is integrated.

Follow the repository's active workflow and Conventional Commits. For one-off work,
AGENTS.md requires committing and pushing changes. Dependency/lock edits require
reinstalling requirements and the editable package into `.venv`. This documentation
plan does not authorize production reset or run native tasks during authoring.

## Task inventory and dependencies

Dependency lists are completion dependencies. Task 22's fixture foundation starts
alongside task 02 and grows with the implementation; its final completion depends
on tasks 01–21. Early tasks own the narrow test support they need, so the final
validation task is not a circular prerequisite. Likewise tasks 06/10 define runtime
and review interfaces before task 13's complete plan lifecycle; task 13 implements
publication through a boundary fulfilled by task 14, exposing unavailable remote
publication until then. No false positive completes those integration obligations.

| Task | Responsibility | Dependencies |
| --- | --- | --- |
| [01 — CLI and application spine](01-cli-application-spine.md) | Installed parser, application API, IPC/offline parity and writer lock. | — |
| [02 — Beads ledger and operations](02-beads-ledger-and-operations.md) | Stock Beads records, receipts and uncertain effects. | [01](01-cli-application-spine.md) |
| [03 — Configuration and projects](03-configuration-and-projects.md) | Authoritative YAML, actor checks, enrollment and isolation. | [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md) |
| [04 — Work and ownership](04-work-and-ownership.md) | Work graph, native intake, ownership operations and dispositions. | [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md) |
| [05 — Observability and inspection](05-observability-and-inspection.md) | Useful JSON status, doctor, trace, logs and waits from the start. | [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [04](04-work-and-ownership.md) |
| [06 — Codex runtime adapter](06-codex-runtime-adapter.md) | Native task lifecycle, prompts, requests and response-loss recovery. | [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md) |
| [07 — Role context and entry](07-role-context-and-entry.md) | Eight formulas, role entry, model selection, skills and compaction. | [04](04-work-and-ownership.md), [06](06-codex-runtime-adapter.md) |
| [08 — Controller supervision](08-controller-supervision.md) | Continuous bounded supervision, retries, progress and loop health. | [04](04-work-and-ownership.md), [05](05-observability-and-inspection.md), [06](06-codex-runtime-adapter.md) |
| [09 — Leadership and admission](09-leadership-and-admission.md) | Decision-focused briefs, intake clarification, current context, deferrals and four-slot admission. | [07](07-role-context-and-entry.md), [08](08-controller-supervision.md) |
| [10 — Task control and independent reviews](10-task-control-and-reviews.md) | Terminal task control/output and independent plan reviews. | [06](06-codex-runtime-adapter.md), [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md) |
| [11 — Workspaces and delivery adapter](11-workspaces-and-delivery-adapter.md) | Worktrees and normalized Tollgate/Git facts. | [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [04](04-work-and-ownership.md) |
| [12 — Executor to Warden delivery](12-executor-warden-delivery.md) | One-way handoff, Warden fixes and observed delivery. | [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md) |
| [13 — Plans and root completion](13-plans-and-root-completion.md) | Drafts, approval, refinement, activation and root closure. | [04](04-work-and-ownership.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md) |
| [14 — Memory and document publication](14-memory-and-document-publication.md) | Curated memory and selected-document Git publication. | [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [13](13-plans-and-root-completion.md) |
| [15 — Brain publication](15-brain-publication.md) | Native database history and dirty-only five-minute publication. | [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [08](08-controller-supervision.md), [14](14-memory-and-document-publication.md) |
| [16 — Usage and completion cost](16-usage-and-completion-cost.md) | Managed-task usage, pricing coverage and root completion cost. | [06](06-codex-runtime-adapter.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [13](13-plans-and-root-completion.md) |
| [17 — Recovery and HUMAN resolution](17-recovery-and-human-resolution.md) | Scoped repair, existing-task recovery and HUMAN resolution. | [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md) |
| [18 — Task continuity and fleet](18-task-continuity-and-fleet.md) | Same-work reuse, archive once and fleet replacement. | [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [13](13-plans-and-root-completion.md), [17](17-recovery-and-human-resolution.md) |
| [19 — Installation and service](19-installation-and-service.md) | Rerunnable installation, services and Desktop attachment. | [03](03-configuration-and-projects.md), [06](06-codex-runtime-adapter.md), [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [17](17-recovery-and-human-resolution.md) |
| [20 — Source refresh and recovery launcher](20-source-refresh-and-recovery-launcher.md) | Quiescent installed swap and independent repair runtime. | [17](17-recovery-and-human-resolution.md), [19](19-installation-and-service.md) |
| [21 — Hard reset and cutover](21-hard-reset-and-cutover.md) | Enumerated resumable reset and dedicated remote history replacement. | [15](15-brain-publication.md), [17](17-recovery-and-human-resolution.md), [18](18-task-continuity-and-fleet.md), [19](19-installation-and-service.md), [20](20-source-refresh-and-recovery-launcher.md) |
| [22 — Deterministic CLI validation](22-deterministic-cli-validation.md) | Installed CLI regression scripts and isolated deterministic providers. | [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [04](04-work-and-ownership.md), [05](05-observability-and-inspection.md), [06](06-codex-runtime-adapter.md), [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md), [13](13-plans-and-root-completion.md), [14](14-memory-and-document-publication.md), [15](15-brain-publication.md), [16](16-usage-and-completion-cost.md), [17](17-recovery-and-human-resolution.md), [18](18-task-continuity-and-fleet.md), [19](19-installation-and-service.md), [20](20-source-refresh-and-recovery-launcher.md), [21](21-hard-reset-and-cutover.md) |
| [23 — Luna end-to-end validation](23-luna-end-to-end-validation.md) | Real eight-role Luna synthetic workflows and actual delivery. | [05](05-observability-and-inspection.md), [10](10-task-control-and-reviews.md), [12](12-executor-warden-delivery.md), [13](13-plans-and-root-completion.md), [14](14-memory-and-document-publication.md), [15](15-brain-publication.md), [16](16-usage-and-completion-cost.md), [17](17-recovery-and-human-resolution.md), [18](18-task-continuity-and-fleet.md), [19](19-installation-and-service.md), [20](20-source-refresh-and-recovery-launcher.md), [21](21-hard-reset-and-cutover.md), [22](22-deterministic-cli-validation.md) |
| [24 — Thirty-task concurrency](24-thirty-task-concurrency.md) | Bounded real 30-task overlap, tool execution and release. | [06](06-codex-runtime-adapter.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [19](19-installation-and-service.md), [22](22-deterministic-cli-validation.md) |
| [25 — Replacement completion](25-replacement-completion.md) | Obsolete-code removal, documentation and actual readiness evidence. | [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [04](04-work-and-ownership.md), [05](05-observability-and-inspection.md), [06](06-codex-runtime-adapter.md), [07](07-role-context-and-entry.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md), [13](13-plans-and-root-completion.md), [14](14-memory-and-document-publication.md), [15](15-brain-publication.md), [16](16-usage-and-completion-cost.md), [17](17-recovery-and-human-resolution.md), [18](18-task-continuity-and-fleet.md), [19](19-installation-and-service.md), [20](20-source-refresh-and-recovery-launcher.md), [21](21-hard-reset-and-cutover.md), [22](22-deterministic-cli-validation.md), [23](23-luna-end-to-end-validation.md), [24](24-thirty-task-concurrency.md) |

### Milestones

1. **Terminal ledger spine — 01–05:** isolated real Beads intake, actor/configuration
   checks, operation receipts and useful offline/served inspection.
2. **Native work — 06–10:** full prompts, all role entry paths, bounded supervision,
   leadership decisions, four-slot admission and CLI-managed independent reviews.
3. **Delivered work — 11–15:** observed Git/Tollgate delivery, plan refinement/root
   closure, memory and remote database/document persistence. Task 11 can start after 04.
4. **Operable installation — 16–21:** costs, recovery, archive/fleet continuity,
   installed services, independent repair, source refresh and safe cutover machinery.
5. **Replacement evidence — 22–25:** completed deterministic coverage, live all-role
   delivery and 30-task concurrency, then final removal/packaging/documentation audit.

These milestones organize dependencies; they do not create new product workflow
stages or request parallel agents. Each task is implemented under its assigned scope.

## Complete CLI and application map

Application names below describe shared service operations, not one new engine per
command. Read commands use projections; resource commands use common receipt-backed
adapter operations. Every write uses the common envelope/identity contract unless
an explicit bootstrap, degraded repair, hook, or test-provider exception applies.
`--json`, IDs and argv next-actions are the machine integration surface.

| Initiating/inspection commands | Application path | Owner | Verification |
| --- | --- | --- | --- |
| All commands: common flags, help, JSON/input/errors, request retry, IPC/offline | parse → application dispatch / writer context | [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md) | Installed subprocess contract checks |
| `setup`; `config show/validate`; `service status` | resource setup / configuration and health projections | [03](03-configuration-and-projects.md), [19](19-installation-and-service.md) | Isolated bootstrap/rerun and missing prerequisites |
| `config set/sync`, `policy set/show` | authorized config update / publication / projection | [03](03-configuration-and-projects.md), [14](14-memory-and-document-publication.md), [15](15-brain-publication.md) | Actor denial, concurrent file edit, selected-path sync |
| `project add/list/show/enable/disable/remove` | project resource operation / config update | [03](03-configuration-and-projects.md), [06](06-codex-runtime-adapter.md), [11](11-workspaces-and-delivery-adapter.md), [19](19-installation-and-service.md) | Exact provider enrollment, disabled admission, owned removal |
| `service start/stop/restart`, `serve [--once]`, `reconcile` | controller lifecycle / reconcile | [01](01-cli-application-spine.md), [08](08-controller-supervision.md), [19](19-installation-and-service.md) | Lock exclusion, loop health and restart |
| `service update` | installed resource operation | [20](20-source-refresh-and-recovery-launcher.md) | Failed probe and interrupted atomic activation |
| `skills reconcile`, `runtime launch-desktop` | owned asset repair / runtime launch | [07](07-role-context-and-entry.md), [19](19-installation-and-service.md) | Link conflicts, no unrelated service interruption |
| `runtime capabilities/status` | runtime resource facts | [06](06-codex-runtime-adapter.md), [19](19-installation-and-service.md) | Protocol/config support, pressure and unknown attachment |
| `enter`, `context`, `hook context` | enter / context projection | [04](04-work-and-ownership.md), [07](07-role-context-and-entry.md) | Eight roles, direct full input, degraded entry, compact hook |
| `work create/show/list/children` | update_work / work projection | [02](02-beads-ledger-and-operations.md), [04](04-work-and-ownership.md) | Native real-Beads intake and interrupted graph |
| `work update`, `work dependencies` | update_work | [04](04-work-and-ownership.md) | Scope changes, cycle rejection and independent waits |
| `work adopt/transfer/reopen` | enter / transfer | [04](04-work-and-ownership.md), [12](12-executor-warden-delivery.md) | Acquisition freshness and stop-before-transfer |
| `work close`, `finish`, `progress`, `report` | finish / update_work | [04](04-work-and-ownership.md), [07](07-role-context-and-entry.md), [12](12-executor-warden-delivery.md), [13](13-plans-and-root-completion.md), [17](17-recovery-and-human-resolution.md) | Sealed finish, disposition evidence and independent report |
| `leader show/replace` | leadership projection / replacement | [09](09-leadership-and-admission.md), [18](18-task-continuity-and-fleet.md) | No unsolicited Vizier turn and preserved owned work |
| `marshal brief/request/decide`, `backlog list` | decision preview / request / decide | [09](09-leadership-and-admission.md) | Decision/evidence relevance, separated purposes, same-bead clarification, current context and mixed stale rows |
| `dispatch [--authorize\|--human]` | decide / enter under admission | [09](09-leadership-and-admission.md) | Four-slot default, queued authorization versus bypass |
| `human list/resolve` | work projection / decide | [17](17-recovery-and-human-resolution.md) | Actual external blocker and selective reason resolution |
| `task list/show/start/send/output/wait` | task resource operations / native projections | [06](06-codex-runtime-adapter.md), [10](10-task-control-and-reviews.md) | Full prompt, lost create/start, non-resuming output |
| `task requests/respond/interrupt` | native request/turn resource operations | [06](06-codex-runtime-adapter.md), [10](10-task-control-and-reviews.md) | Typed pending request, lost response, observed termination |
| `task terminals`, `task terminal stop`, `task release` | native resource inspect/stop/release | [06](06-codex-runtime-adapter.md), [10](10-task-control-and-reviews.md) | Owned target and unsupported targeted-stop behavior |
| `task archive/unarchive/delete` | task lifecycle resource operations | [10](10-task-control-and-reviews.md), [18](18-task-continuity-and-fleet.md) | Archive once, manual suppression, exact owned deletion |
| `worktree prepare/inspect/cleanup` | delivery workspace resources | [11](11-workspaces-and-delivery-adapter.md) | Actual provider path, dirty protection and absence inspection |
| `validation start/show` | delivery resource submit/inspect | [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md) | Immutable source and actual CI outcome |
| `review approve`, `promotion start/show`, `source sync` | finish / delivery resources | [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md) | Warden source freshness, observed promotion and ancestry |
| `operation show/list/wait/cancel/reconcile` | operation projection / reconcile | [02](02-beads-ledger-and-operations.md), [05](05-observability-and-inspection.md), [08](08-controller-supervision.md) | No duplicate effect and truthful cancellation/timeouts |
| `status`, `doctor`, `logs`, `trace`, `logs prune`, `wait --bead` | diagnostic/work projections | [05](05-observability-and-inspection.md), [08](08-controller-supervision.md) | Explain blockers without UI/SQL; evidence survives pruning |
| `recover inspect/takeover/repair/release` | reconcile / transfer / scoped resources | [17](17-recovery-and-human-resolution.md) | Full capacity, stopped writers and retained repair fences |
| `fulcrum-recover inspect/takeover/repair/release` | same essential repair operations | [20](20-source-refresh-and-recovery-launcher.md) | Broken main package/checkout/development environment |
| `plan draft/show/approve/publish/refine` | update_work / decide / publication resources | [10](10-task-control-and-reviews.md), [13](13-plans-and-root-completion.md), [14](14-memory-and-document-publication.md) | Independent reviews, stable keys and exact approved scope |
| `plan review start/finish` | review task resource / finish | [10](10-task-control-and-reviews.md), [13](13-plans-and-root-completion.md) | Separate ordinary tasks with unchanged author ownership |
| `plan activate/complete` | decide / finish | [09](09-leadership-and-admission.md), [13](13-plans-and-root-completion.md) | Human/Vizier authorization, small-plan automatic closure |
| `memory list/show/set`, `knowledge publish` | memory projection / update / publish | [14](14-memory-and-document-publication.md) | Curated scope, bounded context and observed remote commit |
| `ledger sync/status` | publication resource / projection | [15](15-brain-publication.md) | Native writes, fixed cadence and no bookkeeping feedback |
| `usage`, `cost`, `rates list/show/add`, `usage reconcile` | analytics projection / resource reconcile | [16](16-usage-and-completion-cost.md) | Unique turns, provenance, partial coverage and corrections |
| `fleet replace --mode drain\|interrupt` | task resource / transfer | [18](18-task-continuity-and-fleet.md) | Interrupted replacement map and no timeout escalation |
| `reset --hard --yes` | exclusive reset resource operation | [21](21-hard-reset-and-cutover.md) | Temporary Beads authority, remote history and preserved sentinels |
| `fixture create/show/cleanup` | test utility using setup/resources | [22](22-deterministic-cli-validation.md) | No production fallback; exact inventory and caller evidence |
| `fixture barrier prepare/arrive/show/release` | external fixture resource control | [22](22-deterministic-cli-validation.md), [24](24-thirty-task-concurrency.md) | Real overlapping native/tool observations |
| `scenario emit/advance/fault/crash` | deterministic provider/controller test controls | [22](22-deterministic-cli-validation.md) | Named crash boundaries; no arbitrary work-state injection |
| `smoke concurrency` | bounded public-CLI test client | [24](24-thirty-task-concurrency.md) | 30 automatic starts, native overlap, tools and release |

## Requirement and verification map

The command map above covers executable access. This table covers cross-cutting
behavior and retained capabilities; together with the numbered tasks it is the
replacement acceptance checklist, not a new acceptance concept in Fulcrum.

| Requirement | Implementation | Required evidence |
| --- | --- | --- |
| One stock Beads ledger, native fields, no private workflow database | [02](02-beads-ledger-and-operations.md), [03](03-configuration-and-projects.md), [04](04-work-and-ownership.md), [25](25-replacement-completion.md) | Real native intake and ordinary bd reads; source/packaging audit |
| One writer across instance aliases; explicit isolation and configuration repair | [01](01-cli-application-spine.md), [03](03-configuration-and-projects.md), [19](19-installation-and-service.md) | Concurrent locks, dangling config, unavailable Beads repair |
| Outcome preservation and complete direct role prompts | [04](04-work-and-ownership.md), [06](06-codex-runtime-adapter.md), [07](07-role-context-and-entry.md) | Sent native inputs, same-bead transition and compact context |
| Accountable ownership and receipt-based acquisition | [02](02-beads-ledger-and-operations.md), [04](04-work-and-ownership.md), [12](12-executor-warden-delivery.md), [17](17-recovery-and-human-resolution.md) | Stale same-task command and every handoff crash boundary |
| Eight roles, human entry, permanent specialists, independent reviews | [07](07-role-context-and-entry.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [17](17-recovery-and-human-resolution.md), [23](23-luna-end-to-end-validation.md) | Live native task/effect evidence for each role |
| Marshal decision quality and manageable context | [04](04-work-and-ownership.md), [07](07-role-context-and-entry.md), [09](09-leadership-and-admission.md), [14](14-memory-and-document-publication.md), [18](18-task-continuity-and-fleet.md), [22](22-deterministic-cli-validation.md), [23](23-luna-end-to-end-validation.md) | Author intake/unknowns, decision-focused separate batches, focused clarification, no routine wakeups, recovered rationale; actual choices in the existing live workflow |
| Four active tasks, no reserve, goal of 30 | [09](09-leadership-and-admission.md), [17](17-recovery-and-human-resolution.md), [24](24-thirty-task-concurrency.md) | Deterministic admission plus real overlap and no-slot repair |
| Continuous supervision without recurring model jobs | [05](05-observability-and-inspection.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md) | Injected timers, idle quiet, bounded escalation and loop failure |
| Current-source Warden fixes and observed delivery | [11](11-workspaces-and-delivery-adapter.md), [12](12-executor-warden-delivery.md), [23](23-luna-end-to-end-validation.md) | Real submitted/approved/integration source and remote ancestry |
| Substantial plans, small work, future activation and stable refinement | [10](10-task-control-and-reviews.md), [13](13-plans-and-root-completion.md) | Complete approval evidence, cancelled-child blocker, no default validation child |
| Native/ordinary brain Git persistence and authorized YAML | [03](03-configuration-and-projects.md), [14](14-memory-and-document-publication.md), [15](15-brain-publication.md) | Remote commit inspection, cadence and divergence preservation |
| Durable curated memory and task continuity | [07](07-role-context-and-entry.md), [14](14-memory-and-document-publication.md), [18](18-task-continuity-and-fleet.md) | Replacement reads current context without transcript replay |
| CLI diagnostics, requests, evidence and finite operations | [01](01-cli-application-spine.md), [02](02-beads-ledger-and-operations.md), [05](05-observability-and-inspection.md), [06](06-codex-runtime-adapter.md), [08](08-controller-supervision.md), [10](10-task-control-and-reviews.md) | Scripted investigation/response/repair without UI/private SQL |
| Nonblocking usage, root cost and late corrections | [16](16-usage-and-completion-cost.md) | Hand-calculated totals, duplicate/restart and missing telemetry |
| Archive once, manual unarchive, fleet replacement | [18](18-task-continuity-and-fleet.md) | Clock-controlled lifetime archival and preserved work maps |
| Quiescent installed refresh and independent recovery | [19](19-installation-and-service.md), [20](20-source-refresh-and-recovery-launcher.md) | Broken import, failed probe, atomic activation and restart |
| Scoped reset without historical migration or broad brain deletion | [21](21-hard-reset-and-cutover.md) | Fresh remote ledger clone and unrelated resource/history sentinels |
| Report pre-existing problems without reopening shipped work | [04](04-work-and-ownership.md), [07](07-role-context-and-entry.md), [12](12-executor-warden-delivery.md), [17](17-recovery-and-human-resolution.md) | Follow-up outage after accepted finish; standalone report root |
| All actions initiated/managed through Fulcrum tools | [01](01-cli-application-spine.md), [05](05-observability-and-inspection.md), [10](10-task-control-and-reviews.md), [22](22-deterministic-cli-validation.md), [23](23-luna-end-to-end-validation.md), [24](24-thirty-task-concurrency.md), [25](25-replacement-completion.md) | Public-command inventory and complete external validation reports |

## Validation execution and reporting

Implement `scripts/validate-fulcrum2-cli` for deterministic cases and
`scripts/validate-fulcrum2-live` for functional native work. The CLI smoke utility
uses the same public fixture/task commands. Fixture/provider setup is exposed by
Fulcrum; scripts can use independent Git/content assertions as oracles but cannot
advance workflows through hidden SQL, monkeypatches or fabricated native outcomes.

Deterministic cases run against an installed executable, real disposable stock
Beads and persisted fake external providers. Run served/offline paths, response
loss, finite retries, crash boundaries and event/timer recovery. Native calls are
excluded from routine deterministic checks. Start this evidence incrementally.

Explicit native replacement validation uses Luna/low for all eight roles and review
tasks, real Tollgate/Git, required fixture source sync and normal four-slot admission.
Functional budget is 3,000 seconds, reserving its final 120 seconds for evidence and
bounded cleanup. Native turn counts are reported; the driver does not silently rerun failed cases
or introduce a production turn-budget policy. Native concurrency uses a 600-second total budget, begins cleanup by
second 540, and temporarily configures both fixture limits to 30 without changing
production defaults. Idle leaders do not dilute its measured active-task interval.

Reports retain invocation/source commit, fixture/provider/runtime/model facts,
public command/result references, actual bead/task/turn/operation/source IDs,
assertions, observation gaps, API-equivalent cost coverage, and cleanup outcome.
Missing required capability/evidence or exhausted time cannot be called a pass.
Ordinary bounded workflow recovery may run; reports remain external to workflow
state. Preserve failed evidence and require an explicit rerun rather than quietly
resetting failures until a passing result appears. No physical dollar estimate is
used as an enforceable subscription-spend claim.

Before declaring replacement readiness, run appropriate repository checks and
complete both forms of native evidence. Neither this plan nor the historical audit
claims those tests have passed. Production cutover is a separately explicit user
action; destructive tests target only enumerated disposable fixtures.

## Documentation review record

The prior design had conflicting future activation authority, a fixed bootstrap
prompt instead of direct input, redundant ownership tokens, a mistaken 30-task
default/recovery reserve, native-subagent requirements, incomplete CLI actions,
and no precise root-completion rule. Those are corrected in the normative docs.
Writer isolation, bootstrap/degraded paths, decision freshness, publication feedback,
fixture controls and machine-readable observability are now specified as well.

Remaining unknowns are implementation verification obligations, not permission for
workers to select a different architecture: stock Beads field/history behavior,
native task recovery/configuration and resource capabilities, real Tollgate source
mapping, remote-history replacement and actual native runtime behavior. Their owning
tasks must establish evidence and surface unsupported capabilities rather than
invent a fallback, new store or successful result. No native model calls, resets or
product implementation were performed to write this plan.
