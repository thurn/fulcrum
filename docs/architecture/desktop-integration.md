# Fulcrum through Codex Desktop

This design replaces Fulcrum's direct App Server integration with three standing
agents using native task tools in an unmodified public Codex Desktop installation.
**Steward** executes routine native actions, **Marshal** curates the backlog and
handles exceptions, and **Vizier** is the human's system-level interface. Weaver,
Executor, Warden, Sage, Mason, and temporary Justiciars remain worker roles.

Fulcrum's Python code selects eligible work and enforces workflow authority in
fresh CLI processes. Beads is its only durable workflow store; Tollgate owns
worktrees, validation, and promotion. A thin MCP server exposes the CLI operations.
A small Unix-socket broker retains connections, pending instruction/CI responses,
file observations, and background timers. Trusted command hooks restore context
and capture native observations. None of these transports owns a second queue.

Steward supplies the connection that background code lacks: a pending
`wait_for_instructions()` call returns an exact native action, Steward executes
it and reports the result, then waits again. While that call is pending, an event
needs no API to wake an ended task. If Steward stops, scheduled Marshal checks
provide an independent recovery path. This is supervised agent execution, not a
claim that an agent is deterministic or that Desktop guarantees bounded token use.

This document specifies the implementation; editing it does not execute a reset.
The operator has stopped all work and permits cutover at any implementation
stage. Keep admission stopped until the replacement is ready. Cutover replaces
the old instance destructively, without migration, a compatibility layer, or a
transport selector. Ordinary
editing still follows [live iteration](live-iteration.md): commit to local master,
and the next operation uses that source without installation, manual activation,
remote-publication waits, or restarting existing operations and connections.

## Roles and native action ownership

| Standing agent | Model | Responsibility |
| --- | --- | --- |
| Steward | Luna (`gpt-5.6-luna`) | Execute exact routine actions returned by Fulcrum: create, message, name, inspect, and archive tasks. Repeatedly wait for instructions. Make no scheduling, scope, or policy judgments. |
| Marshal | Sol (`gpt-5.6-sol`) | Run every 15 minutes and on exceptional failure alerts. Curate priorities, dependencies, holds, and backlog dispositions; check Steward's health; directly create scoped Justiciars. |
| Vizier | Sol (`gpt-5.6-sol`) | Receive human direction, manage system policy, and present decisions that require the human. Record authorized changes through the same CLI. |

These are the requested model choices, not permission to substitute an available
model silently. Use authorized per-role reasoning effort from configuration;
bootstrap asks only when a required setting is missing. Preserve worker model
selection and explicit per-work overrides. Validate native `model`/`thinking`
support before admission.

Steward is the normal executor of native task mutations. Workers report through
MCP and end after accepted finish; they do not create successors or message
Marshal for ordinary handoffs. Native actions belong to a named executor:

| Action | Executor |
| --- | --- |
| Routine worker creation, continuation, naming, archival, and recorded notifications | Steward |
| Justiciar creation for an escalation, including when Steward is broken | Marshal directly |
| Safe resumption of the existing stopped Steward | Marshal, or Justiciar within its assigned repair scope |
| Recovery-only notification to Vizier when the ordinary relay is unavailable | The assigned Marshal/Justiciar recovery actor |
| Initial standing tasks and schedule setup; explicit leader replacement | The authorized bootstrap/recovery caller |

The same recorded claim/result protocol applies to all these actors. Steward
never claims a Justiciar creation assigned to Marshal. The narrowly defined
best-effort failure alert below is the only notification exception when the
ledger cannot record an action. Native approvals and unavailable interruption
controls still belong to the human in Desktop.

Marshal receives scheduled triggers and exceptional failure alerts, not a
message for every new bead, freed slot, worker finish, or successful repair.
Justiciar outcomes persist through MCP; they do not require a success-message
turn in Marshal. Vizier is the system interface, not a mandatory intake hop for
every piece of work.

## Related information and observed evidence

- [Live iteration](live-iteration.md): source freshness and connection continuity.
- [Current contracts](../fulcrum2/contracts.md): existing business operations,
  source/delivery checks, attribution, and operator interfaces. Explicit changes
  in this proposal supersede conflicting requirements there.
- [Weaver workflow](weaver-workflow.md): behavioral scope and measured Beads costs.
- [Failure analysis](../fulcrum2/failure-analysis.md) and the
  [handoff-delay incident](../postmortems/2026-09-16-bead-46a5eb5e-handoff-delay.md):
  uncertain effects and why one broken bead must not stop unrelated dispatch.
- [Five-minute MCP experiment](../experiments/2026-09-16-desktop-mcp-ci-wait.md):
  actual blocking-wait evidence and retained probe locators.
- [Setup](../setup.md), [validation](../validation.md), and official
  [MCP][mcp-doc], [scheduling][schedule-doc], and [hook][hooks-doc] documentation.
- Inspected Beads [update command][bd-update] and [Dolt implementation][bd-storage]:
  the single-issue transaction used by the commit protocol.

[mcp-doc]: https://learn.chatgpt.com/docs/extend/mcp
[schedule-doc]: https://learn.chatgpt.com/docs/automations?surface=app
[hooks-doc]: https://learn.chatgpt.com/docs/hooks
[bd-update]: https://github.com/steveyegge/beads/blob/6c124203e771/cmd/bd/update.go
[bd-storage]: https://github.com/steveyegge/beads/blob/6c124203e771/internal/storage/dolt/issues.go#L135-L184

Inspection on 2026-09-16 observed Desktop build 26.908.70816 (9275) and Beads
1.2.2 at commit `6c124203e771`. These identify observations, not runtime selectors.
The bundled native tools initially lacked `CODEX_APP_TOOLS_PIPE_PATH`; they became
callable after reboot. This records a session failure, not a general reboot remedy.

A disposable native task made one MCP call that returned synthetic CI failure
in 300.002 seconds. It produced no additional model responses or generated tokens
during the wait, and no wrapper wait/resume calls. Its whole turn used 175 output
tokens, with input/cache overhead separately recorded in the experiment. This
establishes that configured five-minute path, not an unlimited wait or zero total
cost. Implement the specified loop and recovery behavior directly; no additional
exploratory probe phase is required. Validate the completed implementation through
the focused acceptance checks below, recording real gaps without reopening the
architecture as an investigation task.

Bootstrap requires callable `create_thread`, `send_message_to_thread`,
`list_projects`, `list_threads`, `read_thread`, `set_thread_title`,
`set_thread_archived`, and `automation_update`, plus Fulcrum MCP. Check actual
fields and returned shapes. Schemas establish expressibility, not end-to-end
success. Verify trusted hooks and the scoped transcript observation path below.
No internal Desktop pipe, database, WebSocket, App Server endpoint, or GUI
workaround may compensate for a missing capability.

### Resulting capabilities and limits

| Capability | Mechanism and limitation |
| --- | --- |
| Background event reaches native task tools | Complete Steward's pending MCP response; Steward executes the action. A stopped Steward waits for independent recovery. No unsupported background native API is assumed. |
| Dispatch and continuation | Native create/send with exact recorded prompt, model, effort, project, and identity. Creation starts its initial turn; registration gates substantive work. |
| Busy Marshal notification | Schedule and failure messages may arrive during active work. Beads coalescing, one decision operation, and stale-update checks protect workflow state; pre-injection gating is not required. |
| Completion and output | Correlated lifecycle records from registered transcripts, plus scoped native inspection through the relay when needed. Validate target-stock evidence; summaries and Stop alone are insufficient. |
| Workspaces | Saved-project local tasks operate using verified absolute Tollgate worktree paths. Native cwd and UI project may differ from the assigned worktree. |
| Naming and archival | Exact role titles and native IDs; defer archival until all obligations settle. Manual unarchive suppresses automatic rearchive. |
| Interrupt, answer approvals, delete tasks, inspect arbitrary native terminals | No established native task-tool equivalent for these controls. Preserve exact blockers and human task links; do not invent success. |
| Usage and estimated dollars | Scoped transcript collector feeds the existing accounting contracts. Missing evidence is partial/unknown, never free. |
| Execution enforcement | CLI rejects unauthorized transitions. Hooks assist covered calls; prompts and supervision govern the agent. No guaranteed forced exit or universal token ceiling is claimed. |

## Intake, planning, and deliberate feature cuts

New requests enter through `$weaver`. One exception remains: `$bead` files a
small, understood incidental follow-up through `fulcrum report` or MCP `report`
without changing the reporting task's role, name, binding, or assignment. Retain
problem, evidence, required change, acceptance checks, dependencies, and origin.
Deduplicate exact retries by `report_key`; reject changed input under that key.
Return the actual bead ID and filing state without a native handoff by the caller.
An implementation-ready report may enter ready backlog after deterministic
validation; incomplete scope remains unready until a human invokes `$weaver` in
the task where the request was made. Automatic selection never creates a Weaver.

Weaver's accepted `ready` outcome makes ordinary work eligible for automatic
selection, subject to current holds, dependencies, capacity, and source/overlap
rules. No per-bead Marshal approval or immediate Marshal wake is required. Marshal
may remain the recorded backlog steward for accountability; that assignee is not
an approval gate. There is no special small-backlog threshold: the same mechanism
works whether there are two beads or two hundred. Explicit future plans and
human/Vizier scope-approval requirements still need their recorded authorization.

Keep plan drafting, approved scope, stable child keys, refinement, dependencies,
future activation, and mechanical parent completion. There is no independent
plan-review subsystem and no delegated Weaver. The human-invoked Weaver owns its
investigation and scope until it finishes. There are no dedicated `plan review`
commands, review-task registrations, perspective-specific receipts,
review-capacity reservations, or waiver gates.

