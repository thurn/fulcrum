# Fulcrum2 specification and capability audit

Status: specification review and implementation-plan authoring complete;
implementation and replacement validation remain pending.

This audit covers [design.md](design.md), [contracts.md](contracts.md), the current
repository implementation, [historical failure evidence](failure-analysis.md), and
the decisions made during the grill-me review. The [implementation index](plan/README.md)
assigns every command family and retained requirement to implementation tasks and
verification. The 25 task files are implementation instructions, not evidence of
completed product work. No native workers were started, installation reset, or
product code changed during this documentation work.

## Specification findings and resolutions

| Finding | Resolution and implementation owner |
| --- | --- |
| Design claimed to be implementation-ready despite contradictory and missing interfaces. | Status now separates specification from implementation and observed validation. The plan index maps requirements, commands, dependencies and evidence. |
| Worker start was only a fixed request to read a bead. | Retain Beads formulas, store cooked context and send the full role/task prompt directly. Context remains useful for inspection/compaction. Tasks 06–07. |
| A random claim token duplicated ownership-acquisition identity. | Use the establishing operation receipt ID as `ownership_operation`; acting task and acquisition must both match. Ordinary progress changes `last_transition` only. Tasks 02, 04, 12. |
| Global capacity was incorrectly 30 with a recovery reserve. | Defaults are four global and four per-project active managed tasks; all four can serve ordinary work. Idle leadership does not consume slots. Thirty is a tested capacity goal. Tasks 09, 17, 24. |
| Native helpers/subagents were treated as a Fulcrum subsystem. | Independent review work uses ordinary Codex tasks linked through Beads. Remove native-subagent orchestration, discovery and special accounting requirements. Tasks 06, 10, 16. |
| Future-plan authority differed between design and commands. | Human/Vizier authorizes current approved future scope. Marshal can execute that recorded authorization, never grant it autonomously. Tasks 09, 13. |
| Parent completion was not specified. | Re-read approved child/publication/delivery obligations and close mechanically. Cancelled/rejected children do not establish success. Validation children are a planning judgment, never a small-plan default. Task 13. |
| Plan drafting, approval and independent reviews lacked full initiating commands. | Specify draft/save, separate review start/finish, approval evidence and stable-input validation, publication, refinement, activation and completion. Tasks 10, 13. |
| Expected owner/phase alone could accept stale Marshal judgment. | Retain exact decision input and compare relevant scope/dependencies/policy/source facts directly; reject only affected rows. No hashes or versions. Task 09. |
| All-terminal control and pending input were not adequately accessible. | Expose native output, waits, requests/responses and owned terminal inspection/stop. Unsupported targeted stop must not stop unrelated terminals. Tasks 06, 10. |
| CLI completeness omitted dependency edits and operator-queued authorization. | Add explicit dependency mutation and `dispatch --authorize` under normal admission, separately from human bypass. Tasks 04, 09. |
| Different instance directories could acquire different locks against one brain. | One canonical brain-root writer lock plus installation/backend binding and fail-closed explicit-instance discovery. Tasks 01–03, 19. |
| Bootstrap and invalid-YAML behavior depended circularly on unavailable services. | Bounded pre-ledger setup, explicit no-receipt essential repair and read-only diagnosis; receipt workflow effects as soon as Beads works. Tasks 03, 17, 19. |
| Publication bookkeeping could create endless pushes. | Exclude only publication fields and ledger-sync receipts when identifying real pending changes; preserve native/control/YAML writes. Task 15. |
| Reset text did not distinguish empty current state from removed remote ledger history. | Replace dedicated remote ledger history and verify fresh-clone absence, preserving ordinary brain Git history/documents. No host physical-erasure claim. Task 21. |
| Deterministic delivery plus optional concurrency did not prove assembled role behavior. | Require live Luna/low functional testing for all eight roles and actual Tollgate/Git delivery within 50 minutes, plus the ten-minute 30-task smoke. Tasks 23–24. |
| Testing risked becoming another product workflow engine. | External scripts own assertions and reports. Public fixture/provider controls and normal CLI actions manage the test; there is no acceptance work kind or scenario state machine. Task 22. |

## Retained capability and implementation coverage

The existing-code references identify useful behavior or mechanisms to inspect,
not a promise that production execution was reliable. The historical source
baseline cited by the previous audit was `e89ae32`; its original redesign documents
were committed as `8aca63a`. Preserve the operational evidence in the failure
appendix while replacing the mechanisms explicitly changed by the user.

