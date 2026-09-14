# Fulcrum 2.0 requirement and capability audit

Audited against the original redesign request, the subsequent decisions and
approved scope, and the current repository's CLI, implementation, role prompts,
operational guides, and tests. This audit corrected the design documents; it did
not implement Fulcrum, run the native concurrency smoke, or reset any data.

The initial documents preserved the core architecture but were incomplete as a
replacement specification. In particular, they explicitly excluded existing cost
reporting, omitted the exact worker naming table, and lacked contracts for several
existing operational capabilities. Those omissions are corrected in
[design.md](design.md) and [contracts.md](contracts.md). The existing operational
research remains in [failure-analysis.md](failure-analysis.md).

## Original request and conversation coverage

| Requirement or agreed decision | Audited result and implementation location |
| --- | --- |
| Ground-up Python redesign around observability, robustness, recovery, speed, and cost | Design §§1–2, 6–7; finite operations, small controller, compact decision inputs, durable usage/cost facts. Existing failure evidence informs these decisions rather than being treated as proof that Python was the cause. |
| Stock Beads is the sole durable Fulcrum workflow store; no Fulcrum SQLite or parallel ledger | Design §3; contracts §3. Native Git and Codex remain authoritative for their own resources. Config, diagnostics, and exported documents do not become workflow stores. |
| One ledger across projects, every bead uses `fc-`, retained in `~/brain` and GitHub | Design §3; contracts §§4, 9 keep the shared Beads workspace under the brain Git repository, publish native Beads history without issue exports, and push pending changes every five minutes. Project-local configuration connects to that shared backend. |
| Ordinary `bd create --assignee executor …` is sufficient | Contracts §§3–4: role alias is an intake request; enrolled project context and the standing Marshal establish accountable routing before actual task ownership. |
| An unfamiliar agent can implement a bead; use Beads workflows instead of elaborate Python prompt generation | Design §3; contracts §4 gives the inspected stock formula shape and fixed bootstrap prompt. Role transitions update the same bead; no synthetic stage-bead graph. |
| Corrected task naming: role prefix plus suffix from bead ID | Design §4 now specifies all eight exact titles. `fc-51o` becomes `[wvr-51o]`, `[exe-51o]`, or `[war-51o]`; `fc-` is not displayed inside the brackets and role codes do not alter the bead ID. |
| Same bead across implementation/review and scoped investigations; true plan children get their own beads | Design §§3–4; contracts work/plan representations and permanent introspection transitions. |
| Golden rule: actual responsible task ID, HUMAN only when external intervention is necessary | Design §4; contracts ownership/claims and HUMAN commands. Raw intake, transfer intervals, backlog deferral, and completed records have explicit semantics. |
| User-initiated work, continuous supervision, no recurring agent jobs | Design §1 clarifies the agreed boundary: Python continuously watches existing work and periodically checks for stalled agents, with automatic recovery and Marshal/Justiciar escalation. Periodic brain Git pushes are also mechanical maintenance. No recurring model patrol, Sage/Mason reviews, interviews, calendar work, or periodic self-improvement assignments. Future plans activate explicitly. |
| Microskills; every role manually invocable; no implicit invocation | Contracts §§2, 4: same CLI entry, explicit task IDs, `allow_implicit_invocation: false`. All eight roles have degraded entry guidance when registration cannot succeed. |
| Direct human entry begins immediately and overrides queue/admission | Design §§4–5; contracts role entry applies this to every role. Physical runtime failure remains a truthful limitation, not fake task registration. |
| Vizier is one persistent human liaison with supreme policy authority and no system messages | Design §4; policy/leader/task-send contracts. Replacement retains policy/memory; bootstrap/replacement do not send unsolicited Vizier turns. |
| Marshal is one persistent autonomous dispatcher with short context | Design §5: bounded brief, selective full-bead reads, batched decisions, stale-decision checks, overlap/capacity decisions, autonomous Justiciar dispatch, one-time HUMAN surfacing. |
| Deliberate low-priority deferral is allowed | Design §§4–5; typed event-based reconsideration. No progress theater or timer-generated work to touch old backlog. |
| Weaver investigates/answers without editing product code; `$bead` reports from any conversation | Design §4. Answer-only requests are valid; reporting neither changes role nor blocks delivery. The former question-punctuation authorization rule is superseded. |
| Executor implements and validates in an isolated worktree | Design §4; source/worktree/validation contracts. Relevant behavioral checks and UI evidence retained without broad manual gates. |
| Warden independently reviews, fixes itself if necessary, and promotes; Executor stops permanently after handoff | Design §4; contracts finish/ownership/delivery. No ordinary return to Executor. Changed Warden source invalidates earlier approval. |
| Sage investigates logs, tools, prompts, and broken state; no interviews or healthy-state prerequisites | Design §4 and degraded entry; evidence remains available through CLI and independent recovery launcher. |
| Mason investigates architecture, refactoring, single responsibility, and low-value tests | Design §4. Scoped/global work and reporting use the same lifecycle as Sage. |
| Sage/Mason invocation permanently changes role; unfinished implementation returns to Marshal | Contracts §2 handles both open and already-delivered beads. Source/delivery facts survive; no concurrent successor edits during introspection. |
| Justiciar may take over and complete broken work alone, overriding Fulcrum/Tollgate within scope | Design §§4, 6; contracts recovery controls. Explicitly allows substantive scope reduction and known defects, with retained tradeoffs and observed cleanup. |
| Recovery must work under exhausted runtime capacity | Design §6; reuse an existing Marshal as Justiciar or terminal repair. Creating another worker is never the only path. |
| Archive once; manual unarchive must remain visible | Design §6; task record and deterministic archive scenario. Earlier plan participants remain visible while associated implementation is active/pending, independently of releasing subscriptions. |
| Self-improvement reporting and Marshal deduplication | Design §§4–5, 7. Nonblocking finish reminder; canonical duplicate links preserve evidence; no recurring self-improvement runs. |
| Complete ordinary-terminal CLI for all operations and tests | Contracts §§1–2, 7, 9. Explicit identities, stdin/files, JSON, exit codes, receipt status/wait/cancel/reconcile, offline repair, deterministic adapters. |
| Tollgate retained as a narrow replaceable delivery adapter | Contracts §5, including worktree setup, exact-source checks, promotion, configured synchronization, result inspection, and cleanup. |
| Hard reset and wipe, not migration | Design §8; contracts §6. Old managed beads/conversations/logs/worktrees are enumerated for deletion at cutover; project source and unrelated tasks survive. No reset was performed during authoring or this audit. |
| Compact automated validation; one time-limited 30 Luna/low native smoke | Design §8; contracts §§7–8; failure-analysis focused verification. No prolonged stress test, interviews, or live-role smoke requirement for ordinary promotion. |

