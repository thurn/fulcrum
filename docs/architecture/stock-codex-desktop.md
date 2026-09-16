# Fulcrum through stock Codex Desktop

This design replaces Fulcrum's direct App Server integration with agents using
the native task tools in an unmodified public Codex Desktop installation.
Fulcrum continues to decide workflow policy in fresh CLI processes, with Beads
as its only durable workflow store. A local MCP server exposes those processes
to agents; a small Unix-socket broker holds connections, waits, and timers.
Required, trusted Codex command hooks supply lifecycle observations, restore
role context, and capture native tool results through the same fresh processes.

Developing Fulcrum using Fulcrum must retain the existing rapid iteration loop:
commit ordinary changes to local master, and the next operation uses them within
seconds, without installation, a manual rebuild, activation, or restart. Stock
Desktop integration must preserve this invariant, not trade it for convenience.

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
- [Official hooks documentation][hooks-doc]: lifecycle observations, context
  injection, tool interception, and hook trust requirements.
- [Beads update command][bd-update] and [Dolt update
  implementation][bd-storage]: source matching the inspected installed Beads
  commit, establishing the single-issue transaction used by this design.

[handoff-incident]: ../postmortems/2026-09-16-bead-46a5eb5e-handoff-delay.md
[mcp-doc]: https://learn.chatgpt.com/docs/extend/mcp
[schedule-doc]: https://learn.chatgpt.com/docs/automations?surface=app
[goal-doc]: https://learn.chatgpt.com/docs/long-running-work
[hooks-doc]: https://learn.chatgpt.com/docs/hooks
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
long-wait tests were not performed while authoring this design. Required hook
behavior has been checked against documentation, not exercised in Desktop. The
schemas establish which arguments can be expressed, not successful end-to-end
behavior.

During inspection, the bundled `codex_app` MCP initially failed because Desktop
had not supplied `CODEX_APP_TOOLS_PIPE_PATH`. The user rebooted, after which the
project-list call succeeded. This establishes a recoverable startup failure in
that session, not removal of the tools or a guarantee that reboot fixes every
installation. Instructions mentioning a tool are not proof it is callable.

Bootstrap requires callable `create_thread`, `send_message_to_thread`,
`list_projects`, `list_threads`, `read_thread`, `wait_threads`,
`set_thread_title`, `set_thread_archived`, and `automation_update`, plus Fulcrum
MCP. It checks required argument fields and accepted result shapes. An omitted
or unsupported required model/effort pair is a visible failure, never a
substitution.

