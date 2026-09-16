# Fulcrum through stock Codex Desktop

This design replaces Fulcrum's direct App Server integration with agents using
the native task tools in an unmodified public Codex Desktop installation.
Fulcrum continues to decide workflow policy in fresh CLI processes, with Beads
as its only durable workflow store. A local MCP server exposes those processes
to agents; a small Unix-socket broker holds connections, waits, and timers.

Users who do not want the experimental WebSockets/App Server API should retain
Fulcrum's intake, implementation, review, and delivery workflow inside Desktop.
**Beads** is Fulcrum's issue-backed durable ledger; an individual issue is a
**bead**. **Tollgate** is the delivery provider that owns worktrees, validation,
and promotion. Neither is replaced by Desktop's task storage.

**Marshal** is the standing coordination task: it exercises bounded backlog
judgment and executes the exact native actions returned by Fulcrum. Its task
identity persists, but its agent turn ends when coordination becomes idle.
**Vizier** is the standing human-directed policy task. **Weaver** prepares new
work, **Executor** implements approved scope, and **Warden** independently
reviews it. Existing Sage, Mason, and Justiciar investigation and recovery roles
remain available through the same dispatch mechanism.

Core dispatch and messaging are expressible through the observed interfaces;
full unattended feasibility remains conditional. Native tools expose task
creation, messaging, inspection, archival, and scheduled follow-ups. They do not
expose the full App Server control surface. Long MCP waits, restart and Stop
behavior, workspace access, and task completion evidence still require the
specified live acceptance checks. This document fixes the storage and recovery
algorithms; those checks establish whether a particular Desktop installation can
run them. They do not delegate architecture choices to an implementer. No
replacement runtime or bootstrap skill is installed by this document.

## Related information

- [Live iteration](live-iteration.md): local-master freshness, immutable source
  leases, transport continuity, and exceptional maintenance.
- [Fulcrum contracts](../fulcrum2/contracts.md): current role authority,
  ownership, reservations, operation receipts, and delivery requirements.
- [Weaver workflow](weaver-workflow.md): scope handoff and measured Beads
  subprocess costs. Its current controller-driven notification is replaced by
  the action-return protocol below.
- [Operational failure analysis](../fulcrum2/failure-analysis.md): duplicate
  authority, retry amplification, and uncertain external effects.
- [Handoff-delay incident][handoff-incident]: unrelated work blocked discovery
  for almost two hours; one failed bead must not stop other coordination.
- [Current setup](../setup.md) and [validation](../validation.md):
  infrastructure responsibilities and the bounded, provider-independent
  repository check.
- [Official MCP documentation][mcp-doc]: local transports, timeouts,
  configuration, and tool approval settings.
- [Official scheduled-task documentation][schedule-doc]: recurring follow-ups
  within an existing task and the requirement for the local app to be running.
- [Official goal documentation][goal-doc]: goals pursue continuing objectives;
  the Marshal lifecycle here deliberately does not require a standing goal.
- [Beads update command][bd-update] and [Dolt update
  implementation][bd-storage]: source matching the inspected installed Beads
  commit, establishing the single-issue transaction used by this design.

[handoff-incident]: ../postmortems/2026-09-16-bead-46a5eb5e-handoff-delay.md
[mcp-doc]: https://learn.chatgpt.com/docs/extend/mcp
[schedule-doc]: https://learn.chatgpt.com/docs/automations?surface=app
[goal-doc]: https://learn.chatgpt.com/docs/long-running-work
[bd-update]: https://github.com/steveyegge/beads/blob/6c124203e771/cmd/bd/update.go
[bd-storage]: https://github.com/steveyegge/beads/blob/6c124203e771/internal/storage/dolt/issues.go#L135-L184

## Evidence and capability contract

The design inspection on 2026-09-16 observed Desktop build 26.908.70816 (9275)
and Beads 1.2.2 at commit `6c124203e771`. These identify observations, not
runtime compatibility selectors. Startup checks capabilities, not application
versions.

The native task tools' callable descriptions and argument schemas were
inspected. A read-only `list_projects` call succeeded and returned saved project
IDs, paths, hosts, and Git-repository flags. Task creation, mutation, and
long-wait tests were not performed while authoring this design. The schemas
establish which arguments can be expressed, not successful end-to-end behavior.

During inspection, the bundled `codex_app` MCP initially failed because Desktop
had not supplied `CODEX_APP_TOOLS_PIPE_PATH`. The user rebooted, after which the
project-list call succeeded. This establishes a recoverable startup failure in
that session, not removal of the tools or a guarantee that reboot fixes every
installation. Instructions mentioning a tool are not proof it is callable.

Bootstrap requires callable `create_thread`, `send_message_to_thread`,
`list_projects`, `list_threads`, `read_thread`, `wait_threads`,
`set_thread_archived`, and `automation_update`, plus Fulcrum MCP. It checks
required argument fields and accepted result shapes. An omitted or unsupported
required model/effort pair is a visible failure, never a substitution.

### Mapping the existing runtime

Each native call below is executed by an agent. Fulcrum never invokes an
internal Desktop pipe, database, WebSocket, App Server endpoint, or GUI
automation to compensate for a missing task tool.

| Existing capability | Stock Desktop mechanism and resulting behavior |
| --- | --- |
| Create/start a worker | `create_thread` with exact prompt, model, `thinking`, title, and saved project ID. Creation also starts its initial prompt; worker registration gates substantive work. |
| Continue an existing task | `send_message_to_thread` with exact prompt and settings. It is an externally recorded action, not an assumed exactly-once send. |
| Resolve projects | `list_projects`; validate exact root and host. No observed native project-creation tool exists, so bootstrap guides the user to add a missing saved project. |
| Set a worker's actual cwd | No arbitrary existing-worktree parameter. Create under the saved project with `environment.type=local`, then use verified absolute Tollgate worktree paths. |
| Configure allowed roots or permissions per task | No corresponding create-task fields. Validate access under the user's Desktop configuration and report missing access explicitly. |
| Deliver role instructions | Put the complete cooked role contract in the native user-visible prompt. Developer-message injection is unavailable. |
| Select model/effort | Use explicit `model` and `thinking` arguments derived from authorized Fulcrum configuration. Reject unavailable settings. |
| Observe worker progress | Workers report through MCP; provider watchers publish existing-work events. No routine native task inventory polling. |
| Inspect one unresolved task | `wait_threads` with one target and `timeoutMs=0`; use `read_thread` for scoped evidence when needed. Schedule later checks through broker timers. |
| Recover a lost creation reply | Worker self-registration and correlated native history. Inventory search is a bounded exceptional recovery operation, never a normal polling loop. |
| Archive/unarchive | `set_thread_archived`; retain the existing archive-once and manual-unarchive rules. Archival is not termination. |
| Interrupt a worker or answer a native approval | No observed task-tool equivalent. Expose the specific Desktop task for user action and retain the blocker. Do not invent a response or start a conflicting writer. |
| Delete tasks/projects | No observed native deletion tool. Mark automatic deletion unavailable; retain/archive owned task evidence. Hard reset cannot claim native deletion occurred. |
| Release subscriptions, inspect arbitrary terminals, measure native FD use | Desktop owns its runtime resources. Do not reproduce these App Server controls or infer their state from process-name guesses. |
| Read output and accounting | Native summaries are scoped evidence and may be truncated. Missing turn IDs, detailed tool output, or usage accounting remain unknown. |
| Wake a dormant Marshal | A Weaver or other authorized existing-work agent executes a returned native message action; an hourly same-task follow-up is the fallback. |

