# Bead `fc-cdf5657c` Weaver dispatch regression

**Incident date:** 2026-09-17 PDT (UTC-07:00)

**Status:** Resolved in local master; the incident bead itself was stranded at evidence collection

**Severity:** Catastrophic workflow-test failure; no requested README change was delivered

**Bead:** `fc-cdf5657c`

**Creation operation:** `fc-ebaf3c4ef1724f16b20784f03ef7631b`

**Incident source revision:** `25b08f6ba58b82834a52e27caab4911deac50d9b`

**Regression introduced by:** `53a9e7b75e72422c08cc742c7230f4baafcfa70c` (`feat: complete stock desktop cutover`)

**Delivered revision:** The subsequent `fix: enforce weaver and worker boundaries` change

## Investigation boundary

This report covers the user turn in native task
`01a0b1bc-b00b-7852-a75a-18dcbb61a83c`, the prerequisite project-enrollment
operation, creation of `fc-cdf5657c`, Steward's resulting actions, and the native
task that Fulcrum created for that bead. The incident starts with the human's
`$weaver` invocation at 16:39:40 PDT and ends at the first evidence collection at
16:52:52 PDT, when the bead was still stranded. The later postmortem task and its
read-only inspection commands are excluded.

Durations use named boundaries. The invoking turn ran for 9 minutes 10 seconds,
from native turn start to completion. The duplicate worker was interrupted 10
minutes 27 seconds after the invoking turn began. The first durable observation
of the unrecovered state was 13 minutes 12 seconds after that start. The evidence
does not identify why the native worker was interrupted.

## Executive summary

The expected invariant is categorical: Fulcrum never creates or dispatches a
Weaver. The task in which the human invokes `$weaver` is the Weaver. That task
must bind and own the bead, investigate the request, and produce a clear approved
scope. Only then may Fulcrum create the downstream implementation task, which for
this incident should have been titled exactly:

```text
⚒️ [exe-cdf5657c] Add newline to README.md
```

Instead, the current Weaver skill told the invoking task not to act as Weaver. It
made that task a filing shim, copied the human's literal request into both
`outcome` and `context`, and filed a backlog record with
`requested_role: "weaver"`. Steward's dispatcher explicitly treats that state as
eligible and created native task `01a0b1c6-4fd2-7f22-a474-3181336a3b4c`, titled
`Weaver fc-cdf5657c`. The new task began with the executable token `$weaver`, could
not discover that skill, announced that it would implement the README edit
anyway, performed repository inspection before registration, and was interrupted.
No Executor was ever created. At evidence collection the assignment was still
`issuing`, the bead was still owned by `STEWARD`, one capacity slot remained
consumed, and no incident or recovery was recorded.

```text
Expected:
human invokes $weaver in task A
  -> task A owns/scopes bead
  -> approved bead contract
  -> one Executor task with exact role title

Actual:
human invokes $weaver in task A
  -> task A copies request into an unowned backlog bead
  -> Steward creates task B as another Weaver
  -> task B receives active skill syntax and raw request fields
  -> task B works before registration, is interrupted, and remains issuing
```

The primary root cause is confirmed in commit `53a9e7b`. That cutover changed the
Weaver skill from same-task `enter weaver` ownership to “file a new request,” added
automatic dispatch of backlog records whose requested role is Weaver, deleted the
role compiler and formulas that made skill-shaped text inert, and replaced exact
role titles with generic strings. This was also a recurrence of a failure already
documented and remediated in
[`2026-09-16-bead-2bbb5675-misdispatch-recovery.md`](2026-09-16-bead-2bbb5675-misdispatch-recovery.md):
downstream roles must receive the authorized Weaver scope, not raw intake, and
skill syntax inside retained data must never change their role. The cutover
deleted that validated protection.

No requested source change was delivered. `README.md` remained byte-identical to
the incident revision. Project enrollment did, however, leave `.gitignore` dirty;
its modification time falls inside the enrollment operation and the added lines
identify `bd init`. That attribution is strongly inferred rather than present in
the operation receipt, because the receipt does not enumerate filesystem effects.

## Impact and expected versus actual