Bootstrap also requires trusted `SessionStart`, `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `Stop`, and `Interrupt` command hooks. There is no
supported mode without hooks. A verified installation is still subject to
individual missed callbacks, process failures, and uncertain native effects;
those use the recovery protocol below rather than an alternate runtime.

### Mapping the existing runtime

Each native call below is executed by an agent. Fulcrum never invokes an
internal Desktop pipe, database, WebSocket, App Server endpoint, or GUI
automation to compensate for a missing task tool.

| Existing capability | Stock Desktop mechanism and resulting behavior |
| --- | --- |
| Create/start a worker | `create_thread` with exact prompt, model, `thinking`, title, and saved project ID. Creation also starts its initial prompt; worker registration gates substantive work. |
| Name or rename a task | Pass `title` at creation and use `set_thread_title` with the exact native ID for subsequent naming or correction. Preserve Fulcrum's current role/emoji/bead-ID titles; naming is a required usability capability. |
| Continue an existing task | `send_message_to_thread` with exact prompt and settings. It is an externally recorded action, not an assumed exactly-once send. |
| Resolve projects | `list_projects`; validate exact root and host. No observed native project-creation tool exists, so bootstrap guides the user to add a missing saved project. |
| Set a worker's actual cwd | No arbitrary existing-worktree parameter. Create under the saved project with `environment.type=local`, then use verified absolute Tollgate worktree paths. |
| Configure allowed roots or permissions per task | No corresponding create-task fields. Validate access under the user's Desktop configuration and report missing access explicitly. |
| Deliver role instructions | Put the complete cooked role contract in the native user-visible prompt. Required hooks restore bounded, authoritative role context on startup, resume, compaction, and incoming prompts. |
| Select model/effort | Use explicit `model` and `thinking` arguments derived from authorized Fulcrum configuration. Reject unavailable settings. |
| Observe worker progress | Workers report outcomes and meaningful progress through MCP; hooks capture lifecycle and native-result evidence; provider watchers publish existing-work events. No routine native task inventory polling. |
| Inspect one unresolved task | `wait_threads` with one target and `timeoutMs=0`; use `read_thread` for scoped evidence when needed. Schedule later checks through broker timers. |
| Recover a lost creation reply | Worker self-registration and correlated native history. Inventory search is a bounded exceptional recovery operation, never a normal polling loop. |
| Archive/unarchive | `set_thread_archived`; defer worker archival until the bead's delivery and remaining obligations settle, once per native task. Resume the same tasks for repairs. Manual unarchive suppresses automatic rearchive. |
| Interrupt a worker or answer a native approval | No observed task-tool equivalent. Expose the specific Desktop task for user action and retain the blocker. Do not invent a response or start a conflicting writer. |
| Observe a user interruption | The required `Interrupt` hook records the exact managed turn when delivered. It does not interrupt another task, pause Fulcrum, or prove all processes have stopped. |
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
that path. If required workspace access, coordination tools, hooks, or outcome
evidence are unavailable, bootstrap reports the affected capability and leaves
admission closed. It never silently enables an experimental runtime.

### Task names are part of workflow usability

Fulcrum retains its existing `role_title` formatter: standing tasks use
`🧭 MARSHAL 🧭` and `🔮 VIZIER 🔮`; workers use titles such as
`🛠️[exe-123abc] Fix retry handling` and `🛡️[war-123abc] Fix retry handling`.
Fulcrum computes these titles along with the role prompt. The agent does not
choose a replacement name. Weaver entry also names its already-existing task.

The observed `create_thread.title` field is normalized like an automatically
generated title. Therefore passing that field alone does not establish exact
title preservation. After binding the native ID, inspect the observed title
and return a `set_thread_title` action when it differs. The action contains
`threadId` and the exact `title`; its result is recorded like other native
effects. Later authorized title changes use that same path. Names are display
state, never identifiers for adopting a task or recovering a lost creation.

Readiness must verify initial naming, subsequent renaming, emoji and bead-ID
preservation, and persistence after the first turn and Desktop restart. If
Desktop rewrites the names and the rename tool cannot restore them durably,
report the failed naming capability and leave admission closed. Persistent
approval setup includes `set_thread_title` with the other required tools.

## Process ownership and source freshness

The resident boundary changes from owning a native runtime connection to owning
local transport continuity. Policy still belongs in fresh processes.

| Component | Responsibility |
| --- | --- |
| Thin MCP instance | Decode requests, invoke source-following CLI operations, and forward waits to the broker. No independent queue, ledger, or scheduler policy. |
| Unix-socket broker | Own wait registrations, notification generations, timer heap, and bounded job scheduling. Keep reconstructible indexes in memory. |
| Fresh CLI operation | Read current Beads/configuration, validate authority, commit transitions, compute prompts/actions, and perform provider operations. |
| Codex command hook | Pass a scoped native event to a fresh CLI operation and return its hook response. No native dispatch, independent workflow storage, or resident policy. |
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

### Live iteration is a required invariant

Preserve the existing source-following launch path, including its automatic
immutable source preparation and reuse of unchanged dependencies. Neither MCP
nor hooks introduce a separately installed copy of Fulcrum. A local master
commit is sufficient; remote publication is not an execution gate.

- Every business operation reached through MCP, a hook, a watcher, or recovery
  resolves local master before importing application code. Long-lived MCP
  instances and the broker must not import or cache business handlers, policy,
  prompt templates, model selection, or action-compilation logic.
- Keep MCP tool definitions and saved Marshal/schedule prompts focused on the
  calling protocol. Fresh CLI results and hook context supply current policy
  and instructions. An ordinary policy or formula edit must not require a new
  tool schema, recreating a task, updating its saved prompt, or redoing setup.
- Hook commands keep their configured source-following launcher. Changing a
  handler's implementation does not change its trusted hook definition and must
  not require reinstalling or re-trusting that definition.
- A running operation keeps its pinned source and assets. Already-issued native
  actions keep their recorded arguments. Instructions already delivered to an
  agent are not retroactively replaced; subsequent protocol calls and context
  hooks supply the current instructions within the existing role authority.

The long-wait duration and hourly recovery cadence are not source-refresh
intervals. No update waits for either timer. For example:

```text
Marshal remains parked in the same MCP connection
commit a completion-policy change to local master
worker calls finish -> fresh CLI uses the new commit
broker wakes Marshal -> fresh CLI computes the next instruction
the older running operation and its source lease remain undisturbed
```

This guarantees freshness at the next operation boundary, not an immediate
rewrite of every active agent's context or a forced wake when there is no work.
If preparing the new source fails, report that failure; never silently execute
the previous behavior to keep a connection looking healthy.

Retain the existing local target of p95 below one second from a completed local
commit to new behavior in a fresh operation with unchanged dependencies and
state contracts. The recorded launcher baseline is 0.890 seconds p95 across 20
disposable-repository trials; it is not a measurement of the proposed MCP/hook
path. Measure source preparation and local invocation separately from model,
Beads, provider, and native task latency, using a minimal behavior/asset probe
comparable to the existing measurement. Include MCP forwarding and hook dispatch
overhead in the new local-path measurements. A regression must be corrected
before claiming live-iteration parity.

## Required Codex hooks

Hooks are a supported extension point in the stock application. The
[documented contract][hooks-doc] includes developer context from session/prompt
hooks, session and turn identifiers, tool invocation IDs and results, and an
`Interrupt` event. `Stop` can request continuation; it is not a final completion
notification. Hook definitions require native trust, multiple matching hooks
can run, and tool coverage is incomplete. Interrupt command handlers have a
three-second maximum. None of these observations establishes reliable delivery
in this installation; readiness must exercise the selected paths.

### Installation, routing, and source selection

Extend `install_hook_config` in `src/fulcrum/install.py` and replace the narrow
compaction-only behavior of `RoleService.hook_context` in `src/fulcrum/roles.py`
with the contract here. Install one Fulcrum-owned command handler per required
event and instance in the user's active Codex hook configuration, preserving
unrelated handlers and other instances. User-level installation covers both
projectless leaders and project-local workers. Match `SessionStart` sources
`startup|resume|clear|compact`; use exact inspected tool names for tool hooks.
Perform session/instance filtering inside all handlers, including events whose
native matchers do not filter by task.

Each handler runs the retained absolute source-following launcher with proposed
argv `fulcrum hook handle --instance ABSOLUTE_PATH --input -`. Read the native
JSON from stdin and emit only the event-specific hook JSON on stdout; diagnostics
go to stderr. Use command hooks, not MCP-tool hooks, so startup and interruption
reporting do not depend on an initialized MCP connection. Never execute strings
from the event as shell commands. Configure synchronous handlers, a three-second
timeout for `Interrupt`, and a ten-second timeout for the other required hooks.
Set context output to at most 2,000 tokens. Readiness measures these budgets;
timeouts must not be hidden by claiming the event was persisted.

The launcher resolves local master for each invocation and pins its operation's
source and interpreter. Handler implementation changes therefore require no
hook reinstall, approval ritual, or resident restart. Changes to the actual
hook configuration are exceptional setup changes: present the new definition
for native trust review and re-probe affected capabilities. Do not implement a
Fulcrum trust hash, overwrite native trust records, or bypass hook review.

The instance comes from installed argv, never the task's cwd or arbitrary prompt
text. Resolve `session_id` against authoritative work/control bindings; a task
summary is only a lookup hint. Unknown sessions are a no-op except for initial
identity evidence: an exact creation marker or the PreToolUse event for
`register_worker`, with matching creation action and assignment token, can
record a prospective worker binding against that instance's retained action.
This is evidence, not permission to edit. `register_worker` still performs
workspace and single-owner checks.
Bootstrap binds its caller explicitly on `fc-system`; `$weaver` entry binds its
existing task before substantive preparation. Those bindings include hook
participation without granting leadership or worker authority prematurely.

### Event responsibilities

The following is Fulcrum's required behavior, not an assumption that a hook can
enforce every native operation. Hooks never invoke native task tools themselves.

| Event | Fulcrum operation and response |
| --- | --- |
| `SessionStart` | Read current assignment/control state and return bounded role context for an already bound task. Unknown tasks receive no Fulcrum instructions. Include assigned worktree, authority, phase, pause state, and exact pending protocol steps. |
| `UserPromptSubmit` | Record the managed turn, validate any action marker, acknowledge matched delivery, and provide current protocol context. A new worker's marker records prospective identity only. An ordinary human prompt never creates authority merely by mentioning an action ID. |
| `PreToolUse` | Associate covered Fulcrum MCP entry calls with native session/turn evidence. For native coordination mutations, validate the one claimed attempt and exact arguments, then bind `tool_use_id` before execution. Return a native deny decision on a known mismatch; do not rewrite calls or approve permissions. |
| `PostToolUse` | For a bound native invocation, record its result through the shared action-result transition and notify the broker. Return without replacing or hiding the tool output. Do not convert ordinary tool activity into worker progress or successful role outcomes. |
| `Stop` | Record a stop attempt and run the bounded missing-report correction below. Notify the broker of a due completion check; never declare final native completion or release a worktree. |
| `Interrupt` | Record interruption of the identified managed turn. Preserve issued effects, ownership, and reservations. Return without requesting continuation, cleanup, or a change to workflow pause. |

Context is compiled from current authoritative state, not replayed wholesale
from the original work description. Keep approved scope as data and include its
locator; do not promote raw intake, arbitrary prompt text, or tool output into
developer instructions. After finish, context describes the retained reporting
permission and forbids further edits until an explicit renewed assignment is
registered in that same task. A new source commit may refresh policy
instructions but must not rewrite already-issued native arguments.

`PermissionRequest`, `SessionEnd`, and subagent hooks are not required or
installed by this design. Approvals remain human/native policy decisions;
session disposal is not role completion; native worker tasks are not subagents.
Tool guards cover the inspected native coordination mutations and selected
Fulcrum entry calls, not arbitrary shell parsing or filesystem isolation.

### Identity, acceptance, and duplicate observations

Native messages and creation prompts start with one machine-readable JSON line
under the literal prefix `Fulcrum-Action: `. Its object contains `instance`,
`record_id`, and `action_id`; creation and renewed assignments also carry
`assignment_token`.
Invocation records supply the attempt ID without changing the action's
fixed prompt on retry. Validate marker values against retained arguments and
intended recipient before acknowledging delivery. A marker from another instance
or an unmatched marker grants nothing. Never infer an action from a title or parse
prose to choose a mutation. Scheduled prompts use their retained schedule action
marker and are validated against the current leader and schedule binding.

Lifecycle observations carry the native session and, when supplied, turn ID.
Tool observations also retain `tool_use_id`, native tool name, and normalized
arguments/result. Key tool-event deduplication by session, turn, tool-use ID,
and event name; key prompt and interruption acceptance by session, turn, and
event name. Use structured values or literal tuples, not content hashes. Store
the resulting input/result on the owning bead using the same transition and
projection rules as other events. Equal duplicate input returns the saved
result; conflicting input retains a diagnostic conflict without overwriting
accepted evidence. Context-only reads allocate no durable event. Repeated Stop
callbacks retain a bounded per-turn observation and correction state, not an
unbounded receipt per callback.

Admission-call observations are bounded handshake metadata: retain at most one
unconsumed invocation per actor, keyed by request ID, until the corresponding
MCP entry consumes it or scoped recovery settles it. Reject a competing request
instead of overwriting that binding. A repeated callback returns its acceptance;
an older consumed
binding cannot authorize a new request. Overwrite only consumed handshake
metadata, retaining workflow decisions in their ordinary transition receipts.
An unchanged wait renewal therefore adds no historical hook event or receipt.
Native mutation results and unresolved invocation evidence are never pruned as
handshake metadata.

Hook delivery has no assumed total order or replay guarantee. A late callback
may add evidence to its old assignment/attempt but cannot change current
ownership, clear a newer blocker, or supply authority for another turn. Store
turns by native ID; do not order opaque IDs or let a delayed callback overwrite
the current turn. Fulcrum MCP admission calls (`register_worker`,
`wait_for_instruction`, and `claim_action`) require a matching invocation
observation from their `PreToolUse` hook before authorizing new work, correlated
by actor and stable request ID in the MCP arguments. This also supplies turn
evidence when initial prompt
delivery preceded binding. A missing observation returns a hook prerequisite;
reporting, saved-result replay, diagnostics, pause, and recovery remain
available. Do not hold a writer lock while waiting for another hook or an agent.

Each hook mutation follows the detached-operation mechanism. A deadline may
expire while its operation continues; only a verified Beads commit is accepted
delivery. The broker registry, temporary files, and spawned process are not
durable substitutes. If the event is lost before acceptance, normal outstanding
action checks, worker deadlines, and hourly recovery retain the unresolved work.
In particular, interruption persistence must not delay or veto native Stop.

### Native invocation and result capture

The explicit agent claim remains the authorization point. A native mutation's
`PreToolUse` handler finds the actor's current claim awaiting execution, verifies
tool/arguments, retained authority, and applicable pause/replacement fences,
then commits its native invocation identity. Deny a call with no matching claim,
changed arguments, or a second distinct tool-use ID. A repeated callback for the same ID can
return its recorded decision; that is not authorization to invoke the mutation
again. Expired leadership permits result recovery, not a new invocation. Save a
deny decision as an intended hook response, not proof that Desktop applied it.
Only a correlated native rejection establishes pre-execution failure; a lost
hook response can still leave the action uncertain. After an actor reports an
uncertain result, unrelated actions may proceed through a new claim, but the old
attempt and reservation remain unresolved and cannot be reissued.

On normal return, `PostToolUse` calls the same result validator as
`report_action_result`. The validator derives action/attempt identity from the
recorded invocation, checks returned IDs and result shape, and commits the
accepted effect before waking dependents. It records tool errors as evidence;
an error is not automatically proof of no external effect. An unknown shape
retains uncertainty. Agent reports remain mandatory and idempotent: hook and
agent observations of the same normalized result converge on one effect; an
additional uncertainty report cannot downgrade proven success, and conflicting
concrete outcomes require reconciliation. Hook evidence may settle an old
attempt without renewing the actor's expired authority.

A missing pre-hook or post-hook is never evidence of rejection or success.
Supported tool paths and failure behavior must be probed, including nested
code-mode calls. Hook failures cannot provide a universal enforcement boundary;
explicit claims, cooperative agent instructions, registration, and exact
postconditions remain necessary. Known broken hook configuration closes new
admission until repaired; one missed result callback leaves its effect subject
to normal reporting/reconciliation rather than globally pausing the instance.
Unmatched results remain scoped diagnostics, not permission to adopt a task.

### Bounded correction at normal turn exit

When an authorized worker attempts to end without its required finish or a
claimed native result report, the `Stop` handler may return one continuation
asking it to settle that exact obligation through MCP. Commit the correction
claim before returning it. Track the allowance by assignment and obligation,
not only native turn ID, so a hook-created continuation cannot reset the budget.
Honor `stop_hook_active` as an additional veto. The correction may report a
blocker or uncertainty; it may not retry a native mutation, invent an outcome,
or resume source edits after finish.

No correction is issued when Fulcrum is paused, hook readiness is broken, the
turn was interrupted, the worker is awaiting human input, or no immediate
reporting correction is possible. Marshal idle/wait behavior is never sustained
by a Stop-hook continuation loop. If the one correction is lost or ineffective,
retain the missing-report obligation for targeted recovery. Return neutral hook
JSON when allowing exit. A Stop callback, including one that allows exit, is
only a prompt for native inspection: another hook or a queued message may still
continue the task. Keep the exact-task completion and process-cleanup gates.

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

All Fulcrum mutations, including MCP, hook, watcher, recovery, and operator
commands, use the same lock. Direct edits to reserved workflow metadata are
break-glass operations. This is the existing trusted local-user model, not exclusion against
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

Agent capacity is separate from provider delivery, task archival, and worktree
retention. After the final worker's outcome is accepted and its native turn is
confirmed complete, release its agent reservation in one authoritative work-bead
transition. In the normal implementation path this is Warden completion; retain
the slot across the Executor-to-Warden handoff. An unresolved continuation or
uncertain worker creation does not qualify for release.

CI, promotion, synchronization, cleanup, or archival may remain pending after
release without occupying an agent slot. Keep their obligations and workspace
ownership intact; release is neither a successful-delivery claim nor permission
to delete the worktree. Provider operations use bounded broker scheduling and
their existing resource locks. Any later repair resumes the existing role task;
it must acquire a fresh agent reservation under current capacity and pause rules
before its continuation is sent. A late report cannot revive an old reservation
or editing authority. Keeping that task unarchived while delivery is pending
does not occupy an agent slot.

### Events and cursors

Store each meaningful worker, hook, or watcher event on its authoritative work
bead before acknowledging publication. Workers use stable request IDs. Watchers
use their provider's stable event ID when available; snapshot watchers persist a
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
| Hook saw a result but persistence failed | Keep the claimed effect unresolved; accept the agent's report or inspect exact postconditions. Never infer success from hook execution alone. |
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
| `pause` / `resume` | Human/Vizier-authorized CLI/MCP transitions with stable request IDs and a reason. Pause returns the retained boundary hold plus active assignments/in-flight effects; resume revalidates held work without clearing independent fences. |
| `register_worker` | Bind the native task/host ID to the exact creation or authorized continuation action and assignment before substantive work. A repair reuses the retained role task and receives a fresh assignment token. |
| `report_progress` | Persist meaningful progress and renew the assignment's reporting deadline. |
| `finish` | Accept a role outcome, commit the transition, and return exact authorized follow-ups. |
| `wait_for_instruction` | Acquire/renew Marshal coordination and return a decision request, native action, renewal, pause, or idle result. |
| `claim_action` | Revalidate a pending action and durably mark its execution attempt before an external call. |
| `report_action_result` | Record success, definitive rejection, or uncertainty and compute dependent work. |
| `marshal_decide` | Validate bounded judgment against the supplied scope and current state; commit accepted decisions independently by bead. |
| `hook handle` (CLI only) | Validate a native hook event, route it to the shared transition/context handlers, and return event-specific hook JSON. It does not expose an agent-callable hook-identity override. |

Every mutating request includes a stable request ID, actor task ID, and relevant
assignment/leadership token. The MCP instance derives caller identity from its
Desktop-provided session context where available; otherwise it requires the
explicit native ID and checks it against registration. Tokens prevent accidental
stale operations in the cooperative model; they are not a security boundary
against another process controlled by the same local user.

For admission calls, also match the hook-recorded native invocation by actor and
request ID. Hook-originated observations use the native identity and retained
binding instead of manufacturing an agent ownership token. They may add evidence
to a retained attempt but cannot grant a fresh assignment or execute an action.

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
    "prompt": "Fulcrum-Action: {\"instance\":\"/absolute/instance\",\"record_id\":\"work-bead-id\",\"action_id\":\"random-action-id\"}\nCall wait_for_instruction.",
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

Every native message and creation prompt carries the structured marker defined
above. The recipient hook acknowledges matching delivery; the recipient's MCP
entry also acknowledges it idempotently before acting, covering a missed hook.
Schedule prompts carry their creation ID and instance marker. These are
correlation markers, not native idempotency keys. Acknowledgment never implies
the requested work ran. The saved result-reporting instructions include record
ID, action ID, attempt ID, request ID, and token;
the actor must not reconstruct them from prose or replace them on retry.

Successful `finish` transfers workflow ownership. It also grants the old worker
a narrow, retained permission to execute and report its returned follow-up
actions. That permission does not allow another finish, a scope amendment, or
editing after transfer. Once the last follow-up is reported, the worker ends its
turn. A missing follow-up report remains a recoverable obligation.
Only a new, explicitly granted assignment can authorize further editing or a
new finish in that same native task; it never changes the accepted old finish.

### Registration and uncertain native creation

`create_thread` combines task creation with an initial turn. Its first prompt
therefore requires registration before repository work. The complete role prompt
includes instance, work bead, creation action, assigned role, exact workspace,
assignment token, model/effort, and reporting instructions.

The creation result hook records `threadId` and `hostId` when delivered. The
creator also immediately reports them through MCP before any unrelated action;
both paths use the same idempotent binding transition. Fulcrum binds them to the
authoritative work/control record. A returned `clientThreadId` is retained as a
pending setup locator and is never
passed to a tool requiring `threadId`. Registration by the new worker can finish
the binding even if the creator never receives a final creation response.

The registering worker obtains its native task ID from supported session context
or `CODEX_THREAD_ID`, checked against its admission-call hook evidence; it may
not invent an ID from a title. Registration verifies the exact creation action,
role, project/host, assignment, and
workspace evidence. One native task can win that assignment. A second claimant
records a conflict and receives no permission to edit. Duplicate native tasks
are possible after an uncertain external boundary; duplicate authorized workers
are not.

If creation is uncertain and no worker registers, first inspect retained hook
results and prospective worker identity, then use the retained locator and a
bounded native inventory/history search for the exact creation marker. A title
alone, truncated summaries, or absence from a recent-task list
cannot establish absence. Ambiguity remains scoped recovery with no replacement
creation. The Desktop surface lacks a client-supplied creation idempotency key;
the design does not invent one.

### Settling other native effects

An uncertain action keeps any agent reservation it still owns and its dependent
work blocked until
positive evidence settles it. Validated hook-captured results use the same
evidence rules as agent-reported results; a callback alone proves no effect.
Provider or archival uncertainty does not recreate a released agent reservation.
Read-only inspections can be retried; they do not
authorize retrying the mutation they inspect.

| Effect | Settlement evidence and retry rule |
| --- | --- |
| Send a message | Recipient acknowledgment of the exact action ID, or exact persisted native message content and target, proves delivery. A recent summary's omission proves nothing. Without either, keep uncertain. A definitive tool rejection before acceptance permits a new attempt. |
| Create a schedule | Adopt one exact inventory match for instance, action marker, and target, confirmed by native view. Zero incomplete-inventory matches or several candidates remain uncertain. |
| Update/pause a schedule | Native view must match the intended target, cadence, prompt, notification policy, and state. Read back before any repair attempt. A different value alone is not proof the earlier call cannot still land; reconcile the old caller first. |
| Archive/unarchive a task | Native inventory/history must positively show the requested state for the exact task. Missing from the recent list is not archived evidence. Do not repeat a possibly still executing call. |
| Rename a task | A native observation of the exact task ID with the intended title settles success. A different title alone does not prove an outstanding call failed; retain uncertainty until that attempt is settled before issuing a correction. |
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

Desktop's Stop interrupts the current turn. It does not durably pause Fulcrum:
later authorized wake messages and the hourly heartbeat may resume coordination.
Bootstrap explicitly discloses this behavior and the separate "pause Fulcrum"
command. An interruption handler must not immediately restart the stopped turn.
No standing goal is created or resumed.

Explicit Fulcrum pause is durable and honored by every entry point. The control
bead holds `run_control=enabled|paused`, a reason, and the accepting request. An
explicit agent-mediated workflow pause commits `paused` before acknowledging it.
Existing YAML policy pause also denies work; the control bead does not mirror or
override that policy. Only an explicit authorized resume clears a pause.

MCP cancellation releases the parked wait and records interruption evidence when
available; a socket disconnect is transport-loss evidence. Neither changes
`run_control`. An unknown exit reason remains unknown and does not require a
global human resume. Recovery inspects and reconciles outstanding effects before
dependent work continues. An interrupted creation or message remains issuing or
uncertain; Stop never authorizes blind retry, releases a worker's reservation, or
proves that its background processes ended.

Pause holds work at its next workflow boundary. An already-running worker may
finish its current assignment, including its local checks, and report progress,
blockers, or its sealed outcome. Accept and retain those reports without
dispatching the next role. Pause does not terminate running workers or revoke
their current source-writing assignment midway through an edit.

All wake claims, lease acquisition, hourly recovery, dispatch, and provider
effect issuance check pause state under the writer lock. While paused, begin no
new role, continuation/wake message, downstream provider submission, promotion,
remote push, or worktree deletion. A provider watcher may observe and publish
facts but cannot initiate the next delivery step. Enforce this in fresh CLI
provider handlers as well as native action claims; native-tool hooks cannot
guard provider calls made directly by Fulcrum. Explicit recovery authority does
not implicitly bypass pause for a new delivery or cleanup mutation.

Retain blocked follow-ups as pending obligations on their existing work beads,
with pause as the reason; do not mark them failed, superseded, or delivered.
Already-issued external operations may finish. Record their actual results and
uncertainties, including a promotion that lands after pause, without starting
its follow-up push or cleanup. Read-only inspection, result acceptance,
diagnostics, and pause/resume remain available without a coordination lease.

Pause and effect issuance serialize under the same lock. An issuance committed
before pause remains in flight even if the external call lands afterward; a
pending effect whose issuance has not committed is held. The pause result names
active assignments and in-flight effects, so acknowledgment never promises
instantaneous quiescence. A hook may still prevent an unexecuted native call,
but only observed native rejection proves it did so.

Explicit resume removes the workflow pause, not any independent YAML project
pause or recovery fence. Revalidate held actions against current ownership,
source, review approval, dependencies, and policy before issuing them. A sealed
Warden outcome alone cannot authorize promotion of changed source. Pausing the
Desktop schedule stops its triggers; the durable Fulcrum pause holds workflow
boundaries from every entry point.

Readiness verifies this distinction inside and outside an MCP wait. It does not
require Desktop Stop to suppress future scheduled runs or messages. Native
interruption observations improve diagnosis without becoming a prerequisite for
durable pause. The required `Interrupt` hook supplies turn-specific evidence
under the hook acceptance contract. An individual missed callback leaves the
reason unknown; it does not change the agreed semantics or waive readiness's
requirement for a working hook installation.

## Reporting, targeted checks, and delivery

Workers report meaningful progress at least every ten minutes when able to call
tools. The reporting deadline is thirty minutes after registration or accepted
progress. A tool that runs longer may delay reporting; deadline expiry requests
inspection, not a fabricated failure or duplicate worker.

After an accepted finish, checks of that exact native task protect handoff and
cleanup. The first check is immediate. A Stop/Interrupt observation can also
make a targeted check due, coalescing with the retained check rather than
creating another polling chain. Unresolved checks use delays of 5, 15,
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

Stop, Interrupt, and successful PostToolUse reports are not final native
completion. Accepted completion evidence is a successful native snapshot
identifying the exact task and explicitly reporting that its current turn is no
longer running,
with the registered assignment identified in its observed history. Retain the
hook-observed turn ID and match it whenever the native snapshot exposes one.
When the snapshot omits it, require the registered role's completion
acknowledgment plus a current non-running snapshot and no unresolved
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
source and assignment; delivery does not ask Warden to repeat that finish. A
later repair uses a new assignment in the same Warden task and seals its own
outcome for the repaired source.

Fresh CLI operations continue to own provider submission, exact-source
validation, promotion, remote synchronization when required, and cleanup.
Each new downstream provider effect obeys the workflow-boundary pause contract;
an accepted finish during pause retains the next step without executing it.
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

### Same-task repair and deferred archival

Keep the bead's existing worker tasks unarchived through review, provider CI,
promotion, required synchronization, and cleanup. A worker's accepted finish
and native completion can release its agent slot without archiving its task.
Executor stays available while Warden reviews; Warden stays available while
delivery runs. Routine failure, delay, or retry never creates a replacement
native task.

When CI or delivery requires Warden repair, retain the exact failure evidence
and current source, then reserve capacity for the existing Warden native ID.
After verifying its prior turn is complete and no conflicting writer or native
continuation remains, commit a fresh assignment token and an exact
`send_message_to_thread` action targeting that same task/host. The prompt carries
the continuation marker, original approved scope, retained worktree, observed
failure, and expected source. `register_worker` accepts this retained
continuation action, verifies current workspace/ownership evidence, and grants
the new assignment before edits. Current model/effort and pause rules still
apply. If the continuation result is uncertain, reconcile it; do not resend or
create a fresh task.

Preserve the prior sealed finish and review as historical evidence. Repair
changes invalidate approval for the changed source and require new validation
and a new finish under the fresh assignment, not a replay or amendment of the
old finish. If Executor work is needed instead, resume that bead's existing
Executor under the same protocol after the current writer relinquishes authority;
then return to the existing Warden. A missing or unusable retained task is an
explicit operator recovery blocker, never permission for automatic replacement.
If a user archived it early, require an observed unarchive before continuation;
do not treat archival as task deletion.

Automatic archival becomes eligible only after the bead reaches a terminal
disposition and its delivery/repair/cleanup obligations are settled. For work
without a delivery phase, require its terminal disposition and settled
follow-ups. A task associated with several beads waits for all of them to
qualify. Each task also needs an accepted role outcome (or explicit terminal
resolution), positively observed native completion, released ownership, and no
missing reports, human blockers, uncertain assigned effects, or unresolved owned
processes. A failed delivery or cleanup remains visible even when earlier source
promotion succeeded. Recheck eligibility when claiming the archive action; a
new turn, assignment, or repair obligation invalidates an unissued archive.

Persist the archive obligation and exact native task/host locator on the owning
work bead, even after it closes. Discovery includes these unfinished obligations
on closed beads; archival must not depend on a task-summary projection or an
open-work-only scan. Marshal executes the exact native archive action through
the normal claim/result protocol. Pause holds unissued archives. Archival does
not retain or release agent capacity, grant editing authority, delete evidence,
or authorize worktree cleanup. Retain all role task links and outcomes on the
bead for later inspection.

Preserve one automatic archive request per native task lifetime. Record its
attempt before issuing it; an uncertain result requires positive native
reconciliation, never a second blind archive. A failed automatic request stays
visible for explicit repair rather than starting an automatic retry loop.
After an observed automatic archive, an explicit or positively observed manual
unarchive suppresses future automatic archival of that native task. A missing
inventory entry alone cannot establish either archive or unarchive. Standing
Marshal and Vizier tasks are never automatically archived; explicit leadership
replacement retains its separate recovery protocol.

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
   Install the six required command hooks with stable absolute launcher argv,
   preserving unrelated and other-instance definitions. Present any native hook
   trust prerequisite and verify effective configuration plus observed callback
   execution; a file on disk is not evidence that Desktop loaded or trusted it.
   Retain exact installed definitions and probe results on `fc-system`, without
   a configuration hash. Hook verification is required even if MCP is healthy.
3. Inspect callable native tools and run read-only connectivity checks. Explain
   exact missing fields or tools. Discover saved projects; guide the user
   through adding a project if the native tool surface cannot create it.
4. Present the explicit coordination authorization: native task creation and
   messaging for admitted work, configured model/effort values, saved-project
   local targeting with Tollgate worktrees, managed-task hook observations and
   bounded reporting corrections, and hourly same-task recovery. Disclose that
   Desktop Stop interrupts one turn while "pause Fulcrum" durably pauses new
   coordination. The user's bootstrap request supplies this scope; request only
   missing choices or permissions Desktop itself requires.
5. Merge supported persistent per-tool approval settings for Fulcrum MCP and the
   native coordination tools. Verify effective settings after reconnection. Do
   not expand filesystem permissions or disable the sandbox as a side effect.
   Native tool approvals and native hook trust are separate prerequisites;
   satisfying one never substitutes for the other. Fulcrum does not install a
   PermissionRequest hook to answer approvals.
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

Before creating leaders, the bootstrap caller is explicitly bound to its
retained bootstrap request and verifies a covered Fulcrum admission call's
PreToolUse observation. Only disposable probe actions and bootstrap infrastructure
actions are eligible while readiness is incomplete. A newly registered leader
can help finish probes but cannot admit product work until all gates pass. This
avoids requiring ready product coordination to establish readiness itself.

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
Hook trust or hook-loading prerequisites likewise resume the same record;
they never authorize replacement leader creation or a duplicate schedule.

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
  infrastructure, MCP, trusted hooks, leadership tasks, and hourly recovery.
---

Inspect the retained checkout and existing instance, then run the deterministic
setup command: scripts/setup --input setup.json --non-interactive --json.
Resume with fulcrum bootstrap --instance ABSOLUTE_PATH --request-id UUID --json,
or the equivalent connected bootstrap MCP tool. Preserve the request ID on retry.
Use returned instructions; do not recreate policy or setup state yourself.

This setup authorizes the specified Marshal/Vizier tasks, configured worker task
creation and messaging, local-project targeting with assigned Tollgate worktrees,
required persistent tool approvals, managed-task hooks with bounded reporting
corrections, and one hourly follow-up on Marshal. Confirm missing model choices
and explain the scope of each approval configuration. Complete native hook trust
review through the supported user flow; do not bypass it. Explain that Desktop
Stop ends the current turn and "pause Fulcrum" durably pauses coordination.

For every returned native action, claim it, execute its exact arguments once,
then report the result through Fulcrum MCP. Record returned task and schedule IDs
immediately, even when a hook also captured them. Register leaders before they
act. Resume retained uncertain steps through inspection; never create
replacements merely because a reply was lost.

Guide the user through prerequisites that supported tools cannot perform. Do not
start an experimental runtime, change unrelated settings, or claim readiness
until bootstrap returns verified ready. Never create a standing goal.
```