Other explicit cuts:

- Remove automatic brain/ledger and configuration publication: no five-minute
  cadence, startup/shutdown flushes, or autonomous publication retries. Keep
  explicit `ledger sync`, `ledger status`, and `config sync`. Pending remote
  changes remain visible. Required project source synchronization and explicitly
  requested plan publication retain their existing semantics.
- Remove curated memory commands, records, context injection, and memory export.
  Current assignment context and approved plans still come from Beads.
- Remove `fleet replace` and its drain/interrupt machinery. Preserve normal
  same-task repair and explicit standing-task recovery. A permanently missing
  worker or Steward is an operator blocker, not automatic replacement authority.
- Remove other direct terminal intake. Watchers observe existing work and never
  create assignments or make scheduling judgments.

Preserve useful inspection and explicit operator operations (`status`, `doctor`,
`trace`, bounded logs, operation inspection/reconciliation, configuration, plans,
and delivery). Replace unavailable native-control operations with precise
unsupported results and recovery steps. Neither a removed feature nor its old
command may remain an implicit bootstrap or finish prerequisite.

## Shared backlog and atomic transitions

Marshal curation and Steward dispatch are independent processes over the same
Beads state. Steward holds no private queue, and Marshal need not be present for
routine selection. Python deterministically reads readiness, recorded priority,
dependencies, holds, overlap/resource exclusions, and available capacity. Break
otherwise equal priority by stable bead ID. Marshal supplies judgment by updating
those facts; Steward cannot reinterpret them or invent scope or settings.

All Fulcrum mutation paths use the existing process-shared writer lock. This
coordinates cooperating local processes, not arbitrary writes by another process
using the same OS account. Direct edits to reserved workflow metadata are
break-glass operations. Unexpected assignee/ownership conflicts fence that bead.

A work bead owns its phase, assignment, accepted outcomes, action/reservation
state, and event progress. `fc-system` owns standing identities, infrastructure
operations, schedule binding, Marshal decision ownership, and recovery/delivery
coordination. YAML remains authoritative for configuration. Task summaries and
historical receipts are projections, not an alternate state authority. Only the
human/Vizier may change model, capacity, or system-policy configuration; Marshal
curates work within those rules, including the explicitly granted recovery slot.

### Commit algorithm

1. Acquire operation/resource ownership and the shared writer lock. Read fresh
   authoritative state, current policy, and the retained request.
2. Return the saved result for equal input under the same request ID; reject
   changed input. Validate actor, assignment, authority, and relevant conditions.
3. Compute one replacement work state, including action and capacity reservation
   when selecting work. Commit metadata, assignee, and status with one ordinary
   `bd update`; inspect an uncertain response before another write.
4. Release the lock, signal the broker, and return the recorded result. Lost
   notification does not erase the committed obligation.
5. Materialize task/history projections and retain the authoritative transition
   until those copies are verified. Projection failure cannot undo accepted work.

The inspected Beads update supports one issue transaction. Labels, dependencies,
other issues, filesystem operations, and native calls are not part of it. Retain
multi-record plans and recover each boundary; do not claim `bd batch` or a sequence
of updates is an atomic transaction. No raw SQL or Beads fork is introduced.

Never hold the shared lock during model reasoning, native/network calls, or
pending MCP responses. Beads mutation children inherit the lock descriptor until
they exit, so killing a parent cannot expose a still-running stale writer. Kernel
locks are not unlinked. An orphan holding the lock is a storage blocker, not
permission to bypass it. Independent operations can continue when an unrelated
bead has failed, except when the shared storage service itself is unavailable.

Marshal's decision applies targeted changes against fresh state. If relevant
scope/ownership facts changed during reasoning, reject the stale row and preserve
independent accepted rows. Never write back an unchecked whole-backlog snapshot.
Selection and curation serialize at the short commit boundary: selection sees
before or after an update, never half a reservation. Before a native claim,
revalidate readiness, pause, scope, ownership, and capacity. Supersede an unissued
obsolete action; do not pretend reprioritization cancels an in-flight native call.

Weaver ready commits its scope/acceptance, backlog readiness, completed authoring
responsibility, and pending mechanical work together. There is no pending Marshal
approval/wake inserted merely to make ready work runnable. Existing explicit
approval holds remain visible and cannot be removed by this transition.

### Events and durable recovery

Persist meaningful worker, hook, or provider observations on the owning bead
before acknowledging them. Prefer native event IDs; for snapshot-only providers,
compare the actual values under the lock and allocate a local event ID/sequence
with a changed snapshot. Do not hash snapshots or order opaque native IDs.

Process each bead's accepted events in order and commit the consumed cursor with
its resulting effects/actions. There is no global durable queue cursor. Discovery
covers all relevant pages, including closed beads with outstanding cleanup,
archival, or reporting obligations. Broker ordering is only a wake hint.
Unchanged observations, timer ticks, request replays, and empty wakeups do not
create workflow events or model work. Coalesce hints, never outcomes or uncertain
effects. A failed event blocks its own bead, not discovery of unrelated work.

Retain pending actions and deduplication evidence across restarts. After verified
projection, completed transition entries can be removed from the work bead. At
128 unprojected completed entries, stop new transitions on that bead and report
a storage blocker; never prune unresolved evidence just to fit a size limit.

## Native actions, identity, and delivery

An action has an immutable ID, owning record, assigned executor, tool, exact
arguments, expected result, and reporting instructions. Creation/continuation
also identifies the worker assignment. The compiler uses actual native argument
names, including `thinking`, and includes authorized model/effort settings where
the tool accepts them. Full prompts are retained without ellipses. Agents execute
the supplied action rather than constructing a request from explanatory prose.

Action state is `pending`, `issuing`, `succeeded`, `rejected`, `uncertain`, or
`superseded`. `claim_action` records actor, attempt ID, authority, and expected
result before authorizing one native invocation. At most one invocation can be
claimed for that attempt. A repeated issuing claim returns its retained state,
not permission to send again. Hook and agent result reports converge on the same
idempotent transition. A timeout or error with ambiguous external effects remains
uncertain; only definite rejection or reconciled absence permits a new attempt.

Steward executes one returned action at a time and reports before asking for the
next. It can perform many actions within one native turn. Persisting an uncertain
result releases Steward to process unrelated eligible actions; it does not free
that action's reservation or authorize a retry. Marshal and Justiciar may execute
their own separately assigned recovery actions concurrently, under the same locks
and ownership checks. Action claims are not a universal native idempotency key.

Native prompts begin with a structured line:

```text
Fulcrum-Action: {"instance":"/absolute/instance","record_id":"owning-bead","action_id":"action-id"}
```

Creation and renewed assignments also carry `assignment_token`. Attempt IDs live
on invocation records, so retries do not rewrite an action's fixed prompt. Match
the marker to the exact retained action and intended recipient before accepting
it. Titles, arbitrary prose, or markers from another instance grant no authority.
Schedule prompts use their retained schedule/instance binding, not a fresh action
ID on every clock tick. A repeated trigger means inspect current state.

### Creation and registration

`create_thread` starts the initial prompt. Require registration before substantive
worker work; the prompt contains instance, work, role, action, assignment token,
workspace, model/effort, scope, and reporting instructions. Hooks and the creator
report returned `threadId`/`hostId` through the same binding transition. Retain
`clientThreadId` as a pending setup locator, never pass it where `threadId` is
required. A new worker may register before the creator receives its reply.

Registration uses supported native identity checked against hook/assignment
evidence, not a guessed title. Prefer the exact prompt-hook observation. When the
native creation path starts the task without emitting that initial callback, a
claimed and successfully settled `create_thread` result is equivalent creation
evidence only when its returned `threadId` matches the registering runtime task.
Validate exact action, role, project/host, and workspace. Only one native task can
acquire an assignment. A duplicate claimant records a conflict and receives no
editing authority. Duplicate native tasks may exist after uncertain creation;
they must not become duplicate authorized writers.

For lost creation replies, inspect retained results and self-registration first,
then authorize a bounded inventory/history read for the exact marker/locator.
A truncated recent list or matching title cannot prove absence or justify a new
creation. If evidence remains ambiguous, escalate that action and keep its slot
reserved. Do not stall Steward's entire queue behind it.

### Notifications and work continuations

Marshal accepts schedule and failure notifications even while active. Fulcrum
coalesces incidents and admits one active decision operation for the bound
Marshal; subsequent prompts refer to that operation or recompute after it settles.
A notification cannot itself authorize work, clear an incident, reset a retry
budget, or make stale decisions valid. Queueing versus steering behavior must be
observed in Desktop, but prevention of every busy-task prompt is not a correctness
requirement. Native scheduling does not need an unproven pre-injection gate.

Recorded outgoing notifications still have one unresolved delivery per target
and purpose. Merge new evidence before issuance, retain it separately afterward,
and inspect an uncertain send before retrying. A recipient acknowledgment or
matching native result proves delivery, not that the requested work occurred.
Duplicate scheduled/diagnostic prompts are harmless hints to the application,
not assumed exactly-once messages.

Worker continuations are stronger than notifications. Acquire fresh assignment
and capacity when needed, settle the old ownership, and confirm the relevant
native turn ended before authorizing new source work. Messages can arrive or
steer unexpectedly; registration and fresh assignment checks must still reject
concurrent or stale editing. Routine progress stays in Beads or the worker's
eligible MCP response; do not send repeated status prompts to active workers.