| Capability | Existing evidence | Replacement implementation and verification |
| --- | --- | --- |
| Stock Beads intake and durable filing independent of publication | [Beads adapter](../../src/fulcrum/beads.py), [intake](../../src/fulcrum/intake.py) | Tasks 02–04, 15, 22; real isolated ledger, native actor/project resolution and response-loss checks. |
| Native task runtime and project attachment | [Runtime](../../src/fulcrum/runtime.py) | Task 06 retains the existing maintained WebSocket transport and replaces request rejection/recovery assumptions; task 23 provides real execution evidence. |
| Request/operation recovery, current responsibility and progress | [Controller](../../src/fulcrum/controller.py), [lifecycle](../../src/fulcrum/lifecycle.py) | Tasks 01–02, 04, 08–09, 12, 17 use Beads receipts, acquisition IDs and bounded supervision. |
| Human role entry, specialist investigation and incidental reports | [CLI](../../src/fulcrum/cli.py), [prompts](../../src/fulcrum/prompts.py), [recovery](../../src/fulcrum/recovery.py) | Tasks 04, 07, 17, 23 implement all eight roles and degraded investigation without healthy-world gates. |
| Substantial plans, future work and stable refinement | [Weaver prompt](../../src/fulcrum/prompts/weaver.md), [intake](../../src/fulcrum/intake.py) | Tasks 10, 13–14 retain approved scope, independent ordinary-task reviews, explicit activation and selected publication. |
| Delivery review, current-source validation and cleanup | [Tollgate adapter](../../src/fulcrum/tollgate.py), [provider fixtures](../../tests/fixtures/tollgate/README.md) | Tasks 11–12, 23 prove Warden fixes, source identity, promotion, configured synchronization and cleanup separately. |
| Brain source/knowledge persistence | [Brain publication](../../src/fulcrum/brain.py) | Tasks 14–15 retain ordinary Git documents and native Beads Git transport with actual remote inspection and five-minute dirty-only cadence. |
| Curated memory and persistent policy | [Prior Vizier design](../vizier.md), [configuration](../../src/fulcrum/config.py) | Tasks 03, 14, 18 retain curated Beads memory and human/Vizier-owned YAML; prior complete Vizier implementation was not established. |
| API-equivalent cost and native usage | [Store calculations](../../src/fulcrum/store.py), [CLI](../../src/fulcrum/cli.py) | Task 16 replaces SQLite facts with bounded Beads analytics, managed-task attribution, decimal provenance, partial coverage and root completion metadata. |
| Dependencies, priorities, overlaps and capacity | [Scheduling](../../src/fulcrum/scheduling.py) | Tasks 04, 09 replace old scheduler records with native dependencies, composed waiting reasons, decisions and four-slot admission. |
| Native output, input responses and terminal observation | [Runtime](../../src/fulcrum/runtime.py), [resource checks](../../src/fulcrum/resources.py) | Tasks 05–06, 10 expose typed terminal control and observable capability gaps; no native-subagent subsystem is retained. |
| Archive visibility, same-work continuity and fleet replacement | [Lifecycle](../../src/fulcrum/lifecycle.py), [operations guide](../operations.md) | Task 18 uses task records, archive-once lifetime state, manual suppression and exact replacement maps. |
| Compaction and one missing-outcome reminder | [Hook](../../src/fulcrum/hook.py), [hook guide](../hooks.md) | Tasks 07–08 keep advisory read-only context and one scoped reminder, without tool denial or scope replay loops. |
| Installation, skill links, shared runtime and quiescent updates | [Installer](../../src/fulcrum/install.py), [setup](../../src/fulcrum/setup.py) | Tasks 19–20 retain owned service repair and installed environments; dependency failure is capability-specific. |
| Independent emergency launcher and scoped repairs | [Recovery](../../src/fulcrum/recovery.py), [operative](../../src/fulcrum/operative.py) | Tasks 17, 20 preserve essential repair with broken main import and no additional workflow journal. |
| Diagnostic service/loop/resource evidence | [Doctor](../../src/fulcrum/doctor.py), [readiness](../../src/fulcrum/readiness.py) | Tasks 05, 08 expose useful operator JSON from the first milestones, with retained outcome facts after log pruning. |
| Hard reset | [Current reset](../../src/fulcrum/reset.py) | Task 21 replaces broad brain deletion/history rewriting; old-store reads are deletion inventory only, never migration. |

## Intentional removals

- Fulcrum SQLite, private SQL tables, replay JSON journals, runs, numeric lineages,
  interview occurrences, frozen scope tables and separate publication obligations.
- Archon/Overseer/Inquisitor/Operative naming, Executor correction loops and
  role-specific retry engines; their changed responsibilities belong to
  Marshal/Warden/Mason/Justiciar.
- Recurring specialist/model jobs, timed future activation, healthy-world skill
  refusal, mandatory worker interviews and question-punctuation authorization.
- Caller-only context aliases, input-file location ceremonies, random ownership
  tokens, fixed read-the-bead startup prompts and native-subagent coordination.
- Unrelated closed-task worker pools, repeated archival timers, assumed process-name
  resource accounting, routine source force-push and broad brain reset.
- A mandatory validation bead for small plans or a full live role suite on every
  ordinary task promotion. Replacement acceptance itself still requires live evidence.

## Evidence required before shipping

The documentation review checked the current CLI/code and installed CLI help/schema
for relevant stock Beads, Tollgate and Codex interfaces. A generated schema proves
field availability, not complete operational behavior. These tasks retain explicit
implementation verification obligations:

- Tasks 02/15: stock Beads full metadata writes, change inspection and Git-transport
  remote commit proof without a parallel state store.
- Tasks 06/10/19: indexed task recovery before first rollout, actual project/workspace
  context, complete native prompts, pending requests, resource control and attachment.
- Tasks 11/12: provider source regeneration, lost-response handling, promotion and
  configured remote synchronization against real provider facts.
- Tasks 20/21: independent recovery under broken imports, quiescent activation,
  resumable reset authority and dedicated remote history replacement.
- Tasks 22–24: public-CLI failure coverage, all eight Luna roles, actual source
  delivery and observed thirty-task overlap/tool use/subscription release.

Unsupported required capabilities remain explicit blockers; implementers may not
invent successful facts, add an unauthorized compatibility store or silently use
fake providers for required live evidence. Partial usage telemetry is allowed only
with accurate coverage. Documentation readiness does not establish implementation
readiness, production cutover, or completed validation.