The hourly saved prompt has a similarly bounded purpose:

```text
Resume coordination for the registered Fulcrum instance through
wait_for_instruction with recovery intent. Honor explicit Fulcrum pause state.
Settle retained actions before new effects. Follow Fulcrum's exact instructions.
If idle, end the turn without a routine status update. Report only actionable
failures, required user input, or meaningful completed work.
```

## Feasibility checks and operational evidence

Before the runtime rewrite or production state migration, use a disposable
instance to prove required hook loading/trust, identity fields, native-call
coverage, result shapes, and turn-completion observation. A minimal source-
following probe handler is sufficient for this first gate; it does not claim
the complete workflow is ready. Record exact observed payloads after removing
unrelated/private content. A failed native capability produces a bounded
feasibility report before committing to a destructive cutover.

The first assembled-product check crosses the actual boundary: a stock Desktop
Marshal coordinating a registered probe worker parks in Fulcrum MCP; a separate
disposable Weaver task commits prepared scope, executes any returned native wake
instruction, and Marshal receives that exact work once. This check includes real
Beads persistence, the Unix-socket broker, two MCP clients, required hooks, and
native Desktop tools. Component doubles do not establish this behavior.

Bootstrap's readiness probe uses disposable control probe records, not product
work admitted outside Weaver. It verifies required hook observations and trust,
native creation/registration, messaging, worker access to a disposable Tollgate
worktree, report/result delivery, explicit completion observation, and the
required long-wait duration.
Retain probe task IDs and clean them through observed archive/cleanup actions.
Initial full setup is not ready while the long-wait probe is outstanding. On
rerun, preserve already verified steps unless effective tool schemas, hook
definitions/trust, configuration, or connection behavior changed; a failed step
never inherits an earlier success silently.

