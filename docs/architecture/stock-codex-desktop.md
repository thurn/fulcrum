# Fulcrum through stock Codex Desktop

This design replaces Fulcrum's direct App Server integration with agents using
the native task tools in an unmodified public Codex Desktop installation.
Fulcrum continues to decide workflow policy in fresh CLI processes, with Beads
as its only durable workflow store. A local MCP server exposes those processes
to agents; a small Unix-socket broker holds connections and background timers.
Required, trusted Codex command hooks supply lifecycle observations, restore
role context, and capture native tool results through the same fresh processes.

This is a destructive replacement of the previous Fulcrum instance and runtime.
Delete their owned state and obsolete functionality; bootstrap a fresh instance.
Do not migrate old work, receipts, task bindings, or schedules, and do not retain
a compatibility layer, transport selector, or fallback implementation. The
cutover procedure below defines ownership and safe shutdown before deletion.
This document specifies future behavior; editing it neither implements the new
runtime nor authorizes running the production reset as part of that edit.

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
identity persists, but each bounded coordination turn ends after its authorized
handoff and result report, including while workers or CI remain active.
**Vizier** is the standing human-directed policy task and inbox for decisions
requiring human intervention. **Weaver** prepares new work, **Executor** implements
approved scope, and **Warden** independently
reviews it. Existing Sage, Mason, and Justiciar investigation and recovery roles
remain available through the same dispatch mechanism.

A live five-minute experiment established that a pending MCP call can
return synthetic CI failure in the same native turn without model polling or in-wait token
generation. Use this pre-finish blocking call for CI. Core dispatch and messaging
are expressible through the observed interfaces; full unattended feasibility
remains conditional. Native tools expose task creation, messaging, inspection,
archival, and scheduled follow-ups. They do not
expose the full App Server control surface. Enforced turn termination,
event-triggered wake-up of an ended task, completion evidence without agent
polling, restart and Stop behavior, and workspace access still require the
specified live acceptance checks. The blocking CI mechanism is validated within
the experiment limits below. The other missing interfaces are unresolved architecture blockers,
not implementation tasks with an assumed solution. This is not yet an
implementation-ready design for the entire unattended replacement. No
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
inspected. Read-only project/task inventory calls succeeded after the app reboot.
A disposable native task then executed one local MCP wait for 300.002 seconds,
received a synthetic failure, and completed the same turn. It made no polling
calls, wrapper wait/resume calls, or additional model responses during the wait.
The whole worker used 175 output tokens across discovery, call, and final
report; that fixed overhead is distinct from the zero extra generation during
the wait. See the [experiment report](../experiments/2026-09-16-desktop-mcp-ci-wait.md)
for input/cache accounting, exact configuration, identities, and limitations.

This establishes creation and the configured five-minute blocking MCP path.
Required hook enforcement, concurrent-recipient delivery, background wake,
completion recovery, and restart behavior were not established by that test.
Schemas alone are not successful end-to-end behavior.

During initial inspection, the bundled `codex_app` MCP failed because Desktop
had not supplied `CODEX_APP_TOOLS_PIPE_PATH`. Tools became callable after reboot.
That was a recoverable failure in this session, not a guarantee that reboot
fixes every installation. Instructions mentioning a tool are not proof it is
callable.

Bootstrap requires callable `create_thread`, `send_message_to_thread`,
`list_projects`, `list_threads`, `read_thread`,
`set_thread_title`, `set_thread_archived`, and `automation_update`, plus Fulcrum
MCP. It checks required argument fields and accepted result shapes. An omitted
or unsupported required model/effort pair is a visible failure, never a
substitution.

`wait_thread` and `wait_threads` are forbidden for every managed agent, including
zero-timeout snapshots and recovery turns. Inventory/history reads are permitted
only as a bounded, explicitly authorized evidence request, never as a status
loop. Removing wait tools from the required set is not sufficient enforcement.

The follow-up audit found only a Fulcrum compaction-context handler in the
current installer and inspected user hook configuration. No implemented
Fulcrum pre-tool polling guard or forced-exit mechanism was established. The
documented stock hook contract is not a complete enforcement boundary. Three
required capabilities therefore remain **unproven and release-blocking**:

1. A supported runtime boundary rejects polling and further unauthorized calls,
   and ends a managed turn after its final result report, including on hook
   failure and through nested calls. Prompt compliance alone cannot pass.
2. Fully idle background-only attention needs a supported way to reach the
   existing Marshal without agent polling. No such interface is established.
   CI uses the active Warden MCP response and does not depend on this mechanism;
   other unattended transitions must not pretend that gap is solved.
3. A supported completion source establishes exact task/turn termination and
   recovers missed observations without agent-driven task polling.

Record the actual supported interfaces and live evidence before implementing
the transport around them. If stock Desktop cannot supply a capability, report
that incompatibility and keep admission and destructive cutover closed. Do not
weaken these requirements or park a coordinator as a workaround. The explicitly
authorized pre-finish Warden CI wait is assigned work, not coordination polling.