Observed exact state can settle rename/archive effects, but disappearance from a
recent list cannot. One native inspection under a recorded evidence request is
permitted; status-polling agent loops are not. New evidence, a scheduled Marshal
health check, or explicit recovery can justify another scoped inspection. Native
`wait_thread`/`wait_threads` loops are not the production monitoring mechanism.

## Steward instruction loop

Only the current registered Steward may hold the routine execution lease and
call `wait_for_instructions(request_id)`. The lease identifies the task/session
and active loop acquisition. It is not a timer that authorizes a second relay.
At most one newly granted action and one instruction request may belong to that
loop; instruction and claim admission reject concurrent callers. Older uncertain
attempts retain their own reservations while unrelated actions remain eligible.
A recovery actor cannot revoke an in-flight call merely by expiring a lease.

The MCP tool forwards to a bounded fresh CLI operation. That operation first
reconciles prior returned instructions/results, reads the current backlog, and
returns one eligible action, an explicit recovery disposition, or a wait
registration. On a wait, the policy process and all state/resource locks exit.
The MCP transport and broker retain the pending response, not a policy process
or a source snapshot executing business logic for hours.

A committed event or changed eligibility wakes a fresh CLI computation using
current local master. It checks the registered request and current lease/state,
selects and reserves an action, persists the response identity, and resolves the
pending MCP call. Serialize registration with the eligibility check so an event
cannot fall between checking for work and beginning to wait. Recheck after
registration and reconstruct pending obligations after broker restart. A lost
response replays the same instruction/action, not a second selection.

The operating prompt is deliberately small:

```text
Register as the existing Steward and recover any outstanding instruction/result.
Call wait_for_instructions. Execute only its exact authorized native action:
claim it, invoke it once, and report the actual result. Then wait again.
Do not choose priorities, invent prompts, retry uncertain effects, or poll tasks.
On an explicit stop, end. On an unrecoverable connection/protocol failure,
attempt the permitted failure alert once and end rather than spin.
```

A healthy idle call stays pending; it does not return `pending` every few seconds.
Provider/file observation and timer handling are software work. There should be
no repeated model generation attributable to an unchanged idle interval. Each
action, tool discovery, context processing, and result report still has overhead;
measure it for Luna rather than describing the relay as free or deterministic.

Use a 3,600-second instruction idle deadline and a 1,800-second CI deadline,
configured in YAML. Set the MCP tool timeout to 3,900 seconds and request a
wrapper lifetime beyond the applicable application deadline by at least 60
seconds. These are implementation defaults, not measured Desktop guarantees.
An instruction idle expiry returns `stop` with reason `idle_deadline`; Steward
ends without a failure alert and the next scheduled Marshal check may resume it.
Recovery targets the next 15-minute check; actual delay depends on Desktop and
machine availability as described below.
An earlier transport failure follows the diagnostic failure path. Preserve the
pending obligation in either case; never renew through a short model polling
loop. Unsupported lifetimes are explicit runtime blockers, not permission to
silently shorten the budget or add a new transport.

A long wait is not a missed-progress failure for Steward. Health includes a
registered pending call, its connection state, and no unresolved protocol error,
not a periodic agent heartbeat. Desktop closing, a broken MCP channel, or a
native turn ending can stop the relay. Background code cannot send native tools
on its behalf; scheduled Marshal is the independent restart path. There is no
fourth standing recovery agent or self-message loop.

## Marshal schedule, curation, and recovery

Bootstrap creates one recurring heartbeat on the existing Marshal task, every
15 minutes. It is not a new task per run. The saved prompt asks Marshal to call
`marshal_check`, handle the resulting bounded brief/recovery work, and end when
that work is settled or blocked. No-op runs end quietly. Desktop/machine
availability affects actual timing; 15 minutes is a target recovery cadence,
not a guaranteed SLA or a claim that scheduled model invocations cost nothing.

`marshal_check` reads current backlog, policy, active decision operation,
incidents, and Steward health. It returns one compact purpose-specific brief
with omitted counts and continuation references, retaining the existing bounded
brief/context conventions. Urgent recovery precedes ordinary grooming. Marshal
may examine relevant evidence and apply independent curation decisions, but does
not monitor every worker or wait for CI/Justiciar completion. It ends when its
bounded work is done; there is no universal one-native-action-per-turn allowance.
Heartbeat health distinguishes delivery from successful settlement: a delivered
cycle remains running until `marshal_decide` completes its exact decision, and
only that completion advances the loop's last-success timestamp.

Only the current bound Marshal may own its decision operation. Record the exact
native turn and accepted input; overlapping triggers join or defer to that
operation. Validate current authority on writes and merge results against fresh
facts. An abandoned decision requires recovery, not an automatic second writer
based on lease expiry. Busy-task notifications may queue or steer the agent; they
must not duplicate action claims, Justiciar creation, or backlog updates.

If the prior Marshal turn is positively complete, its next scheduled turn can
reconcile and resume the same abandoned decision operation under a new recorded
turn binding. This is recovery of that operation, not another concurrent curator;
retain its accepted rows and uncertain native effects before applying more work.

### Steward health

| Observation | Marshal action |
| --- | --- |
| Healthy instruction wait, active authorized call, or expected pause | Leave Steward alone; no wake or progress prompt. |
| Positively ended Steward, no conflicting relay, and recoverable outstanding effects | Reconcile the retained instruction/action, then directly resume the same Steward task under a recorded recovery action. |
| Stuck, contradictory, or unknown native state; unresolved effect that code cannot reconcile | Retain the incident and directly create a scoped Justiciar when authorized and capacity permits. Do not start a competing relay. |
| Native interruption/approval or changed human intent is required | Present the exact task and decision through Vizier; do not invent a control API. |

After repairing Steward or its infrastructure, the assigned Justiciar may
reconcile outstanding effects and directly resume the same positively stopped
Steward without another Marshal decision. Its scope must authorize that recovery;
recovery permission cannot duplicate the existing relay or replace its task.
A healthy Steward notices repaired state through its next instruction operation
and does not need a native restart. Ordinary source edits never trigger recovery
or a broker/controller restart.

### Failures and bounded escalation

Understood transient failures use the existing bounded retry policy in code,
only where retry safety is established. Normal CI failures use the worker repair
allowance below. They do not individually wake Marshal. Escalate exhausted
repairs, unresolved native effects, ownership conflicts, and stuck workers that
require judgment or intervention. A failed bead remains isolated where possible.

Persist an incident on the relevant work/control record with evidence, scope,
required decision, notification state, and any recovery action. Repeated evidence
updates the same unresolved incident, not a new alert/Justiciar. Successful
recovery settles it through MCP; routine success does not wake Marshal. Marshal's
scheduled run also discovers retained incidents if an alert was missed.

When Beads is available, Steward receives a recorded exceptional-alert action
through its instruction protocol and sends it to Marshal. If Fulcrum MCP/CLI or
Beads itself is unavailable, Steward may instead send one best-effort diagnostic
message directly to the already-known Marshal task, then stop. Bootstrap supplies
that exact target and this narrow authority. No work creation, uncertain-action
retry, policy change, or ownership transfer is allowed through this exception.

That message may have no durable receipt, and a crash can duplicate diagnostics;
report this honestly. Do not loop attempting delivery. If native messaging also
fails, the scheduled Marshal check remains the recovery route. Diagnostic alerts
are not worker-creation authority. Preserve accepted local work without a second
emergency queue; restore ledger-backed reconciliation before routine dispatch.

If Beads is down, Marshal first uses any already-authorized, deterministic
bootstrap repair for the exact owned infrastructure. It cannot invent an
unrecorded Justiciar creation when no durable recovery action can be authorized
or recovered. If storage cannot be restored that way, surface the human
prerequisite; the diagnostic escape hatch does not promise autonomous repair of
every failure in the ledger itself.

## Capacity, repair allowances, and human decisions

Routine reservations use configured global and per-project worker limits. Count
reserved, issuing, active, and uncertain assignments from authoritative work
records, not a separate counter or task-summary projection. The three standing
agents are coordination overhead, not ordinary worker slots. A crash after a
reservation consumes capacity until that reservation is settled; no missing
native ID or expired timeout frees it automatically.

Executor-to-Warden handoff retains its logical work slot, with Warden admitted
only after the old assignment has relinquished source-writing authority and its
native turn has ended. Warden retains the slot while its MCP CI wait is pending.
After accepted final outcome and native completion, pending promotion, source
synchronization, cleanup, and archival retain their obligations/workspace but
need not occupy worker capacity. Post-finish repair reacquires capacity for a
fresh assignment in the same native task.

### One additional recovery slot

Marshal may reserve one instance-wide Justiciar slot beyond ordinary global and
per-project worker caps. This additional slot is exclusively for exceptional
recovery; it cannot dispatch routine work or grow with the number of incidents.
Further Justiciar requests wait until it is released. Reserve before Marshal's
native creation, including when creation becomes uncertain, and release only
after accepted outcome and observed native completion or explicit settlement.

Recovery capacity does not bypass source ownership, pause, permissions, or the
assigned recovery scope. Before a Justiciar mutates a worker's worktree, stop or
settle the conflicting writer and verify the actual boundary. A worker blocked
in CI is still an active assignment; capacity alone is not takeover authority.
An unavailable native interruption may require human action. Justiciar may
repair non-conflicting infrastructure while preserving other active operations.

### Three ordinary repairs, then one Justiciar intervention