These are manual compatibility checks on a disposable instance. They do not
restore the retired expensive live harnesses or make ordinary code edits run
external providers. The normal repository check remains provider-independent.

| Required observation | Failure consequence |
| --- | --- |
| Native task tools callable with required schemas | Admission stays closed; report missing tool/field or MCP startup error. |
| All six command hooks load, are trusted, and execute for projectless leaders and local-project workers | Admission stays closed; report the exact trust, loading, identity, or handler failure. |
| Direct and nested native calls expose matching pre/post invocation IDs and parseable results | Admission stays closed; do not promise result capture for an unobserved path. |
| Startup/resume/compaction restore bounded current role context; initial prompts and resumed turns acquire hook admission evidence | Refuse affected task admission; never authorize work from stale context or cwd alone. |
| Hook deadlines, duplicate callbacks, missed results, and one bounded Stop correction preserve outstanding effects | Report hook protocol failure; no blind retry or completion inferred from callback receipt. |
| Thirty-minute wait returns an event promptly and renews before timeout | Report long-wait capability failure; no silent short polling substitute. |
| Another MCP client can report while Marshal waits | Report transport concurrency failure; do not serialize the whole instance behind a parked request. |
| Ordinary local-master changes reach the next MCP and hook operation within the live-iteration target, preserving existing connections and operations | Refuse live-iteration readiness; do not substitute installation, manual activation, or periodic source refresh. |
| Native IDs and assignment can be registered before worker edits | Refuse dispatch readiness. |
| Initial and updated task titles preserve the required role/emoji/bead-ID format | Admission stays closed; report normalization, persistence, or rename-tool failure. |
| Worker can use the exact Tollgate worktree | Block the affected project and show the required access change. |
| Targeted inspection distinguishes completion from unknown/error | Admission stays closed: safe review handoff and cleanup both require it. |
| Hourly same-task scheduling coexists with an active wait and respects Fulcrum pause | Report recovery scheduling failure; do not substitute standalone agents or a perpetual goal. |
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
failures. Hook diagnostics add event/session/turn/tool-use identity, source,
latency, committed acceptance or timeout, correction allowance, and effective
trust/configuration failures. Do not log full unrelated prompts, transcripts,
or arbitrary tool output. Logs are bounded diagnostic evidence, not replay state.
Failed work must not abort discovery or completion of unrelated work. Monitor
the age of pending actions, not merely whether the socket answers.