Bootstrap also requires trusted `SessionStart`, `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `Stop`, and `Interrupt` command hooks. There is no
supported mode without hooks. A verified installation is still subject to
individual missed callbacks, process failures, and uncertain native effects;
those use the recovery protocol below rather than an alternate runtime.

### Mapping the existing runtime

Agent-facing native task tools are executed by agents. Background wake and
completion require their own verified supported interfaces; access to an
agent-facing tool does not establish those interfaces. Fulcrum never invokes an
internal Desktop pipe, database, WebSocket, App Server endpoint, or GUI
automation to compensate for a missing capability.

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
| Observe worker progress | Workers report meaningful progress through MCP; hooks capture lifecycle and native-result evidence; provider watchers publish existing-work events. No agent status polling. |
| Establish native completion | Required supported background completion source with exact task/turn identity and recovery. Its interface remains unproven; Stop is not sufficient and agent `wait_threads` is prohibited. |
| Inspect one unresolved effect | One explicitly authorized, scoped `read_thread` or inventory/history request for new evidence or human-directed diagnosis. No timer-driven agent rechecks or alternate-tool polling. |
| Recover a lost creation reply | Worker self-registration and correlated native history. Inventory search is a bounded exceptional recovery operation, never a normal polling loop. |
| Archive/unarchive | `set_thread_archived`; defer worker archival until the bead's delivery and remaining obligations settle, once per native task. Resume the same tasks for repairs. Manual unarchive suppresses automatic rearchive. |
| Interrupt a worker or answer a native approval | No observed task-tool equivalent. Expose the specific Desktop task for user action and retain the blocker. Do not invent a response or start a conflicting writer. |
| Observe a user interruption | The required `Interrupt` hook records the exact managed turn when delivered. It does not interrupt another task, pause Fulcrum, or prove all processes have stopped. |
| Delete tasks/projects | No observed native deletion tool. Mark automatic deletion unavailable; retain/archive owned task evidence. Hard reset cannot claim native deletion occurred. |
| Release subscriptions, inspect arbitrary terminals, measure native FD use | Desktop owns its runtime resources. Do not reproduce these App Server controls or infer their state from process-name guesses. |
| Read output and accounting | Native summaries are scoped evidence and may be truncated. Preserve task/workflow token usage and API-equivalent cost reports through the hook-supplied transcript collector below; missing evidence stays explicitly partial or unknown. |
| Wake a dormant Marshal | Agent-originated handoffs may send one authorized native message and end. Background changes require the separately verified event-triggered wake mechanism; hourly recovery cannot substitute for it. |

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

### Preserve token usage and cost estimates

Task and workflow accounting is required product functionality. Preserve the
existing `usage`, `cost`, rate-card, attribution, and completion-summary contracts;
replace their App Server evidence source with a scoped transcript collector.
The [documented hooks][hooks-doc] supply `session_id`, `transcript_path`, and
turn-scoped `turn_id`, not token counters directly. Read only the transcript
identified by a hook for a registered Fulcrum task; no Desktop database access,
global transcript scanning, or agent polling is needed.

Local inspection on 2026-09-16 found `token_usage_record` entries containing
task/turn/response IDs, per-response input, cached-input, cache-write, output,
and reasoning counters, plus cumulative turn and task totals. This is evidence
from the inspected installation, not a guarantee for the target stock Desktop.
The transcript format is explicitly unstable; readiness must verify the fields
and semantics used by the collector rather than assume a stable hook usage API.

Hooks trigger bounded collection through fresh source-following CLI operations.
The broker observes changes to registered transcript files and launches the same
collector to catch delayed writes without keeping an agent alive. Keep parsing
and pricing out of the resident broker. Persist normalized accounting evidence
and collection progress in Beads; incremental reads tolerate incomplete trailing
lines, and bounded replay after restart or missed hooks deduplicates by native
task/turn/response identity. Reconcile unique response usage against cumulative
totals; never sum successive cumulative samples. A Stop callback or usage record
does not establish native completion or release capacity.

Reuse existing bead/role/workflow attribution and retained rate cards. Correlate
usage with native model/tier and reroute evidence; configured settings alone do
not prove the effective model. Price disjoint input categories and output per
response, including applicable long-context rules; reasoning is already included
in output. Missing model, tier, counter, or pricing evidence preserves observed
tokens but marks the affected cost partial or unknown, never zero. Retain the
existing priced subtotals, frozen completion summaries, and explicit late
corrections. These remain API-equivalent estimates, not subscription bills.

Before cutover, verify target-stock transcript access and accounting through
multiple turns, repair continuations, compaction, interruption, delayed writes,
duplicate callbacks, restart, and model changes. Demonstrate correct attribution
and deduplication, plus honest coverage when evidence is unavailable. Missing
collection capability blocks readiness; an individual recoverable accounting gap
is recorded without blocking delivery. This permission to read usage evidence
does not establish a supported background completion or wake interface.

## Process ownership and source freshness

The resident boundary changes from owning a native runtime connection to owning
local transport continuity. Policy still belongs in fresh processes.

| Component | Responsibility |
| --- | --- |
| Thin MCP instance | Decode requests and invoke source-following CLI operations. Retain the transport for an authorized Warden CI wait; promptly return ordinary coordination calls. No independent queue, ledger, or scheduler policy. |
| Unix-socket broker | Own local connections, pending CI response subscriptions, timer heap, and bounded background jobs. Route provider results to pending MCP calls. A local signal cannot wake an ended native task. Keep reconstructible indexes in memory. |
| Fresh CLI operation | Read current Beads/configuration, validate authority, commit transitions, compute prompts/actions, and perform provider operations. |
| Codex command hook | Pass a scoped native event to a fresh CLI operation and return its hook response. No native dispatch, independent workflow storage, or resident policy. |
| Marshal agent | Answer bounded judgment requests, execute its authorized handoff, report the result, and end the turn. Never monitor another task or provider. |
| Worker agent | Register, validate its assignment/workspace, do authorized work, await CI through the permitted pre-finish MCP call when assigned as Warden, report outcomes, and execute authorized post-finish instructions. |
| Provider watcher | Observe an existing provider resource, publish meaningful changes durably through fresh CLI operations, and notify the broker. |

The broker imports only transport/bootstrap mechanisms. It receives timer
descriptors and wake reasons computed by fresh CLI processes, rather than
deciding what an expired timer means. One broker per instance holds a kernel
process lock; stale socket cleanup is permitted only after acquiring that lock.
Closing an MCP instance does not terminate the broker or another task's operation.

Each policy operation resolves local master at launch and pins one source and
interpreter for its lifetime. Background timer/event registration holds no
application process or state lock: an event launches a fresh computation. That
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

Background timer and hourly recovery cadences are not source-refresh intervals.
No update waits for either timer. For example:

```text
Marshal has ended its turn; the MCP and broker connections remain available
commit a completion-policy change to local master
worker calls finish -> fresh CLI uses the new commit
worker receives an authorized idle-recipient handoff, sends, reports, and ends
Marshal requests one bounded instruction -> fresh CLI uses the new commit
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
| `PreToolUse` | Deny forbidden polling and calls outside the managed turn's allowance on every covered path. Associate Fulcrum admission calls with session/turn evidence. For authorized native mutations, validate the one claimed attempt and exact arguments, then bind `tool_use_id`. Return a native deny decision on mismatch; do not rewrite calls or approve permissions. |
| `PostToolUse` | For a bound native invocation, record its result through the shared action-result transition and notify the broker. Return without replacing or hiding the tool output. Do not convert ordinary tool activity into worker progress or successful role outcomes. |
| `Stop` | Record a stop attempt. Suppress continuation for an orchestrator or completed handoff; only the bounded pre-handoff worker correction below is eligible. Never declare final native completion or release a worktree. |
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
Hook guards cover inspected paths; they do not establish shell isolation or
complete tool coverage. The separate runtime enforcement gate below is required.

### No polling and mandatory turn exit

The managed-agent contract is **claim, send, report, end**. It applies to Marshal,
Weaver, Vizier, bootstrap/recovery coordinators, and worker handoffs. No
orchestrator remains active to monitor work it dispatched. Executor and Warden
may perform their assigned work and local tests. Warden may additionally call
`wait_for_ci_results` before finish while retaining its assignment. One pending
provider call suspends model execution; it is not a model status loop. No worker
waits for another role, CI, promotion, or cleanup after its final handoff.

For coordination and terminal handoffs, persist an allowance for the exact native
turn: one bounded instruction request, its explicit decision if needed, at most
one authorized native action, and its
result report. Idempotent replays return saved results without granting more
allowance; they do not authorize an agent retry loop. Claiming the action changes
the turn to report-only after that exact invocation; accepting its result
closes the allowance. An idle,
paused, blocked, or report-only terminal result also requires exit. Remaining
actions stay durable for a subsequent authorized event-driven turn. Do not issue
self-messages or recurring status tasks merely to keep a coordinator running.
Already-recorded result acceptance remains available to hooks and recovery even
after the agent's allowance closes. Accepting hook-captured action evidence
does not consume the agent's one result-report opportunity. That report returns
an exit instruction, never another action for the same turn. If there is no
native follow-up, accepted finish itself is the worker's terminal protocol step.

The runtime must also bound protocol attempts and end on transport failure or
an exhausted allowance. Permit at most one agent result-report attempt after
the native call. A lost or failed report leaves recoverable evidence; it cannot
keep the actor alive retrying MCP. Apply a runtime-enforced request/turn limit
to prevent repeated denied calls or endless reasoning before a terminal step.
Allow the registered pre-finish CI call its explicit transport deadline; do not
apply a short coordination timeout to legitimate pending worker work.
Record and probe the actual supported limits; a ledger outage must not make
the restriction fail open. Identical requests can be reconciled by background
handlers or a separately justified recovery turn, not an automatic retry chain.

Fulcrum CLI/MCP handlers reject new work outside that allowance, independently
of hook delivery. `PreToolUse` additionally denies `wait_thread`, `wait_threads`
(including `timeoutMs=0`), parked instruction requests, sleep/status loops,
repeated `read_thread`/inventory reads, CI watch commands, and equivalent calls
through other tools. A scoped diagnostic read requires an exact retained
evidence request and consumes its bounded allowance. Expiration of a timer does
not authorize an agent to repeat a status query. Cover actual tool aliases,
direct calls, nested code-mode calls, and delegated attempts; a tool-name list
alone cannot detect shell scripts or prevent a bypass. Permit only the registered
Warden CI wait for its exact current candidate and assignment, with one pending
request. Marshal and other coordinators cannot use it. Returning `pending` on a
short timer and having the model call again is prohibited polling.

Hooks are an additional guard, not the promised hard boundary. Official
documentation permits pre-tool denial on covered nested calls but exempts some
paths; `write_stdin` does not repeat that check. Post-tool `continue: false`
changes result processing, not guaranteed turn termination. Stop can veto
continuation only once exit is attempted. Therefore readiness must prove a
supported runtime-level restriction and terminal handoff that do not depend on
the model obeying an instruction or on every hook succeeding. No such interface
has yet been established by this design's inspection.

Probe attempts to bypass through alternate tools, shell/network access,
already-running command sessions, concurrent calls, subagents, hook timeouts,
disabled/untrusted hooks, and caught code-mode errors. The required result is
no polling side effect and no further model turn after the terminal protocol
step, not merely a denial followed by repeated model retries. A queued new
authorized event is a distinct turn with a new allowance. If enforcement cannot
meet this contract, fail readiness; do not describe cooperative instructions or
passing happy-path tests as an ironclad guarantee.

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
An unchanged request replay therefore adds no historical hook event or receipt.
Native mutation results and unresolved invocation evidence are never pruned as
handshake metadata.

Hook delivery has no assumed total order or replay guarantee. A late callback
may add evidence to its old assignment/attempt but cannot change current
ownership, clear a newer blocker, or supply authority for another turn. Store
turns by native ID; do not order opaque IDs or let a delayed callback overwrite
the current turn. Fulcrum MCP admission calls (`register_worker`,
`get_instruction`, and `claim_action`) require a matching invocation
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
tool/arguments, retained authority, and applicable pause/replacement fences.
For a native send, it additionally verifies the matching recipient reservation
and confirmed-idle evidence. It then commits its native invocation identity. Deny a call with no matching claim,
changed arguments, or a second distinct tool-use ID. A repeated callback for the same ID can
return its recorded decision; that is not authorization to invoke the mutation
again. Expired leadership permits result recovery, not a new invocation. Save a
deny decision as an intended hook response, not proof that Desktop applied it.
Only a correlated native rejection establishes pre-execution failure; a lost
hook response can still leave the action uncertain. After an actor reports an
uncertain result, that turn ends. Unrelated actions may proceed in a later
authorized turn, but the old attempt and reservation remain unresolved and
cannot be reissued.

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
explicit claims, registration, exact postconditions, and the separately verified
runtime restriction remain necessary. Known broken hook configuration closes new
admission until repaired; one missed result callback leaves its effect subject
to normal reporting/reconciliation rather than globally pausing the instance.
Unmatched results remain scoped diagnostics, not permission to adopt a task.