| Stage | Expected | Actual | Result |
| --- | --- | --- | --- |
| Weaver identity | The invoking task is the only Weaver | Invoking task became a filer; Steward created another Weaver task | Hard invariant violated |
| Weaver work | Same task investigates and prepares scope | Invoking task copied the request and invented acceptance without owning the bead | Authoring boundary skipped |
| Downstream role | Executor after approved scope | A second Weaver was created; no Executor existed | Wrong role |
| Downstream title | `⚒️ [exe-cdf5657c] Add newline to README.md` | `Weaver fc-cdf5657c` | Wrong role, emoji, spacing, code, suffix presentation, and subject |
| Downstream contract | Clear approved outcome, acceptance, evidence, and optional notes | Active `$weaver` token plus duplicated raw request fields rendered as prose | Role and data boundaries collapsed |
| Admission | Registration precedes substantive work | Worker ran repository searches before `register_worker` | Gate unenforced |
| Failure handling | Interrupted unregistered creation is failed/recovered and capacity released | Create action stayed succeeded; assignment stayed `issuing`; capacity stayed used | Bead stranded |
| Repository effect | Only the eventual README change | No README change; project enrollment apparently modified `.gitignore` | Objective failed; unrelated dirty state |
| Observability | One trace joins role, action, native task/turn, interruption, and recovery | Trace reported no gaps but omitted the native interrupted turn and reason | Manual correlation required |

The test had no explicit time budget. It nevertheless failed functionally before
latency is considered: after 13 minutes 12 seconds, there was no registered
Weaver ownership, no approved scope, no Executor, no source change, and no
automatic recovery.

## Actors and durable identifiers

| Logical actor | Native task/turn | Durable identity | Observed role |
| --- | --- | --- | --- |
| Human-invoked Weaver | Task `01a0b1bc-b00b-7852-a75a-18dcbb61a83c`; turn `01a0b1bd-7c71-73a1-a09f-402038c66fd2` | Origin of `fc-cdf5657c`; creation operation `fc-ebaf3c4ef1724f16b20784f03ef7631b` | Not registered or assigned as Weaver |
| Vizier | Standing task `01a0b19d-e3f3-7500-b984-cb278288b440` | Project enrollment `fc-d19b56a87fae45c5a8bb7f71c5ad4fda` | Authorized configuration actor |
| Steward | Standing task `01a0b19e-5256-7972-86c8-ba62f5f36ad8` | Creation action `action-f4dba58c-51f5-480b-9f9f-ff97f990dac4`; title action `action-36203333-29d5-441f-a37d-bcbb37646319` | Native action executor |
| Incorrectly spawned Weaver | Task `01a0b1c6-4fd2-7f22-a474-3181336a3b4c`; turn `01a0b1c6-5181-7c02-8a31-23593dda14dd` | Assignment retained on `fc-cdf5657c` | Never registered; native turn interrupted |
| Executor | None | None | Never created |

The postmortem task `01a0b1c7-6986-71e3-919e-0c60f355ee66` is excluded from the
incident path.

## Detailed timeline

All times are PDT. Fulcrum receipts are UTC and have been converted by subtracting
seven hours. Native task timestamps are second-resolution in the retrieved record;
durable receipts retain their original precision.