## Cutover and ongoing maintenance

Pass the disposable native-capability gate before fencing the existing instance
or migrating its ledger. Scope probe hooks to that disposable instance. Do not
attach the new hook protocol to old-runtime tasks that are still draining.

Existing operations finish on their pinned source and existing transport. Fence
new old-runtime admission, let active turns, pending approvals, provider work,
and source-writing assignments settle, and preserve their durable outcomes. Do
not switch a running worker to the new contract halfway through its turn. This
is a drained transport cutover, not a permanent dual-backend mode.

Under exclusive maintenance ownership, convert authoritative pending work to the
single-record transition representation. Reconcile existing task/receipt facts
before choosing authority; conflicting ownership remains fenced. Transfer
standing IDs only if native inspection verifies them. Install and trust the
required handlers for the migrated instance, establish its caller/turn bindings,
and run stock Desktop bootstrap/readiness, then retire Fulcrum's owned
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
agent turns. Hook commands retain their stable launcher and obtain fresh policy
on their next invocation; an already running hook operation retains its source
lease. Remove the old owned compaction-only hook when installing the complete
handler set, without removing another instance's handlers. Re-trust/re-probe only
actual hook-definition changes, not ordinary handler implementation edits.

Keep maintenance exceptions narrow and explicit. Dependency changes may prepare
a new isolated environment; breaking durable-state changes require the existing
fenced migration; broker/MCP transport implementation changes require a safe
connection handoff. Changes to the actual MCP tool surface or connection
configuration may require reconnecting that MCP connection, and hook-definition
changes require native trust review. None of these is an acceptable dependency
of an ordinary business-logic, formula, or prompt-template edit. First-run
bootstrap and the drained transport cutover are setup, not an editing workflow.