### Bounded correction at normal turn exit

Only when a worker has not yet handed off and attempts to end without its
required finish may the `Stop` handler return one continuation asking it to
settle that exact obligation through MCP. Never continue an orchestrator or
reopen a turn after a native handoff, even for a missing result report; retain
that report obligation for event-driven recovery. Commit the correction
claim before returning it. Track the allowance by assignment and obligation,
not only native turn ID, so a hook-created continuation cannot reset the budget.
Honor `stop_hook_active` as an additional veto. The correction may report a
blocker or uncertainty; it may not retry a native mutation, invent an outcome,
or resume source edits after finish.

No correction is issued when Fulcrum is paused, hook readiness is broken, the
turn was interrupted, the worker is awaiting human input, or no immediate
reporting correction is possible. If the one correction is lost or ineffective,
retain the missing-report obligation for event-driven recovery. Return
`continue: false` to veto Stop continuations for orchestrators and completed
handoffs; never use `decision: block` to keep them alive. A Stop callback remains
an exit attempt, not final native completion. A queued authorized message can
start a new turn. Keep the independent completion and process-cleanup gates.

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
and leadership actions plus the per-recipient delivery gates described below,
not copies of per-work authority. A transition affecting several work beads is a recoverable sequence of independent commits, never a
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

No shared state lock spans agent execution, network calls, or waiting for a
provider or another process. An unchecked old whole-record snapshot is never
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

The Warden CI wait retains its assignment and consumes its agent slot. CI
failure and permitted repair within that assignment do not reacquire capacity or
send a new native message. A successful Warden finish requires passing CI for the
reviewed source; a blocked/failed finish may relinquish work with its obligations
retained. Promotion, synchronization, cleanup, or archival may remain pending
after accepted finish and verified native completion without occupying a slot.
Keep their obligations and workspace ownership intact; release is neither a successful-delivery claim nor permission
to delete the worktree. Provider operations use bounded broker scheduling and
their existing resource locks. Any repair after that release resumes the existing
role task; it must acquire a fresh agent reservation under current capacity and pause rules
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

Unchanged provider observations, timer ticks, request replays, and socket wakeups
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
| `register_worker` | Bind the native task/host ID to the exact creation or authorized continuation action and assignment before substantive work. A post-finish repair reuses the retained role task with a fresh assignment token; pre-finish CI repair retains its current assignment. |
| `report_progress` | Persist meaningful progress and renew the assignment's reporting deadline. |
| `submit_candidate` | Warden only, before finish: retain exact reviewed source and local-check evidence, then submit or recover its provider candidate idempotently without transferring assignment ownership or granting promotion. |
| `wait_for_ci_results` | Warden only, before finish: validate the exact submitted candidate and current assignment, register one pending MCP response, and return terminal CI evidence or an explicit blocker. No periodic `pending` responses. |
| `finish` | Accept a role outcome, commit the transition, and return exact authorized follow-ups. Warden success requires passing CI for the exact reviewed source; blocked/failed outcomes retain unresolved delivery. |
| `get_instruction` | Acquire bounded Marshal coordination and promptly return one decision request, native action, end, or pause result. Never wait for external progress. |
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
    "prompt": "Fulcrum-Action: {\"instance\":\"/absolute/instance\",\"record_id\":\"work-bead-id\",\"action_id\":\"random-action-id\"}\nCall get_instruction.",
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
arguments, calls the named tool once, reports its result, and ends its turn.
Another action requires a later authorized turn. Independent agents can execute
actions for different work.

`claim_action` revalidates the matching recipient gate for any native message
and commits the executing actor, current token, attempt ID, and expected result
before returning arguments. A busy/ending/unknown recipient holds the action
without granting a call. Recheck at invocation; a stale idle observation cannot
authorize delivery after another native turn has started. Enforcing that final
race against user/scheduler input requires the still-unproven runtime boundary.
A repeated claim while issuing returns status and reconciliation instructions, not permission to resend. A
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
editing after transfer. At most one native follow-up is returned to the worker;
after reporting it, the worker ends its turn. Additional follow-ups remain
durable for event-driven coordination. A missing report remains a recoverable
obligation and cannot sustain the old turn.
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
Read-only inspection does not authorize retrying the mutation it observes.
An agent may inspect only under a bounded evidence request tied to new evidence
or explicit human diagnosis. Repeating inspections on timers is prohibited.

| Effect | Settlement evidence and retry rule |
| --- | --- |
| Send a message | Recipient acknowledgment of the exact action ID, or exact persisted native message content and target, proves delivery. A recent summary's omission proves nothing. Without either, keep uncertain. A definitive tool rejection before acceptance permits a new attempt. |
| Create a schedule | Adopt one exact inventory match for instance, action marker, and target, confirmed by native view. Zero incomplete-inventory matches or several candidates remain uncertain. |
| Update/pause a schedule | Native view must match the intended target, cadence, prompt, notification policy, and state. Read back before any repair attempt. A different value alone is not proof the earlier call cannot still land; reconcile the old caller first. |
| Archive/unarchive a task | Native inventory/history must positively show the requested state for the exact task. Missing from the recent list is not archived evidence. Do not repeat a possibly still executing call. |
| Rename a task | A native observation of the exact task ID with the intended title settles success. A different title alone does not prove an outstanding call failed; retain uncertainty until that attempt is settled before issuing a correction. |
| Delete a retired paused schedule | Native view reports the exact retained ID absent and complete local inventory confirms absence. Unknown/error is not absence; keep replacement fenced. Never delete a merely similar schedule. |
| Inspect a task | Record the observation or error under the exact bounded evidence request. No scheduled agent rechecks; completion uses the required background source. New evidence or explicit human diagnosis may authorize another scoped read. |

A recipient acknowledgment commits delivery evidence on the action's owning bead
even if the sender's result is lost. It never marks the message's requested
workflow complete. Conversely, native send acceptance records successful
notification but does not clear Marshal attention; the work transition that
handles the attention does that. The sender may report only actions granted to
it; the recipient's acknowledgment grants no sender authority.

Uncertain delivery retains the recipient reservation. There is no redundant
wake exception: a newer recovery generation cannot send another native message
while an older delivery is unacknowledged or uncertain. Pending information
remains durable and is merged only before delivery, never by repeating a send.

### One outstanding delivery per recipient

Use recipient state, not whether the whole system is idle. Apply the same rule
to Marshal, Vizier, Executor, Warden, and recovery tasks. The control bead
`fc-system` owns one delivery gate per exact native task/host across all senders
and work beads. This gate holds only delivery coordination: observed native turn,
idle/active/ending/unknown state, one reserved action or MCP response, and its
acknowledgment. Work scope, pending payloads, assignments, and outcomes remain on
their owning work beads; task summaries remain projections.

All delivery channels consult this gate under the shared writer lock:

| Recipient state | Delivery |
| --- | --- |
| Positively idle, no outstanding delivery or unresolved continuation | Reserve one exact native message action. Its authorized sender may claim, send, report, and end. |
| Active with an eligible MCP response not yet committed | Reserve that response and include applicable current-assignment information there. No native message. |
| Active without an eligible MCP response | Retain pending information until its next normal MCP call or positively observed idle state. Do not interrupt or ask it to poll. |
| Ending, unknown, or with an unacknowledged/uncertain delivery | Hold further delivery. Do not queue a second native prompt. |

A response is eligible only if its tool contract permits the information.
`wait_for_ci_results` returns the candidate outcome and applicable blocker/control
information. It does not return early merely because unrelated mail arrived.
Terminal `finish`/result-report responses still say end and never admit a new
assignment. New scope waits until the current assignment is released and the
task is positively idle. MCP acknowledgment alone is not idle evidence.

The originating work transition first retains the obligation. Under the writer
lock, reserve its action/response locator on `fc-system`, then bind that
reservation to the originating record before exposing delivery. These are
recoverable separate commits, not an atomic cross-bead transaction. A crash
between them leaves the gate occupied; reconcile the exact recorded locator
before proceeding. Never return send permission before both are confirmed.
Acknowledgments settle the origin first and then clear only the matching gate
reservation. A crash here delays another delivery rather than permitting two.
Only one response can claim a payload; concurrent MCP calls cannot both drain it.

The agent acknowledges an MCP delivery on its next normal protocol request; no
acknowledgment-only polling turn is created. A terminal response requires native
completion evidence to settle its gate if no subsequent request exists. Lost
responses retain uncertainty and their original IDs. Native acknowledgment can
settle delivery but the observed active turn still prevents another native send.