| Time | Event | Evidence and gap |
| --- | --- | --- |
| 16:39:40 | User invokes `$weaver` in task `01a0b1bc-...` and asks to add one newline to `README.md`. | Native user turn. |
| 16:39:40–16:47:11 | The invoking task follows the skill's filing-only design, attempts `work create`, discovers that the project is not enrolled, attempts unauthorized enrollment itself, and then asks standing Vizier to enroll the project. | Native transcript. Exact item timestamps are not exposed, so substage durations are not allocated. |
| 16:47:11.210–16:47:17.901 | Vizier enrollment operation `fc-d19b...` enrolls project `fulcrum`. | Durable operation receipt. `.gitignore` mtime is 16:47:14 and its new comment says it was added by `bd init`; causation is inferred because the receipt omits file effects. |
| 16:48:34.526–16:48:35.016 | Invoking task creates bead `fc-cdf5657c` with the literal request in both `outcome` and `context`, plus `requested_role: "weaver"`. | Durable creation receipt. |
| 16:48:42.301 | Steward reserves an ordinary assignment with role Weaver and creates a `create_thread` action. | Work state and `work_selected` event. |
| 16:48:58.936 | Steward claims the create action. | Action attempt receipt. |
| 16:49:19 | The new native task's initial turn starts. Its prompt begins with `$weaver`. | Native task record. |
| 16:49:19–16:50:07 | The task says the Weaver skill is unavailable, promises to make the EOF edit, and runs repository searches before registration. | Native transcript. |
| 16:49:29.252 | Fulcrum records native creation as succeeded and changes the assignment from reserved to issuing. | Action result and assignment state. This proves a task ID, not a healthy registered worker. |
| 16:49:29.252 | Fulcrum unconditionally creates a second action to set the task title to the same title supplied during creation. | Retained title-normalization action. |
| 16:49:58.211 | Steward claims the title action while the worker turn is already running. | Action attempt receipt. |
| 16:50:07 | Native worker turn ends as `interrupted`, without registration or progress. | Native task record. The reason is unknown. |
| 16:50:19.268 | Steward reports the redundant title action succeeded. | Durable action result. |
| 16:52:52 | First evidence bundle observes the bead still open in backlog, owned by `STEWARD`, assignment `issuing`, no progress, no incident, and one ordinary capacity slot consumed. | `work show`, `status`, and trace. |

## Causal analysis and failure inventory

### Primary root cause

Commit `53a9e7b` replaced the same-task Weaver entry contract with a two-task
filing/dispatch design. The new skill explicitly says “Do not investigate ... in
the invoking task,” requires `requested_role: "weaver"`, and tells Steward to
select the bead later. The dispatcher added backlog Weaver eligibility and uses
the retained requested role to create a worker. Removing either side prevents
the duplicate; removing both restores the intended invariant. This survives the
counterfactual test and is the primary root cause.

The same cutover removed `roles.py`, all role formula assets, and the compiler
that made the authorized role dominant and treated skill syntax, Markdown,
quoted instructions, evidence, and code blocks as inert contract data. It also
removed the exact `role_title` formatter. Those deletions explain the prompt and
naming regressions; they are not consequences of the model's later choices.