### Implementation handoff

Implement in dependency order, with each step's checks passing before the next:

1. Capture disposable native/hook capability evidence and accepted result
   shapes. Stop here on a missing required capability.
2. Implement authoritative transitions, action attempts, hook observations, and
   deduplication in the ledger/coordination layer. Cover crash boundaries with
   local process fixtures, including lock inheritance and duplicate reports.
3. Add the source-following hook command, event routing, context compilation,
   and installation/trust diagnostics. Replace the current compact-only handler;
   add direct and nested invocation fixtures, bounded Stop-correction checks,
   and old-assignment/out-of-order-event cases.
4. Implement CLI/MCP action handlers, broker waits/timers, registration, and
   agent prompt changes. Keep existing provider validation/promotion semantics;
   replace direct runtime dispatch in role/completion/leadership services.
5. Assemble bootstrap and run the full disposable live scenarios below,
   including long waits and actual heartbeat delivery. Then perform the drained
   migration, remove obsolete runtime paths, and update operational docs.

The normal repository check stays provider-independent. Live probe evidence is
a separate release prerequisite, not a substitute for fault-injection coverage.

## Manual QA

Use a disposable instance, ledger, provider repository, and explicitly
authorized native probe tasks. Record observed results and locators; do not mark
a scenario passed because its component tests passed. Inject failures at
operation boundaries without modifying production state.