This specifies the invariant; the experiment did not prove its enforcement.
Native user input and Desktop scheduling can bypass Fulcrum's gate, so no claim
of an absolute native guarantee is valid without a tested runtime boundary.
An hourly scheduler must gate before injecting its prompt; queuing it inside a
busy native task does not satisfy this contract.

## Marshal lifecycle and wake delivery

A coordination turn handles a bounded instruction and then ends. Workers,
provider CI, pending delivery, human blockers, and durable deferrals never keep
Marshal's turn alive. Task identity persists across separate event-driven turns.

`get_instruction` computes under the writer lock and returns promptly:

- `decision`: one bounded Marshal judgment with exact scope and comparison facts;
  accepting it may return the turn's one authorized action or an exit result.
- `action`: claim it, execute its exact native call once, report, and end.
- `end` or `paused`: close the turn allowance, release the lease, and end.

Marshal has no `wait` or `renew` disposition, parked MCP request, or long-wait
configuration. The Warden CI tool is a separate pre-finish worker contract. An unchanged status cannot authorize another instruction request
in the same turn. Native creation returns accepted identity/setup evidence or
uncertainty; the creator reports it and ends without waiting for the worker to
finish. More pending actions require subsequent authorized turns under the
retained event protocol, not a loop inside the current turn.

### Attention delivery and exit races

An incoming work transition commits its attention generation and delivery
obligation with the work state under the writer lock. Closing Marshal's turn
commits its handled generation and releases its lease under that same lock.
Native delivery or acknowledgment alone never clears unhandled attention.

For an agent-originated handoff, return a native message action only when the
recipient gate permits it. If Marshal is active, retain the attention for an
eligible MCP response; otherwise hold it. The sender reports its own result and
ends without waiting for Marshal. Warden CI results return through Warden's
pending MCP call, so the normal five-minute failure case needs neither a new
Marshal turn nor a background native wake.

There is currently no verified supported way for background code to wake a fully
idle Marshal, or to deliver held attention once the last eligible sender has
ended. A broker signal is not a native task message. Do not base implementation
on an unspecified event-to-task API, GUI automation, undocumented pipes/databases,
an App Server connection, a waiting coordinator, or frequent assistant timers.
Human resumption can discover retained attention, but is not unattended delivery.
This remaining gap blocks claims of a complete unattended architecture; the CI
experiment does not resolve it. Resolve that separate design question with a
concrete supported experiment before implementing dependent transitions.

Reconstruct pending attention and delivery reservations from Beads after restart.
Events committed before, during, or after exit remain pending until handled.
Ending and unknown states prohibit native sends. Never use a lease expiry,
missing summary, or receipt acknowledgment as proof that a recipient is idle.
Tests must cover multiple senders, response commitment races, lost replies,
uncertain sends, and restart without duplicate delivery or lost generations.

### Leadership leases and turn allowances

`fc-system` retains the current Marshal native ID, lease token, expiry, native
turn binding, allowance, and handled attention. A fresh CLI operation grants the
registered Marshal a lease under the writer lock. The default expiry is five
minutes as crash protection, not a reason to stay active or run renewal timers.
Instruction, claim, decision, and result calls validate the bound native turn;
legitimate protocol progress may extend its lease without extending its action
allowance. The terminal result releases it. There is no lease-renewal-only call.

Only the currently bound Marshal task may acquire leadership. Expiry permits a
later authorized turn of that same task to reacquire; it cannot authorize a
second task or make a consumed turn allowance reusable. Changing the native ID
requires the explicit fenced replacement protocol below. Competing or stale
turns may report retained effects but may not claim new actions.

A claimed action remains issuing if its lease expires or the agent exits during
the native call. Late results are retained against the exact attempt without
renewing old authority. A subsequent turn cannot reissue that effect merely
because the lease expired; unrelated work remains independently eligible.

Changing leadership does not fence a native call already in flight. Worker
registration and assignment checks prevent it from becoming a second authorized
writer. A stale worker already editing requires observed termination before
another writer starts. The inspected task tools expose no interrupt operation;
that exceptional case requires user action. This limitation does not relax the
separate mandatory terminal-handoff enforcement gate.

### Hourly recovery

After verifying pre-delivery gating, bootstrap creates one heartbeat attached
to the Marshal task, recurring hourly. Until that check passes, keep it disabled
and report recovery scheduling as unavailable.
It does not create a new standalone task each hour. The saved prompt directs
Marshal to call `get_instruction` with recovery intent, settle durable
pending actionable work within one turn allowance, and end in every case.
Notification policy is stored through the automation tool; the prompt says to
produce no routine status message when there is no actionable change.

The hourly trigger is a nominal cadence, not a guaranteed recovery SLA. Desktop
must be running and the machine available. Scheduling must obey the recipient
gate before native prompt injection: when busy, ending, unknown, or reserved,
retain/coalesce the obligation outside the native task. Do not queue or steer
another native prompt into an active turn. The available schedule schema alone
does not establish such a guard; this is unproven and blocks enabling the
heartbeat. A guard that runs only after the prompt arrives is too late.
Persisted action claims make an interrupted call recoverable; they do not prove
whether that call happened. An hourly event cannot reset a turn allowance. The heartbeat cannot repair a broken native-tool MCP
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

MCP cancellation records interruption evidence when available; a socket
disconnect is transport-loss evidence. Neither changes
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

Readiness verifies this distinction during a protocol call and after turn exit.
It does not require Desktop Stop to suppress future scheduled runs or messages.
Native interruption observations improve diagnosis without becoming a prerequisite for
durable pause. The required `Interrupt` hook supplies turn-specific evidence
under the hook acceptance contract. An individual missed callback leaves the
reason unknown; it does not change the agreed semantics or waive readiness's
requirement for a working hook installation.

## Reporting, completion evidence, and delivery

Workers report meaningful progress at least every ten minutes when able to call
tools. The reporting deadline is thirty minutes after registration or accepted
progress. A long-running local tool may delay reporting. An authorized pending CI call
records its own deadline and suspends the ordinary progress deadline until it
returns; do not wake the model merely to report unchanged waiting status.
Deadline expiry records
a missing-progress obligation; it is not failure evidence, permission to start
a duplicate worker, or permission for Marshal to poll the task. Emit one scoped
recovery notice for the changed condition; unchanged overdue status does not
produce another model turn. New progress clears the obsolete obligation.

### Native completion without agent polling

Accepted finish, Stop, Interrupt, and PostToolUse are not proof of final native
completion. Handoff, capacity release, and cleanup require evidence from the
separately verified supported background completion source. It must identify the
exact task, host, and assigned native turn, positively establish that turn ended,
and account for queued or unresolved continuations. An old completion event
cannot establish that a newer turn is finished. Missing IDs, errors, ambiguous
history, and a missing task remain unknown. A final-text snippet is insufficient.

Before release, the capability probe must establish the actual event/subscription
or supported non-agent observation contract, including missed-event recovery and
app restart. If recovery requires a bounded background read, prove its supported
interface and identity checks. Do not convert it into an agent `wait_threads`,
`read_thread`, or inventory loop. Stop is an observation to correlate with final
evidence, not a substitute. The inspected hooks/task tools alone have not yet
established this capability; without it, admission remains closed.

Retain the last accepted completion evidence and any unresolved obligation on
its work bead. Background recovery may retry the supported source with bounded
backoff; no model is invoked for unchanged running/unknown status. Loss of the
completion source closes affected admission and retains reservations/workspaces
until positive evidence or explicit operator recovery settles them. Never
release them on a timeout or because the worker said it was done.

A completed turn without finish retains a missing-outcome recovery obligation.
A native approval or user-input request becomes a blocker linked to its exact
task. No agent supplies an invented answer. A later authorized event or explicit
human resolution can resume the same task; the old turn stays ended.

Executor finish seals its source-writing outcome. Warden starts only after the
old assignment relinquishes source access and native completion is established.
Warden may fix findings directly. One accepted Warden finish seals review for
the exact source and assignment; delivery never asks it to repeat that finish.
A repair after final finish uses a fresh assignment in the same task and seals
a new outcome. Pre-finish CI repair retains the current assignment.

### Blocking Warden CI wait and the five-minute failure case

Warden reviews and fixes the candidate, runs local checks, and submits its exact
source through `submit_candidate` and the existing delivery handler before final
`finish`. The candidate approval identifies immutable source and permits CI
validation only; it neither relinquishes Warden ownership nor authorizes promotion. Fresh CLI
operations still own provider submission, source/review checks, promotion,
synchronization, and cleanup. Persist candidate/run identity and submission
uncertainty before waiting; neither reconnect nor a lost reply resubmits it.