All new work enters through `$weaver`. Internal CLI commands still perform
registration, persistence, reconciliation, and delivery; direct terminal intake
is removed from the supported product workflow. Watchers may update existing
work but may not create new assignments. Vizier may resolve policy or activate
previously authored work under its existing authority; that transition returns
the same attention instructions as Weaver's handoff.

The normal Executor-to-Warden-to-delivery path must work autonomously after
setup. Optional lifecycle controls can report unavailable without preventing
that path. If required workspace access, coordination tools, or outcome evidence
are unavailable, bootstrap reports the affected capability and leaves admission
closed. It never silently enables an experimental runtime.

## Process ownership and source freshness

The resident boundary changes from owning a native runtime connection to owning
local transport continuity. Policy still belongs in fresh processes.

| Component | Responsibility |
| --- | --- |
| Thin MCP instance | Decode requests, invoke source-following CLI operations, and forward waits to the broker. No independent queue, ledger, or scheduler policy. |
| Unix-socket broker | Own wait registrations, notification generations, timer heap, and bounded job scheduling. Keep reconstructible indexes in memory. |
| Fresh CLI operation | Read current Beads/configuration, validate authority, commit transitions, compute prompts/actions, and perform provider operations. |
| Marshal agent | Answer explicit judgment requests, execute claimed native actions exactly, and report results. |
| Worker agent | Register, validate its assignment/workspace, do authorized work, report progress and outcomes, and execute authorized post-finish instructions. |
| Provider watcher | Observe an existing provider resource, publish meaningful changes durably through fresh CLI operations, and notify the broker. |

The broker imports only transport/bootstrap mechanisms. It receives timer
descriptors and wake reasons computed by fresh CLI processes, rather than
deciding what an expired timer means. One broker per instance holds a kernel
process lock; stale socket cleanup is permitted only after acquiring that lock.
Closing an MCP instance does not terminate the broker or another task's wait.

Each policy operation resolves local master at launch and pins one source and
interpreter for its lifetime. A parked wait holds no application process or
state lock: arrival of an event launches a fresh instruction computation. That
computation uses current master even if the MCP instance has been alive for
days. An already-issued action retains its recorded arguments until settled or
explicitly superseded before execution; a newer source commit does not silently
rewrite it. Source preparation failure is visible and blocks new actions.

Use existing detached mutation processes and temporary output files. A client
timeout returns the operation locator without killing a possibly successful
mutation. Beads-only inspection and repair remain possible with the broker
stopped. Ordinary source edits require no installation, activation, remote push
completion, or resident restart. Skills retain direct links to local master.

## Beads commit boundaries

**A transition entry** is the accepted request, resulting state, and outstanding
effects retained on the authoritative bead. **An action** is one externally
executable effect with an ID and fixed arguments. These entries make an accepted
transition recoverable without consulting the broker or a second database.

For ordinary work, the work bead is authoritative for its ownership, phase,
accepted outcomes, reservations, event progress, and actions. Standing
leadership and bootstrap have the same transition structure on `fc-system`; a
task summary never becomes a second authority. Configuration remains
authoritative in YAML.

`fc-system` is the instance's singleton control bead. It holds infrastructure
and leadership actions, not copies of per-work authority. A transition affecting
several work beads is a recoverable sequence of independent commits, never a
claimed multi-bead transaction.

The inspected Beads update path sends ordinary metadata, status, and assignee
changes to one transactional issue update. Label changes, dependencies, other
issues, filesystem effects, and native task operations are outside that commit.
`bd batch` cannot update the required arbitrary metadata and is not the
solution. Fulcrum uses ordinary Beads commands, with no raw SQL writes or Beads
fork.

### Committing a local transition

Every fresh command follows the same algorithm. It serializes cooperating
writers through the existing process-shared state lock, not a Python-only lock.

1. Acquire operation/resource ownership and the shared writer lock. Read fresh
   authoritative records, current policy, and retained request evidence.
2. Return the saved result for the same request ID and equal input. Reject
   changed input under that ID. Validate actor, ownership token, and state
   preconditions.
3. Compute one replacement work state, including accepted input/result, incoming
   event progress, pending actions, and any capacity reservation.
4. Commit those fields, native assignee, and native status through one ordinary
   `bd update`. Do not include separate label/dependency writes in the atomicity
   claim. Verify the returned record; inspect on an uncertain response.
5. Release the writer lock. Notify the broker and return the recorded result.
   Notifications are hints; their loss cannot erase the pending action.
6. Materialize task summaries and historical operation receipts from the
   committed entry. Keep the entry until those copies are verified.

No shared state lock spans agent execution, network calls, waiting for a
provider, or an MCP wait. An unchecked old whole-record snapshot is never
written after such a boundary. New progress and action acknowledgments merge
against fresh state with the same ownership checks.

The Beads mutation subprocess inherits the writer-lock descriptor until it
exits. Killing the requesting CLI must not release exclusion while that child
can still commit an old replacement. Kernel locks are never unlinked. Recovery
obtains the same lock before inspecting an uncertain write; an orphan that keeps
it indefinitely becomes an explicit storage blocker, not permission to bypass
the lock.

For Weaver ready, the one commit includes the following related facts:

```text
accepted request and exact result
prepared scope and acceptance
owner = standing Marshal; phase = backlog
attention generation and pending wake/review action
consumed incoming event, if any
```

Changing a historical receipt to completed is not what makes the handoff true.
If a receipt is absent or stale, commands consult the work bead. Historical
copies use predetermined IDs and can be regenerated. A failure to write a copy
is reported as a projection failure, without undoing accepted work or blocking
unrelated beads.

Transition entries may accumulate while a projection is unavailable. Remove a
completed entry only after its request/result and terminal action evidence have
been copied and verified in Beads. Retries check both retained entries and their
historical receipts. Pending actions/events are never pruned for size. At 128
unprojected completed entries on one bead, stop new transitions on that bead
until projection repair succeeds; other beads continue. This bounds ordinary
growth without silently losing deduplication evidence.

