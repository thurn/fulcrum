# Fulcrum: Deterministic Execution with Verified Agent Review

Fulcrum should spend model time implementing and reviewing changes. Its initial
workflow instead spends substantial time creating roles, registering identities,
exchanging status messages, and deciding whether another agent is still working.
Reported symptoms include five-minute task creation, minutes of pair setup, and
Executor/Overseer conversations that both remain busy while waiting on each
other. These are incident observations, not controlled benchmark results.

The replacement is one **Executor**, the live desktop task that implements an
approved collection of work, using Luna/xhigh. It invokes one Sol/high subagent
when independent review is needed. A Python **controller** performs scheduling
bookkeeping, runtime operations, evidence collection, and ordinary delivery. The
**Archon**, the human-facing agent responsible for scheduling judgment, approves
concrete batches instead of administering every handoff.

This document specifies the target implementation, including its cross-project
interfaces and failure behavior. It does not claim that the described
controller, review verifier, or Tollgate enforcement already exists. Saving this
document does not activate work, publish tasks, or change the running fleet.

## Related Information

The existing documentation explains the system being replaced. This design takes
precedence for runtime ownership, review, scheduling, and communication.
Existing requirements for approved scope and certified delivery continue where
this design does not replace them.

- [Original requirements](../original-prompt.md): the reasons for the model
  split, strategic scheduling, recurring improvement work, and independent
  review.
- [Existing architecture](../technical-design.md): project boundaries and the
  standing agent roles being simplified.
- [Existing contracts](../contracts.md): Beads, plan activation, dependency
  semantics, operational ownership, and synchronization.
- [Existing operations](../operations.md): holds, recovery, scheduled
  specialists, worktree ownership, and delivery obligations.
- [Existing implementation plan](../implementation-plan.md): historical scope;
  its pair-based execution requirements are superseded here.
- [Runtime compatibility observations](../compatibility.md) and
  [setup](../setup.md): previously inspected integrations, not permanent proof
  that the current desktop connection supports this design.
- [Hooks](../hooks.md) and [validation evidence](../validation.md): the existing
  reminder-based behavior and its tested limits.
- [Dashboard](../dashboard.md): existing views that must expose the new state.
- [Current state ownership](../../src/fulcrum/state.py),
  [intake](../../src/fulcrum/beads.py), and [review
  accounting](../../src/fulcrum/delivery.py): reusable behavior and the
  agent-owned boundaries that need replacement.
- [Tollgate service][tg-service] and [IPC][tg-ipc]: the authorization boundary;
  candidate approval can also authorize pending dependencies.
- [Codex app-server][codex-server] and [Python SDK][codex-sdk]: external task
  control and event transport.
- [Codex subagents][codex-subagents] and [runtime hooks][codex-hooks]: model
  selection, invocation evidence, and lifecycle instrumentation.
- [Apple LocalAuthentication][apple-auth]: native user authentication for the
  human-only promotion override.

[tg-service]: https://github.com/thurn/tollgate/blob/master/crates/tollgate-service/src/lib.rs
[tg-ipc]: https://github.com/thurn/tollgate/blob/master/crates/tollgate-ipc/src/lib.rs
[codex-server]: https://learn.chatgpt.com/docs/app-server
[codex-sdk]: https://learn.chatgpt.com/docs/codex-sdk
[codex-subagents]: https://learn.chatgpt.com/docs/agent-configuration/subagents
[codex-hooks]: https://learn.chatgpt.com/docs/hooks
[apple-auth]: https://developer.apple.com/documentation/localauthentication/lapolicy/deviceownerauthenticationwithbiometrics

## Responsibilities and Completion

**Beads** stores the issue graph; an individual issue is a **bead**. The
**brain** is the existing private repository containing plans and shared memory,
with Beads-managed issue history. **Tollgate** owns source worktrees, CI,
certification, Git promotion, and configured source synchronization. These
systems retain their existing responsibilities.

A **run** is a project-scoped collection of explicitly approved tasks executed
sequentially by one reusable Executor conversation. A **batch** is the exact set
of runs and constraints approved by Archon. Different runs may execute in
parallel, including runs in the same repository.

- Archon selects work, changes priorities, sets exclusions, and resolves
  material scope or scheduling exceptions. It remains directly accessible to the
  human.
- The controller prepares batch proposals, starts approved runs, records
  returned identities, tracks stages, and performs deterministic recovery.
- Executor investigates, edits, validates, and fixes code. It does not
  administer other desktop tasks or decide whether review can be skipped.
- The Sol reviewer assesses the approved requirements and exact submitted code.
  It does not assign tasks, run the fleet, or grant authority to different code.
- The **verifier** is a Python evidence-validation component launched by
  Tollgate through private process pipes. It reads actual runtime evidence and
  produces a verified review result; Tollgate persists the authoritative
  receipt. The controller stores receipt references, not independently editable
  approvals.