Warden then calls `wait_for_ci_results(candidate_id, assignment_token, request_id)`
once. These define the proposed production arguments; the experiment used a
small synthetic tool and did not implement this handler. The handler verifies
caller, assignment, source, candidate, and absence of another pending wait. It
returns already-terminal retained evidence immediately, or registers a pending
response and releases the policy process and all state/resource locks. The thin
MCP transport and broker hold the connection/subscription, not model execution
or a resident policy computation. Registration and terminal-result lookup must
serialize with event publication so a result cannot fall between them.

Prefer provider events. Where the provider requires status queries, bounded
software jobs query the exact run through Tollgate; this is server-side polling,
not Executor/Warden/Marshal polling. Use a 30-second pending interval and error
backoff of 60, 120, then 300 seconds. Unchanged observations generate no agent
response, token generation, or durable event. A terminal result launches a fresh
CLI operation from current local master, validates candidate/source identity,
persists the evidence, and completes the pending MCP response. No policy process
or old source snapshot is kept alive for the wait.

For CI that takes five minutes and fails:

1. Warden retains its task, turn, assignment, worktree, and capacity reservation
   while the single MCP call is pending. Marshal may be ended throughout.
2. Software receives/observes terminal failure and returns exact candidate,
   source, failed checks, and bounded diagnostics in that call's response.
   No native message, new assignment, or Marshal wake is needed.
3. A fresh transition grants a repair cycle under the existing scope, pause,
   and failed-cycle allowance. Warden repairs within the same assignment,
   resubmits changed source, and waits once for that new candidate. A terminal
   failure justifies substantive repair; a timer or unchanged status does not.
4. Passing CI for the reviewed source permits final successful `finish`, its
   authorized handoff, result report, and turn exit. Native completion evidence
   then permits capacity release. If repair is blocked, paused, or exhausted,
   return that disposition; Warden reports a blocked outcome and ends instead
   of waiting for human action or automatically starting another wait.

Capacity is deliberately occupied during CI. This is the cost of retaining the
active assignment and simple in-turn repair. Executor does not wait for Warden's
CI. Promotion/cleanup after final handoff run in software; further agent work
then needs the post-finish repair protocol below.

Configure an explicit MCP deadline and, when code mode is used, a wrapper yield
budget longer than the intended wait. The measured configuration was
`tool_timeout_sec=420` and wrapper `yield_time_ms=360000` for a 300-second result.
The default MCP timeout is 60 seconds. This proves neither arbitrary durations
nor that production can use an infinite wait. Before enabling real CI, measure
the supported duration and choose a finite provider deadline below both transport
budgets. If that deadline expires, return an explicit blocked/unknown outcome,
not `pending` plus instructions to call again. Do not implement model-driven
short-timeout retries or wrapper-resume loops as the production wait mechanism.

On cancellation, disconnect, or restart, preserve the candidate, wait request,
ownership, and delivery uncertainty. Software may reconcile provider state but
must not infer native completion, release the slot, resubmit, or blindly deliver
a second response/message. Resumption reconciles that exact wait and candidate
under the recipient gate before further work. Automatic reconnection and long
wait recovery remain untested; do not claim transparent restart support.

The live [five-minute experiment](../experiments/2026-09-16-desktop-mcp-ci-wait.md)
passed with zero worker polling and no extra model generation during the wait.
Real-provider integration, concurrent delivery, long-duration limits,
cancellation, and restart are still required acceptance tests.

Passing CI or native turn success alone is not promotion evidence. Delivery
revalidates the exact source, approval, and pause state. Source changes invalidate
review and prior CI. Cleanup failure after promotion remains a cleanup obligation.
Before deleting a worktree, require accepted outcomes, observed native completion,
settled delivery, and no unresolved owned background-process obligation. Workers
terminate their own background commands before finish and retain process locators;
unknown ownership blocks deletion. Never terminate unrelated processes.

### Same-task repair and deferred archival

Keep the bead's existing worker tasks unarchived through review, provider CI,
promotion, required synchronization, and cleanup. A worker's accepted finish
and native completion can release its agent slot without archiving its task.
Executor stays available while Warden reviews; Warden stays available while
delivery runs. Routine failure, delay, or retry never creates a replacement
native task.

Pre-finish CI repair stays in Warden's active assignment as described above; it
does not send a continuation or allocate another slot. When a failure discovered
after final handoff requires Warden repair, retain the exact failure evidence
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

Allow at most three unsuccessful automatic repair cycles per bead before
requiring a human decision. The original implementation/review and the failure
that first requests repair do not consume this allowance. Start a cycle when
Fulcrum grants substantive repair against retained failure evidence, either
within the active pre-finish Warden assignment or in a fresh post-finish
assignment; associate any Executor-to-Warden return with that same cycle so role
handoffs cannot multiply or reset the allowance. Record its identity,
assignments, source before/after, attempted correction, and observed result on
the work bead.

A cycle fails when its attempted correction is complete and authoritative
validation/delivery evidence establishes that further repair is required, or
the worker explicitly reports that the attempted correction did not resolve the
problem. Count that cycle once in the same transition that accepts its failure.
Waiting for CI, repeated status observations, capacity waits, native message
retries, and transport errors do not count. An uncertain outcome remains
unresolved; do not count it as failure or grant another repair to bypass it.
Edits and local test iterations within one repair assignment are not separate
cycles. Passing one check, changing source, switching roles, restarting Desktop,
or resuming the same task does not reset the bead's failed-cycle total.

On the third failed cycle, atomically retain the failure history and a
`repair_limit_reached` human blocker instead of another repair grant or
continuation action. An active Warden receives it through MCP, reports the
blocked outcome, and ends.
Hold only that bead; keep its tasks visible, preserve its worktree, and let
unrelated work continue. Once its current worker is confirmed complete, release
the agent slot under the normal completion rules. Surface one actionable summary
with the three attempted corrections, current failure, source, and task links;
unchanged hourly recovery does not repeatedly announce it. The hold prohibits
further repair dispatch and promotion but permits evidence collection and
report acceptance.

Only an explicit human decision resolves this limit: supply revised direction,
authorize a specific additional number of cycles, or choose a terminal
disposition. Use the existing human-resolution path with the exact blocker and
stable request ID; retain the decision and prior failure history. Marshal or
Vizier cannot autonomously renew the allowance. A global resume, source commit,
or duplicate resolution request does not clear this bead's blocker. Additional
repair still uses the existing tasks and fresh capacity/assignment checks.
Extend the resolution payload with a positive `additional_repair_cycles` field
when continuing repair; revised direction alone does not replenish the budget.
Vizier may record the human's explicit grant, with its evidence, but may not
invent one from its general policy authority.

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

### Human decision inbox

Route requests for human intervention to the existing Vizier task. Persist the
decision obligation on the affected work bead, with the exact blocker, current
evidence, requested decision, and links to the relevant native tasks. This
includes exhausted repair allowances and native approvals or interruption that
Fulcrum cannot perform. Native approvals still require action in the original
Desktop task; a response in Vizier is not evidence that Desktop accepted one.

Delivery uses the same recipient gate: Marshal may execute a returned native
message to the retained Vizier ID only while Vizier is confirmed idle and
unreserved. If Vizier is busy, retain the request for an eligible MCP response
or later idle delivery. Use the normal claim/result protocol and action marker.
Hooks and provider watchers only publish the durable obligation; they do not
send native messages. Such a wake authorizes Vizier to acknowledge the action,
read current decision evidence, present one actionable request, and end its
turn awaiting human direction. It does not authorize autonomous resolution,
additional repair cycles, policy changes, or resume. Include this restricted
notification authority in bootstrap and the cooked Vizier prompt.

Keep notification delivery separate from blocker resolution. Acceptance or
acknowledgment of a message never clears the blocker. A human response uses the
existing resolution path with the exact blocker and stable request ID; validate
that the decision is still applicable before granting any dependent action.
Unrelated beads continue while one waits for that decision.

Retain one notification obligation per blocker and material decision revision.
Repeated observations, hourly recovery, and app restart cannot create another
notification for unchanged evidence. Changed evidence warrants a new revision
only when it changes the required decision or invalidates the prior request;
routine progress does not. Supersede unissued notices when their blocker is
resolved. An issued notice still requires settlement, and Vizier rereads current
state before presenting a request so stale delivery cannot reopen a blocker.
Use the normal uncertain-message evidence rules; no redundant wake exception
permits a second unacknowledged delivery. A missing or unusable
Vizier task leaves notification pending and exposes the failure in diagnostics;
never create a replacement inbox automatically.

Global pause holds these unissued notification wakes like other messages.
Pending decisions remain readable through status and explicit human interaction
with Vizier. Resume revalidates pending notices before sending them. Exercise
delivery while Vizier is already handling a human response: incoming notices
must remain pending or use its eligible MCP response, preserving the human
response and action receipts. No native prompt may be injected into that active
turn, and no second independent policy authority is granted.

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