### Concurrency and capacity

All Fulcrum mutations, including MCP, watcher, recovery, and operator commands,
use the same lock. Direct edits to reserved workflow metadata are break-glass
operations. This is the existing trusted local-user model, not exclusion against
an arbitrary process writing Beads or Git behind Fulcrum's back. Observed native
assignee conflicts fence the affected bead for reconciliation.

Admission reads authoritative reservations while holding the writer lock and
commits the new reservation on the selected work bead before releasing it. Count
reserved, issuing, active, and uncertain assignments against capacity. Do not
maintain a separately authoritative counter or derive admission from
task-summary rows. A crash after reservation consumes a slot until that specific
reservation is settled; it cannot admit a replacement merely because no task ID
has yet arrived.

A reservation is identified by its dispatch action and assignment token. An
Executor-to-Warden transfer reuses its logical work slot only after the old
assignment relinquishes source-writing authority. Late Executor reports cannot
acquire the Warden's assignment. Independent work admission sees the complete
committed reservation or its absence, never a counter/work-row disagreement.

### Events and cursors

Store each meaningful worker or watcher event on its authoritative work bead
before acknowledging publication. Workers use stable request IDs. Watchers use
their provider's stable event ID when available; snapshot watchers persist a
locally allocated event ID in the same work-bead update as the snapshot. Under
the lock, compare the observation with the latest accepted snapshot for that
resource. Equal state returns the saved acceptance; changed state receives a new
ID and sequence. A restarted watcher rereads the current provider state rather
than replaying an unrecorded old sample. Compare values directly, not content
hashes. Native observation timestamps alone are not IDs.

Each work bead has an increasing acceptance sequence and pending-event entries.
Under the writer lock, append an event and allocate its sequence in the same
update. Processing advances that bead's consumed cursor together with the
workflow effects and next actions. It processes accepted events in order; a
failed event blocks only its work bead. An external event that merely prompts
inspection cannot establish a stronger fact than the authoritative provider
observation used by the transition.

```text
publish -> one bead update: event ID, payload, acceptance sequence
process -> one bead update: resulting state, consumed cursor, actions
notify  -> best-effort broker signal referencing that bead
```

There is no global durable queue cursor. Marshal discovery uses pending entries
and per-bead cursors; broker order is not workflow authority. A reconnect scans
all relevant pages of active work and unfinished control actions. A bounded
result page must return continuation state, never imply the remaining work is
absent. Ready work is ordered by authorized priority and then stable bead ID.

Unchanged provider observations, timer ticks, wait renewals, and socket wakeups
do not create durable events. A deadline is durable on the pending action or
assignment; an expired timer is recomputable. Coalesce only redundant wake hints
and superseded observations whose evidence is retained. Never coalesce away a
worker outcome, approval decision, or outstanding external effect.

### Crash outcomes

The protocol has one local commit point and an explicit uncertain boundary for
external operations. It does not promise exactly-once native task execution.

| Interruption | Required recovery |
| --- | --- |
| Before the authoritative update | Replay the same request; no new transition was accepted. |
| Beads commit succeeded but reply was lost | Inspect its retained request ID/result before any retry. |
| Commit succeeded but notification was lost | Pending state remains discoverable by reconnect or hourly recovery. |
| Summary or historical receipt write failed | Recreate it from the authoritative transition; no workflow replay. |
| Event processing committed but publisher missed acknowledgment | Repeated event ID returns its recorded acceptance/result. |
| Agent disappeared before claiming an action | The pending action remains eligible for an authorized actor. |
| Native effect may have happened | Preserve issuing/uncertain state and inspect its exact locator. Never reset it to pending on a timeout. |
| Beads is unavailable | Acknowledge no new event or outcome; start no new external action. Preserve accessible diagnostics and return a retryable failure. |

## Shared agent action interface

The following names define the proposed Fulcrum interfaces, not commands claimed
to exist today. MCP routes all business operations to the corresponding fresh
CLI handler. `fulcrum finish` and MCP `finish` return the same recorded result
and action envelope; managed workers use MCP for reports.

| Interface | Contract |
| --- | --- |
| `bootstrap` | Run/recover deterministic setup and return the next native action or explicit user prerequisite. |
| `register_worker` | Bind the native task/host ID to the exact creation action and assignment before substantive work. |
| `report_progress` | Persist meaningful progress and renew the assignment's reporting deadline. |
| `finish` | Accept a role outcome, commit the transition, and return exact authorized follow-ups. |
| `wait_for_instruction` | Acquire/renew Marshal coordination and return a decision request, native action, renewal, pause, or idle result. |
| `claim_action` | Revalidate a pending action and durably mark its execution attempt before an external call. |
| `report_action_result` | Record success, definitive rejection, or uncertainty and compute dependent work. |
| `marshal_decide` | Validate bounded judgment against the supplied scope and current state; commit accepted decisions independently by bead. |

Every mutating request includes a stable request ID, actor task ID, and relevant
assignment/leadership token. The MCP instance derives caller identity from its
Desktop-provided session context where available; otherwise it requires the
explicit native ID and checks it against registration. Tokens prevent accidental
stale operations in the cooperative model; they are not a security boundary
against another process controlled by the same local user.

An action contains these fields. Complete prompts are stored without ellipses;
the small example illustrates the shape only.

```json
{
  "action_id": "random-action-id",
  "record_id": "work-bead-id",
  "actor_thread_id": "weaver-thread-id",
  "ownership_token": "assignment-token",
  "tool": "send_message_to_thread",
  "arguments": {
    "threadId": "marshal-thread-id",
    "prompt": "Fulcrum action random-action-id: call wait_for_instruction.",
    "model": "configured-marshal-model",
    "thinking": "configured-marshal-effort"
  },
  "expected_result": "native send accepted; target and host match",
  "report_to": "report_action_result"
}
```

The compiler uses the actual native argument names, including `thinking` for
reasoning effort. It includes the configured model/effort whenever an action
creates or continues a Fulcrum task. The bootstrap authorization explicitly
covers those settings and local-project targeting; agents do not infer either
from a tool default. Omitted fields are deliberate compiler choices for tools
that do not accept them, never permission to invent a setting.

Action state is `pending`, `issuing`, `succeeded`, `rejected`, `uncertain`, or
`superseded`. Only a pending, unissued action can be superseded automatically
when its assumptions change. The agent claims one action, receives the persisted
arguments, calls the named tool once, and reports its result before claiming
another mutation. Independent agents can execute actions for different work.