## Existing capabilities retained

“Implemented” below means there is a code path in the inspected repository; it
is not a claim that production execution was reliable. “Prior contract” means a
documented capability whose complete implementation was not established. Original
source evidence is available at code baseline `e89ae32`; the initial replacement
documents were committed as `8aca63a` before this audit.

| Existing capability | Evidence and status | Replacement contract |
| --- | --- | --- |
| Durable native token usage and API-equivalent cost reporting | Implemented: [CLI](../../src/fulcrum/cli.py), [store](../../src/fulcrum/store.py), [README](../../README.md). | Contracts §9 restores usage/cost filtering, decimal rate cards, model reroutes, response pricing, helper causality, partial coverage, and frozen workflow totals in Beads. No SQLite analytics subsystem. |
| Substantial plans, direct small-task intake, future activation, stable-key refinement | Implemented CLI/intake/controller paths and [Weaver prompt](../../src/fulcrum/prompts/weaver.md). | Contracts §9 adds publish/refine/show/activate. Scope changes are explicit; interrupted graph creation resumes without duplicating children. |
| Independent cold-reader and requirements reviews of substantial plans | Existing [Weaver instructions](../../src/fulcrum/prompts/weaver.md). | Design §9 retains both authoring perspectives and explicit waiver evidence if unavailable. These do not become routine live-role execution tests. |
| Plan Mode remains read-only | Existing Weaver instructions and CLI `--plan-mode`. | Design §9 respects actual platform capability; unavailable registration is reported honestly. No fabricated bead or hidden writes. |
| Plans/knowledge committed and synchronized without discarding local or remote work | Implemented [brain publication](../../src/fulcrum/brain.py) and controller publication. | Contracts §9 retains `~/brain`, its GitHub remote, native Beads history, inspected success, resumable receipts, and retained conflicts. Pending changes push every five minutes. Canonical live scope remains Beads. |
| Curated shared/project knowledge and persistent Vizier memory | [README](../../README.md), [Vizier design](../vizier.md). Full Vizier behavior is a prior contract, not established live implementation. | Design §9 and memory CLI/Beads schema preserve curated durable knowledge and selective reload after replacement. Old restrictions on Vizier policy authority are superseded by the user's new supreme authority. |
| Soft/draining and hard/interrupting fleet replacement without deleting work | Implemented [CLI](../../src/fulcrum/cli.py), controller, [operations](../operations.md). | `fleet replace --mode drain|interrupt`, distinct from destructive `reset --hard`. Preserve policy, worktrees, ownership history, and explicit old/new task mapping. |
| Safe native task continuity and reuse across restart | Implemented lineage provisioning in controller, [contracts](../contracts.md). | Same-bead/same-role compatible task reuse remains. Numeric lineage pools and overflow suffixes are replaced by stable bead titles and native task IDs. |
| Read-only compaction reminders and one missing-outcome reminder | Implemented [hook](../../src/fulcrum/hook.py), controller; [hook contract](../hooks.md). | `hook context` plus shared context provider; no Stop-hook enforcement, prompt replay, or mutation. Missing finish gets one scope-preserving reminder. |
| Per-project/model/effort selection and per-work overrides | Implemented [config](../../src/fulcrum/config.py), CLI, intake and prompts. | Explicit flags and work/project/instance role maps, advertised capability checks, selection provenance. No silent model substitution. |
| Dependency ordering, priority, parallel projects, overlap/capacity control, composable holds | Implemented [scheduling](../../src/fulcrum/scheduling.py), lifecycle/controller. | Beads dependencies and typed waiting reasons; resolving one reason never clears another. Marshal decisions remain separate from mechanical dispatch. Time-scheduled work is removed. |
| Initial independent review, exact-source validation, promotion, synchronization and cleanup | Implemented [Tollgate adapter](../../src/fulcrum/tollgate.py), [lifecycle](../../src/fulcrum/lifecycle.py). | Executor-to-Warden flow retains the delivery postconditions; Warden owns fixes. Post-promotion failures are delivery repair, never a request to redo promoted code. |
| Proportionate verification and rendered evidence for visible changes | Existing [Executor prompt](../../src/fulcrum/prompts/executor.md). | Design §4 retains the capability without a universal end-to-end skill test or a manual validation matrix. |
| Native helpers, terminal observation, approvals/input handling | Implemented runtime/controller and recovery. | Runtime adapter and task CLI preserve exact IDs, helper observation, safe interruption, and terminal responses. No invented fixed two-helper limit. |
| Setup/rerun repair, deterministic service environment, skill-link repair, shared Desktop launcher | Implemented [setup](../../src/fulcrum/setup.py), [installer](../../src/fulcrum/install.py), [setup guide](../setup.md). | Contracts §9 gives public CLI equivalents, unattended configuration, owned-service repairs, model/project probes, and explicit Desktop launch. Unrelated services/skills are preserved. |
| Quiescent automatic controller refresh after source changes | Implemented controller source watcher and installed snapshot activation. | Design §2 retains event-triggered refresh through a probed installed-package swap. No unsafe in-place hot loading. |
| Emergency recovery independent of broken source/.venv/main runtime | Implemented [recovery](../../src/fulcrum/recovery.py), installer, [operations](../operations.md). | Separate `fulcrum-recover` launcher uses the same repair contracts. No healthy task/database prerequisites and no second workflow journal. |
| Evidence-preserving ordinary recovery and scoped stop-before-repair | Implemented recovery/Operative controls. | Exact repair actions, quarantine when appropriate, observed writer termination, retained uncertainty, and no automatic unfencing after failed repair. Deliberate cutover wipe is the separate exception. |
| Archived task history, idle visibility, safe cleanup of temporary handoffs | Implemented lifecycle/controller and [operations](../operations.md). | Archive-once metadata plus associated-work eligibility. Input/evidence files remain caller-owned; only recorded Fulcrum temporary artifacts are cleaned. |
| Diagnostic status, operational trace, resource observations, loop-health readiness | Implemented doctor, readiness, resource/runtime/controller modules. | CLI status/doctor/logs/trace and isolated resource lifecycle remain. Dependency failure is scoped to the affected capability, not a universal investigation gate. |
| Publication retry independent of successful intake | Implemented controller/brain; operational incident evidence. | Local durable filing, remote sync, dispatch, and completion have distinct observed results. Unrelated downstream failure never changes accepted intake into failure. |