The skill accepts a retained Fulcrum checkout and optional existing new-contract
instance selection. A legacy instance is a destructive-reset prerequisite, never
an upgrade/adoption candidate; ordinary setup cannot implicitly delete it. It
discovers installed prerequisites and existing configuration
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
2. Configure local Fulcrum MCP for bounded protocol calls through supported
   Codex configuration. Configure the explicit pre-finish Warden CI timeout and
   wrapper budget after measuring supported bounds; other coordination calls
   return promptly. Restart only that
   MCP connection when initial configuration
   requires it; a full application restart is a reported exceptional
   prerequisite, not an ordinary update step. Resume the same bootstrap record
   afterward.
   Install the six required command hooks with stable absolute launcher argv,
   preserving unrelated and other-instance definitions. Present any native hook
   trust prerequisite and verify effective configuration plus observed callback
   execution; a file on disk is not evidence that Desktop loaded or trusted it.
   Establish the separately required runtime turn restriction, recipient gate,
   and completion source with tested supported configurations. Fully idle
   background delivery still requires resolution of the documented design gap;
   it is not an assumed configuration option.
   Missing capabilities stop setup before product admission.
   Retain exact installed definitions and probe results on `fc-system`, without
   a configuration hash. Hook verification is required even if MCP is healthy.
3. Inspect callable native tools and run read-only connectivity checks. Explain
   exact missing fields or tools. Discover saved projects; guide the user
   through adding a project if the native tool surface cannot create it.
4. Present the explicit coordination authorization: native task creation and
   messaging for admitted work, notification-only decision requests in the
   existing Vizier task, configured model/effort values, saved-project
   local targeting with Tollgate worktrees, managed-task hook observations and
   bounded pre-handoff worker corrections, enforced send-report-end turns,
   recipient-gated delivery, the pre-finish blocking Warden CI call, background
   completion/provider observation, and verified hourly same-task recovery. Disclose that Desktop Stop interrupts one turn while
   "pause Fulcrum" durably pauses new
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
   performing leadership work. Vizier completes readiness and ends its turn;
   human direction or retained decision notifications can resume it. Marshal
   handles a bounded instruction and ends, even while other work is active.
7. Verify pre-injection recipient gating before enabling the hourly heartbeat
   on Marshal; unavailable gating leaves scheduling disabled and readiness
   incomplete. Persist its returned ID,
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

These rerun rules apply only within the freshly bootstrapped new-contract
instance, not to legacy resources discarded at cutover.

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
creation and messaging, notification-only decision requests in the existing
Vizier task, local-project targeting with assigned Tollgate worktrees,
required persistent tool approvals, managed-task hooks with bounded reporting
corrections before worker handoff, enforced send-report-end turns, supported
recipient-gated delivery and completion observation, the pre-finish Warden CI
wait, and one hourly follow-up on Marshal only after its delivery gate is
verified. Confirm missing model choices and explain the scope of each approval
configuration. Complete native hook trust
review through the supported user flow; do not bypass it. Explain that Desktop
Stop ends the current turn and "pause Fulcrum" durably pauses coordination.