`claim_action` commits the executing actor, current token, attempt ID, and
expected result before returning arguments. A repeated claim while issuing
returns status and reconciliation instructions, not permission to resend. A
definitive rejection permits a fresh recorded attempt after its cause is fixed.
An ambiguous error is uncertain even if its message sounds like a timeout.

Every native message and creation prompt carries its action ID as literal
content. The recipient acknowledges that ID through MCP before acting on the
message. Schedule prompts carry their creation ID and instance marker. These are
correlation markers, not native idempotency keys. The saved result-reporting
instructions include record ID, action ID, attempt ID, request ID, and token;
the actor must not reconstruct them from prose or replace them on retry.

Successful `finish` transfers workflow ownership. It also grants the old worker
a narrow, retained permission to execute and report its returned follow-up
actions. That permission does not allow another finish, a scope amendment, or
editing after transfer. Once the last follow-up is reported, the worker ends its
turn. A missing follow-up report remains a recoverable obligation.

### Registration and uncertain native creation

`create_thread` combines task creation with an initial turn. Its first prompt
therefore requires registration before repository work. The complete role prompt
includes instance, work bead, creation action, assigned role, exact workspace,
assignment token, model/effort, and reporting instructions.

The creator immediately reports `threadId` and `hostId` through MCP before any
unrelated action. Fulcrum binds them to the authoritative work/control record. A
returned `clientThreadId` is retained as a pending setup locator and is never
passed to a tool requiring `threadId`. Registration by the new worker can finish
the binding even if the creator never receives a final creation response.

The registering worker obtains its native task ID from supported session context
or `CODEX_THREAD_ID`; it may not invent an ID from a title. Registration
verifies the exact creation action, role, project/host, assignment, and
workspace evidence. One native task can win that assignment. A second claimant
records a conflict and receives no permission to edit. Duplicate native tasks
are possible after an uncertain external boundary; duplicate authorized workers
are not.

If creation is uncertain and no worker registers, recover through the retained
locator and a bounded native inventory/history search for the exact creation
marker. A title alone, truncated summaries, or absence from a recent-task list
cannot establish absence. Ambiguity remains scoped recovery with no replacement
creation. The Desktop surface lacks a client-supplied creation idempotency key;
the design does not invent one.

### Settling other native effects

An uncertain action keeps its reservation and dependent work blocked until
positive evidence settles it. Read-only inspections can be retried; they do not
authorize retrying the mutation they inspect.

| Effect | Settlement evidence and retry rule |
| --- | --- |
| Send a message | Recipient acknowledgment of the exact action ID, or exact persisted native message content and target, proves delivery. A recent summary's omission proves nothing. Without either, keep uncertain. A definitive tool rejection before acceptance permits a new attempt. |
| Create a schedule | Adopt one exact inventory match for instance, action marker, and target, confirmed by native view. Zero incomplete-inventory matches or several candidates remain uncertain. |
| Update/pause a schedule | Native view must match the intended target, cadence, prompt, notification policy, and state. Read back before any repair attempt. A different value alone is not proof the earlier call cannot still land; reconcile the old caller first. |
| Archive/unarchive a task | Native inventory/history must positively show the requested state for the exact task. Missing from the recent list is not archived evidence. Do not repeat a possibly still executing call. |
| Delete a retired paused schedule | Native view reports the exact retained ID absent and complete local inventory confirms absence. Unknown/error is not absence; keep replacement fenced. Never delete a merely similar schedule. |
| Inspect a task | Record the returned observation or error. An interrupted read may be issued again under current authority; it has no native mutation to duplicate. |

A recipient acknowledgment commits delivery evidence on the action's owning bead
even if the sender's result is lost. It never marks the message's requested
workflow complete. Conversely, native send acceptance records successful
notification but does not clear Marshal attention; the work transition that
handles the attention does that. The sender may report only actions granted to
it; the recipient's acknowledgment grants no sender authority.

Recovery may create a new wake action for a later recovery generation while an
older message remains uncertain. This is an explicitly redundant coordination
hint, containing no worker dispatch or editable scope. Keep the older attempt
uncertain, and suppress new hints while one current recovery hint is pending.
Never use this exception for creation, worker instructions, or schedule changes.

## Marshal lifecycle and wake delivery

**Coordination idle** means there is no runnable decision/action, progressing
worker, in-flight provider operation, or pending targeted completion check
requiring attention. Open historical/control records do not keep Marshal
running. Human-blocked or explicitly deferred work alone does not keep a turn
alive. The hourly follow-up can reconsider elapsed durable deferrals; resolving
a blocker through an agent returns immediate wake instructions.

`wait_for_instruction` distinguishes coordination idle from waiting for an
active worker. It computes under the writer lock, records the control state, and
returns one of the following:

- `decision`: bounded Marshal judgment with exact scope and comparison facts.
- `action`: claim and execute the returned native operation.
- `wait`: keep the MCP request parked while current work is in flight.
- `renew`: the long wait reached its renewal boundary; call again.
- `idle` or `paused`: release the coordination lease and end the agent turn.

`wait` is the CLI's internal disposition to the MCP front end; it does not
complete the agent's MCP call. The broker holds that call until another listed
result is ready. A human-blocked worker with no due inspection is not
progressing work for the idle test; its durable blocker still retains the
assignment.

The default parked interval is 30 minutes, with MCP `tool_timeout_sec` set to 31
minutes so the server can return a renewal first. This configuration must pass
the real Desktop long-wait test. A client that cannot support it is reported
unready; the implementation does not silently fall back to frequent model turns.
Users may configure another interval explicitly, subject to the same test.

### Wait registration and idle races

The MCP front end registers a waiter with the broker before requesting a fresh
state evaluation. The broker maintains an in-memory notification generation.
After evaluation, park only if no signal arrived since registration; otherwise
evaluate again. On broker restart, reconstruct pending work and deadlines from
Beads before accepting waits. No policy process or state lock remains parked.

When returning idle, commit `coordination=idle` on `fc-system` under the same
writer lock used by incoming work transitions. Weaver's handoff always retains a
Marshal-attention obligation on its work bead. It returns a native wake action
when Marshal is idle, absent from the broker's wait registry, or has a stale
coordination lease. When Marshal is waiting, a broker signal suffices for prompt
delivery; the durable obligation remains until Marshal records its handling.

An event committed before the idle check is observed by that check. An event
committed afterward sees idle and produces a wake action. If notification or
native sending still fails, the pending obligation survives for recovery.
Repeated wake messages are harmless prompts to reread authoritative state; they
never contain a second independent dispatch authorization.

Native messaging can arrive before the previous turn has visibly ended. The
acceptance check must demonstrate that the message is queued or delivered
without losing either action receipt. A resumed turn starts by settling any
issuing action and reading current state, not by replaying its last tool call.

### Leadership leases