## Intentional replacements and removals

These are justified by the approved redesign, rather than accidental feature loss:

- SQLite tables, SQL repair, parallel JSON journals, runs/assignments/lineage
  counters, compatibility aliases, and migration code are replaced by stock Beads
  records, native IDs, and one application path. Historical data is wiped at cutover.
- Archon, Overseer, Inquisitor, and Operative names become Marshal, Warden, Mason,
  and Justiciar. Their useful capabilities remain subject to the explicitly
  changed authority and handoff rules.
- Executor correction loops, read-only Overseer restrictions, frozen obsolete
  review scope, and human-only Operative creation are replaced by Warden-owned
  fixes and autonomous scoped Justiciar recovery.
- Scheduled specialists, interviews, specialist cadence policies, timed future-work
  activation, and setup's initial recurring-policy gate are removed.
- Caller-only context lookup, action-scoped mandatory input-file locations,
  role-binding refusal conditions, and the Weaver question-punctuation gate are
  replaced by explicit terminal identities, ordinary JSON inputs, and useful
  degraded entry. Platform Plan Mode restrictions still apply.
- Old lineage-wide worker pooling and overflow names are replaced by same-bead
  continuity. Independent deliverables no longer inherit another bead's task
  identity or implementation history. Idle visibility no longer implies loading.