For the turn's returned native action, claim it, execute its exact arguments once,
report the result through Fulcrum MCP, and end the turn. Do not poll, sleep, call
wait_thread/wait_threads, or request another instruction after handoff. Remaining
setup actions resume through retained event-driven turns or explicit user input.
Record returned task and schedule IDs
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
get_instruction with recovery intent. Honor explicit Fulcrum pause state.
Settle retained actions before new effects. Follow Fulcrum's exact instructions.
Handle at most the authorized bounded instruction. After any handoff, report its
result and end. If no action is available or coordination is paused, end without
a routine status update. Never poll tasks or CI, sleep, use wait_thread or
wait_threads, or park an MCP call. Report only actionable failures, required user
input, or meaningful completed work.
```

## Feasibility checks and operational evidence

The five-minute pending-MCP mechanism has passed the linked disposable test.
That result corrects the earlier assumption that a long tool call necessarily
burns model tokens. It does not establish enforcement or the entire architecture.

Before the runtime rewrite or destructive production reset, use a disposable
instance to prove the three unresolved capability gates: enforced terminal
handoff without polling, background event-triggered wake of an ended Marshal,
and exact completion evidence without agent polling. Also verify hook trust,
identity, direct/nested coverage, and native result shapes. A minimal
source-following probe handler is sufficient for hook evidence, but cannot prove
a missing runtime boundary. Record actual supported interfaces and observed
payloads after removing unrelated/private content. A missing capability stops
implementation of dependent transport and blocks destructive cutover.

The first assembled-product check crosses the actual boundary: a stock Desktop
Marshal dispatches a registered probe worker, reports, and ends. The worker
finishes; background completion evidence releases the next transition, and the
verified wake mechanism resumes the same Marshal exactly as authorized. Repeat
with a separate Weaver handoff and a provider failure after five minutes of
CI while Warden has one pending MCP call and Marshal is ended. The CI result
returns directly to Warden in its existing assignment without a native wake.
Include real Beads persistence,
broker scheduling, separate MCP clients, hooks, and supported native interfaces.
Component doubles cannot establish this behavior or the absence of model
execution while only external work remains.

Bootstrap's readiness probe uses disposable control records, not product work
admitted outside Weaver. Verify creation/registration, naming, messaging,
worker access to a disposable Tollgate worktree, reports, independent completion
evidence, forced turn exit, and event-triggered wake. Attempt forbidden polling
and post-handoff calls, including hook failure and bypass paths. Retain probe
IDs and clean them through observed archive/cleanup actions. Readiness remains
incomplete while any required capability is unproven. Reruns preserve verified
steps only when effective schemas, hook trust, runtime restrictions,
configuration, and connection behavior are unchanged; failures cannot inherit
an earlier success silently.

These are manual capability checks on a disposable instance. They do not
restore the retired expensive live harnesses or make ordinary code edits run
external providers. The normal repository check remains provider-independent.

| Required observation | Failure consequence |
| --- | --- |
| Registered-task transcripts support recoverable token accounting and cost reports with verified attribution and coverage | Admission and cutover stay closed if the collection capability is unavailable; individual evidence gaps remain explicit in reports. |
| Native task tools callable with required schemas | Admission stays closed; report missing tool/field or MCP startup error. |
| All six command hooks load, are trusted, and execute for projectless leaders and local-project workers | Admission stays closed; report the exact trust, loading, identity, or handler failure. |
| Direct and nested native calls expose matching pre/post invocation IDs and parseable results | Admission stays closed; do not promise result capture for an unobserved path. |
| Startup/resume/compaction restore bounded current role context; initial prompts and resumed turns acquire hook admission evidence | Refuse affected task admission; never authorize work from stale context or cwd alone. |
| Hook deadlines, duplicate callbacks, missed results, and one bounded Stop correction preserve outstanding effects | Report hook protocol failure; no blind retry or completion inferred from callback receipt. |
| Runtime denies direct/nested/alternate polling and ends the managed turn after terminal reporting even when hooks fail | Admission and cutover stay closed; hook-only or prompt-only compliance cannot pass. |
| Fully idle background attention has a concrete supported delivery path | Unresolved architecture blocker for unattended operation; the Warden CI wait removes CI from this dependency but does not solve other cases. |
| One registered Warden CI call returns terminal evidence without polling or intermediate model responses | Synthetic five-minute case passed; real provider, supported duration, cancellation, and restart still require evidence. |
| Native sends and eligible MCP responses share one per-recipient reservation | Reject concurrent/uncertain delivery, busy native sends, and heartbeat injection that bypasses the gate; experiment did not establish enforcement. |
| Independent MCP clients and background provider jobs progress while Marshal is ended or handling one bounded action | Report transport concurrency failure; one actor cannot block unrelated work. |
| Ordinary local-master changes reach the next MCP and hook operation within the live-iteration target, preserving existing connections and operations | Refuse live-iteration readiness; do not substitute installation, manual activation, or periodic source refresh. |
| Native IDs and assignment can be registered before worker edits | Refuse dispatch readiness. |
| Initial and updated task titles preserve the required role/emoji/bead-ID format | Admission stays closed; report normalization, persistence, or rename-tool failure. |
| Worker can use the exact Tollgate worktree | Block the affected project and show the required access change. |
| Supported background completion evidence distinguishes the exact finished turn from a newer/running/unknown turn, including restart recovery | Admission and cutover stay closed; Stop, final text, and agent polling cannot substitute. |
| Hourly same-task recovery coexists with bounded turns and respects Fulcrum pause | Report recovery scheduling failure; do not substitute standalone agents or a perpetual goal. |
| Restart reconnects or exposes a recoverable state without duplicate effects | Retain the affected action as uncertain and show its recovery step. |

Measure Beads calls and elapsed time independently from model latency. The
historical embedded-mode Weaver measurements found substantial per-command
overhead, including a small later sample of 13 subprocesses and about 11.6
seconds for a warm finish. They do not predict server-backed production latency.
The new protocol removes duplicate authoritative writes and avoids durable timer
ticks; it does not claim an unmeasured speedup.

Diagnostics report accepted transition time, source commit, Beads call count and
duration, broker notification time, action claim/result, native IDs, wake
latency, turn allowance, terminal-handoff enforcement, lease state, oldest
unhandled event, deadlines, provider observation count, model turn/tool-call
counts during external waits, and projection failures. Hook diagnostics add
event/session/turn/tool-use identity, source, latency, committed acceptance or
timeout, correction allowance, and effective
trust/configuration failures. Do not log full unrelated prompts, transcripts,
or arbitrary tool output. Logs are bounded diagnostic evidence, not replay state.
Failed work must not abort discovery or completion of unrelated work. Monitor
the age of pending actions, not merely whether the socket answers.

## Destructive cutover and ongoing maintenance

The initial cutover discards the previous instance. It is not a migration or
adoption of pending work. No previous ledger records, receipts, reservations,
task bindings, automation IDs, or provider candidates become authoritative in
the new instance. Old role/runtime functionality that is superseded by this
contract is deleted, not retained behind configuration, wrappers, or fallback
commands. Retain the required business workflow and live-iteration mechanism,
not compatibility with the old runtime or state representation.

Pass the disposable capability gates before fencing or deleting production
state. Probe hooks target only the disposable instance. Do not attach this
protocol to old-runtime tasks. A failed gate leaves the old instance untouched
and reports the missing capability; it cannot trigger a partially destructive
cutover or a weaker implementation.

After the explicit reset operation has been authorized, perform these steps
under exclusive maintenance ownership:

1. Inventory resources owned by the old Fulcrum instance: its ledger/database,
   task and provider locators, workspaces, scheduled wakes, service/connection,
   hook/MCP definitions, state caches, logs, and obsolete configuration. Confirm
   exact ownership before deleting anything; shared infrastructure and unrelated
   user data are not old Fulcrum state. Identify native resources for which
   supported deletion is unavailable before mutation begins.
2. Fence old admission and disable its future wake/recovery schedules. Allow
   already-running turns, approvals, provider effects, and source-writing
   operations to settle, or obtain explicit cancellation through supported
   controls. Never delete a workspace or ledger while an old writer can still
   mutate it. This safety boundary does not carry outcomes into the new system.
3. Retire the old Fulcrum-owned App Server connection/service and remove its
   hook/MCP entries, launch configuration, and recovery paths. Never stop or
   delete Desktop's own runtime or another instance's configuration. Ensure old
   launchers cannot recreate the deleted state or admit new work.
4. Delete the old owned workflow state and disposable resources, including
   retained receipts, task/leader bindings, schedules, caches, logs, and settled
   owned workspaces/provider resources. Use supported provider deletion where
   available. Do not import a backup, translate records, reuse standing task
   IDs, or silently leave a live old instance. Preserve source repositories and
   delivered source changes; these are not disposable workflow state.
5. Delete owned native task history through supported deletion if available.
   The inspected native task tools have no deletion operation. Archival and
   removal of Fulcrum bindings do not delete Desktop history. Any such retained
   resource requires an exact manual deletion prerequisite or an explicitly
   human-approved retention exception; do not claim a complete wipe while it remains.
   The same rule applies to provider resources with no supported deletion.
6. Remove obsolete runtime implementation, commands, configuration selectors,
   tests, and instructions from the maintained source. Bootstrap a fresh ledger,
   fresh leader tasks, fresh task bindings, and a fresh heartbeat using the new
   contract. Re-enter approved project/policy/credential references as fresh
   configuration; do not load a legacy instance as an upgrade source.
7. Pass new-instance readiness before opening admission. No automatic rollback
   restores the old state or runtime. A partial reset remains fenced and reports
   exact remaining cleanup/setup prerequisites; it resumes from verified resource
   postconditions, never from replaying old workflow records.

The reset is destructive and rerunnable from actual resource postconditions; it
has no state conversion, schema-version chain, compatibility reader, or
transport-selection escape hatch. After cleanup, the fresh ledger is the only
workflow authority. A reset receipt may record verified deletion and explicit
exceptions, but must not preserve the deleted workflow payloads as a second
state store. A maintenance fence survives partial failure outside the ledger
being removed and is cleared only after verified cleanup and new readiness.

Ordinary application, formula, and instruction edits still become available
from local master on the next operation with no installation, manual activation,
remote-publication wait, or restart. Running operations retain their pinned
source and existing connections. Hook handlers use their stable launcher;
ordinary implementation edits do not require re-trust. This destructive initial
reset is an exceptional operator operation, never an editing workflow step.

Future broker/MCP transport implementation changes require a safe connection
handoff after in-flight effects settle; reconstruct event registrations and
background timers from the new instance's ledger. Dependency changes may prepare
a separate environment. Later incompatible durable-state changes use the
explicit fenced maintenance contract in live-iteration.md without adding
backward-compatible readers or schema-version chains. None of these exceptions
can become a dependency of an ordinary policy, formula, or instruction edit.
Changes to actual MCP/hook definitions require their supported reconnect/trust
flow, scoped to the changed connection or handler.

### Implementation handoff

Implement in dependency order, with each step's checks passing before the next:

1. Capture disposable native/hook capability evidence and accepted result
   shapes. Reuse the measured blocking-CI evidence within its limits. Prove
   runtime-enforced exit, per-recipient delivery, and background completion;
   resolve fully idle background delivery before implementing dependent flows.
   Stop here on a missing required capability; record it as a feasibility blocker,
   not a future best-effort improvement.
2. Implement authoritative transitions, action attempts, hook observations, and
   deduplication and per-turn allowances in the ledger/coordination layer.
   Cover crash boundaries with local process fixtures, including lock
   inheritance and duplicate reports.
3. Add the source-following hook command, event routing, context compilation,
   and installation/trust diagnostics. Replace the current compact-only handler;
   add direct/nested polling-denial fixtures, pre-handoff worker correction
   checks, orchestrator continuation vetoes, and old-assignment/out-of-order
   cases. Integrate the independently verified runtime enforcement boundary.
4. Implement bounded CLI/MCP action handlers, background broker timers, the
   verified wake/completion interfaces, registration, and agent prompt changes.
   Add the single pending Warden CI response and real-provider tests without
   changing exact-source validation/promotion authority; replace direct runtime
   dispatch in role/completion/leadership services.
   Cover boundary pause, capacity retained during CI, release after final
   completion, pre-finish and post-finish same-task repair with their
   failed-cycle allowance, deferred archival, and the Vizier decision inbox.
5. Assemble bootstrap and run the full disposable scenarios below, including
   five-minute CI failure, enforced exit, restart recovery, and actual heartbeat
   delivery. Prepare and verify the destructive reset procedure. Under explicit
   reset authorization, remove old state and obsolete paths, bootstrap fresh,
   and update operational docs. Do not add a migration or compatibility mode.

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
3. **Idle handoff.** Marshal has ended its turn. Weaver submits ready scope,
   receives one wake action, sends, reports, and ends. Marshal handles the stored
   scope under one turn allowance and ends after its handoff. Native message
   acceptance alone cannot authorize implementation. Repeat for Vizier resolving
   existing blocked work. No actor checks the recipient after sending.
4. **Per-recipient delivery.** Race multiple senders and pending MCP responses
   against the same Marshal, Vizier, Executor, and Warden. Confirm exactly one
   unacknowledged delivery per recipient across work beads and both channels.
   Idle permits one native send; active permits only applicable information in
   an eligible MCP response. Active without such a response retains pending
   information. New scope never overlaps an active assignment. Lose replies,
   crash between gate/origin commits, and restart; no duplicate delivery occurs.
   A terminal response never injects another assignment. Uncertain sends block
   redundant recovery hints. User/native scheduler races remain a failed
   guarantee until a supported runtime guard has been demonstrated.
5. **Exit race and fully idle attention.** Commit ready attention before and
   after Marshal's terminal write and native completion. Ending/unknown status
   prohibits native delivery; all unhandled generations remain durable. Do not
   clear attention on acknowledgment alone. Separately demonstrate how held
   attention reaches a fully idle Marshal after its last sender has ended.
   There is currently no verified supported mechanism for that case; record it
   as unresolved, not a passing test or a dependency on an invented API.
6. **Polling prohibition and mandatory exit.** Attempt wait_thread/wait_threads
   with zero and nonzero timeouts, repeated read_thread/list calls, sleep loops,
   CI watch commands, alternate tools, shell/network scripts, subagents,
   write_stdin on an existing session, and direct/nested/concurrent calls.
   Repeat with hooks disabled, untrusted, timed out, and returning errors, and
   with code catching a denied nested call. Verify no forbidden effect and
   runtime-enforced exit after the final report, without further model retries.
   Reject new claims/instruction requests from the consumed turn, including
   after compaction or lease expiry. A hook-only success fails this scenario.
   Also verify that one registered Warden CI call is permitted before finish
   and cannot be used by a coordinator, after finish, for another candidate,
   concurrently, or as a short-timeout retry loop.
7. **Broker and app restarts.** Kill the broker after a Beads commit but before
   notification. Discard all its memory, restart, and rebuild pending work and
   deadlines. Restart Desktop after Marshal exits and during native creation. The
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
11. **Reports and background completion recovery.** Omit worker progress,
    publish late progress, lose a completion event, and restart its supported
    observation source. Overdue status creates one scoped recovery obligation;
    unchanged status produces no agent rechecks. Late progress clears that
    obligation. Exact completion evidence must distinguish an old finished turn
    from a newer running turn. Unknown evidence retains capacity/workspace. A
    completed turn without finish is still a missing-outcome case.
12. **Review, promotion, and cleanup.** Verify exact-source Warden approval,
    native completion, provider validation/promotion, and required
    synchronization. Hold the native task running after finish and retain an
    owned background process in separate trials. Neither trial deletes the
    worktree prematurely. Lost promotion replies reconcile provider state
    instead of resubmitting.
13. **Hourly recovery and user control.** Leave a wake obligation unsent and let
    the actual hourly heartbeat resume the same Marshal. Exercise a bounded active
    turn, a user Stop, a durable Fulcrum pause, and a paused schedule. Busy,
    ending, unknown, and reserved recipients must hold the heartbeat before
    native injection. A queued prompt or a hook rejecting it after arrival
    fails the invariant. Keep scheduling disabled if this cannot be verified.
    An idle run produces no routine status update. Stop during a protocol call
    and during agent work: it ends that turn without a hook-driven restart;
    a later authorized wake or heartbeat may resume coordination after
    reconciliation. Explicit "pause Fulcrum" instead prevents new coordination
    actions until authorized resume. An unknown interruption does not globally
    pause the instance or permit reissuing an uncertain effect.
14. **Unavailable tools and approvals.** Remove access to one required native
    tool, reject a model setting, and interrupt the app-tools MCP connection.
    Admission reports the exact dependency failure. Trigger a real
    approval/input blocker; identify the task and end pending human resolution,
    without invented answers, automatic archival, or a conflicting replacement
    worker.
15. **Workspace identity.** Present a wrong branch, wrong path, dirty Executor
    workspace, and denied write access. Registration blocks each mismatch.
    Confirm that both workers use the assigned worktree even though Desktop
    shows the saved project, and that the project's main checkout remains
    untouched.
16. **Source freshness and isolation.** Commit a policy/instruction change to
    local master while another operation runs and MCP/broker connections remain
    open. The next MCP operation and hook invocation use the new commit; the
    older operation
    retains consistent imports/assets and an issued action retains its exact
    arguments. Repeat with no Git remote, no install/build/activation command,
    unchanged MCP and hook configuration, and unchanged running connections.
    Record 20 trials of the minimal behavior/asset probe separately for CLI,
    MCP forwarding, and hook invocation; report preparation and invocation
    times and verify the local p95 target below one second with unchanged
    dependencies/state contracts. No trial waits for a provider timer or the
    hourly schedule. Commit during a pending CI call: result processing uses
    the new source while the same MCP connection remains open; no old policy
    process or state lock remains held for the wait. Break source preparation
    and verify visible failure rather than stale fallback. One failed bead must not stop another bead's handoff.
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
21. **Exit correction and interruption.** Omit pre-handoff worker finish,
    omit a post-handoff action result, block on human input, pause Fulcrum, and
    end Marshal in separate trials. Only the pre-handoff worker obligation may
    receive one correction. Orchestrators and completed handoffs never receive
    Stop continuation, even when another hook requests it. Compaction, a new
    turn, duplicates, or lost responses cannot replenish the correction budget.
    Stop cannot establish completion or release capacity. Interrupt during a
    native effect and with Beads unavailable; handlers meet their deadline,
    never restart the turn, and preserve effect recovery.
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
24. **CI capacity and later release.** Fill all slots with Wardens waiting for
    CI. Each pending call retains its assignment and slot; independent admission
    cannot use them. Terminal CI failure returns to the same Warden and permitted
    repair needs neither a new assignment nor a native message. After passing
    CI (or a blocked outcome), accepted finish and exact native completion allow
    release. Promotion, cleanup, or archival may still be pending without
    retaining the slot. Delay completion or leave a continuation uncertain and
    verify release is withheld. A post-finish repair with all slots occupied
    waits for capacity and cannot revive the old assignment. Restart around
    release without losing or double-counting reservations.
25. **Deferred archival and same-task repair.** Executor ends while Warden
    reviews and waits for CI. Both tasks stay visible. Fail CI and repair in the
    existing Warden assignment, then pass CI and finish. A later delivery failure
    requires fresh capacity, a fresh assignment, positive idle evidence, and
    one gated continuation to that same Warden task. No replacement creation is
    issued; the old finish remains sealed and cannot authorize edits. Lose the
    continuation response and retain uncertainty without another message.
    Exercise return to the same Executor followed by the same Warden. A missing
    native task produces an operator blocker. Only settled terminal work permits
    archival; failed cleanup, reports, follow-ups, blockers, new turns, and
    owned-process uncertainty prevent it. Pause before archive issuance and
    resume afterward. Restart with a closed bead's archive pending and
    rediscover it. Lose an archive reply, then manually unarchive an observed
    archived task: no duplicate archive or automatic rearchive occurs. Leaders
    remain visible; retained links still identify archived workers.
26. **Repair-cycle limit.** Fail the original delivery, then complete three
    unsuccessful repair cycles in the retained tasks. Only the three repairs
    count. Exercise both pre-finish MCP repair grants and post-finish native
    continuations; neither can reset the count. Repeated failure events, CI
    waiting, transport errors, local test iterations, and Executor/Warden handoffs cannot increment the total again.
    The third failure commits the scoped human hold without a fourth repair
    action; unrelated work proceeds and idle workers release capacity. Restart
    and run hourly recovery: the count/hold survive without repeated alerts.
    Generic resume cannot clear it. Resolve the exact blocker with a human
    allowance for one more cycle, retry that resolution, and verify precisely
    one additional cycle is authorized with the full prior history retained.
27. **Human decision inbox.** Commit a repair-limit blocker and a native approval
    blocker on separate beads. Send each retained request to the same existing
    Vizier, preserving worker links and the original native approval location.
    Notification receipt cannot resolve either blocker or authorize repair.
    Restart, replay events, and run hourly recovery without duplicate notices.
    Lose a send result and reconcile it without resending. Change the required
    decision, and verify one new revision; resolve a blocker before an old
    notice arrives, and verify no stale request reopens it. Deliver a notice
    while Vizier handles a human response: retain it for an eligible MCP
    response or later idle delivery, never another native message. Preserve
    both receipts and the human response. Pause before issuance: the notice
    remains pending and readable,
    and resume revalidates it. An unavailable Vizier remains a visible delivery
    problem without replacement task creation; unrelated work continues.
28. **Five-minute blocking CI failure.** The synthetic Desktop/MCP case passed
    on 2026-09-16; see the linked experiment report. Repeat with real Tollgate
    CI: Warden submits before finish and makes one pending MCP call; Marshal
    ends. CI fails after five minutes. Return exact-source failure directly
    into the same Warden turn and assignment, with no native message, Marshal
    wake, model status query, wrapper-resume loop, or interim model response.
    Record whole-task usage separately from wait-interval usage. Repair under
    the cycle allowance, resubmit changed source, and wait once for that new
    candidate. Pass CI, finish, end, and release only after native completion.
    Duplicate failure observations cannot produce multiple responses or repair
    grants. Exercise pause, provider errors, supported duration boundaries,
    cancellation, client/broker/app restart, and concurrent incoming information.
    Deadline expiry returns a blocker, never a model-driven `pending` retry loop.
    Lost connections retain candidate/assignment uncertainty without resubmission
    or conflicting delivery. These additional cases remain untested.
29. **Destructive reset.** On a disposable legacy instance, inventory owned state
    and resource IDs, including old work/receipts, leaders, schedules, hooks,
    service paths, caches, logs, and workspaces. Fail each capability gate before
    reset and verify nothing is deleted. After authorization and settled writers,
    interrupt each reset boundary; the maintenance fence survives and rerun uses
    actual postconditions. Verify old state is deleted, no pending work or native
    IDs are adopted, no obsolete launcher can revive it, and fresh setup creates
    only new bindings/resources. Preserve unrelated infrastructure, source, and
    another instance's hooks. Missing native/provider deletion must remain an
    explicit manual prerequisite or recorded authorized exception; archival
    cannot pass a deletion assertion. No compatibility path or rollback restores
    the legacy instance. New-instance bootstrap reruns retain only that new
    instance's IDs and never reinterpret them as a migration.