`fc-system` retains the current Marshal native ID, lease token, expiry, and
coordination state. A fresh CLI operation grants or replaces the lease under the
shared writer lock. The default lease is five minutes. While a registered MCP
wait is connected, the broker requests a fresh CLI renewal every two minutes;
these are local operations, not agent turns or durable workflow events.

Only the currently bound Marshal native task may acquire that lease. Expiry
allows its next turn to reacquire, not another native task to adopt the role.
Changing the bound native ID requires the explicit fenced replacement protocol
below. Competing unregistered Marshals are rejected even if the lease expired.

Lease renewal updates only the current control lease and its monotonically
increasing renewal sequence. Retain the latest renewal request/result there; an
equal sequence retries that result, and a lower sequence is rejected as
superseded. It does not append historical transition receipts. This bounded
liveness metadata cannot authorize a work transition by itself.

Validated Marshal wait, claim, decision, and result requests renew its current
lease under the writer lock. A request with an expired token first reacquires
leadership; it cannot silently replace another owner's lease. An agent making a
native call must hold a current lease at action claim. A claimed action remains
issuing if the lease expires during the call. A later turn of the registered
Marshal may coordinate unrelated work, but cannot reissue that action. Late
results are retained as evidence and reconciled against its exact attempt; an
old lease cannot authorize new effects or overwrite newer ownership.

Changing leadership does not fence a native call that has already left the
agent. Worker registration and assignment checks prevent it from becoming a
second authorized writer. A stale worker already editing files requires observed
termination before another source-writing assignment starts. Desktop has no
observed native interrupt tool, so that exceptional case requires user action.

### Hourly recovery

Bootstrap creates one heartbeat attached to the Marshal task, recurring hourly.
It does not create a new standalone task each hour. The saved prompt directs
Marshal to call `wait_for_instruction` with recovery intent, settle durable
pending work, and end if idle or paused. Notification policy is stored through
the automation tool; the prompt says to produce no routine status message when
there is no actionable change.

The hourly trigger is a nominal cadence, not a guaranteed recovery SLA. Desktop
must be running and the machine available. Busy-task scheduling behavior must be
tested: a healthy blocked wait must not accumulate an unbounded prompt queue or
interrupt an unrecorded native mutation. Persisted action claims make an
interrupted call recoverable; they do not prove whether that call happened.

An hourly prompt on an already active or resumed task reevaluates state, rather
than admitting a second coordinator. It cannot repair a broken native-tool MCP
connection by itself. Bootstrap/doctor reports that dependency failure with the
specific missing capability. A task that remains natively hung despite scheduled
follow-ups requires a visible operator recovery action; this design promises no
automatic kill through an unavailable tool.

Explicit Fulcrum pause is durable and honored by every entry point. A user Stop
is not interchangeable with a crash. The control bead holds
`run_control=enabled|paused|resume_required`, a reason, and the accepting
request. An explicit agent-mediated stop commits `paused` before acknowledging
it. Existing YAML policy pause also denies work; the control bead does not
mirror or override that policy. Only an explicit authorized resume clears a
pause.

On an explicit MCP cancellation, the front end invokes a fresh CLI operation to
record `resume_required` before releasing the wait. A socket disconnect alone is
recorded as transport loss, not as a claimed user stop. If cancellation cannot
be persisted, no success acknowledgment is returned. Before recovery or an idle
wake, Fulcrum returns a targeted inspection action to the requesting agent when
the prior Marshal turn's exit reason is unresolved. An unknown reason commits
`resume_required`; it does not guess crash or Stop. Confirmed transport failure
can recover automatically, preserving all issued actions.

All wake claims, lease acquisition, hourly recovery, and dispatch check this
control state under the lock. While paused or requiring resume, retain work and
outcomes but issue no new work or wake mutations. Previously issued effects
remain subject to reconciliation; read-only diagnostics and explicit recovery
actions remain available without a coordination lease. Pausing the Desktop
schedule stops its triggers; the durable Fulcrum pause stops workflow actions
from every entry point.

Desktop's Stop outside an MCP wait needs a supported native interruption fact or
native suppression of later messages/scheduled execution. The readiness probe
must demonstrate one of those protections before accepting unattended operation.
If Desktop exposes only an indistinguishable completed turn and automatically
resumes after Stop, this installation fails the stop-safety gate. The current
tool schemas do not establish that protection. Do not claim a prompt mentioning
Stop supplies it, or silently reinterpret Stop as consent to resume. Bootstrap
discloses the missing capability and leaves admission closed. No standing goal
is created or resumed.

## Reporting, targeted checks, and delivery

Workers report meaningful progress at least every ten minutes when able to call
tools. The reporting deadline is thirty minutes after registration or accepted
progress. A tool that runs longer may delay reporting; deadline expiry requests
inspection, not a fabricated failure or duplicate worker.

After an accepted finish, checks of that exact native task protect handoff and
cleanup. The first check is immediate. Unresolved checks use delays of 5, 15,
45, 135, then 300 seconds, capped at 300 seconds. An accepted new progress event
resets the reporting deadline and ends the obsolete missing-report check. Each
pending check retains its purpose, target, attempts, and next due time on the
work bead. Repeated observations update that check, not a new operation row.

The broker wakes Marshal at the recorded deadline. Fulcrum returns one
`wait_threads` action with that target and `timeoutMs=0`. `read_thread` is added
only when the compact snapshot cannot answer the specific recovery question.
There is no polling inventory and no long `wait_threads` loop. The native cursor
is retained as observation context; it is distinct from Beads event progress.
Absence of newly returned final text does not prove a task is running or
complete.

Accepted completion evidence is a successful native snapshot identifying the
exact task and explicitly reporting that its current turn is no longer running,
with the registered assignment identified in its observed history. When a turn
ID is available, retain and match it. Otherwise require the registered role's
completion acknowledgment plus a current non-running snapshot and no unresolved
continuation action. A final-text snippet alone is insufficient. Unknown status,
per-target errors, ambiguous history, and missing targets remain unknown.

- A task still running retains its assignment/capacity as appropriate and gets
  another targeted check. Thirty minutes of overdue reporting also creates a
  scoped Marshal recovery decision; it never authorizes a concurrent writer.
- A completed turn without a finish report creates a missing-outcome recovery
  action. Native completion alone cannot approve source or close the work.
- A native approval or user-input request becomes a visible, specifically
  identified blocker. No agent invents the answer. Resume instructions follow
  the durable resolution of that blocker.
- A failed or unknown inspection retains the worktree. Repeated identical
  failures are summarized, back off, and remain isolated to that bead.

Executor finish seals its source-writing outcome and returns any follow-up
reporting actions. Warden starts only after the old assignment has relinquished
source access and its native turn is observed complete. Warden can fix findings
directly. One accepted Warden finish seals the review judgment for the exact
source; delivery does not ask Warden to finish again.