1. **Complete setup and rerun.** Start from a retained checkout with no
   instance. Configure infrastructure, MCP, hook trust, and approvals, register
   both leaders, create the hourly heartbeat, and pass readiness. Rerun; retain
   the same leader/schedule IDs and unrelated configuration. Missing project enrollment
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
    accumulation or duplicate coordinator may be hidden by the implementation.
    An idle run produces no routine status update. Stop both inside and outside
    an MCP wait: it ends that turn without an immediate hook-driven restart;
    a later authorized wake or heartbeat may resume coordination after
    reconciliation. Explicit "pause Fulcrum" instead prevents new coordination
    actions until authorized resume. An unknown interruption does not globally
    pause the instance or permit reissuing an uncertain effect.
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
    MCP operation and hook invocation use the new commit; the older operation
    retains consistent imports/assets and an issued action retains its exact
    arguments. Repeat with no Git remote, no install/build/activation command,
    unchanged MCP and hook configuration, and unchanged running connections.
    Record 20 trials of the minimal behavior/asset probe separately for CLI,
    MCP forwarding, and hook invocation; report preparation and invocation
    times and verify the local p95 target below one second with unchanged
    dependencies/state contracts. No trial waits for long-wait renewal or the
    hourly schedule. Break source preparation and verify visible failure rather
    than stale fallback. One failed bead must not stop another bead's handoff.