- Repeated archival timers are replaced by a lifetime archive-once policy; manual
  unarchive is respected. The recovery path does not depend on archival reclaiming
  resources immediately.
- No new dashboard, historical-data preservation, broad manual validation matrix,
  or prolonged live stress suite is introduced. The older full assembled-product
  checklist is not a promotion gate for the replacement.

## Follow-up correction: brain location and remote cadence

The user identified that the first audit still moved the live ledger out of
`~/brain` and made GitHub persistence optional. That was an unintended removal,
not an agreed consequence of Beads-only storage. The corrected design keeps
Beads in the existing brain Git repository, preserves remote database history
with stock Beads Git transport. The user explicitly excluded issue exports; none
are generated or required.
A five-minute dirty-only push cadence is required mechanical maintenance; the
ban on recurring agent jobs does not prohibit it. `ledger sync` exposes immediate
publication and `ledger status` exposes pending/failed publication.

## Remaining qualifications

There is no unresolved product-scope question from this audit. Native runtime
capabilities, stock Beads behavior, and Tollgate results still require the narrow
adapter checks already specified; a design document cannot certify a future
implementation. Historical failures are evidence for requirements, not promises
that the replacement has already fixed them.

Validation remains compact. Existing public-CLI scenarios should also assert
retained behaviors where relevant: model choice and compaction on entry, memory
and claim continuity on fleet replacement, composable wait reasons, and usage
idempotence after duplicate/restart events. Small deterministic fixtures cover
pricing/reroute/helper attribution and interrupted plan refinement. They need no
model calls, manual review checklist, or additional stress run.