Fresh CLI operations continue to own provider submission, exact-source
validation, promotion, remote synchronization when required, and cleanup.
Provider watchers publish their state changes to existing work. A successful
native turn or notification is never promotion evidence. Changed source
invalidates review under the existing rules. Cleanup failure after promotion
does not turn delivered code back into an implementation failure.

Before deleting a worktree, require accepted role outcomes, observed native
completion, settled delivery, and no unresolved owned background-process
obligation. Worker instructions require terminating their owned background
commands before finish and reporting any retained process locators. A completed
turn is not proof that all terminals or subprocesses ended; unknown resource
ownership blocks automatic deletion. Never terminate unrelated processes.

## Workspace and authority preservation

Fulcrum prepares the Tollgate worktree and records its actual path, branch,
base, and ownership evidence before creating the Executor. Both Executor and
Warden are created against the enrolled saved project with explicit local
targeting. Their prompts require absolute-path operations in the assigned
worktree.

Before permission to edit, `register_worker` requires the worker's observed Git
root, branch, and HEAD plus Fulcrum's provider-backed workspace inspection. For
Executor, the workspace must match the clean prepared assignment; for Warden, it
must match the submitted source. Verify access with a disposable file in the
assigned worktree during readiness testing. An inability to access it returns a
setup blocker; it never authorizes editing the saved project's main checkout.
Tools resolving paths relative to the native task's cwd must receive explicit
absolute paths or an explicit working-directory argument.

The native UI may show the saved project while commands use the assigned
worktree. This is an accepted product difference, disclosed at registration.
Fulcrum cannot configure an arbitrary native cwd or enforce filesystem access
through `create_thread`. The boundary is verified configuration and cooperative
agent instructions, with stale operations rejected by Fulcrum.

Marshal judgment remains limited to ownership, priority, capacity, dependencies,
overlap, deferral, policy application, recorded blockers, and usable outcomes.
It may not invent worker settings, implementation instructions, or new policy.
Vizier/human policy authority and existing plan-approval requirements remain.
Downstream prompts use the exact approved Weaver scope, never raw intake that
could accidentally invoke Weaver again. Sage, Mason, and Justiciar receive their
existing bounded roles; recovery does not silently relabel an agent human.

## The fulcrum-bootstrap skill

`$fulcrum-bootstrap` is the user-facing first-run and repair entry point. It
orchestrates deterministic CLI setup and native actions; it does not embed
workflow policy in prose or ask the model to reconstruct setup state.

The skill accepts a retained Fulcrum checkout and optional existing instance
selection. It discovers installed prerequisites and existing configuration
before asking for missing brain location, project enrollment, credentials, or
user-owned model choices. It runs on macOS under the existing local-user trust
model. A missing required executable is reported with the exact prerequisite;
setup never starts an experimental App Server as a workaround.

The proposed command entrypoints are concrete. They replace the current setup
command's App Server assumptions; these examples must not be run against the
current implementation expecting this design's behavior:

```sh
scripts/setup --input setup.json --non-interactive --json
fulcrum bootstrap --instance /absolute/instance --request-id UUID --json
```

The first command performs local infrastructure setup only and returns the
retained bootstrap record. The second resumes that record and emits the shared
action envelope or a structured prerequisite. `setup.json` supplies brain,
projects, provider credentials references, and per-role models; it contains no
runtime endpoint. Reuse the retained checkout's source-following launcher when
it already exists. CLI results provide exact `next_command` argv arrays for
local steps; the skill executes them literally, without shell interpolation.
Once MCP is connected, its `bootstrap` handler uses the same command protocol.

### Setup algorithm and authority

Before Beads exists, use idempotent filesystem/service setup primitives and
inspect their actual postconditions on rerun. Do not introduce a pre-ledger
workflow journal. Once the ledger is available, `fc-system` retains the
bootstrap transition and deterministic locators for every planned native
resource.

1. Discover checkout/local master, dependencies, brain/provider configuration,
   and existing setup. Prepare the isolated dependency environment, source-
   following launchers, Beads services, skill links, and local broker using
   deterministic commands. Preserve unrelated files and configuration.
2. Configure local Fulcrum MCP and its long-wait timeout through supported Codex
   configuration. Restart only that MCP connection when initial configuration
   requires it; a full application restart is a reported exceptional
   prerequisite, not an ordinary update step. Resume the same bootstrap record
   afterward.
3. Inspect callable native tools and run read-only connectivity checks. Explain
   exact missing fields or tools. Discover saved projects; guide the user
   through adding a project if the native tool surface cannot create it.
4. Present the explicit coordination authorization: native task creation and
   messaging for admitted work, configured model/effort values, saved-project
   local targeting with Tollgate worktrees, and hourly same-task recovery. The
   user's bootstrap request supplies this scope; request only missing choices or
   permissions Desktop itself requires.
5. Merge supported persistent per-tool approval settings for Fulcrum MCP and the
   native coordination tools. Verify effective settings after reconnection. Do
   not expand filesystem permissions or disable the sandbox as a side effect.
6. Create or recover projectless local Marshal and Vizier tasks. Their complete
   initial prompts require registration against the bootstrap action before
   performing leadership work. Vizier completes readiness and waits for human
   direction; Marshal coordinates pending work or ends idle.
7. Create or update the hourly heartbeat on Marshal. Persist its returned ID,
   exact target, prompt, cadence, and observed state on `fc-system`.
8. Exercise the readiness probe described below, finalize the bootstrap result,
   and expose task links, schedule identity, verified capabilities, and any
   unresolved operator step. Open admission only after required checks pass.

Bootstrap explicitly authorizes the named native operations in its instructions;
it does not expect an agent to infer permission to create user-visible tasks.
Model configuration is confirmed as the user's desired configuration when not
already established. Both leader and worker prompts retain that authorization.

The native task tools are bundled under `codex-app-tools`. Supported per-tool
approval overrides allow creation, messaging, and schedule management to run
without repeated approval prompts. Configure only the required tools, including
Fulcrum's report/claim operations; do not set a blanket approval for unrelated
plugins. State the effective scope: a host-wide per-tool approval may apply to
that tool in other tasks and cannot be represented as argument-level restriction
to one Fulcrum instance. Preserve existing stricter organizational policy; if it
prevents unattended coordination, report that limitation instead of bypassing
it.

### Reruns and lost bootstrap responses

For each leader, persist the planned creation action before creation. Matching
registration can recover a lost response. Never adopt an unrelated task based on
a matching title. A missing original leader is replaced only through explicit
recovery, carrying forward authoritative policy and unresolved obligations.