| # | Failure | Layer | Evidence | Causal role | Confidence |
| ---: | --- | --- | --- | --- | --- |
| 1 | Fulcrum created a Weaver task. Weaver creation must be impossible; the invoking `$weaver` task is the Weaver. | Role/admission | Skill, dispatcher source, creation action, native task | Primary root cause | Confirmed |
| 2 | The invoking task was reduced to a filing shim and never registered, owned, investigated, or finished the bead as Weaver. | Intake/ownership | Native transcript and bead origin/owner | Destroyed conversation continuity and skipped intended authoring | Confirmed |
| 3 | The skill itself requires `requested_role: "weaver"`, while the dispatcher explicitly selects `phase=backlog` records with that role. | Product contract | `skills/weaver/SKILL.md`; incident source `desktop_protocol.py` | Deterministically produces failure #1 | Confirmed |
| 4 | The managed prompt begins with the active instruction token `$weaver` instead of a fixed role contract. | Prompt/role | Retained create action and native transcript | Caused skill lookup and role ambiguity | Confirmed |
| 5 | The cutover deleted the previously validated inert-data authorization compiler and formulas. | Prompt security | Parent source versus `53a9e7b`; prior `fc-2bbb5675` postmortem | Reopened a known prompt/role injection class | Confirmed |
| 6 | Literal human input is copied into both `outcome` and `context`, then concatenated into worker prompts without encoding, escaping, or a non-executable data boundary. | Intake/prompt | Creation receipt and prompt builder | Duplicates instruction-shaped text and permits role/skill activation | Confirmed in this prompt; exploitability confirmed by source |
| 7 | The downstream Executor path would compile `fc.scope` using Python string rendering and also forward the original `fc.context`; it does not select only the approved behavioral contract. An Executor was not reached in this incident. | Prompt/contract | Incident `completion.py` and `desktop_protocol.py` | Would repeat the previously observed raw-intake leak | Confirmed latent defect; not exercised here |
| 8 | `implementation_notes` are stored inside `fc.scope` by Weaver finish, but the dispatcher reads nonexistent top-level `fc.implementation_notes`, so approved notes would be silently dropped. | Contract compilation | Incident source | Corrupts future Executor instructions | Confirmed latent defect |
| 9 | The Weaver prompt says “before editing” and never states that Weaver must not implement. The spawned task therefore announced it would make the README change. | Role instructions | Retained prompt and native commentary | Encouraged role violation | Confirmed |
| 10 | The spawned task could not discover the `$weaver` skill even though the architecture claims Weaver skill links point directly to local master. | Installation/discovery | Native command output listed only three `.agents/skills`; agent reported skill unavailable | Forced improvisation; exposed setup/dispatch mismatch | Confirmed symptom; underlying discovery cause unknown |
| 11 | Registration was not an effective admission gate. The worker ran substantive repository searches before `register_worker`; no hook or command boundary stopped it. | Admission/enforcement | Native transcript; assignment never active | Allowed unowned work | Confirmed |
| 12 | The title was `Weaver fc-cdf5657c`, not `⚒️ [exe-cdf5657c] Add newline to README.md`. Current source and tests intentionally use generic role-plus-ID titles, while the contracts document also retains a different stale emoji/spacing convention. | Naming/UI | Native title, source, tests, human-supplied required format | Made role and bead purpose misleading | Confirmed |
| 13 | Native task creation was recorded as succeeded before role registration or a healthy initial turn. After interruption, the assignment remained `issuing`, capacity remained consumed, and no recovery incident existed. | Lifecycle/recovery | Action result, native interrupted turn, later status | Stranded the bead and leaked capacity | Confirmed |
| 14 | Fulcrum always scheduled a title action after successful creation even though the exact same title was already supplied to `create_thread`. It completed after the worker was interrupted. | Protocol/performance | Action history and source | Added needless state transitions and about 50 seconds of title-action lifecycle | Confirmed contributing defect |
| 15 | Ordinary Weaver intake triggered project enrollment, first from an unauthorized task and then through Vizier. Enrollment apparently dirtied `.gitignore` without the operation receipt disclosing that filesystem effect. | Setup/scope | Native transcript, enrollment receipt, file mtime/diff | Added delay, changed unrelated repository state, and risked blocking clean-worktree preparation | Enrollment confirmed; file attribution inferred |
| 16 | The bead trace reported `gaps: []` but lacked the spawned task's native turn ID, interrupted outcome/reason, registration failure, recovery decision, and useful action identifiers. The origin task's task trace returned `NOT_FOUND`. | Observability | Collector probes, trace, native records | Made a supposedly single-bead investigation require manual cross-system joins | Confirmed |

### Why this is a regression, not a novel edge case

The `fc-2bbb5675` postmortem had already established and validated these rules:

- downstream workers receive the exact authorized Weaver scope rather than raw
  intake;
- retained text is inert even when it contains skill invocations, role names,
  Markdown links, or instruction-shaped code blocks;
- the explicit authorized role dominates prompt-like payload data;
- task titles do not replay raw scope text.

The implementation at the parent of `53a9e7b` enforced those rules in
`_cook_role`, `_compiled_contract`, `_role_authority_instructions`, role formulas,
and `role_title`. The cutover deleted those files and replaced them with direct
f-string prompt assembly. No equivalent invariant survived. Current tests then
institutionalized two wrong behaviors: they assert that a managed Executor prompt
contains `$fulcrum-executor`, and that title normalization uses generic values such
as `EXECUTOR fc-a`. The focused standard-library tests pass, proving the tests
protect the regression rather than detect it.

## Observability assessment

`fulcrum trace --bead fc-cdf5657c` provided the bead creation and generic action
state changes, but it could not independently answer what the created worker did
or why it stopped. Its action events omit action and attempt IDs in the rendered
trace. It showed a synthetic `task_observed` event before native creation was
reported, then no native turn lifecycle. It reported no gaps despite the missing
interrupted-turn evidence. `trace --task` for the invoking Weaver returned
`NOT_FOUND` because that task was never bound; `trace --task` for the spawned
worker still lacked the native interruption.