Allow at most three unsuccessful automatic worker repair cycles per bead. The
original implementation and initial failure do not count. Grant a cycle against
retained failure evidence, within the active pre-finish Warden assignment or a
fresh post-finish assignment. Executor/Warden handoffs belong to that same cycle.
Count one failure when the attempted correction ends unsuccessfully according
to provider/validation evidence or the worker's explicit unsuccessful outcome.

Local edits/test iterations, unchanged CI status, capacity waits, native message
retries, and transport errors are not additional repair cycles. Unknown outcomes
remain unresolved. Restart, compaction, changing source, switching roles, or
renaming/reopening the incident does not replenish the allowance.

After the third failed ordinary cycle, retain a scoped repair hold and escalate
to Marshal instead of another ordinary repair. An active Warden receives the
hold through MCP, reports blocked, and ends. Its eventual completion frees the
ordinary slot without deleting its tasks or worktree. Marshal may authorize and
directly create **one** Justiciar intervention for that unresolved incident.

The intervention records its incident, scope, current source, permitted repairs,
and verification before creation. It does not reset the ordinary repair budget
or silently reduce acceptance criteria. Justiciar may diagnose independently and
repair the cause, including direct tools within granted scope when Fulcrum is
broken; preserve evidence and reconcile actual effects afterward. Any required
verification/review continuation is part of that retained intervention, not a
new allowance for three more substantive corrections. Keep exact-source review,
CI, and promotion requirements unless the human explicitly changes them.

If that intervention fails, cannot proceed without unavailable human controls,
or needs a change to the intended outcome, hold the affected work and escalate
to Vizier. Do not manufacture another incident identity to create another
Justiciar for the same unresolved failure. Infrastructure incidents that bypass
ordinary worker repairs likewise get one scoped intervention before human
escalation. Successful resolution records the facts establishing the old incident
is resolved; genuinely new failures retain their relationship to prior history.

Only an explicit human decision can authorize further attempts, revise scope,
or choose a terminal disposition after this limit. Record that decision against
the exact blocker and stable request ID; an additional ordinary allowance names
its positive `additional_repair_cycles`. Marshal cannot grant itself another
Justiciar attempt, and Vizier cannot infer permission from general policy
ownership. Generic resume or a source commit cannot clear these holds.

### Vizier decision inbox

Persist the exact question, evidence, affected source/tasks, and required human
action on the owning bead. Steward normally delivers a recorded notice to the
existing Vizier; an assigned recovery actor may deliver it directly when the
relay is unavailable. Native approvals and interruption still require action in
the original Desktop task. A Vizier response is not proof that Desktop accepted
an approval or stopped a process.

Coalesce unchanged notices by incident and material decision revision. Delivery
or acknowledgment does not resolve the blocker. Vizier rereads current state
before presenting a request or recording a response, so delayed notices cannot
reopen a resolved incident. Preserve human conversations and retain later notices
in Beads when delivery is uncertain or inappropriate. No strict universal
pre-injection gate is claimed. A missing Vizier is a visible operator problem,
not permission to create another inbox automatically.

## Hooks, context, and observation