For the schedule, inspect the recorded automation ID first. If creation may have
succeeded without returning its ID, use Desktop's documented local automation
inventory to match its exact target and instance/action marker, then confirm
with `automation_update` view. One exact match is adopted; multiple matches or
an unavailable inventory leave the step uncertain. Do not create a second
schedule to resolve uncertainty. Update through the native automation tool,
preserving fields unrelated to this request; never write scheduler files.

A bootstrap call may return setup accepted with a remaining prerequisite. That
does not mean ready. Its retained control record determines the next step after
credentials, approvals, or a required MCP restart are supplied.

### Replacing a standing Marshal

Replacement is an explicit bootstrap recovery operation, not a consequence of
lease expiry. Its steps live on the control bead and resume from observed
postconditions after interruption:

1. Commit a leadership-replacement fence. Deny new dispatch, wake claims, and
   leases; accept existing reports. Retain the old native ID and issued actions.
2. Pause the recorded heartbeat and verify its native state. Obtain observed
   termination of the old Marshal, requesting user action when necessary. Settle
   its known effects or carry unresolved attempts forward unchanged.
3. Create/register the replacement under a retained bootstrap action. The new
   task performs no coordination before the control record binds it. Lost
   creation responses use the normal registration/uncertainty protocol.
4. Commit the replacement ID and fresh leadership generation on the control
   bead. Scan work beads independently: supersede only unissued wakes targeting
   the old leader, and retain each work's attention until the new Marshal
   handles it. A claim always checks the current control generation, so a crash
   midway through this scan cannot authorize an old pending wake. Issued old
   messages remain uncertain or settled evidence; their receiver has no current
   authority.
5. Retarget the existing paused heartbeat through native update and verify it.
   If retargeting is unsupported, delete the confirmed paused old heartbeat,
   verify deletion, then create one replacement with a retained action marker.
   An uncertain deletion or creation stops recovery; never overlap active
   schedules. Missing required schedule operations leave the fence in place.
6. Activate and verify the one current heartbeat, then remove the replacement
   fence. Preserve any independent user pause. Return one wake action to the
   bootstrap caller if coordination is enabled and work needs attention.

No multi-record atomicity is required: the control fence survives until every
step has recorded its postcondition. Reruns rescan per-work obligations and view
the exact schedule; they never repeat an uncertain native mutation. Vizier
replacement uses the same identity/fence rules without heartbeat retargeting.

### Skill entrypoint contract

The eventual skill should use the following concise entrypoint, with this
design's interface contract implemented by the CLI and MCP handlers. This is
proposed skill content, not an instruction to run setup while reading the
document.

```markdown
---
name: fulcrum-bootstrap
description: Set up or repair Fulcrum on stock Codex Desktop, including local
  infrastructure, MCP, leadership tasks, tool approvals, and hourly recovery.
---

Inspect the retained checkout and existing instance, then run the deterministic
setup command: scripts/setup --input setup.json --non-interactive --json.
Resume with fulcrum bootstrap --instance ABSOLUTE_PATH --request-id UUID --json,
or the equivalent connected bootstrap MCP tool. Preserve the request ID on retry.
Use returned instructions; do not recreate policy or setup state yourself.

This setup authorizes the specified Marshal/Vizier tasks, configured worker task
creation and messaging, local-project targeting with assigned Tollgate worktrees,
required persistent tool approvals, and one hourly follow-up on Marshal. Confirm
missing model choices and explain the scope of each approval configuration.

For every returned native action, claim it, execute its exact arguments once,
then report the result through Fulcrum MCP. Record returned task and schedule IDs
immediately. Register leaders before they act. Resume retained uncertain steps
through inspection; never create replacements merely because a reply was lost.

Guide the user through prerequisites that supported tools cannot perform. Do not
start an experimental runtime, change unrelated settings, or claim readiness
until bootstrap returns verified ready. Never create a standing goal.
```

The hourly saved prompt has a similarly bounded purpose:

```text
Resume coordination for the registered Fulcrum instance through
wait_for_instruction with recovery intent. Honor explicit pause/stop state.
Settle retained actions before new effects. Follow Fulcrum's exact instructions.
If idle, end the turn without a routine status update. Report only actionable
failures, required user input, or meaningful completed work.
```

## Feasibility checks and operational evidence

The first assembled-product check crosses the actual boundary: a stock Desktop
Marshal coordinating a registered probe worker parks in Fulcrum MCP; a separate
disposable Weaver task commits prepared scope, executes any returned native wake
instruction, and Marshal receives that exact work once. This check includes real
Beads persistence, the Unix-socket broker, two MCP clients, and native Desktop
tools. Component doubles do not establish this behavior.

Bootstrap's readiness probe uses disposable control probe records, not product
work admitted outside Weaver. It verifies native creation/registration,
messaging, worker access to a disposable Tollgate worktree, report/result
delivery, explicit completion observation, and the required long-wait duration.
Retain probe task IDs and clean them through observed archive/cleanup actions.
Initial full setup is not ready while the long-wait probe is outstanding. On
rerun, preserve already verified steps unless effective tool schemas,
configuration, or connection behavior changed; a failed step never inherits an
earlier success silently.

These are manual compatibility checks on a disposable instance. They do not
restore the retired expensive live harnesses or make ordinary code edits run
external providers. The normal repository check remains provider-independent.

| Required observation | Failure consequence |
| --- | --- |
| Native task tools callable with required schemas | Admission stays closed; report missing tool/field or MCP startup error. |
| Thirty-minute wait returns an event promptly and renews before timeout | Report long-wait capability failure; no silent short polling substitute. |
| Another MCP client can report while Marshal waits | Report transport concurrency failure; do not serialize the whole instance behind a parked request. |
| Native IDs and assignment can be registered before worker edits | Refuse dispatch readiness. |
| Worker can use the exact Tollgate worktree | Block the affected project and show the required access change. |
| Targeted inspection distinguishes completion from unknown/error | Admission stays closed: safe review handoff and cleanup both require it. |
| Hourly same-task scheduling coexists with an active wait and respects stop | Report recovery scheduling failure; do not substitute standalone agents or a perpetual goal. |
| Restart reconnects or exposes a recoverable state without duplicate effects | Retain the affected action as uncertain and show its recovery step. |

Measure Beads calls and elapsed time independently from model latency. The
historical embedded-mode Weaver measurements found substantial per-command
overhead, including a small later sample of 13 subprocesses and about 11.6
seconds for a warm finish. They do not predict server-backed production latency.
The new protocol removes duplicate authoritative writes and avoids durable timer
ticks; it does not claim an unmeasured speedup.