Reconstruction required:

1. `work show`, status, trace, logs, and both operation receipts;
2. native reads of the invoking task, Steward, and the spawned task;
3. historical source from incident commit `25b08f6` and regression commit
   `53a9e7b`;
4. current and historical tests and role formulas;
5. Git status plus `.gitignore` modification time to identify the enrollment side
   effect.

The minimum telemetry improvement is not more prompt logging. A single bead trace
needs stable joins from origin task/turn to assignment, action/attempt, created
task/initial turn, registration handshake, lifecycle outcome, recovery decision,
capacity release, and title observation. Raw payload retention should stay
bounded and redacted; the trace can retain a compiled-contract identifier and
field provenance instead of prompt text.

## Corrective actions

The investigation itself was read-only. The subsequent remediation implemented
the actions below in local master.

| Status | Action | Failure prevented or detected | Proof required |
| --- | --- | --- | --- |
| Implemented — P0 | Restore same-task Weaver entry. `$weaver` binds the invoking native task to the bead and returns its scoped instructions. | #1–#3 | Tests show same-task ownership and `native_task_created: false`. |
| Implemented — P0 | Make Weaver creation structurally impossible: remove Weaver from automatic dispatch and reject work creation, adoption, stale dispatch, and worker registration paths that request it. | #1, #3 | Unit and state-transition tests reject attempted Weaver creation. |
| Implemented — P0 | Compile Executor and Warden prompts only from accepted scope fields and keep raw intake as provenance. | #5–#8 | Adversarial prompt test proves raw outcome/context and active skill tokens are absent. |
| Implemented — P0 | Restore a dominant-role/inert-data boundary with escaped deterministic JSON and no role-skill invocation. | #4–#7 | Prompt fixture asserts the structured boundary and escaped syntax. |
| Implemented — P0 | Restore exact native role-title compilation. | #12 | Exact-string test asserts `⚒️ [exe-cdf5657c] Add newline to README.md`. |
| Implemented — P0 | Enforce registration before any repository or native tool call. | #9–#11 | Hook test rejects repository access and permits only registration. |
| Implemented — P0 | Release and record terminal unregistered assignments, queue archival, and stop automatic retry after the second failure. | #13 | Lifecycle test observes capacity release, history, incident, and archival action. |
| Implemented — P1 | Remove unconditional title normalization. | #14 | Matching/unreported titles emit no correction; only an explicit mismatch can emit one. |
| Implemented — P1 | Separate enrollment from Weaver entry and initialize Beads in stealth mode. | #15 | Entry reports missing enrollment without setup; enrollment test requires `--stealth`; incident `.gitignore` pollution was removed. |
| Implemented — P1 | Join actions, attempts, tasks, lifecycle, registration failure, and capacity release in bead trace; report missing created-task lifecycle as a gap. | #16 | Trace regression test covers the joined interrupted-registration path. |
| Implemented — P1 | Replace tests that asserted active skill tokens and generic titles with the correct invariants. | Regression prevention | The complete repository check covers the new boundaries. |

## Evidence quality and final assessment

Confirmed facts come from durable operation/action receipts, current work state,
native task records, incident source, and Git history. The cause of the native
worker interruption is unknown. The `.gitignore` attribution to enrollment is an
inference supported by exact timing and the `bd init` marker, not a declared
operation effect. The raw-intake defect in a future Executor is confirmed in
source but was not exercised because this incident never reached Executor.

The next operational test must demonstrate this complete path without manual
interpretation:

```text
one human task invokes $weaver
  -> that same task owns and finishes Weaver scope
  -> zero Weaver task creations
  -> one Executor created from the approved bead contract
  -> exact title: ⚒️ [exe-cdf5657c] Add newline to README.md
  -> no raw invocation/session text in the Executor prompt
  -> registration before repository access
  -> only the requested README diff
  -> automatic review, delivery, closure, and capacity release
```

Anything that creates a Weaver task is a test failure by definition.