- A **promotion mandate** is Tollgate's durable authorization for one candidate,
  derived from a verified review or an authenticated human override. Its
  mechanics are specified in [Promotion Enforcement](#promotion-enforcement).

A **candidate** is Tollgate's retained immutable source submission. The
submitted source commit and the commit reconstructed and tested by Tollgate may
differ. Review approves the source. **Certification** is Tollgate's retained
proof that its required checks passed for the reconstructed commit and
configuration. Tollgate remains responsible for that evidence and for promoting
the tested result. A **queue prefix** is the earlier ordered set of candidates
onto which Tollgate reconstructs a candidate.

Code-task completion requires all of the following:

- The required review or human override authorized the exact candidate.
- Tollgate certified and promoted the candidate.
- The configured source remote was synchronized.
- Owned worktree, branch, and runtime-process cleanup finished.
- Beads recorded completion. Failed brain-history pushes remain separate,
  durable synchronization obligations with an identified controller owner.

For example, a promoted change with a failed remote push is delivery recovery.
It is not a reason to create another Executor or repeat the implementation.

## Desktop Runtime Integration

Python and the desktop must control the same live Codex runtime. Reading saved
conversations from a second server is insufficient: the user must be able to
open the running Executor, inspect activity, and steer it in the desktop.

The external thread/turn API exists. In the inspected installation, the default
shared control socket was absent, while desktop code contained explicit
WebSocket and opt-in local-daemon connection paths. Those observations establish
possible connection mechanisms, not a successful assembled integration. The
WebSocket transport is documented as experimental. [Runtime
reference][codex-server].

### Connection and capability requirements

The controller attaches to an explicitly configured shared endpoint and verifies
its identity before dispatch. It must not silently launch an unrelated server
when the endpoint is unavailable.

- Use the app-server API, directly or through an SDK capable of attaching to the
  selected server. Do not use UI automation for scheduling or review evidence.
- Keep the connection local to the host. Do not expose a new public listener or
  copy authentication secrets into agent prompts.
- Verify that desktop and controller observe the same task IDs, active turns,
  and events. A shared data directory alone does not establish shared ownership.
- Check creation, resumption, interruption, steering, model selection, history
  access, subagent evidence, and completion subscriptions independently.
- Preserve the user's installed project configuration and required tools,
  including the configured Playwright MCP server for browser work.
- Report capabilities individually. Missing review evidence disables agent
  promotion even if basic task creation works.

The diagnostic contract distinguishes a working connection from usable evidence:

```json
{
  "connected": true,
  "desktop_observes_same_runtime": true,
  "thread_control": "verified",
  "review_invocation_evidence": "unavailable",
  "dispatch_enabled": false,
  "reason": "Effective reviewer settings cannot be verified"
}
```

Configuration changes and any necessary desktop restart must preserve active
work. Installation must verify the selected connection in the actual desktop; it
must not treat an environment-variable name found in application code as a
stable public configuration contract.

### Adapter boundary

The runtime adapter normalizes installed API behavior into the controller's
small internal contract. The following names describe Fulcrum operations, not
additional methods claimed to exist in Codex:

```python
create_executor(dispatch_id, project, worktree, model, instructions)
start_turn(thread_id, dispatch_id, instructions)
interrupt_turn(thread_id, turn_id)
read_execution(thread_id, turn_id)
read_review_execution(parent_id, reviewer_id, turn_id)
subscribe(thread_ids)
```

Each result includes actual thread/turn identifiers and one of `accepted`,
`rejected`, or `unknown`. A request accepted by the server is not a completed
turn. A completed turn is not a completed code task.

- Preserve original event IDs, tool-call IDs, and response references where the
  runtime provides them. Store no invented delivery acknowledgments.
- Normalize effective model settings, input messages, tool results, terminal
  state, and usage observations separately.
- Runtime statuses describe observed execution. Workflow stages describe what
  Fulcrum expects to happen. Display both when they disagree.
- Use the installed protocol schema and capability checks. Do not maintain
  multiple historical wire formats or scrape unstable transcript files.

### Creation without duplicate work

Task creation is an external side effect and cannot be made atomic with a local
SQLite transaction. Record intent before sending the request, then reconcile
uncertain outcomes without creating a duplicate.

- Allocate a `dispatch_id` and persist its project, run, operation, and inputs.
- Create the desktop task without starting implementation when the runtime
  supports separate task creation and turn start. Persist the returned ID first.
- Use a supported correlation field, or a retained descriptive task name before
  starting its first turn. Names aid recovery; returned IDs remain
  authoritative.
- Persist turn-start intent before sending the initial or resumed instruction.
- On timeout, inspect supported runtime history for the recorded operation.
  Exactly one match is recoverable; multiple matches require intervention.
- Retry creation only after an authoritative rejection or proof that the
  original operation was not accepted. An empty eventually consistent list or
  elapsed time does not provide that proof.

For example, losing the connection after `thread/start` but before its response
leaves the run in `dispatch_unknown`. It retains its capacity reservation and
cannot be replaced until the controller resolves the original task.

## Controller State and Command Contracts

The controller is one Python process per host, supervised by a per-user launchd
job on macOS. It owns operational SQLite writes and accepts narrow commands from
CLI and runtime tool adapters. A process lock prevents two controllers from
dispatching the same fleet. launchd restarts a failed process; no agent
heartbeat is responsible for keeping the controller alive.

Beads remains the editable issue source of truth. SQLite stores approved scope
snapshots and execution facts, rather than a second independently editable copy
of each issue. Git commit references identify approved plan documents.

### Durable records and transactions

Use foreign keys and database constraints to protect relationships, not
agent-written ownership assertions. The minimum durable records are:

- Project enrollment: repository identity, Codex project/host, Tollgate
  identity, enabled state, review policy, and observed integration health.
- Intake operation: caller-supplied stable key, requested content, actual bead
  IDs, graph reconciliation, and outstanding publication work.
- Batch: proposed runs, exact scope snapshots, order, exclusions, approval
  evidence, and lifecycle state.
- Run and assignment: task ordering, current task, Executor ID, worktree,
  current stage, correction counters, and active holds.
- Dispatch operation: intended external call, observed outcome, and returned
  runtime identities.
- Review request and observations: exact candidate, requirements snapshot,
  canonical instructions, runtime references, and rejection reasons.
  Authoritative verified receipts and mandates live in Tollgate; SQLite stores
  their references and last observed disposition.
- Delivery obligation: candidate, promotion/push/cleanup facts, pending action,
  last observed failure, and next bounded retry time.
- Notification and scheduled job: pending decision or condition, delivery
  status, cadence, due occurrence, and current run.

Enforce these invariants transactionally:

- A bead has at most one unfinished assignment.
- A run has at most one active assignment and one outstanding turn-start intent.
- A review request has at most one accepted terminal verdict per actual turn.
- An external operation or runtime event cannot apply the same transition twice.
- There are at most five occupied work slots.
- One condition has at most one unresolved notification record.

Use SQLite WAL mode, foreign keys, and durable commits. Make short transactions;
never hold a write transaction while awaiting a model, Git, Beads, or Tollgate.
A monotonic local event sequence orders observations. It is not a format version
or an authority epoch.

Store request text and existing Git IDs directly. Do not introduce new content
hashes, schema-version fields, generated role numbers, or compatibility aliases.

### Archon binding and local administration

The controller stores one binding between Archon authority and an existing,
human-created desktop task. Authenticating a task ID does not appoint that task
as Archon. Bootstrap and replacement require a separate human decision.

```sh
fulcrum admin bind-archon --thread actual-desktop-task-id
```

This command requests a native user-presence dialog from the controller. The
controller's native LocalAuthentication component evaluates the biometric policy
in its own process and associates the callback with a single-use pending
administration operation. The CLI cannot supply the authentication result.
Cancellation or unavailable authentication leaves the binding unchanged.

- Bootstrap verifies the named task exists in the configured shared runtime,
  displays its actual title and ID, and records the authenticated binding.
- Replacement pauses new batch approvals and dispatch while the old Archon's
  active decision turn is completed or explicitly interrupted and reconciled.
- Commit the new binding atomically. Later decisions from the old task are
  rejected, even if delayed messages or an old tool call arrive afterward.
- Existing approved batches retain their scope and authority; replacement does
  not cancel them. Resume dispatch after the new binding is established.
- With no Archon binding, intake and observation work, but batch approval and
  new dispatch do not. Existing delivery recovery keeps its explicit ownership.

The same native administration channel controls enrollment and integration
configuration. It displays the requested change and records the authenticated
operation. It is a concrete local control path, not a caller-provided `human`
flag or an unaudited alternative to runtime-origin checks. See [Human-only
Override](#human-only-override) for the authentication policy and failure
behavior; administration does not grant promotion authority.

### Commands and outcomes

All commands below are target interfaces to implement. Commands return JSON on
stdout, concise diagnostics on stderr, and stable symbolic outcome codes.
Arguments identify existing records rather than permitting arbitrary state
writes.

```sh
fulcrum intake --input intake.json
fulcrum schedule propose --limit 20
fulcrum schedule approve --batch batch-27
fulcrum run --batch batch-27
fulcrum status --run run-12
fulcrum pause --run run-12 --reason "Revising acceptance criteria"
fulcrum resume --run run-12 --decision decision-9
fulcrum brief --archon
```

- `schedule propose` freezes a proposal; it grants no execution authority.
- `schedule approve` requires evidence of the current Archon's decision for that
  exact proposal. An Executor-supplied `--archon-id` cannot establish authority.
- `run` asks the controller to dispatch eligible members of an approved batch;
  it does not start a second controller or wait in an LLM turn.
- `status` and `brief` read stored observations and identify their timestamps.
  They do not initiate an expensive full-fleet scan on each call.
- `pause` creates a hold and begins reconciliation. It does not immediately
  claim that all processes are quiet.
- `resume` applies a retained scope/scheduling decision after the hold's release
  conditions are verified. It does not erase review or correction history.

Mutating runtime tools bind their caller to runtime-observed task identity.
Retain the originating tool call and decision text. Do not authenticate a role
from a field that the calling model is free to invent. Human CLI administration
uses the native authenticated channel described above; it does not impersonate
Archon.

A repeated command with the same operation ID and identical arguments returns
its previous result. Reuse with different arguments returns
`operation_conflict`. Suggested process exit codes are `0` for accepted/read
success, `2` for invalid input, `3` for a conflict or policy rejection, and `4`
for unavailable evidence. Expected waits return a successful state response, not
an error.

## Concise Intake and Approved Scope

Intake must be cheap for a small change while preserving enough information for
independent review. A standalone task does not require a planning document or an
eight-section essay.

The required content is the project, title, requested outcome, bounded scope,
and acceptance/validation. Dependencies, context references, plan association,
priority, and explicit model overrides are optional.

```json
{
  "intake_key": "fulcrum-readme-install-command",
  "project": "fulcrum",
  "title": "Correct the README installation command",
  "outcome": "The documented command matches the supported installation",
  "scope": "Only the README installation example",
  "acceptance": "Compare the example with the actual CLI help",
  "activation": "queued"
}
```

Use native Beads fields and labels. `queued` means eligible for batch proposal;
it does not authorize dispatch. `future` excludes work from proposals.
Standalone direct intake defaults to queued unless the human requests future
work. Plan intake inherits the plan's explicitly selected activation.

- `intake_key` is a stable human-readable operation identity, not a title hash.
  Reusing it finds the existing issue through its external reference.
- An identical repeated request reconciles only missing publication actions.
  Multiple existing matches are an error, not permission to choose one.
- Refinement uses an explicit update operation naming the existing issue and
  observed content. Conflicting external edits require reconciliation.
- Reconcile the full desired dependency set, including deliberate removals.
  Reject cycles and references to unknown required tasks before dispatch.
- A partially published plan graph remains ineligible. Record the intended
  issue/dependency set in the intake operation; do not infer completeness from
  the presence of one plan label.
- Commit and push approved Markdown and Beads history through their respective
  native commands. Failed remote pushes are separate obligations; successful
  local intake need not be rewritten merely to retry synchronization.

An assignment stores the exact normalized task content approved in its batch,
plus the approved plan Git commit and relevant section when there is a plan.
This immutable snapshot is what Executor and reviewer receive. It is evidence of
past authorization, not an editable alternate task tracker.

Changing active requirements creates a new approval decision. First place the
affected assignment on hold and suspend its Tollgate authorization; then attach
the replacement scope snapshot. Unrelated tasks and already-approved scopes
continue unchanged. Moving a task to `future` blocks undispatched work and
triggers reconciliation for active work; it does not undo a completed promotion.

## Batch Scheduling and Capacity

Archon approves each dispatch batch. Python performs readiness checks and
bookkeeping, but cannot add an unapproved task to a batch or infer that a
proposed scope change was accepted.

### Proposal and approval

A proposal contains at most 20 named tasks or scheduled jobs, their run
grouping, exact scope references, dependency order, resource exclusions, and
reasons for waiting. The full proposal is available for inspection before
approval.

The default ordering is deterministic:

- Recovery obligations for already-started work are serviced first and require
  no new batch unless they change the approved scope.
- Ready urgent tasks precede ordinary work by native Beads priority.
- Within a priority, prefer work unblocking more approved tasks, then older
  creation time, then stable issue ID.
- Due specialist jobs appear as low-priority proposals and do not bypass Archon.
- Archon may reorder or exclude work, recording the reason in the approved
  batch.

The proposer puts independent tasks in different runs by default. It may reuse
one run for a linear dependency chain within a project. Tasks with independent
ready branches must not be serialized merely because they share a plan label.
Archon approves the explicit run grouping along with the task set.

This example authorizes two parallel runs, with two sequential tasks in one run:

```json
{
  "batch_id": "batch-27",
  "runs": [
    {"run_id": "run-12", "tasks": ["fc-31", "fc-32"]},
    {"run_id": "run-13", "tasks": ["fc-33"]}
  ],
  "exclusions": [],
  "max_active_slots": 5
}
```

Approval binds the stored proposal contents. Editing an unapproved proposal
creates a new proposal ID; changing an approved task requires a new decision. At
each start, recheck current dependencies, activation, holds, ownership, and
project health. A newly ineligible task waits while other approved tasks
proceed. A new external prerequisite blocks affected work rather than silently
expanding the batch.

The controller requests another batch decision when eligible unapproved work
exists and its addition could use available capacity. Existing approved work
continues while Archon considers that decision. Silence is never batch approval.

### Dependencies and exclusion claims

A task dependency is satisfied only by completed required work. For a code task,
that means certification, promotion, required source synchronization, owned
cleanup, and Beads closure. A closed issue without its required delivery
evidence is not ready evidence. Cancellation does not satisfy a dependency
unless a new approved scope decision removes that prerequisite.

An exclusion is a claim on a named resource with `shared` or `exclusive` access.
Claims from different runs conflict when their resource keys match and either
claim is exclusive. There is no implicit repository-exclusive claim.

```json
{
  "run_id": "run-12",
  "claims": [
    {"resource": "host:local", "access": "shared"},
    {"resource": "repository:fulcrum", "access": "shared"},
    {"resource": "demo:local:3000", "access": "exclusive"}
  ]
}
```

- Every active assignment claims its host and project repository in shared mode
  by default. A repository-wide refactor requests exclusive repository access.
- A quiet benchmark requests exclusive host access. It starts only after other
  claims are released and native process/Tollgate observations confirm drainage.
- Additional named resources are exact keys declared in the approved batch;
  agents cannot invent a different spelling to circumvent a conflicting claim.
- Acquire the assignment's complete claim set atomically before preparing work.
  Keep claims until its delivery and owned cleanup are complete, including
  during CI waits when the model slot is released. This prevents overlapping
  operations against resources still in use.
- A held assignment retains claims while it has owned resources. Releasing
  claims requires a verified checkpoint and resource drainage, recorded
  separately from the hold; resumption reacquires them before model work begins.

For example, two ordinary Fulcrum tasks can run together because both hold
shared repository claims. An exclusive refactor waits even when those tasks are
in CI and currently consume no model slots. A task depending on either waits for
full completion, not merely the first successful test result.

### Slot ownership and waits

A **work slot** is permission for one run to have active model work. It follows
the run across implementation and review. It is not a count of open
conversations or UI busy indicators.

- Claim a slot before task creation or turn dispatch. Release it only after the
  relevant model turn is terminal or its inactivity has been reconciled.
- During review, Executor yields to native subagent waiting and the reviewer
  uses the same slot. Five waiting parents can therefore have five reviewers.
- Do not acquire a sixth slot for a reviewer. Do not permit parent code edits or
  a second concurrent review while the first review owns the slot.
- Release the slot during controller-owned CI, push retries, or a human decision
  wait once no agent turn is active. Resume corrections only after reclaiming
  it.
- An uncertain runtime state retains its reservation until reconciled. It must
  not be counted as spare capacity and trigger overlapping replacements.
- Archon's brief decision turns are outside the five execution slots; their
  usage is still measured. Scheduled specialists occupy execution slots.

Tollgate continues to admit heavy commands. Fulcrum does not add a token permit
for every shell command. Explicit exclusions cover operations such as a quiet
benchmark, a repository-wide refactor, or a shared demo service.

A run whose exclusion conflicts with active work waits in SQLite. It does not
start an Executor merely to wait for permission. Host-pressure observations are
collected once per scheduling evaluation and shared by that evaluation's tasks.

## Executor Lifecycle and Correction Limits

Executor keeps implementation context within one approved run. Each task gets a
fresh owned Tollgate worktree; subsequent fixes remain in that task's worktree.
The desktop task is created in the saved project context without independently
creating a second Codex worktree.

The controller records one primary assignment stage. Holds and outstanding
obligations are separate records so a pause cannot erase the previous stage.

- `queued`: approved but waiting for dependencies, a slot, or an exclusion.
- `preparing`: Python verifies the repository and creates the owned worktree.
- `implementing`: Executor investigates, edits, and runs proportionate checks.
- `review_pending`: an immutable candidate and complete review request exist.
- `reviewing`: the verified reviewer invocation is running.
- `correcting`: Executor addresses specific findings or a diagnosed code
  failure.
- `delivering`: Python drives candidate authorization and ordinary CI handling.
- `recovering`: promotion, source synchronization, cleanup, or integration needs
  a retained recovery action.
- `completed` or `canceled`: no further implementation is permitted under this
  assignment. Cancellation retains unfinished cleanup and synchronization work.

Expected transitions have explicit preconditions. For example:

```python
if result.kind == "ready_for_review":
    candidate = tollgate.read_candidate(result.candidate_id)
    require(candidate.source_oid == result.source_oid)
    require(candidate.owner == assignment.worktree_owner)
    require(assignment.current_scope == result.scope_id)
    set_stage("review_pending")
```

Executor returns structured outcomes naming `ready_for_review`, `blocked`, or
`checkpointed`, with relevant candidate, source, checks, and retained artifacts.
These are claims to validate against native tools; they are not direct writes to
controller state or proof of completion.

### Handoff within the Executor turn

A normal implementation turn includes reviewer invocation. It does not stop and
require a new parent turn solely to ask another agent for review.

1. Executor commits its validated source and calls the candidate helper, which
   submits without promotion authority and returns the retained candidate ID.
   Speculative CI may start immediately. Adequate local evidence allows review
   preparation without waiting for that CI to finish.
2. Executor calls `review prepare` during the same turn and receives the exact
   native spawn arguments. The controller records `review_pending`.
3. Executor invokes the Sol/high subagent and yields through native waiting.
   Runtime-observed spawn acceptance transfers its slot to `reviewing`.
4. On subagent completion, the verifier can assess the actual runtime result.
   Executor receives the native result and resumes within its existing turn.
5. For changes requested, the controller records a correction request and
   Executor performs the bounded fix. For approval, Executor requests `deliver`
   and ends its turn; Python performs delivery after observing that parent turn
   and its subagent are terminal.

`ready_for_review` is an intermediate structured report accepted by the helpers,
not a requirement to end the parent turn. A premature final response creates no
receipt; the controller may resume that parent once with the already-prepared
review action. Repeated premature exits become one workflow exception.

A concise successful sequence distinguishes the authority artifacts:

```text
Executor: submit candidate, prepare review, invoke Sol, wait
Sol: complete the exact review with an approved verdict
Verifier: validate runtime evidence; Tollgate retains the receipt
Executor: request delivery and end its turn
Controller: observe inactive agents and request authorization
Tollgate: create mandate, certify, promote, and synchronize
Controller: verify cleanup and close the bead
```

The controller consumes runtime completion events; agents do not write slot
release flags. It releases the slot only after both parent and reviewer are
inactive. Escalating the implementation model ends the current parent turn at a
checkpoint; the controller starts the next correction turn with Sol/high after
reacquiring capacity. It never changes model authority mid-turn by assumption.

### Reusing a conversation across worktrees

Reusing a run preserves useful conversation context, not the previous task's
filesystem ownership. The next task cannot start while the old task is still
editing, reviewing, or completing delivery obligations.

- Wait for parent/subagent inactivity and the previous assignment's completion.
  Ensure no persistent terminal or demo process remains rooted in its worktree.
- Create the next task's worktree through Tollgate and record its actual path,
  branch, base, owner, and new assignment ID before dispatch.
- Resume the same desktop thread with the new effective working directory and
  permitted write roots through supported runtime turn configuration. Keep its
  saved-project association, but replace the old task's writable worktree scope.
- Verify the server accepted the new directory and permissions. The first
  task-local command confirms the expected Git root before any edit. This is one
  assignment-boundary check, not a recurring progress poll.
- Supply a new task instruction block that identifies current scope and states
  that prior candidates and review decisions cannot authorize the new task.

If the shared runtime cannot change effective working directory and tool roots
safely on a resumed thread, report that capability failure and hold the run. Do
not silently continue in the old directory or claim run reuse was verified.

### Correction accounting

A **correction round** begins after a specific review finding or diagnosed code
failure requests a bounded fix. It ends when the corrected source is assessed.
The first implementation submission is not a correction round.

- Count one unsuccessful round when the requested fix remains unsatisfied.
  Combine findings for the same corrected submission into one assessment.
- Do not count duplicate messages, missing evidence, an unchanged candidate
  observation, canceled reviews, or a transient infrastructure failure.
- Share the counter across review and code-related CI corrections for the task;
  switching failure categories must not reset the limit.
- After two unsuccessful Luna rounds, resume the existing Executor as Sol/high.
  Keep a separate Sol/high review subagent; self-review is insufficient.
- After two unsuccessful Sol rounds, retain the work, release capacity when
  inactive, and send one escalation to Archon with the unresolved findings.
- Archon records a bounded new approach, changes scope with appropriate
  approval, or requests human input. It does not erase previous attempts.

A successful correction followed by another failure does not reset the task's
unsuccessful-round count. A new independently approved task starts fresh. Model
unavailability is a capability failure, not permission to choose another model.
Existing explicit user overrides take precedence; Astra is never an automatic
escalation target.

## Independent Review and Verifiable Evidence

The reviewer must assess the original requirement and actual source, rather than
merely validate Executor's explanation. A fixed reviewer profile and
runtime-backed evidence make this a reproducible operation.

### Preparing the review request

`fulcrum review prepare --assignment assignment-31 --candidate candidate-42`
creates an immutable request after validating candidate ownership, source,
scope, and required evidence. The request is retained before any reviewer is
spawned.

The canonical request contains:

- Request, assignment, project, and candidate IDs.
- The complete approved task requirements and relevant plan excerpt, with the
  plan's actual Git commit reference.
- The exact source and base commit, plus readable diff and repository access.
- Validation commands, outcomes, and links to retained logs or UI evidence.
- Fixed review instructions and the exact required model/reasoning settings.
- A strict result contract and the previous findings for a correction review.

Do not truncate required scope to meet a prompt budget. If context is too large,
provide immutable artifacts with explicit reading requirements and record their
contents. Optional background can be omitted; required material cannot be
replaced by an Executor-authored summary.

The fixed instructions include the following substance:

```text
Review the approved requirements against the exact candidate source.
Inspect relevant surrounding code and tests independently.
Look for incorrect behavior, missing cases, regressions, and scope changes.
Treat implementation explanations as claims, not conclusions.
Return the required structured verdict; missing evidence is not approval.
Do not edit the source, spawn reviewers, or authorize another candidate.
```

The first review for a task uses fresh subagent context with explicit
`gpt-5.6-sol` and `high`. Do not inherit the Executor's model or entire history.
Within a correction cycle, the same reviewer may be resumed with a canonical
follow-up containing the new source, original requirements, and prior findings.
A new task starts a fresh reviewer context.

Executor invokes the native subagent operation using the prepared request.
Instrumentation checks the requested profile and prompt before invocation when
supported. A nonconforming spawn never becomes an acceptable review just because
it returned a positive answer.

### Reviewer output

The structured verdict distinguishes completed assessment from incomplete work.
Review findings have stable IDs within the task's review cycle so correction
results can refer to the specific unresolved behavior.

```json
{
  "review_request_id": "review-18",
  "candidate_id": "candidate-42",
  "source_oid": "0123456789abcdef0123456789abcdef01234567",
  "verdict": "approved",
  "scope_assessment": "Matches the approved README-only correction",
  "checks_assessed": ["CLI help comparison"],
  "blocking_findings": [],
  "advisories": []
}
```

- Allowed verdicts are `approved`, `changes_requested`, and `incomplete`.
- Approval requires no blocking findings and an explicit scope assessment.
- A blocking finding identifies the affected behavior, location, evidence, and
  requested correction. Advisories do not prevent approval.
- An incomplete result explains missing evidence or access and cannot authorize
  promotion. One bounded evidence repair may resume review; repeated incomplete
  results become an integration exception rather than an unlimited loop.
- The verifier checks required fields and identity agreement. It does not use
  another LLM to interpret whether a prose answer probably meant approval.

Review is read-only with respect to the candidate source. Controller validation
may provide additional test evidence; the reviewer does not mutate the
Executor's worktree or operate a second build pipeline. UI changes require
relevant rendered walkthrough evidence and screenshots when appearance matters.

### Proving origin, instructions, and completion

A **review receipt** is Tollgate's retained record of the verifier's actual
matching invocation and verdict. It is not a file the Executor can manufacture
by filling in a JSON schema. `fulcrum review verify --request review-18` takes
an existing request ID, not a caller-provided verdict or asserted model name.

The verifier obtains evidence from the configured Codex runtime:

- The original parent spawn tool call and its returned reviewer identity.
- The parent/run relationship and actual reviewer turn identity.
- Server-accepted model and reasoning configuration for that turn, including any
  runtime override or rerouting. Static profile text alone is insufficient.
- The actual initial input or canonical follow-up, compared with the retained
  request. JSON key order is irrelevant; instruction and scope text must match.
- A normally completed review turn and the reviewer's actual structured output.
- The prior input history of a reused reviewer, restricted to approved requests
  and their review outputs. An unaccounted steering message invalidates the
  result.

The installed adapter must expose enough information to establish these facts.
If effective settings or accepted input cannot be observed, the result is
`unverified`; do not infer them from a parent claim or a successful spawn alone.
No synthetic receipt may stand in for unavailable runtime evidence.

Hook observations can help correlate local events. The documented subagent-stop
payload includes a subagent ID and last response, while tool hooks expose call
arguments/results. Hook coverage is incomplete and transcript paths are not a
stable format. Corroborate receipt authority through supported runtime records;
a hook-invoked script accepting arbitrary JSON stdin is not an authenticated
receipt-ingestion endpoint. [Hook contract][codex-hooks].

A candidate change during review invalidates that review for delivery. Keep the
old review as historical evidence, and prepare a new request for the new source.
The verified receipt binds an immutable requirements snapshot and candidate; it
must not be rebound to a different assignment by editing metadata.

These checks establish provenance and explicit approval. They cannot establish
that Sol performed perfect reasoning. Defect detection remains part of the
acceptance tests and model-workflow comparison.

## Promotion Enforcement

Tollgate is the final authority boundary because Executor can call its CLI or
other interfaces directly. A Fulcrum wrapper or shell-command hook alone cannot
prevent bypassing required review.

### Repository policy and verifier interface

Enrolled repositories use a service-enforced `verified_review_required` policy.
The policy follows the canonical repository identity across worktrees and CLI,
UI, and IPC entry points. It cannot be disabled by an ordinary agent command, by
omitting a Fulcrum label, or by re-registering the same repository path.

Tollgate queries a configured verifier connection with candidate identity. It
must not execute a caller-supplied verifier program or trust a caller-supplied
endpoint, receipt body, `approved_by`, or model name.

The logical verifier request and response are deliberately small:

```json
{
  "repository_id": "repo-fulcrum",
  "candidate_id": "candidate-42",
  "source_oid": "0123456789abcdef0123456789abcdef01234567"
}
```

```json
{
  "result": "verified",
  "repository_id": "repo-fulcrum",
  "candidate_id": "candidate-42",
  "source_oid": "0123456789abcdef0123456789abcdef01234567",
  "receipt_id": "receipt-18",
  "assignment_id": "assignment-31",
  "scope_id": "scope-31",
  "reviewer_thread_id": "reviewer-8",
  "reviewer_turn_id": "turn-6",
  "evidence_reference": "review-18"
}
```

The response repeats and binds the request's repository, candidate, and source
identity. Its other outcomes are `rejected`, `unverified`, and `unavailable`,
each with a stable reason and retained evidence reference. All three prevent new
authority.

Tollgate starts the verifier as its own long-lived Python child using private
stdin/stdout pipes. Its enrolled executable and runtime endpoint come from
human-authenticated installation configuration, never from the approval caller.
Use an absolute certified runtime executable, a fixed module invocation, and an
explicit environment that excludes caller-controlled Python import overrides.

- The child reads immutable review requests through read-only operational data
  access and queries Codex using its own configured runtime connection.
- Only replies on Tollgate's private child pipe are verifier results. Every
  reply correlates with an outstanding request ID and repeats its exact
  identity. Neither a worker process nor a public IPC request can submit a
  substitute reply.
- The child performs no operational SQLite writes. Tollgate stores the resulting
  receipt and mandate; the controller retrieves their observed status.
- Public `review verify` asks Tollgate to invoke this verifier and returns its
  retained result. It cannot import a purported receipt or change the
  executable.
- Child exit, malformed framing, an unexpected response, or unavailable Codex
  evidence fails closed. Restart the child and retry read-only verification;
  never replace it with a model-authored answer.

Executor and reviewer permission profiles must exclude writes to installed
control code, operational state, and authority configuration. They mutate their
owned source worktree and invoke typed controller/Tollgate APIs. The trust model
excludes a hostile administrator replacing processes or bypassing host
permission boundaries; ordinary direct CLI/API calls and forged receipt inputs
are in scope.

### Authorization and race handling

An **authorization closure** is the requested candidate plus every pending
dependency that Tollgate would authorize with it. Compute this set from native
queue state; it is not a dependency list supplied by Executor.

A valid receipt is necessary but not sufficient to authorize a candidate.
Tollgate also checks candidate state, policy, review suspension, and native
queue conditions inside its mutation boundary.

- Read the candidate and authorization closure, then obtain verifier results
  without holding a long-lived repository mutation lock.
- Reacquire the lock and recheck each exact candidate/source, required
  dependency set, policy, suspension, and cancellation state before recording
  mandates.
- If the closure changed, restart validation for the new closure. Do not grant
  partial authority based on stale observations.
- Persist the mandate, reviewed identities, and authority source with the normal
  authorization transaction. Retain verified evidence for restart recovery.
- A replay of the same authorization operation cannot authorize different code.
- Immediately before promotion, require a persisted valid mandate for the exact
  candidate. Missing authority must block even if CI already passed.

Every candidate in an authorization closure requires its own mandate. In
particular, approving a child must not automatically approve an unreviewed
pending ancestor. Reordering and retries must preserve this invariant.

Direct `tg approve <revision>` in an enrolled repository resolves or creates an
unauthorized candidate and applies the same validation. If no matching receipt
exists, return `review_required` with the candidate ID; do not silently grant
approval as part of enqueueing it.

A changed source commit creates a different candidate requiring new review. An
unchanged native validation retry may reuse the same mandate. Tollgate may
reconstruct the reviewed source onto its queue prefix without asking Sol to
review an internal tested commit; passing certification remains mandatory.

### Scope changes, pauses, and revocation

Tollgate owns the effective authorization state so that a controller pause and a
promotion cannot race through unrelated local files. Add a service operation to
suspend or revoke a candidate's mandate and acknowledge its observed
disposition.

- Before revising approved scope or allowing edits after review, the controller
  requests suspension for every affected unpromoted candidate.
- Tollgate serializes suspension with promotion. Its response states whether
  promotion already completed or whether further promotion is blocked.
- The controller does not claim the assignment is safely paused until that
  response and actual process inactivity are established.
- A scope change revokes the previous mandate and requires renewed review. A
  temporary hold can release an unchanged mandate after rechecking scope,
  candidate/source identity, and the original release conditions.
- If promotion won the race, preserve the completed result and create separately
  approved follow-up work. Do not pretend revocation undid the promotion.

For example, changing acceptance criteria while a candidate is ready must block
that candidate before the new requirements become the assignment's active scope.
A local SQLite update alone cannot provide this guarantee.

### Human-only override

The human override authorizes one exact candidate and scope without claiming a
Sol review occurred. It still requires successful Tollgate certification, source
synchronization, and cleanup.

On macOS, use native LocalAuthentication with
`deviceOwnerAuthenticationWithBiometrics`. The system can reject this policy
when biometrics are unavailable, unenrolled, or locked out. In that case the
override is unavailable and ordinary verified review remains required. [Apple
API][apple-auth].

- Tollgate displays repository, candidate, full source commit, requested scope,
  and the reason for bypassing review before requesting authentication.
- Create a fresh authentication context for that authorization; do not reuse a
  prior successful prompt as a blanket approval for later candidates.
- Evaluate authentication in the trusted native Tollgate component. Do not
  expose a public `authentication_succeeded: true` parameter or CLI `--human`
  flag.
- The native callback is tied to a service-owned single-use request expiring
  after two minutes. Recheck candidate/source and scope before consuming it.
- Cancellation, expiration, lockout, service restart, or changed source grants
  no authority. The request cannot approve dependencies not explicitly listed.
- Record human authority distinctly from reviewer authority, including exact
  scope, authentication time, and the stated reason.

Disabling the repository review policy requires the same human-authenticated
administration path. Clicking an ordinary dialog, possessing a task ID, or
asserting a human caller identity is insufficient.

## Scripted Delivery and Operational Recovery

Python performs ordinary delivery without asking a model to watch CI. The
controller uses native Tollgate status, wait, logs, and diagnosis operations and
retains concise results for any agent work that becomes necessary.

### Delivery behavior

`fulcrum deliver --assignment assignment-31` resumes delivery of the existing
candidate after review. Candidate submission and review preparation belong to
the earlier Executor helpers; delivery never repeats them. Missing prerequisites
return the specific unmet condition without creating a candidate or reviewer.

- Resolve the assignment's retained candidate, exact source, owned worktree,
  review request, and receipt. Reuse retained validation evidence; a status
  request does not trigger another build.
- Confirm the parent and reviewer are inactive before advancing authorization.
  Any speculative CI already started by submission belongs to this candidate;
  continue observing it rather than starting a second validation pipeline.
- After a verified review receipt, request authorization. Tollgate creates the
  mandate in that operation, then completes required CI and promotion. Stop
  owned demo/runtime processes at the required delivery boundary.
- Read the promotion certificate and configured remote synchronization result.
  Perform native push only when required; confirm its actual outcome.
- Verify owned cleanup through Tollgate and observed process exit. Then close
  the bead and commit/push its native Beads history.

The controller never manually advances release, pushes a worktree branch, or
turns a local test result into a Tollgate certificate. It does not stop the
shared Beads database during cleanup.

### Recoverable failures

Classify failure by the boundary needing action. A code agent should not be
started to retry an ordinary network push or to discover that CI is still
running.

- Transient read/connect failures use Python retries after 5, 15, and 60
  seconds. Continued failure opens one retained condition; reconnect attempts
  then occur at most once per minute without model turns.
- An uncertain mutating operation is reconciled before retry, regardless of the
  retry schedule. Preserve its operation ID and native command result.
- Candidate failure invokes Tollgate's `tg diagnose` operation to obtain its
  retained CI and candidate failure evidence. Permit at most one unchanged retry
  when evidence supports a transient infrastructure failure.
- A failure requiring source changes resumes Executor with the exact failure,
  relevant logs, and approved scope. New source returns through review.
- Cross-project repair, material product changes, and work outside the approved
  batch require Archon's recorded decision; they are not automatic scope growth.
- Push and cleanup failures remain obligations attached to the promoted task.
  They cannot trigger a fresh implementation run.

Keep process ownership explicit for heavy commands and demo services. A lost
Executor's worktree, source commits, candidates, and live processes must be
inventoried before replacement. Archive only after obligations have a durable
owner; never delete another run's resources based on a similar task title.

### Human steering and bounded liveness checks

The user may interrupt or steer any Executor in the desktop. Runtime events for
an input not initiated by the controller place the affected assignment on hold;
Python stops automatic advancement and reconciles outstanding candidates.

- Do not race a new controller turn against an active human-directed turn.
- Direct changes to the candidate invalidate earlier review as usual.
- Resume unchanged work after verified hold release. Changed requirements need a
  retained approval decision and new scope snapshot.
- Silence or an idle UI indicator is not completion or a crash. Check actual
  runtime state before replacement or cancellation.
- A lack of runtime activity for five minutes prompts one read-only Python
  reconciliation. Emit an exception only for unavailable state, a failed turn, a
  violated deadline, or a concrete blocked action; elapsed silence alone does
  not justify killing a healthy long-running turn.

Hooks may refresh a compact assignment summary after compaction. Remove the
handoff-reminder stop loop. Do not make agents repeatedly rewrite progress or
re-read the full fleet after every tool call.

## Scheduled Specialists and Archon Briefs

**Sage** is the scheduled workflow postmortem agent. **Inquisitor** is the
scheduled project-wide architecture reviewer. Both remain Sol/high jobs;
**Weaver** is the human-invoked planning and intake skill, without a durable
fleet identity. The old **Watchman** agent is replaced by controller
observation.

### Scheduled work

Preserve the current daily cadence: one fleet Sage occurrence per 24 hours and
one Inquisitor occurrence per enabled project, offset twelve hours from the Sage
anchor. Due work becomes a batch proposal requiring Archon approval.

- Keep a stable job key, cadence anchor, next due time, and active run
  reference.
- Allow one unfinished occurrence per job. After downtime, propose one overdue
  occurrence rather than a backlog of identical daily runs.
- Advance the next due time to the first future cadence point when the
  occurrence is completed or explicitly skipped. Failures retain the same
  occurrence.
- Specialist jobs share the five execution slots and normal exclusion rules. A
  whole-project review must identify its reviewed certified source commit.
- Sage consumes recorded measurements, failures, and review/delivery evidence.
  Interviews address a specific unanswered question and contain one bounded
  request; do not reopen every completed Executor by default.
- Findings identify the underlying problem, evidence, expected benefit, affected
  project, and acceptance criteria. Reuse stable problem keys and append
  evidence to existing findings rather than publishing duplicate work daily.
- Findings are proposals with `future` activation until Archon queues them. A
  specialist cannot change active implementation scope or authorize promotion.

If a specialist is waiting for a bounded interview, persist that wait and
release its model slot once inactive. A missing answer is reported as missing
evidence; it does not create repeated reminders or a peer waiting cycle.

### Specialist lifecycle and completion

A **specialist run** is a non-code occurrence with a retained role, approved
scope, and cadence identity. It is not an Executor assignment and does not use
the code-task worktree, review, or promotion stages. It still shares dispatch
reconciliation, capacity, resource claims, holds, and notification rules.

- Sage runs in the enrolled brain project's desktop context and reads fleet
  evidence. Inquisitor runs in its enrolled target project's desktop context
  against the recorded certified commit. Missing context blocks the occurrence;
  the controller must not select an unrelated project to make dispatch succeed.
- Dispatch a Sol/high specialist task with the occurrence ID and bounded
  instructions. Persist its actual runtime ID before starting the turn. A
  specialist is read-only against source; no new Tollgate worktree is required.
- Use stages `queued`, `running`, `waiting_for_evidence`, `publishing`, and
  `completed`, with holds retained separately. An interview wait checkpoints its
  question and current result, ends the turn, and releases the model slot.
- Return a structured result identifying the occurrence, inspected evidence and
  source commits, findings with stable problem keys, and unanswered questions.
  An explicit empty findings list is valid; an absent result is not completion.
- After observed turn completion, Python validates that result, retains the full
  report, and publishes or updates findings through intake with `future`
  activation. Record actual Beads IDs and report references on the occurrence.
- Completion requires the retained report, reconciled findings publication, and
  verified exit of owned processes. It requires no fabricated candidate, review
  mandate, or source promotion. Failed brain-history push remains a separately
  owned synchronization obligation, as with code-task Beads history.
- Publication failure remains `publishing`; retry the same occurrence and stable
  finding keys without rerunning analysis. Missing output permits one bounded
  resume for the missing result, then one exception if still unresolved.

Archive the specialist only after its outputs and any remaining synchronization
obligations have durable ownership. Advance cadence from observed completion or
an explicit skip, using the previously defined cadence rules.

### Coalescing decisions and exceptions

The controller stores the latest brief before notifying Archon. Notifications
point to that brief rather than reproducing every task event.

- Use one scheduling-decision record for the current proposed work and one
  exception record per condition key, such as `(run_id, failure_boundary)`.
- Coalesce ordinary changes for 30 seconds before the first notification. Stop
  new dispatch immediately for a safety or ownership failure; notify once
  without waiting for ordinary coalescing.
- Once a notification is accepted, further changes update its stored brief and
  do not enqueue another message until Archon acknowledges or resolves it.
- If delivery is uncertain, inspect the runtime outcome before retry. Never
  treat acceptance as evidence that Archon has made the requested decision.
- Routine task completion is visible in status and the dashboard without waking
  Archon. A completed batch or newly useful batch proposal can require a
  decision.

A brief has a default 6,000-character bound and shows at most ten actionable
entries, with total counts and a reference to the complete stored proposal.
Never silently truncate the exact set of work being approved.

```text
Decision needed: approve batch-27, 3 runs, 6 tasks.
Capacity: 2 of 5 slots occupied; 1 run waiting on CI.
Exception: run-9 needs a scope decision after two Sol corrections.
No action: 4 completed tasks; source push retry is controller-owned.
```

Weaver publishes through concise intake and needs no role/progress registration.
Its human-created desktop task retains its descriptive title. Intake completion
is observed by the controller; it does not require an Archon acknowledgment
before the planning task can finish.

## Status, Diagnostics, and Measurement

Status must explain both progress and waiting without requiring another agent to
investigate the same records. Read-only interfaces expose observed evidence and
its age, not optimistic summaries inferred from the absence of errors.

A run status includes:

- Current task, approved scope, workflow stage, hold, and next responsible
  action.
- Actual Executor/reviewer task and turn IDs, effective models, and observation
  times, with links or native navigation targets for inspection.
- Slot ownership and the reason an approved run is not currently dispatched.
- Candidate/source/tested/promoted identities, review verdict, mandate source,
  and each remaining delivery obligation.
- Correction counts, last meaningful event, and current integration failures.

Provide a bounded `fulcrum status --run ... --events 20` history and `fulcrum
review explain --request ...` that names each passed, failed, or unavailable
verification condition. The dashboard uses these same readers rather than
reimplementing ownership or completion logic.

Runtime events supply timings and usage where available. Record creation, setup,
implementation, review, correction, CI, push, cleanup, and Archon decision wait
separately. Measure dependency installation in the validation command as its own
interval; optimize it only with evidence that it matters.

- Attribute usage to actual model turns, including Archon and specialists.
- Record input, cached input, output, and reasoning tokens when exposed.
  Unavailable categories are null, not zero.
- Determine whether reported usage is cumulative or per-turn before aggregating.
  Parent/subagent totals must not be counted twice.
- Distinguish raw tokens, attributable account usage, and API-price estimates.
  API rates do not establish Codex subscription allowance consumption.
- Retain full external logs outside model context; default readers return the
  relevant result and a handle for deeper inspection.

For a six-line Markdown task, the desired observable path is one intake
operation, one batch decision, one Executor creation, one Sol review, and
scripted delivery. There should be no role-registration conversation or agent
turn whose only purpose is checking whether another agent has finished.

## Migration and Compatibility

The new controller must not coexist with a legacy scheduler owning the same
assignments. This is an explicit cutover of operational ownership, while source
history, Beads issues, approved plans, and unfinished delivery work are
preserved.

The import operation produces a read-only inventory before any ownership
changes:

- Current project mappings and Archon identity, verified against live
  integrations.
- Active Executor/Overseer tasks and their actual runtime state.
- Approved scope, worktrees, dirty files, retained source/candidates, and
  mandates.
- Outstanding source push, cleanup, brain push, holds, and scheduled
  occurrences.
- Existing human model overrides and the evidence authorizing them.

Disable new legacy dispatch before transferring ownership. Drain old assignments
where possible; otherwise checkpoint them and verify candidate disposition and
process inactivity. Do not automatically interrupt all desktop work or archive
an agent merely because its role is obsolete.

- Import existing issue and plan identities instead of recreating tasks.
- Import unfinished implementation as held until its source and scope are
  reconciled. Existing role numbers may remain in historical text but are not
  part of new scheduling identity.
- Treat old agent-written review flags as historical evidence, not verified Sol
  receipts. Unpromoted work needs a new valid review or human override.
- Import already-promoted work as delivery recovery when push or cleanup
  remains. Never require an impossible retroactive review to retry an existing
  push.
- Preserve explicit plan activation. Incomplete graph publication remains
  blocked.
- Retain the specialist cadence anchors and current occurrences. Disable the old
  Watchman heartbeat and duplicate specialist launch paths at ownership cutover.

Tollgate policy activation and controller ownership must agree before dispatch
is enabled. Previously authorized unpromoted candidates must be drained or
suspended; none may slip through without the new service check during
activation. The human-authenticated path provides a recovery route if verified
review is unavailable; it must not weaken CI or certification.

Remove obsolete agent-owned operational formats after a verified import. Do not
implement dual reads, backward-compatibility adapters, or new format versions.
Archive an inert export for diagnosis; it cannot remain a live source of truth.
A failed import leaves the new scheduler disabled and identifies exact
unresolved records. After new work has started, recovery repairs the new state
rather than restarting the legacy scheduler against the same work.

The cutover has a durable operation ID and the following recorded checkpoints.
These are recovery states of one ownership transition, not parallel operating
modes or format versions.

```text
legacy_dispatch_stopped
candidate_dispositions_verified
import_committed
enforcement_enabled
controller_active
```

- Record `legacy_dispatch_stopped` only after legacy scheduling and scheduled
  launch paths are observed disabled. No new controller dispatch is permitted.
- Record `candidate_dispositions_verified` after every inherited candidate is
  terminal or acknowledged suspended, and active resource ownership is known.
- Commit the imported assignments, holds, cadence, and obligations in one local
  transaction. An import replay uses original identities and cannot duplicate
  them.
- Enable Tollgate enforcement while controller dispatch remains disabled. Record
  `enforcement_enabled` only after the live restarted service confirms the
  policy and the candidate inventory is reconciled again.
- Set `controller_active` only after all prerequisites and the Archon binding
  are verified. This is the sole checkpoint that enables new dispatch.

After a crash, read the checkpoint and observe external state before retrying
its unfinished operation. A recorded checkpoint does not override a
contradictory live service observation. If policy was enabled before the process
died, retain it and finish reconciliation; do not reenable the legacy scheduler.
If import has not committed, the read-only export and disabled schedulers
preserve work while the same operation is retried. Operator status must name the
remaining checkpoint and affected records.

Update skills, setup, readiness, hooks, and status readers together. Readiness
must require shared runtime control and mandate enforcement, rather than a
registered Watchman or first-patrol record. Documentation and any already-landed
Weaver simplifications should be reconciled with the actual checkout, not undone
by copying an older specification.

Tollgate changes retain its native worktree, certification, installation,
restart, and health-verification requirements. Dependency changes in Fulcrum
require reinstalling locked requirements and the editable package before checks.

## Automated Validation and Adoption Criteria

Validation must prove the assembled boundaries as well as pure state
transitions. An in-memory fixture that accepts a fabricated reviewer receipt is
not evidence that a real Sol subagent granted a mandate.

### Contract and recovery tests

Use deterministic runtime and Tollgate adapters for failure injection, paired
with live integration tests that establish the source of their recorded
evidence. Test the following behaviors:

- Intake retries reuse an issue, reconcile dependency removals, and reject
  cycles, incomplete publication, duplicate identities, and conflicting
  refinements.
- Batch approval freezes exact scope; new tasks cannot join implicitly. Holds,
  future activation, dependencies, and exclusions prevent dispatch as specified.
- Five occupied parent runs can each invoke a reviewer without a sixth slot. CI
  waits release capacity; uncertain runtime ownership does not.
- Lost creation and turn-start responses reconcile without duplicate work.
  Replayed events do not repeat transitions, closure, or source push.
- Wrong model/effort, altered instructions, inherited parent history, unexpected
  reviewer steering, mismatched source, rejected findings, incomplete output,
  and interrupted turns cannot create a valid receipt.
- A parent-written receipt and a manually invoked hook payload cannot authorize
  promotion without corroborated runtime evidence.
- Direct revision approval, dependency authorization, retries, reorder, restart,
  and alternate service APIs all preserve mandatory candidate-specific
  authority.
- Scope suspension races safely with authorization and promotion. The result
  reports either effective suspension or an already-completed promotion.
- Human authentication cancellation, expiration, wrong candidate, replay, and
  unavailable biometrics cannot authorize work. A valid override still requires
  CI.
- Correction limits survive reconnect, compaction, and model upgrades without
  counting missing evidence as a code failure.
- Promoted-but-unsynchronized work resumes push/cleanup rather than
  implementation. Notification updates do not flood an unavailable or slow
  Archon.

### Comparative performance evaluation

Compare Luna-plus-Sol-review with Sol alone using identical isolated starting
commits and acceptance checks. The Sol-alone arm performs implementation and
self-checking without a second reviewer; it never bypasses production promotion
policy. Evaluate outputs before any production enrollment or promotion.

Use six representative cases with two repetitions per arm: a tiny Markdown edit,
a localized bug, a multi-file change, a UI change, a merge conflict, and a CI
repair. Record the same initial dependencies, tool availability, and environment
warmth. Alternate arm order to reduce cache and load bias.

- Freeze acceptance checks and expected behavior before running either arm.
  Evaluate final outputs independently of the workflow's own approval verdict.
- Include deliberate defect cases that a reviewer should reject. Passing build
  checks alone cannot establish equivalent review quality.
- Include failed attempts and automatic upgrades in total cost. Do not report
  only successful cheap runs and omit their failed predecessors.
- Report total cost divided by successful tasks, completion rate, observed
  defects, median elapsed time, slowest case, and per-case timings.
- Compare elapsed time from dispatch-ready task to completed task. Separately
  report intake, Archon decision waiting, native startup, and CI intervals so
  neither arm benefits from hidden setup or omitted waiting.

The adoption defaults are at least 20% lower cost per successful task, no
observed correctness regression, and median completion no more than 1.5 times
Sol alone. Report per-case regressions and failed attempts even if the aggregate
passes. The two repetitions are a pilot, not statistical proof of equivalence.

If actual cost cannot be attributed, publish raw usage and clearly labeled price
estimates; mark the financial criterion unproven instead of claiming
subscription savings. Archon presents the measured outcome for the
default-workflow decision. Do not silently change production model routing
because a small pilot passed.

Administrative acceptance additionally requires zero coordination-only model
turns for registration or status polling, no peer waiting cycles, no duplicate
dispatch, and no unchanged-condition message flood. Run repository-required
format, type, and focused integration checks for the implementation; repeat or
broaden tests only when changes or failures justify it.

## Manual QA

Manual QA uses disposable tasks and a disposable Tollgate repository with review
policy enabled. Provide diagnostic commands for runtime capability, run events,
review verification, mandate status, and pending notifications. Test-only
failure injection belongs to an isolated harness and must not be an enrollment
bypass available to production agent callers.

### Shared desktop task and genuine review

Exercise this assembled-product flow before trusting broader component results.
It crosses the desktop, Python controller, actual subagent runtime, and
Tollgate.

1. Connect the desktop and controller to the same runtime. Confirm the
   diagnostic identifies matching live task state and usable reviewer evidence.
2. Publish a small documentation task, inspect its concise Beads content, and
   approve its proposed batch through Archon.
3. Watch Python create one Executor in the desktop. Open that task and verify
   that implementation starts without registration or setup dialogue.
4. Let Executor submit a candidate and invoke one Sol/high reviewer. Inspect the
   subagent input and result, then use `fulcrum review explain` to inspect the
   model, reasoning, prompt, identity, and source checks.
5. Attempt direct candidate approval before a valid receipt exists. Expect
   `review_required`, no authority, and no promotion despite any passing CI.
6. Complete a genuine approving review. Observe Tollgate obtain verified
   evidence, grant the exact mandate, certify/promote, synchronize, clean up,
   and close Beads.
7. Confirm status names every completed obligation and retains evidence. Archon
   receives no per-stage messages; the batch-completion decision is coalesced.

### Steering, capacity, and recovery

Use the diagnostic history to distinguish waiting from model work and to verify
that recovery preserves ownership rather than creating replacement agents.

- Start five independent approved runs and arrange for all to request review.
  Each reviewer must start while its parent waits; no run waits for a sixth
  slot.
- Steer an Executor in the desktop during implementation and during review.
  Expect an affected-run hold, no overlapping controller turn, and rejection of
  any stale review after source or scope changes.
- Disconnect the controller after task creation is accepted but before its
  response is recorded. Reconnect and verify one task is reconciled, not
  recreated.
- Restart the controller during CI. Observe the existing candidate and resume
  native waiting without new implementation or repeated Archon notifications.
- Complete two tasks sequentially in one run. Confirm the desktop conversation
  is reused, the next turn has the new worktree and write roots, and its first
  Git-root check occurs before edits. The new task has independent review
  context.
- Hold two runs sharing an exclusive resource claim. Confirm the second cannot
  acquire any partial claim set or begin setup; releasing a model slot during CI
  must not release the first run's resource claims.
- Trigger two unsuccessful Luna corrections, then two Sol corrections. Verify
  the automatic model change and one retained escalation with full attempt
  history.
- Fail source push and cleanup after promotion. Verify the bead remains in
  recovery, no duplicate code work starts, and completion follows actual repair.

### Rejected authority and human override

Use the isolated harness to prepare candidate and evidence states. Ordinary
production interfaces must never accept fabricated runtime history for this
test.

- Try a wrong-model review, incomplete prompt, forged parent receipt,
  interrupted reviewer, missing effective effort, and a changed source commit.
  Each must explain its rejection without granting authority.
- Send a purported verifier response through public IPC and terminate the real
  verifier child during verification. Neither grants authority; recovery uses
  the private child connection and fresh authoritative runtime observations.
- Approve a child whose pending parent lacks a mandate. Expect the entire
  authorization closure to remain unauthorized until every required receipt
  exists.
- Race a scope hold with promotion. Confirm the UI reports either a successfully
  blocked candidate or the exact promotion that completed before suspension.
- Open the native human override for a named candidate. Cancel authentication,
  let a request expire, and change the candidate before confirming; none grants
  authority. Replaying a consumed request must fail.
- Authenticate a valid override with actual user presence. Confirm the mandate
  is visibly human-issued and still waits for required CI. Disable biometrics in
  an appropriate test environment and verify the override is unavailable.

### Scheduling, migration, and measured overhead

Finish by exercising the fleet-facing behavior that caused the original problem.
The expected result is inspectable progress with a small number of useful
decisions.

- Replace Archon through authenticated local administration while a decision is
  pending. Confirm dispatch pauses during reconciliation, delayed approval from
  the old task is rejected, and existing approved batches survive replacement.
- Interrupt cutover after each recorded checkpoint, including after enforcement
  is enabled but before its checkpoint is written. Recovery must observe live
  state, preserve imported ownership, and keep legacy dispatch disabled.
- Queue more work while Archon has an unanswered batch proposal. Confirm the
  stored brief changes but the inbox does not accumulate repeated messages.
- Make Sage and two project Inquisitors overdue. Confirm one occurrence per job,
  normal batch approval, shared capacity, and no catch-up storm or duplicate
  findings. Inspect the retained report and future findings, then fail their
  publication once; recovery must reuse the occurrence without repeating model
  analysis. An empty findings report can complete without a source candidate.
- Inspect an import containing an active legacy task and a promoted task with a
  failed push. Verify held ownership transfer for the first and delivery-only
  recovery for the second, with the old scheduler disabled before new dispatch.
- Run the tiny Markdown benchmark and inspect the timing/usage report. Confirm
  that model work is implementation and review, while setup, CI waiting, and
  bookkeeping are controller operations with separately reported elapsed time.
- Verify all task/reviewer inspection links open the correct desktop context and
  that unavailable observations are labeled unavailable rather than completed.