Diagnostics report accepted transition time, source commit, Beads call count and
duration, broker notification time, action claim/result, native IDs, wake
latency, lease state, oldest unhandled event, deadlines, and projection
failures. Logs are bounded diagnostic evidence, not replay state. Failed work
must not abort discovery or completion of unrelated work. Monitor the age of
pending actions, not merely whether the socket answers.

## Cutover and ongoing maintenance

Existing operations finish on their pinned source and existing transport. Fence
new old-runtime admission, let active turns, pending approvals, provider work,
and source-writing assignments settle, and preserve their durable outcomes. Do
not switch a running worker to the new contract halfway through its turn. This
is a drained transport cutover, not a permanent dual-backend mode.

Under exclusive maintenance ownership, convert authoritative pending work to the
single-record transition representation. Reconcile existing task/receipt facts
before choosing authority; conflicting ownership remains fenced. Transfer
standing IDs only if native inspection verifies them. Run stock Desktop
bootstrap/readiness against the migrated instance, then retire Fulcrum's owned
App Server connection/service and obsolete configuration. Remove only resources
proven owned; never stop Desktop's own internal runtime. A failed cutover
retains the maintenance fence and repair evidence.

Use the existing explicit state-upgrade maintenance mechanism, with one
idempotent migration and no schema-version chain. Once cutover succeeds, remove
the old runtime path and update operational commands/docs to the new capability
contract. No stale configuration silently selects the old transport.

Ordinary application, formula, and instruction edits continue to become
available from local master on the next operation. Replacing broker
implementation or changing its state contract is exceptional maintenance: wait
for safe transport handoff, preserve durable obligations, and re-register waits.
An application policy change does not restart the broker, MCP instances, or
agent turns.

## Manual QA

Use a disposable instance, ledger, provider repository, and explicitly
authorized native probe tasks. Record observed results and locators; do not mark
a scenario passed because its component tests passed. Inject failures at
operation boundaries without modifying production state.

1. **Complete setup and rerun.** Start from a retained checkout with no
   instance. Configure infrastructure, MCP and approvals, register both leaders,
   create the hourly heartbeat, and pass readiness. Rerun; retain the same
   leader/schedule IDs and unrelated configuration. Missing project enrollment
   or credentials produces one exact prerequisite and resumes the same bootstrap
   record.
2. **Partial bootstrap.** Lose each leader-creation response and the schedule
   response separately. Recover from registration or exact automation evidence.
   Ambiguous matches remain uncertain with no duplicate creation. Interrupt
   before Beads exists and verify idempotent infrastructure inspection on rerun.
   Replace a missing Marshal and interrupt after every replacement step. The
   fence survives; old pending wakes cannot execute; issued actions survive;
   exactly one verified heartbeat targets the replacement before reopening.
3. **Idle handoff.** With no active work, Marshal ends its turn. Weaver submits
   ready scope through MCP, receives a wake action, sends it, and reports the
   result. Marshal reviews the stored scope; notification acceptance alone does
   not dispatch work. Repeat for Vizier resolving existing blocked work.
4. **Active handoff.** While Marshal waits, another MCP client publishes ready
   scope or a provider event. The wait returns promptly, with one committed
   transition and no inventory polling. A lost broker signal leaves the work
   discoverable through durable pending state.
5. **Idle race.** Commit Weaver ready immediately before and immediately after
   Marshal's idle-state write. In both orders, Marshal either sees the work or
   receives a retained wake obligation. Deliver the wake while the old turn is
   still ending; its queued/interrupted behavior loses no issued action.
6. **Long waits and cancellation.** Keep the real MCP call open for thirty
   minutes, publish near its renewal boundary, and cancel the client once. No
   event disappears, no writer lock stays held, and other clients continue.
   Confirm that a supported renewal does not create a new event/receipt per
   tick.
7. **Broker and app restarts.** Kill the broker after a Beads commit but before
   notification. Discard all its memory, restart, and rebuild pending work and
   deadlines. Restart Desktop during a wait and during native creation. The
   resulting registration/recovery settles state without blind redispatch.
8. **Every Beads write boundary.** Interrupt before and after authoritative
   commits and each historical/task projection. Accepted outcomes, incoming
   event progress, and follow-ups remain inseparable on the work bead. Missing
   projections can be regenerated. Retry equal input, conflicting input, and an
   older request after projection; verify exact deduplication or rejection.
9. **Concurrent admission.** Have two fresh CLI processes compete for the last
   slot. Exactly one durable reservation succeeds. Kill its process immediately
   afterward; the reservation still consumes capacity until reconciled. A task
   summary lag cannot admit another assignment.
10. **Competing Marshals and stale workers.** Expire a leadership lease while a
    native call is outstanding. A successor does not reissue it. A late result
    is retained as evidence. Two tasks registering for one creation action
    cannot both receive source-writing authority. Reject an old ownership token
    after Executor-to-Warden transfer.
11. **Reports and targeted backoff.** Omit worker progress, then publish late
    progress. Inspect only that task after its deadline; observe the specified
    backoff and cancellation of obsolete checks. A completed turn without a
    finish remains a missing-outcome case, not successful delivery.
12. **Review, promotion, and cleanup.** Verify exact-source Warden approval,
    native completion, provider validation/promotion, and required
    synchronization. Hold the native task running after finish and retain an
    owned background process in separate trials. Neither trial deletes the
    worktree prematurely. Lost promotion replies reconcile provider state
    instead of resubmitting.
13. **Hourly recovery and user control.** Leave a wake obligation unsent and let
    the actual hourly heartbeat resume the same Marshal. Exercise a healthy busy
    wait, a user Stop, a durable Fulcrum pause, and a paused schedule. No prompt
    accumulation, duplicate coordinator, or unsolicited resumption may be hidden
    by the implementation. An idle run produces no routine status update. Stop
    both inside and outside an MCP wait. If native evidence cannot distinguish
    an intentional stop or prevent automatic resumption, readiness fails
    explicitly. An unresolved interruption requires human resume.
14. **Unavailable tools and approvals.** Remove access to one required native
    tool, reject a model setting, and interrupt the app-tools MCP connection.
    Admission reports the exact dependency failure. Trigger a real
    approval/input blocker; identify the task and wait for the user's resolution
    without invented answers, automatic archival, or a conflicting replacement
    worker.
15. **Workspace identity.** Present a wrong branch, wrong path, dirty Executor
    workspace, and denied write access. Registration blocks each mismatch.
    Confirm that both workers use the assigned worktree even though Desktop
    shows the saved project, and that the project's main checkout remains
    untouched.
16. **Source freshness and isolation.** Commit a policy/instruction change to
    local master while another operation and MCP wait remain active. The next
    fresh operation uses the new commit; the older operation retains consistent
    imports/assets. Break source preparation and verify visible failure rather
    than stale fallback. One failed bead must not stop another bead's handoff.