17. **Task naming.** Verify leader and worker titles against Fulcrum's formatter.
    Exercise Weaver entry naming, creation-time normalization, a later rename,
    a lost rename response, first-turn completion, and Desktop restart. Titles
    remain exact; recovery binds by native ID and never creates a replacement
    to fix a name. Missing or ineffective naming tools fail readiness.
18. **Hook installation and isolation.** Exercise all six handlers in a
    projectless leader, a saved-project worker, and an unrelated task. Only
    retained Fulcrum bindings or valid initial action markers affect the ledger.
    Preserve unrelated hooks and a second instance's handlers on rerun. Remove
    trust, disable hooks, change a definition, and restart Desktop separately;
    readiness reports the exact prerequisite. No task or schedule is recreated.
19. **Context and first-turn identity.** Start a worker before its creator's
    result arrives. Its initial marker can record identity but cannot grant
    editing authority. Test registration after a missed prompt callback using
    its admission-call hook, then test startup, resume, clear, and mid-turn
    compaction. Current scope/authority/worktree are restored; raw intake is not
    injected as developer policy. After finish, restored context grants only
    retained reporting actions. Unrelated prompts and forged/mismatched markers
    cannot acknowledge another action.
20. **Native invocation capture.** Run creation, messaging, rename, archive,
    and schedule mutation through both direct tools and code-mode nested calls.
    Alter one claimed argument and attempt a second native invocation for the
    same claim. Verify guard decisions and correlated native results. Drop the
    pre-hook response after it commits a deny decision: the ledger cannot call
    that definitive rejection without native evidence. Drop the post-hook,
    kill it before/after persistence, and race its result with the agent report;
    one normalized effect is accepted, with no duplicate binding or dispatch.
21. **Exit correction and interruption.** Omit finish, omit an action result,
    block on human input, pause Fulcrum, and end an idle Marshal in separate
    trials. Only a correctable worker report receives one continuation. A new
    native turn, compaction, duplicate callback, or lost correction response
    cannot reset that obligation's allowance. Another hook that continues the
    task cannot trick cleanup into treating Stop as completion. Interrupt during
    a native effect and with Beads unavailable; the handler stays within its
    deadline, never restarts the turn, and leaves effect recovery intact.
22. **Hook ordering and freshness.** Deliver a late result after ownership
    transfer, an old interruption after a newer turn, and duplicate callback
    inputs after historical projection. Retain old evidence without modifying
    current authority. Commit a handler policy change while a hook operation
    runs: the next invocation uses local master with no reinstall/re-trust;
    the older operation keeps its source. A broken required handler produces a
    visible prerequisite while reports and read-only recovery remain usable.
23. **Pause at workflow boundaries.** Pause during Executor edits, Warden review,
    provider validation, promotion, and source synchronization in separate
    trials. Active workers may finish and report, and already-issued effects
    may settle; no next role, downstream submission, promotion, push, or cleanup
    begins after the pause commit. Race pause against issuance under the writer
    lock and verify the returned in-flight list. Accept Warden finish while
    paused, change source before resume, and require renewed validation/review
    instead of releasing stale promotion. Resume preserves independent project
    pauses and recovery fences. Missed hooks cannot bypass CLI provider guards.
24. **Capacity release before delivery.** Fill all agent slots, accept the
    Wardens' outcomes, and confirm their native turns complete while CI or
    promotion is still pending. New Executors on independent work can acquire
    those slots; the old worktrees and delivery obligations remain retained.
    Repeat with pending cleanup and archival. Delay native completion or retain
    an unresolved worker continuation and verify that slot release is withheld.
    Trigger a later repair with all slots occupied: it waits for a fresh
    reservation and cannot reuse the completed Warden's authority. Restart
    between release and delivery; no capacity is lost or counted twice.
25. **Deferred archival and same-task repair.** Complete Executor handoff while
    Warden is active, then Warden handoff while CI is pending. Both tasks remain
    visible without retaining completed worker capacity. Fail CI and resume the
    exact same Warden task after acquiring a fresh reservation and assignment;
    no `create_thread` action is issued. Its old finish stays sealed, its old
    token cannot edit or finish again, and the repair validates and finishes new
    source under the new assignment. Lose the continuation response: retain
    uncertainty without duplicate messaging or a replacement task. Exercise
    a return to the same Executor followed by the same Warden. A missing native
    task produces an operator blocker. Only settled terminal work permits
    archival; failed cleanup, reports, follow-ups, blockers, new turns, and
    owned-process uncertainty prevent it. Pause before archive issuance and
    resume afterward.
    Restart with a closed bead's archive still pending and rediscover it. Lose
    an archive reply, then manually unarchive an observed archived task: no
    duplicate archive or automatic rearchive occurs. Leaders remain visible;
    retained links still identify archived workers.