Require exactly five trusted command hooks on the selected installation:
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`. Do not
install an Interrupt hook. Transcript `turn_aborted` evidence and transport
disconnects are the authoritative interruption path. The hooks provide
context and evidence and help catch protocol mistakes. They are not a security
boundary against another same-user process or a guarantee that a model stops.
Known broken configuration closes affected new admission; one missed result hook
leaves its action recoverable through other evidence rather than globally
stopping unrelated work.

Extend `install_hook_config` and replace the existing compaction-only handler.
Use stable absolute source-following launcher arguments:
`fulcrum hook handle --instance ABSOLUTE_PATH --input -`. Decode native JSON from
stdin, return event-specific hook JSON on stdout, and put diagnostics on stderr.
Never execute strings from hook input as shell commands. Preserve unrelated
hooks and other instances. User-level routing covers projectless standing tasks
and saved-project workers; match startup/resume/clear/compact where supported.

Use ten-second hook deadlines with at most 2,000 tokens of context. Verify the
actual supported behavior. Interruption collection must not delay or veto the
user's Stop. Detached writes
can finish after a hook deadline; only their verified Beads commit counts as
accepted evidence. Setup/trust changes require the supported native flow;
ordinary handler edits require neither reinstall nor re-trust.

| Event | Responsibility |
| --- | --- |
| `SessionStart` | Restore current role, assignment/workspace, scope, pause state, and outstanding protocol state. Steward recovers its loop/action; Marshal its current decision/incident. No curated memory. |
| `UserPromptSubmit` | Capture native turn identity and validate action/schedule markers. Coalesce Marshal notifications against current state; a prompt does not grant work authority. |
| `PreToolUse` | Correlate admission calls and native invocation IDs. On covered paths, reject unclaimed native effects, changed arguments, or calls outside assigned authority. |
| `PostToolUse` | Normalize and persist claimed native results through the same validator as agent reports. Capture transcript locators and signal fresh reconciliation. |
| `Stop` | Record an exit attempt and any missing worker outcome. Do not infer completion or force a healthy Steward to exit after an action. |

Only bound instances/tasks receive role context. Unknown tasks are untouched,
except for prospective identity evidence tied to an exact retained creation
marker or registration invocation. `register_worker` still validates ownership
and workspace before edits. For a newly created worker, it derives task, session,
turn, and host identity from the retained successful creation result when the
first registration call omits them; the returned task binding is required on
later worker calls. Incidental `report` filing needs no managed binding.
Raw intake, tool output, and arbitrary markers remain data, not developer policy.
After accepted worker finish, restored context permits reporting/reconciliation,
not renewed editing without a fresh assignment.

Correlate session/turn/tool-use/event identity and stable request IDs. Equal
callbacks return retained acceptance; conflicting input produces a diagnostic
conflict. Late evidence can settle its original attempt but cannot overwrite a
newer assignment or current native turn. Keep one unconsumed admission handshake
per actor/request until consumed or explicitly settled; context reads need no
new event. Retain unresolved mutation evidence independently of handshake data.

For managed admission, require supported session identity and matching recorded
invocation evidence; a missing required observation returns a prerequisite.
Reports, exact result replay, read-only diagnostics, pause, and authorized
recovery remain available. Validate a native pre-hook against the claimed tool,
arguments, actor, and pause/scope conditions. A denied hook response is not proof
that a native effect never occurred. Post-hook and agent reports converge; an
unknown result shape stays uncertain rather than becoming success.

### Cooperative execution limits

Keep Steward's prompt focused on the relay loop, and worker prompts on assigned
work. Discourage native status-wait loops, speculative retries, and unrelated
investigation by Steward. A registered pending instruction call and Warden's CI
call are intentional blocking operations, not prohibited monitoring loops.
Application checks reject duplicate claims, unauthorized transitions, and stale
assignments even if a prompt or hook is missed.

Steward and Warden long MCP waits run inside one long-yield `functions.exec` cell.
Its yield duration covers the MCP timeout, and the agent must not poll the cell with
`functions.wait`; every such poll is another model sample with the full cached
context. If the native turn is stopped, the closed client connection cancels the
broker evaluation immediately, while transcript terminal evidence reconciles the
durable wait. This keeps the broker-held connection model without spending model
turns merely to keep it parked.

Remove the old universal send-report-end allowance and the requirement to prove
runtime-enforced termination across every tool, shell, nested call, or hook
failure. Rejecting a workflow operation cannot guarantee a malfunctioning model
stops generating tokens. Supervision, diagnostic evidence, and human intervention
cover that residual risk; do not present prompt compliance as hard enforcement.

An optional single Stop correction may ask a pre-finish worker to submit its
missing outcome, respecting `stop_hook_active`, pause, human input, and the same
assignment's correction budget. It may not reopen accepted work or replenish
repair limits. Never use repeated Stop continuations to sustain Steward or
Marshal. If Steward exits, record the obligation for independent recovery.
`PermissionRequest`, `SessionEnd`, and subagent hooks are not prerequisites;
Fulcrum does not auto-answer native approvals.

### Native completion and transcript recovery

Use the hook-supplied transcript of each registered task for scoped lifecycle
observation as well as accounting. The inspected local transcript contains
turn-context IDs and native task-start/task-complete records. Target-stock
acceptance must establish their actual shape, turn identity, finality, and
ordering through interruption, compaction, and restart. This format is unstable;
a usage record, Stop callback, final-text snippet, or missing recent-list entry
is not interchangeable with terminal evidence.

The broker watches registered file changes and invokes fresh bounded collectors.
Persist normalized observations/cursors in Beads, tolerate incomplete trailing
lines, and replay safely after missed notifications or restart. Observe exact
native task/host/turn identity, not lexicographic ordering of opaque IDs. A late
old completion cannot finish a newer turn or authorize new work over a live one.
Do not scan unrelated transcripts or use a private Desktop database.

When lifecycle evidence needs corroboration, Steward may execute an explicitly
requested bounded `read_thread`/inventory action; Marshal may inspect Steward
under its scheduled health check. Validate the actual returned fields. Neither
summary truncation nor absence proves termination. There is no dependency on a
new unsupported background native subscription API, and no agent status loop is
introduced to compensate for missing completion evidence.

Before source handoff, capacity release, or cleanup, require accepted outcome
(or explicit disposition), positive completion of the relevant native turn, and
settled ownership/continuation obligations. Source/process ownership remains a
separate check. If the target installation cannot establish these facts through
the specified observation path and scoped tools, block the affected operation
and fail its acceptance check. Do not claim the five-minute CI test proves this
collector. The cooperative model does not promise exclusion against arbitrary
new user-directed edits outside Fulcrum's ownership protocol.

## Worker execution, CI, and delivery

Fulcrum prepares and records the Tollgate worktree, branch, base, and ownership
before Steward creates Executor. Use the enrolled saved project with explicit
local targeting; there is no assumed arbitrary-worktree field in `create_thread`.
Executor and Warden use verified absolute paths or explicit command working
directories. Registration compares observed Git root/branch/HEAD with provider
facts: clean prepared assignment for Executor, submitted source for Warden.

Probe workspace access with a disposable file during readiness. Missing access
is a setup blocker, not permission to edit the main checkout. The native UI may
show the saved project while commands operate in the assigned worktree; disclose
this difference. Native permissions remain the user's Desktop configuration.
Full cooked role prompts preserve behavioral scope and acceptance, not raw
intake or obsolete implementation suggestions as mandatory implementation facts.

Workers report meaningful progress at least every ten minutes when able to call
tools. The reporting deadline is thirty minutes after registration or accepted
progress. Long local tools may delay reporting; a registered CI wait suspends
that ordinary deadline in favor of its explicit provider deadline. Expiry records
one missing-progress incident, not proof of failure or permission to duplicate a
worker. No extra model turn is needed for unchanged waiting status.

Accepted Executor finish seals its outcome and relinquishes editing authority.
Code records pending Warden work; Steward creates Warden after completion and
ownership checks. The old worker ends without a native handoff. A missing result
report remains recoverable, not permission to reopen implementation. Warden may
fix findings directly and must retain the exact-source, local-validation, and
commit-topology requirements of the delivery contract, including the required
single task commit atop the retained base.

### Blocking Warden CI wait

Warden submits its reviewed source and local-check evidence through
`submit_candidate` before final finish. Persist source, candidate/run, and
submission uncertainty. This authorizes validation, not promotion or assignment
release. Reconnect or a lost response must inspect the existing submission,
never create another candidate blindly.

Warden calls `wait_for_ci_results(candidate_id, assignment_token, request_id)`.
The fresh CLI validates actor, exact source/candidate, current assignment, and
absence of another pending wait. Return retained terminal evidence immediately,
or atomically register a pending response with the terminal-result check. Release
all policy processes and locks; the broker/MCP transport holds the connection.
Warden retains its native turn, assignment, worktree, and ordinary worker slot.

Prefer provider events. If Tollgate needs status queries, bounded software jobs
observe the exact run at a 30-second pending interval, with error backoff of 60,
120, then 300 seconds. No agent polls CI, and unchanged observations cause no
model response or durable event. A terminal result launches a fresh CLI operation
from current master, persists its evidence, and completes the pending response.
A wait does not pin application policy to the commit from when it began.

For a five-minute CI failure:

1. Warden remains in its one pending MCP call; Steward handles unrelated actions
   and Marshal need not run.
2. Background code returns exact candidate/source, failed checks, and bounded
   diagnostics in that same call's response.
3. If scope, pause, and remaining allowance permit, grant a repair cycle in the
   same assignment. Warden repairs, submits changed source, and waits once for
   the new candidate. No native continuation or reacquired slot is needed.
4. Passing CI for the reviewed source permits final successful finish. Code
   handles authorized promotion/synchronization/cleanup and Steward handles any
   later native action. Native completion then permits capacity release.

If repair is blocked, paused, or exhausted, Warden reports the retained blocker
and ends rather than waiting for human input. A failed CI result is not an
instruction to wake Marshal until deterministic repair cannot proceed.

Use the finite budgets specified for the instruction loop; measure the CI
deadline from candidate submission so reconnect cannot replenish it. Expiry
returns an explicit blocked/unknown disposition, not `pending` and an agent
short-timeout retry loop. Preserve the exact candidate and wait through
cancellation/disconnect; reconcile before resuming, resubmitting, or sending a
second response. Do not infer native termination or release a slot from timeout.

### Same-task repair, completion, and archival

After a final accepted finish, later repair resumes the same Executor/Warden
task under a fresh assignment and capacity reservation. Keep prior finish/review
as historical evidence. New source invalidates old review/CI; require current
validation and a new outcome. A missing or unusable task remains a blocker.
Manual early archival requires observed unarchive before continuation; it is not
task deletion or permission to create a replacement.

Passing CI or a successful native turn is not evidence of Git promotion. Code
rechecks exact source, approval, pause, provider outcomes, required source sync,
and cleanup separately. A successful promotion with failed cleanup remains
promoted with a cleanup obligation. Workers stop their own background commands
before finish and retain process locators. Never delete a worktree while source
ownership, native completion, or an owned process is uncertain.

Steward archives a worker only after all associated beads are terminal and their
delivery, repair, cleanup, reporting, and process obligations are settled. Retain
archive actions even on closed beads. Revalidate when claiming; a new turn or
repair cancels only an unissued obsolete archive. A possibly executed archive
needs reconciliation, not another automatic attempt. Preserve role/task links.
An explicit or observed manual unarchive suppresses automatic rearchive. The
three standing tasks are not automatically archived.

## Preserve token usage and cost estimates

Task and workflow accounting remains required. Preserve `usage`, `cost`, retained
rate cards, attribution, coverage, completion summaries, and explicit corrections.
The [documented hooks][hooks-doc] supply session and transcript paths, plus
turn-scoped IDs, not token counters directly. Read only registered task transcripts
through the scoped collector; do not estimate token counts from transcript text.

Inspection on 2026-09-16 found `token_usage_record` entries containing native
task/turn/response IDs, per-response input, cached-input, cache-write, output,
and reasoning counters, plus cumulative turn/task totals. This is local evidence,
not a guarantee of the target stock format. Verify the fields and their semantics
before opening admission; maintain the parser as an unstable-format dependency.

Hooks initiate collection; broker file observation catches delayed writes. Fresh
CLI collectors persist normalized evidence and progress in Beads, tolerate partial
trailing lines, and replay with native response identity for deduplication. Never
sum successive cumulative samples. Reconcile unique response usage with terminal
totals; completion evidence remains distinct from usage evidence.

Correlate effective model, service tier, and reroute evidence before pricing.
Configured settings alone do not prove effective execution. Use disjoint input
categories and per-response long-context/tier rules; reasoning is already included
in output. Missing counters/model/tier/rates retain observed tokens and mark cost
partial or unknown, never zero. Retain priced subtotals and frozen completion
summaries with explicit later corrections. These are API-equivalent estimates,
not subscription bills; accounting gaps do not block delivery.

Include Steward, Marshal, Vizier, and Justiciar overhead under the existing
attribution rules. A long Steward turn may serve several beads: use recorded
actions and native response evidence for attribution, label any allocation as an
estimate, and keep unattributable usage as coordination overhead. Do not charge
the entire continuing Steward turn to its first bead. Expose provisional usage
while it runs, and allow workflow cost summaries to close over settled attributed
responses/actions without waiting for the whole Steward turn to end. Retain
explicit coverage and issue corrections for later evidence. Count native totals
once; subagent attribution gaps must not become invented extra usage. Verified
terminal evidence still seals final whole-turn totals.

Measure healthy idle-wait usage separately from per-action and scheduled-run
costs. Missing collection capability fails readiness; an isolated recoverable
evidence gap is explicit in reports. Verify multiple tasks per Steward turn,
repair continuations, compaction, interruption, model changes, delayed writes,
and restart without changing the existing rate provenance or coverage semantics.

## Process ownership and live iteration

| Component | Responsibility |
| --- | --- |
| Thin MCP instance | Decode requests, invoke fresh CLI operations, and retain transport for authorized pending instruction/CI responses. No business policy or durable queue. |
| Unix-socket broker | Own connections, wait subscriptions, registered file observation, timers, and bounded background jobs. Rebuild indexes from Beads; never invoke private native APIs. |
| Fresh CLI operation | Read current Beads/YAML, enforce authority, select work, compute prompts/actions, commit transitions, and perform provider operations. |
| Command hook | Pass scoped events to fresh handlers and return native hook responses. No dispatch policy or native task calls. |
| Provider watcher | Observe existing resources and publish changed facts through fresh operations. No new assignments or agent judgment. |

One broker per instance holds a kernel process lock; stale socket cleanup follows
successful lock acquisition. Closing one MCP client must not terminate the broker
or other clients' operations. Shared pending responses are independent, so a
Warden CI wait, idle Steward, and Marshal curation can coexist. No Beads lock or
resident policy process is parked behind either waiting agent.

Each bounded operation resolves local master before importing application code
and pins one source/interpreter for its lifetime. The broker/MCP process must not
cache business handlers, prompts, model selection, or action compilation. Event
arrival and wait-result computation each launch a fresh operation. An already
issued action retains its exact arguments despite a newer source commit.

Keep source-following launchers, automatic immutable source preparation, and
unchanged dependency reuse. A local master commit is enough even with no remote.
Skills remain direct links into the checkout. Handler edits do not alter trusted
hook definitions. Stable MCP schemas and saved prompts describe the protocol;
current policy comes from fresh CLI results/context, without recreating standing
tasks or editing the schedule after ordinary formula/policy changes.

| Change | Visibility and maintenance boundary |
| --- | --- |
| CLI/MCP business handlers, hook handlers, transcript parsers, formulas, prompt compilation, and assets | Next fresh operation uses local master; no installation, reconnect, re-trust, or restart. Running operations retain their source. |
| YAML policy, model/capacity settings, and timer intervals | Next operation reads current configuration; fresh policy returns updated timer deadlines to the broker. An issued action or existing wait retains its recorded arguments/deadline. |
| Skills and standing-role context | Next skill read sees the checkout; next context/protocol response supplies current role instructions. Already-delivered instructions remain part of the current turn. |
| Broker/MCP connection machinery, exposed tool schemas, or trusted hook definitions | Exceptional safe handoff/reconnect or native trust flow after affected calls settle. Keep these surfaces small and stable; do not put routine behavior here. |
| Dependencies or durable-state layout | Exceptional maintenance from live iteration; never require reinstalling dependencies for ordinary edits. |

This is operation-boundary hot reload, not replacement of executing code. Keep
parsing, policy, diagnostics formatting, and timer decisions outside resident
processes so normal development stays entirely in the first three rows.

For example, Steward may already be waiting when a completion-policy change is
committed. The next worker finish and the operation resolving Steward's response
use the new commit, while an older running operation keeps consistent imports
and assets. No install, activation, remote push completion, or restart intervenes.
Failure to prepare source blocks new behavior visibly rather than falling back
to stale application code.

Retain detached mutation processes and temporary result locators on client
timeout; do not kill a potentially successful write. Beads-only inspection and
repair remain available with the broker stopped. The local p95 target stays below
one second with unchanged dependencies/state contracts. The recorded 0.890-second
launcher baseline is not a measurement of this MCP/hook path; measure CLI, MCP,
and hook overhead with production diagnostics enabled, concurrent clients, and
retained logs. Separate preparation, lock wait, Beads, and logging time.

## Shared CLI and MCP interfaces

These are proposed interfaces to implement, not claims that the names exist
today. MCP forwards business calls to fresh CLI handlers; the same transition
and result protocol applies to direct authorized CLI entry. It must remain
possible to inspect and repair the installation without a working agent relay.

| Interface | Contract |
| --- | --- |
| `bootstrap` / `register_standing` | Resume deterministic setup and bind the exact Steward/Marshal/Vizier task to its retained creation or recovery action. |
| `wait_for_instructions` | Steward only: recover an outstanding response or select/reserve the next routine action; otherwise register a pending response without keeping policy code alive. |
| `claim_action` / `report_action_result` | Authorize the named executor's one invocation and record its actual outcome. Hook and agent results share this validator. |
| `marshal_check` / `marshal_decide` | Inspect health/incidents/backlog, own one bounded decision operation, and apply targeted current-state curation. No per-task permission is needed for ordinary ready dispatch. |
| `recovery_prepare` | Marshal only: authorize a scoped Justiciar intervention, reserve the extra recovery slot, and return a native creation action assigned to Marshal. |
| `register_worker` | Validate exact native identity, assignment, role, scope, project, and workspace before substantive work or resumed work. |
| `report_progress` / `report` | Persist meaningful assigned-work progress, or file an independent incidental follow-up without changing the caller's assignment. |
| `submit_candidate` / `wait_for_ci_results` | Warden only: submit exact reviewed source and retain one pending CI response for the current assignment/candidate. |
| `finish` | Seal a role outcome and retain dependent work for code/Steward. Successful recovery may return the assigned Justiciar's exact safe Steward-resume action. |
| `pause` / `resume` | Human/Vizier-authorized durable run control; return in-flight work/holds and revalidate before releasing them. |
| `hook handle` | CLI-only native hook event routing; no agent-callable override of hook identity. |

Ordinary mutations carry stable request ID, actor, and applicable assignment or
standing-operation token. Native actions add action/attempt IDs. `report_key`
is the incidental filing identity and does not require ownership of a managed
assignment. Derive actor identity from supported session context or explicit
native ID checked against registration; actors cannot label themselves human.
The emergency diagnostic alert has the explicit narrower exception above.

Durable result envelopes retain accepted input, source/operation identity,
actual state, next authorized action, and explicit gaps. On client timeout,
return the operation locator when available. Equal retries replay their result;
changed requests need new authorization rather than an invented new retry ID.
Keep pagination and byte bounds for inspection, with omitted counts/continuations.
Never turn a partial summary or a successful inspection into a completed-work
claim. `status` separates ownership, capacity, delivery, source sync, and cleanup.

### Minimal durable protocol

Use the existing CLI result envelope and request UUIDs, with these authoritative
fields. IDs are opaque identities, not hashes or schema versions. Omit unrelated
fields from compact responses; retain complete accepted input in Beads.

| Record | Required fields and authority |
| --- | --- |
| Assignment on owning work bead | `assignment_token`, actor task/host, role, workspace/source, scope, capacity class, state, and accepted finish. Only registration changes a reserved assignment to active. |
| Action on owning work/control record | `action_id`, executor, tool, exact arguments, assignment reference, state, and attempts `{attempt_id, native_tool_use_id, outcome, evidence}`. Claim changes pending to issuing before invocation. |
| Request transition on owning record | `request_id`, accepted input/actor, source/operation identity, state, saved result, and projection locator. Equal input replays; changed input conflicts. |
| Instruction wait on `fc-system`; CI wait on work bead | `request_id`, actor task/host/turn, loop or assignment reference, candidate if applicable, deadline, state, and saved response. States are waiting, resolved, cancelled, or expired; disconnect alone does not erase a wait. |
| Admission handshake on the owning record | Request ID, session/turn/tool-use identity, exact input, and consumed disposition from the trusted pre-hook. Never accept actor identity solely from caller-supplied labels. |

Instruction responses are `action` (exact action and reporting arguments), `stop`
(reason and retained obligation), or `blocked` (reason and recovery reference).
CI responses are `passed`, `failed`, or `blocked`, with exact candidate/source and
evidence. Waiting is internal transport state, never an interim model response.
Uncertain results retain their attempt; they cannot transition back to pending
until definite rejection or reconciled absence authorizes a new attempt.

For instruction grants, first retain the wait/loop intent on `fc-system`. Commit
the action, reservation, and originating wait/loop IDs together on the action's
owning record; this is the authoritative grant. Then resolve the system wait and
deliver its saved response. Before selecting again, recover any grant for that
wait/loop across owning records, including closed records. A crash between these
writes repairs the response reference, never selects a second action. A new wait
requires the previous grant's result/disposition to be recorded; older uncertain
attempts may remain reserved as specified above.

After verified projection, replay resolves the request's deterministic operation
ID in Beads and compares its retained full input/result; these replay records
outlive removal of completed entries from the work bead. Missing or conflicting
evidence is a storage blocker, never a fresh request. Registration consumes the
matching pre-hook handshake under the writer lock, checking session identity and
creation/assignment marker. If the native initial prompt produced no callback,
registration instead requires the settled claimed creation result's exact task
identity. A worker may register before the creator's result when the hook evidence
exists; the later result must agree with that binding. Direct CLI managed
mutations use the same observed invocation binding; operator recovery retains its
separately authorized scope. Post-hooks and agent reports settle the same native
attempt.

## Pause and operator control

Fulcrum pause is a durable Human/Vizier transition, not the Desktop Stop button.
Record `run_control=paused`, reason, and accepted request on `fc-system` under the
writer lock. YAML project pauses and recovery fences remain independent. Every
entry point rechecks these conditions; no standing agent may impersonate a human
to bypass them. Resume revalidates held work against current source and policy.

Pause prevents new ordinary assignments, continuations, Justiciar creation,
provider submissions, promotion, source push, and cleanup. Issued effects may
settle and active assignments may finish/report within already granted scope.
Do not start another repair cycle merely because an existing CI call failed.
Observers may persist evidence and reports, and operator diagnostics remain
available. Native diagnostic notices grant no authority to resume work.

Selection and pause issuance serialize under the same lock. Return an explicit
list of in-flight effects; a pause cannot promise to revoke a native request
already issued. Keep Warden's existing candidate/watch and remaining delivery
obligations without permitting downstream work. Resume cannot promote stale
source or clear a repair-limit hold. A previously accepted review remains tied
to its exact source, never automatically to the latest checkout.

A paused Steward can remain in its pending instruction call; it receives no
work until authorized state changes. Scheduled Marshal may inspect paused state
and end quietly but cannot restart dispatch or create recovery work implicitly.
Desktop Stop interrupts one turn and may leave a pending native effect uncertain.
Without a Fulcrum pause, later scheduled recovery may resume a stopped Steward;
bootstrap must disclose this. No standing goal or automatic Stop-continuation
loop is part of the design.

## Bootstrap, naming, and standing-task recovery

The checkout exposes `$fulcrum-bootstrap` through `.agents/skills` before global
installation. The MCP-refresh continuation prompt also names the canonical
`~/fulcrum/skills/fulcrum-bootstrap/SKILL.md` source path explicitly. The skill
discovers the retained checkout and existing configuration, initializes stock
configuration when it is absent, then drives deterministic CLI setup and exact
returned native actions. It does not reconstruct workflow policy in prose. Missing
executables, credentials, saved projects, model choices, or native trust are
specific prerequisites. Complete work already authorized by setup without
asking repeatedly. A ready socket alone is not successful setup.

### Setup algorithm

1. Discover local master, dependency runtime, brain/Beads configuration, provider
   configuration, projects, and instance ownership. On a fresh clone, provision
   the retained `.venv` and atomically create the authoritative configuration from
   validated stock defaults plus explicit setup input. Never overwrite an existing
   file. Use the existing singleton
   writer identity at the resolved brain root; reject a conflicting instance or
   mismatched backend. Bootstrap primitives before Beads exists are bounded,
   idempotent, inspected filesystem/service operations, not a second journal.
2. Once Beads is available, retain one setup operation before native effects.
   Provision source-following launchers, broker, and thin MCP definitions. Set
   the specified instruction/CI wait budgets. Preserve unrelated configuration.
   Because an existing task retains the MCP catalog with which it started, an
   initial MCP configuration change creates one bounded continuation task in the
   saved Fulcrum project. Its fresh bootstrap call rebinds pending setup actions
   to the new native task. No Desktop process restart is part of setup.
3. Install and trust the five scoped command hooks. Verify actual callback paths for projectless
   standing tasks and saved-project workers. Check native task
   tools, model/effort support, workspace access, transcript observation, and
   accounting. Unsupported project creation requires the user to add that exact
   saved project; do not invent a second project after an uncertain result.
4. Establish exact standing task identities under retained creation actions:
   Steward on Luna, Marshal and Vizier on Sol. Prompts require registration
   before activity. Marshal/Vizier initially finish their setup work; Steward
   registers its instruction loop without receiving work before admission.
   Record all three IDs/hosts and return them on partial setup.
5. Create one 15-minute heartbeat targeting that Marshal. Confirm actual task
   targeting and overlapping-run behavior; no separate hourly recovery task or
   heartbeat on Steward. Persist the automation ID and its intended/observed
   state. Configure notifications for meaningful failures/decisions, not routine
   healthy status on every run.
6. Complete the five evidence-bearing checks, then create the verified schedule
   active while admission remains paused. Retaining that exact creation result may
   enable admission; real delivery and overlap health are monitored afterward.
   Pending ready work can resolve Steward's instruction call independently of a
   Marshal decision. Expose task links,
   exact schedule, capability gaps, and any required operator action.

Bootstrap authorization covers ordinary Steward task actions, Marshal's direct
Justiciar creation and the extra recovery slot, safe same-Steward resumption by
Marshal/assigned Justiciar, notification-only recovery alerts, scoped hooks and
transcript reads, provider operations, and the scheduled checks. It does not
expand filesystem permissions, auto-answer native approvals, grant arbitrary
recovery scope. Initial cutover timing is already authorized as recorded below;
bootstrap does not authorize unrelated deletion or later resets.
Explain the prompt-supervision limits and the Stop-versus-pause behavior.

The bootstrap skill's short entrypoint calls `scripts/setup` for first-use
dependency provisioning and configuration creation, then calls deterministic
bootstrap directly. It uses a stable request ID for an exact timed-out invocation,
executes only returned actions, and starts a new receipt after prerequisites or
native results settle. Configure native tool
approvals and hook trust through supported flows. Do not bypass native trust or
install an experimental App Server fallback. Saved Steward/Marshal prompts name
the protocol and current-state calls; role details come from fresh CLI context.

### Titles and reruns

Preserve the role-title formatter and existing worker emoji/bead-ID conventions.
Use fixed standing titles `🧰 STEWARD 🧰`, `🧭 MARSHAL 🧭`, and `🔮 VIZIER 🔮`.
Creation-time `title` normalization may require an exact `set_thread_title`
correction after binding. Verify titles after first turn and MCP continuation;
rename is a recorded native effect, never a reason to replace a task. `$bead`
reporting must not rename the reporting conversation.

Rerun setup by inspecting recorded native IDs and operation postconditions, not
by recreating tasks or replacing policy defaults. Lost creation replies recover
through self-registration and exact native evidence. For an uncertain schedule,
use the documented local automation inventory and native view to match the exact
instance/target/marker. One exact match can settle it; ambiguity retains the
blocker. Update through the automation tool, preserving unrelated settings; do
not write scheduler files or duplicate an uncertain schedule.

### Explicit replacement and same-Steward resumption

Normal recovery resumes the existing Steward only after reconciling its pending
instruction/action and confirming no competing relay is running. Lost resume
replies stay uncertain. A still-active or unknown old turn cannot be replaced by
expiry of a software lease. Justiciar needs no additional Marshal decision to
perform a verified resumption inside its already-granted repair scope.

Permanent loss of a standing task uses explicit operator-authorized replacement,
not `fleet replace` or an automatic new Steward. Retain a recovery fence, pause
relevant schedule/dispatch, observe termination or obtain the needed human action,
settle/carry forward issued effects, and create/register one replacement under the
same recovery receipt. Update authoritative bindings only after that evidence.
For Marshal replacement, retarget the one existing heartbeat and verify it before
lifting the fence. Supersede only unissued actions aimed at the retired identity.
A failed/uncertain step remains fenced and rerunnable without duplicate native
creation. Unrelated work may continue only where its authority is unaffected.

## Destructive cutover and ongoing maintenance

The operator has stopped all work and authorized initial cutover whenever useful
during implementation. There is no requirement to keep the old instance runnable
through intermediate commits or to complete a separate probe phase first. Before
the first incompatible commit, retain the maintenance fence and disable remaining
old schedules/revival paths; verify no residual writer or issued effect remains
before deleting its resources. This verifies the stopped state, not another
request for cutover permission. Keep admission closed until replacement acceptance
passes; partial implementation must not restart old work.

The initial cutover discards old workflow records, bindings, and schedules without
migration, compatibility code, or a transport selector. Commit directly to local
master. Implementation and cutover may interleave using these retained steps:

1. Inventory exact owned ledger, service, schedule, native-task, hook/MCP, cache,
   log, and workspace/provider resources. Preserve unrelated repositories,
   delivered changes, shared infrastructure, and other instances. Identify
   resources without supported deletion before mutation.
2. Fence old admission and disable old schedules. Settle active writers and
   effects or obtain authorized interruption through available controls. Do not
   delete a workspace or ledger under a live writer. Retain the maintenance
   fence outside the resettable ledger through partial failures.
3. Retire only the Fulcrum-owned App Server connection/service, old hook/MCP
   entries, launchers, and revival paths. Never terminate Desktop's own runtime
   or another instance. Remove obsolete source paths, tests, and configuration
   selectors as part of the implementation.
4. Delete enumerated old workflow state and disposable resources. Do not import
   receipts, reuse old standing IDs, translate records, or keep a live legacy
   instance. Native deletion remains unsupported where no tool exists; archival
   is not deletion. Require exact manual cleanup or an explicit authorized
   retention exception rather than falsely claiming a complete wipe.
5. Bootstrap fresh state, three standing tasks, bindings, and one 15-minute
   Marshal heartbeat. Re-enter approved configuration references without treating
   old workflow state as an upgrade source. Pass new readiness before reopening.
   Rerun partial reset from verified postconditions; do not restore old state as
   an automatic rollback or add a compatibility reader.

No runtime or reset is performed by this document revision. Ordinary code,
formula, and instruction edits remain available from local master immediately
at the next operation boundary. Future broker/MCP transport changes require a
safe connection handoff after in-flight effects settle; dependency/durable-state
maintenance follows live-iteration's exceptional fenced path. Rebuild transport
indexes from Beads and preserve existing operations' pinned imports/assets.
Neither exceptional maintenance nor remote publication becomes an ordinary
source-editing step.

## Implementation handoff

Implement this design directly; no discovery/probe milestone precedes coding.
Take bounded slices with an observable outcome, relevant checks, diagnostic
events, and a landing boundary. Establish causal logging with the first protocol
slice, not as a final instrumentation pass. Work in this order:

1. Fence the stopped old instance as described above. Adapt ledger transitions,
   current-backlog selection, reservations, native claims/results, and Marshal
   targeted updates. Remove mandatory
   per-bead Marshal approval for ready ordinary work. Cover equal retries,
   changed inputs, stale decisions, lost results, and lock inheritance.
2. Implement thin pending-response transport and fresh event computations for
   Steward instructions and Warden CI. Add scoped hook/transcript collection,
   standing registration, and exact native action ownership. Preserve source
   freshness through long waits and independently progressing clients.
3. Add 15-minute Marshal health/curation, coalesced escalations, direct Justiciar
   creation, the additional recovery slot, the one-intervention limit, safe
   same-Steward resumption, and Vizier decisions. Implement the narrow no-ledger
   diagnostic exception without an unrecorded dispatch path.
4. Preserve incidental reporting, plan lifecycle with prompt-level subagent
   review, worker/delivery invariants, naming, and accounting. Remove automatic
   publication, curated memory, fleet replacement, old review receipts/gates,
   forced coordinator exit, and pre-injection notification requirements. Update
   command contracts, prompts, bootstrap skill, and operator docs together.
5. Assemble bootstrap and finish the implementation acceptance checks. Complete
   any remaining cutover steps and verify the fresh instance before reopening
   admission. Fix concrete capability failures without inventing native success
   or turning implementation into an open-ended architecture investigation.

## Acceptance and operational evidence

Preserve the fast [repository check](../validation.md): formatting, full types,
and relevant regression coverage without dependency installation, provider/model
calls, real CI waits, or scheduled delays. The audit baseline on 2026-09-16 was
176 tests and 5.10 seconds for the complete check. Target that scale; retain the
30-second normal budget and 55-second hard deadline. Use narrow production-boundary
tests with fake clocks/events and recorded adapter results. Do not revive the
retired integration harness or add tests that merely mirror implementation.

The cases below specify coverage, not 18 mandatory live experiments. Exercise
crash/race/timeout paths locally; retain actual native payloads as small parser
fixtures during implementation. Focused end-to-end acceptance checks establish
real task actions, hook/identity/lifecycle collection, CI delivery, and schedule
targeting on disposable resources. Record observations and gaps honestly; unit
results do not prove native behavior. No additional exploratory probe series is
required, and no long-duration model run belongs in the normal check.

Establish acceptance once for the replacement; repeat affected native checks only
when their boundary changes or a failure invalidates the evidence. Ordinary edits
use focused checks during development and the complete repository check before
commit. Neither bootstrap nor each commit reruns the full live matrix. Test and
logging overhead are part of the development-speed budget. Required coverage:

1. **Repeated Steward loop and idle cost.** Process several create/send/archive
   actions in one Steward turn, with current-state selection between them. Verify
   a pending idle call and separate idle usage from action/discovery overhead.
   Exercise deadline handling with controlled clocks and retained timeout evidence;
   cover compaction and reject short empty-status polling loops.
2. **Dispatch without Marshal.** Leave Marshal idle while Weaver and incidental
   reports produce ready work. Steward fills eligible capacity and performs
   Executor/Warden handoffs without per-bead approval or routine Marshal wakes.
   Future-plan/approval holds, dependencies, project pauses, and conflicts still
   block their affected work. Repeat with small and large backlogs.
3. **Concurrent curation and admission.** Race priority/hold/dependency updates
   with two CLI attempts for the last slot. Observe one complete reservation.
   A stale Marshal row cannot overwrite worker progress. Pause before/after
   action issuance; an already-issued effect remains explicitly in flight.
4. **Marshal schedule and busy notifications.** Exercise the actual 15-minute
   schedule and an overlapping failure alert during curation. Record whether
   Desktop queues or steers. Verify one current decision operation, coalesced
   incidents, no duplicate native effects, and correct stale-update rejection.
   Healthy/no-op runs are quiet; scheduled model overhead remains measured.
5. **Steward resumption.** End Steward with no work, during a wait response, and
   during native creation in separate trials. Marshal discovers the stopped
   relay and resumes the same task only after reconciliation. A healthy wait
   receives no recovery wake; active/unknown state cannot create a second relay.
6. **Failure isolation and emergency notification.** Fail one bead while another
   is dispatchable. Routine retries stay in code; exceptions produce one alert.
   Break MCP/CLI/Beads separately: Steward attempts its single diagnostic native
   alert and stops, making no unrecorded creation or ownership mutation. Break
   native messaging too and recover through the scheduled Marshal path.
7. **Justiciar ownership and extra slot.** Fill ordinary capacity, then have
   Marshal directly create one scoped Justiciar. Steward cannot also claim it;
   another incident cannot acquire a second extra slot. An uncertain creation
   retains its reservation. Repair requires settled conflicting ownership.
   Verify Justiciar can safely resume the same Steward without another Marshal
   decision and cannot act outside its recorded scope.
8. **Repair limits and human escalation.** Fail three ordinary repair cycles,
   then one Justiciar intervention. Preserve counts across handoffs, compaction,
   restarts, and duplicate reports. No fourth ordinary repair or second automatic
   Justiciar is admitted. Required scope changes and failed intervention reach
   Vizier with exact evidence; explicit human grants authorize only their scope.
9. **Five-minute Warden CI.** Against the real provider, keep one MCP call pending
   while CI runs for five minutes, then return failure to the same Warden turn
   and assignment. Ordinary capacity remains occupied. Repair/resubmit under
   its allowance; passing exact-source CI permits finish. No worker polling,
   interim `pending` replies, or routine Marshal notifications.
10. **Wait cancellation and restart.** Interrupt Warden and Steward waits; restart
    broker, MCP, and Desktop separately. Preserve candidate, instruction, action,
    and native-turn uncertainty. Rebuild registrations, reconcile lost responses,
    and never blindly resubmit or duplicate a worker. Measure actual recovery
    latency; a missing connection is not automatic native completion.
11. **Native completion.** Verify target-stock lifecycle records against actual
    completed, failed, interrupted, resumed, and newer turns. Delay transcript
    writes and deliver old observations late. Stop/final text alone cannot
    release ownership, capacity, or worktrees. Scoped native inspection reports
    missing identity evidence honestly instead of turning into a status loop.
12. **Receipt and event crash boundaries.** Lose replies before/after each Beads
    write, projection, native claim/result, and broker notification. Replays
    settle the same IDs. Kill parents while lock-owning children run; no second
    writer commits stale state. Projection failures affect only the relevant
    bead, and all pages of pending/closed-work obligations remain discoverable.
13. **Same-task repair and archival.** Retain Executor/Warden tasks through CI
    and delivery. Post-finish repair reacquires assignment/capacity in the same
    task and invalidates stale review. Cleanup/archival do not retain a released
    slot. Lost archive replies reconcile once; manual unarchive suppresses
    automatic rearchive; standing tasks remain visible.
14. **Accounting.** Compare response, turn, task, and workflow totals, including
    multi-bead Steward turns and Justiciar overhead. Exercise cache counters,
    model changes, delayed records, compaction, and restart. No cumulative or
    subagent double counting; missing pricing stays partial/unknown. Final
    corrections preserve prior summaries and idle-wait measurements. Complete
    a bead and its cost summary while Steward continues serving other work.
15. **Hooks and cooperative authority.** Exercise direct and nested calls, missing
    hooks, conflicting markers, and out-of-order results. CLI checks reject
    unauthorized assignment/claims regardless of prompt arrival; hooks capture
    covered native evidence. Measure failure behavior without claiming a hard
    guarantee of zero further model generation or forced turn exit.
16. **Source freshness.** Commit ordinary policy/assets with Steward and Warden
    calls pending and an older operation running. Subsequent CLI, MCP event, and
    hook operations use local master without install/restart/remote access;
    older operations/actions retain their source/arguments. Measure each entry
    path against the local p95 target with production logging enabled; report
    sample count and conditions. Preparation failure is visible; no stale fallback.
17. **Product scope and human interaction.** File `$bead` from an unmanaged and an
    active worker task without role/name changes; retry exact and conflicting
    keys. Review plans through a native subagent without old review gates; verify
    explicit future activation, stable refinement keys, and parent completion.
    Manual sync works, but automatic publication and curated memory do not
    return through bootstrap or prompts. Exercise Vizier responses, pause,
    native approvals, and an unavailable inbox without duplicate tasks.
18. **Bootstrap and destructive reset.** Verify all three titles/identities,
    project/worktree access, hook trust/isolation, and one 15-minute schedule.
    Lose creation/setup replies and rerun from exact evidence. On disposable
    resources, verify stopped writers and exact ownership before deletion.
    Interrupt each authorized reset/replacement boundary; fences and unrelated
    resources survive. A fresh instance adopts no old workflow bindings and
    never treats archival as native deletion.

## Causal diagnostics without slowing development

Structured logging is on by default across hooks, MCP, broker, CLI, Beads, native
actions, and provider operations. Every meaningful boundary records start and
completion/failure with UTC time, monotonic duration, component/process identity,
source commit, and applicable instance/bead/request/operation/action/attempt,
task/host/turn/tool-use, wait, and candidate IDs. Carry a causal predecessor ID
across processes and callbacks; preserve native IDs rather than inventing hashes.
Record assignment identity without exposing authorization tokens. Missing
correlation is an explicit gap, never an inferred success or invented link.

Log eligibility/hold decisions and the relevant facts, reservation and release,
instruction commit/delivery, claim, observed invocation/result, acknowledgment,
wait registration/resolution/expiry, hook rejection, transcript cursor movement,
recovery decisions, and source preparation. Include queue/lock wait, Beads, native
call, provider, and logging durations separately. Unexpected failures retain
exception type, message, stack and cause chain plus bounded relevant adapter
input/output or an exact evidence locator. A start without an outcome exposes
the last confirmed boundary after a crash. Coalesce unchanged observations and
empty checks with counts/time ranges; do not sample away transitions or errors.

`trace` joins Beads evidence and logs into a causal timeline: why work was selected
or held, whether an instruction was committed/delivered/claimed/invoked/observed,
and where time accumulated. Support lookup by bead, operation, action, task, or
wait ID, including standing-agent work with no bead. Show uncertain ordering,
pruning, truncation, missing callbacks, and dropped records as gaps. `status` and
`doctor` distinguish socket liveness from progress and expose pending waits,
action age/uncertainty, Marshal decisions, capacity, incidents/allowances, provider
state, accounting coverage, and logging health. Local logs and their bounded
reader work without Beads, MCP, or native messaging; no debug rerun is needed.

Keep diagnostics outside the Beads critical section and off the shared workflow
lock. Append/flush critical starts, transitions, and failures promptly; synchronize
them to disk before an external invocation and at operation completion/failure.
Routine timing/detail may buffer for at most one second and flush on process exit;
only that window may be lost on a crash. Rotate by size and prune periodically,
not on every event. Preserve existing 14-day/1-GiB defaults and bounded payloads;
emit retention/drop summaries and preserve incident evidence references in Beads.
Measure latency with these production settings and concurrent writers enabled.

Log-write failure must surface through stderr/result diagnostics and component
health (including broker health when available), with dropped-event counts when
observable. Never silently discard it, block forever, or turn a committed workflow
effect into a reported failure that invites retry. Logs remain diagnostic evidence,
not another workflow journal. Redact credentials, omit unrelated conversations,
and keep critical correlation fields even when payloads are truncated. Logging
adds no model turns or automatic publication.

As a focused acceptance check, reconstruct a lost native reply, a rejected hook,
and a delayed CI result from retained evidence alone. Identify the last confirmed
boundary, cause or explicit unknown, source commit, and recovery disposition
without rerunning the failed operation. Exercise log loss/rotation locally and
verify visible gaps; include logging cost in the ordinary startup latency budget.
