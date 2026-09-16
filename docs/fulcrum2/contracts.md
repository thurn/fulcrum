# Fulcrum contracts

This is the normative implementation contract for the replacement described in
[design.md](design.md). Commands shown as `fulcrum ...` are installed product
interfaces. Examples using `bd` describe stock Beads capabilities used by the product.
No CLI, record, or adapter compatibility/version negotiation is introduced.

## 1. Instance and command conventions

An instance is selected by `--instance ABSOLUTE_PATH`, then `FULCRUM_INSTANCE`,
then `~/Library/Application Support/Fulcrum`. All commands accept the option;
tests always pass it. One instance owns one ledger and one controller writer lock.
An isolated test setup must supply a disposable `brain.root` and a disposable
Git/Dolt remote; it must never inherit `~/brain` or its production remote.
Production Codex attachment is shared with Desktop; test instances explicitly
choose a deterministic adapter or a disposable native namespace.

```text
<brain.root>/fulcrum.yaml      authoritative configuration; human/Vizier edits only
<instance>/config              discovery symlink to that YAML, never a second copy
<brain.root>/.beads/           shared stock Beads workspace (default ~/brain/.beads)
<brain.root>/.beads/dolt/       live server data, excluded from ordinary Git tracking
<brain.root>/plans/            default Markdown plan publication destination
<instance>/controller.sock     ephemeral IPC endpoint
<instance>/controller.lock     discovery link to the brain-root advisory lock
<instance>/threads/            native-task creation locator directories
<instance>/worktrees/          managed worktrees, separated by project
<instance>/logs/               rotated diagnostics, never replayed as state
<instance>/runtime/            installed controller environment and role assets
<instance>/recovery/           independently installed emergency launcher
```

Configuration holds executable paths, provider endpoints, project paths/provider
IDs, model defaults, task-slot limits, standing policy, and operational timing.
It lives only in `<brain.root>/fulcrum.yaml`; actual dispatch decisions and work
state live in Beads. Only the human and Vizier may modify the YAML. Credentials are referenced through environment or the
provider's native authentication, not copied into beads.

The worktree directory above is the default for providers that accept an explicit
destination. Tollgate's current CLI chooses a repository-local `.worktrees` path;
record and use its returned absolute path instead. A worktree is owned because of
its retained provider/Beads identity, not merely because it is below the instance
directory. Reset and cleanup enumerate these provider-owned paths too.

### Common CLI options

| Option | Contract |
| --- | --- |
| `--config ABSOLUTE_PATH` | Select the authoritative `fulcrum.yaml`; resolves before the instance discovery link and default `~/brain/fulcrum.yaml` |
| `--json` | Emit one JSON result on stdout; human-readable text is the default |
| `--input PATH` / `--input -` | Read a UTF-8 JSON object from a file/stdin; supported mutation payloads are listed below |
| `--project ID` | Explicit project selection; required if it cannot be resolved without ambiguity |
| `--thread-id ID` | Explicit acting/native task; no requirement to run inside that task |
| `--actor human` or `--actor task:ID` | Audit initiator; ordinary shell defaults to human, a managed environment defaults to its task |
| `--model NAME` / `--effort LEVEL` | Explicit model choice for role entry or task start; validate against runtime capabilities |
| `--ownership-operation ID` | Receipt establishing current ownership for owner-restricted mutations |
| `--request-id UUID` | Identity of a logical mutation; reuse the same ID and payload for an exact retry |
| `--wait` | Wait for this command's operation to settle, not for an entire bead to finish |
| `--timeout SECONDS` | Maximum client wait; default 30; never means automatic cancellation |
| `--offline` | Execute locally after obtaining the same writer lock; fail clearly if the controller still holds it |

Flags may follow the subcommand. `--input` and explicit payload fields may coexist
only when they do not specify the same field. Duplicate specification is an input
error. Reject unknown fields with their names. Strings remain literal argv/JSON
values; never interpolate descriptions into a shell command.

Environment task discovery checks `CODEX_THREAD_ID` only. Explicit `--thread-id`
wins. Do not preserve the old aliases or caller-lineage prerequisites. In a managed
task, owner-restricted mutations carry the `--ownership-operation` supplied in
the full turn prompt. Do not silently substitute a newer acquisition from the
current task record: delayed commands from the same task must remain stale.
Human operator actions use their documented override/transfer operations; a
terminal simulating a worker passes both task ID and ownership operation.

This is a trusted local-user system, not an adversarial multi-tenant permission
boundary. Explicit human/operator commands are allowed from the terminal. Role
checks and ownership-operation references prevent accidental stale writes; they do not pretend to
stop a local user with direct Git or Beads access from changing state. Automatic
agents must not relabel themselves `human` to evade normal policy.

### Results and exit codes

Every JSON response has this shape; omitted optional result values are `null`.

```json
{
  "ok": true,
  "state": "completed",
  "operation_id": "fc-11111111111141118111111111111111",
  "request_id": "11111111-1111-4111-8111-111111111111",
  "result": {"bead_id": "fc-51o"},
  "warnings": [],
  "error": null
}
```

`state` is `accepted`, `running`, `completed`, `failed`, `uncertain`, `cancelled`,
or `degraded`. Read-only queries return `completed` and null operation/request
IDs. A completed setup can carry warnings about unavailable optional facilities;
that is not a failed reset or failed intake. `ok` means the requested command was
accepted/executed, not that downstream work has shipped. A wait expiry uses
`ok=false`, preserves the operation's actual state, and returns `WAIT_TIMEOUT`.

IPC/input requests are capped at 4 MiB; oversize payloads return `INPUT_TOO_LARGE`
(exit 2), never clipped task requirements.

Errors contain `code`, `message`, `retryable`, `next_command`, and optional
`details`. `next_command` is a structured argv array, never executable prose. JSON
parsing/validation errors follow this envelope too. Diagnostics go to stderr.

`work show` and work-list entries return a `WorkView`: `id`, `title`, `status`,
`priority`, `description`, `acceptance_criteria`, `dependencies`, and `fc` (the
native `metadata.fc` object). They also return `effective_owner` for unadmitted
intake. This explains the `.result.fc.owner` selectors in the examples; the CLI
normalizes native Beads JSON rather than exposing inconsistent adapter nesting.
An unadmitted issue has null `fc`, an explicit effective owner, and phase intake
in its computed status view.

| Exit | Meaning |
| --- | --- |
| 0 | Successful query, accepted mutation, or completed operation |
| 2 | Invalid command/payload or definitive operation rejection/failure |
| 3 | Client wait timed out; inspect the returned operation |
| 4 | Uncertain side effect or unavailable dependency |
| 5 | Ownership, stale decision, or request-ID conflict |
| 6 | Degraded role entry/repair; useful instructions/evidence returned, requested registration not completed |

Cancellation after an external commit does not erase that commit. Its response
states the actual observed outcome. A repeated command with the same request ID
and semantically equal parsed input returns the existing receipt. Compare stored
values directly; do not hash input. Changed input under the same ID returns
`REQUEST_CONFLICT`. Retry an intentional new action with a new UUID.

Role `enter` and `finish` return compact `result` objects: relevant IDs, the
achieved step, role-specific facts, instructions once (entry), `next_action`, error,
and `next_commands` for explicit `operation show ID --json`. The outer envelope
retains operation/request IDs, state, warnings, and error. Full receipts retain
accepted input, planned work, recovery checkpoints, and timestamps; they are not
repeated in normal command output. Receipt results describe the achieved state at
that operation, not a live status query; use `context`/`work show` for current state.

Weaver `ready` retains scope and observable acceptance for Marshal's decision
brief and stale-decision comparison. It proposes Executor review/dispatch but
never authorizes it. Reconciliation discovers unapproved backlog, coalesces
attention, and starts a Marshal review turn when leadership is available and idle.
Successful finish reports discovery eligibility, not notification. Failed review
requests remain eligible for bounded retries; uncertain requests require inspection.
Missing leadership explicitly leaves HUMAN responsible. Only a subsequent Marshal
decision records dispatch authorization; capacity/dependencies still gate launch.
Long prepared scope is excerpted with an explicit `complete: false` and `work show`
continuation; Marshal must inspect that full scope before deciding.

When the caller omits a request ID, generate it before contacting the controller
and print it to stderr so a lost response is still retryable. Structured callers
should supply it themselves. Normal stdout stays one parseable JSON document.

## 2. Complete command surface

All workflow mutation commands use operation receipts and common options unless
the bootstrap/reset/degraded-repair exception is expressly documented. The hook is
read-only and uses its native response format. All listing commands
accept `--limit N` (default 20, `0` means all) and `--cursor CURSOR` and return
`items` plus `next_cursor`. File/log data is paginated by count or byte limit.

### Installation, projects, and service

“Installation” is retained below only as a legacy command-family label. Fulcrum
itself is never installed: `~/fulcrum` on master is the sole behavior and skill
source, and no command may create or select an independently authoritative copy.
Setup may provision dependencies and service supervision, but an application,
role, or skill change never requires setup, install, build, copy, deployment,
activation, or restart. The
[master-only source invariant](../architecture/live-iteration.md#master-only-source-invariant)
overrides any historical wording elsewhere in this contract or its plans.

| Command | Purpose and required input |
| --- | --- |
| `setup --input FILE` | Install/bootstrap from the configuration object below; no UI wizard required |
| `config show` | Read effective `fulcrum.yaml` and its source path; credentials omitted |
| `config validate` | Read-only schema/capability checks with actionable field errors |
| `config sync` | Commit/push the existing authorized YAML contents to the brain branch; never edits the file |
| `config set --input FILE` | Human/Vizier-only patch to YAML; validate, atomically replace, and report changes/restart requirements |
| `project add --input FILE` | Human/Vizier-only YAML enrollment: register `id`, `root`, `codex_project_id`, `delivery`, `integration_branch`, `prepare_argv`, `validate_argv`, optional `source_remote`, `require_source_sync`, `models` |
| `project list` / `project show ID` | Inspect enrollment and observed provider availability |
| `project disable ID --reason TEXT` | Human/Vizier-only configuration change to stop automatic new work; retain active ownership and allow recovery/direct human work |
| `project enable ID` | Human/Vizier-only configuration change to enable automatic new work |
| `project remove ID` | Human/Vizier-only YAML enrollment removal after open work/worktrees are disposed; Justiciar may repair that work but cannot edit this file |
| `service start` / `service stop` / `service restart` | Supervise the installed controller; stop observes drain/interruption according to explicit `--interrupt` |
| `service status` | Service and loop health without requiring the controller socket |
| `serve [--once]` | Run foreground controller; once processes one eligible reconciliation pass and returns |
| `reconcile [--bead ID] [--operation ID]` | Inspect and settle recorded state through the normal application path |
| `reset --hard --yes [--input FILE]` | Reset the enumerated Fulcrum-owned instance and bootstrap clean state |

Setup input has `runtime` (`kind`, `endpoint`, `executable`), `delivery` defaults,
`beads` (`executable`, `host`, `port`, `database`), `brain` (`root`, `remote`,
`branch`, `push_interval_seconds`), `projects`, `models`, `policy`, and optional `knowledge`
(`root`, `remote`, `branch`, `require_remote_sync`) maps. Production `brain.root`
defaults to `~/brain`; `remote` defaults to its existing `origin`, `branch` to its
configured branch, and `push_interval_seconds` to 300. Setup verifies that remote
is the intended GitHub repository. Knowledge defaults to the brain root/remote/
branch with required synchronization. A different knowledge destination never
changes where the shared Beads ledger lives. Model maps key role to `{model, effort}`. Projects
default `source_remote=null`, `require_source_sync=false`; enrollment of a project
with a source remote asks or requires an explicit synchronization choice. A true
requirement needs a configured remote; it is never inferred from a successful CI
result. `source_watch_root` optionally names the retained Fulcrum checkout; setup
defaults it to the installation source. Changes enqueue a quiescent refresh;
`null` disables the watcher. This is operational maintenance of user-edited source,
not scheduled agent work.
Production defaults use a dedicated loopback Dolt server on port 3309, database
`fulcrum`, and the configured shared Codex endpoint (default port 4500). Bind only
to loopback. An occupied port belonging to another installation is a configuration
error, not permission to kill that process. `kind` must be `codex` for runtime
and `tollgate` for delivery.

Project preparation and validation are argument arrays, never arbitrary shell
fragments generated from a bead. For a shell-based repository check, configure
`["/bin/sh", "path/to/checked-in-script"]` explicitly. Empty arrays mean no
project-specific command. Setup writes absolute executable paths for services.

### Role entry and work

| Command | Behavior |
| --- | --- |
| `enter ROLE --description TEXT [--bead ID] [--origin human\|dispatch]` | Create/adopt work and bind the current explicit task, or create a native task when none is supplied; default origin human |
| `leader show vizier\|marshal` | Native ID, fixed title, and control-bead reference |
| `leader replace ROLE --reason TEXT` | Explicitly replace broken leadership, invalidate old leadership ownership references, retain policy in `fulcrum.yaml` |
| `context [--bead ID] [--role ROLE]` | Current work context; `--role marshal` derives standing leadership’s current decisions/rationale; unavailable context returns truthful fallback guidance |
| `work create --input FILE` | Create an outcome or graph; returns root and child IDs |
| `work show ID` / `work list` | Work-only view, excluding control/operation issues; filters `--project`, `--role`, `--owner`, `--phase` |
| `work adopt ID --role ROLE` | Explicit adoption; existing owner must consent/stop or caller must use takeover authority |
| `work update ID --input FILE` | Update outcome, acceptance, summary, or annotations; active substantive scope changes are made visible to the owner |
| `work transfer ID --to-thread ID --role ROLE --reason TEXT` | Controlled ownership transfer; refuse to run a second writer while the old turn is active |
| `work children ID` | Deliverable children and dependency progress |
| `work close ID --outcome OUTCOME --summary TEXT` | Terminal disposition; ordinary implementation requires observed delivery, while answered/rejected/duplicate work does not |
| `work reopen ID --reason TEXT [--role ROLE]` | Explicit new ownership cycle on the same ID; invalidate old ownership references and return to Marshal unless human entry starts the specified role |
| `progress --bead ID --input FILE` | Record a substantive checkpoint; no heartbeat-only event |
| `finish --bead ID --outcome OUTCOME --input FILE` | Role-specific finish described below |
| `report --input FILE` | File an incidental follow-up without changing the reporting task's role |

`enter` roles are exactly vizier, marshal, weaver, executor, warden, sage, mason,
and justiciar. Task titles follow the exact table in
[design.md](design.md#persistent-bead-identity-and-visible-task-names); format them
as `<emoji>[<role-code>-<full-bead-suffix>] <bead-title>`, with the two fixed
leadership exceptions. Existing leadership receives only explicit human entry requests;
`enter vizier --origin dispatch` is rejected without starting a turn. Every explicit human role entry bypasses ordinary admission; the existing
leader receives leadership requests in its own task. If dependencies
are physically unavailable, start whatever useful work the current task can do
and return truthful degraded instructions, rather than pretending work started.

A terminal worker call without `--thread-id` creates or safely reuses its native
task; leadership entry instead routes to the standing leader. A call from an
already-active human task binds that task and returns instructions; it does not
send a redundant turn into itself. A role change owns the same bead unless a
different bead is explicitly provided. Entry returns `bead_id`, `thread_id`,
`role`, `ownership_operation`, `instructions`, and any outstanding operation ID.

One non-leadership task actively executes one work bead at a time. Entry into a
different bead checkpoints its prior unfinished work and returns that work to
Marshal before adopting the new one. Preserve the actual source/worktree and
stop conflicting managed tasks before new edits. Leadership may steward many
backlog beads while handling one request turn. This does not create a worker pool
that reuses an unrelated task's conversation history automatically.

Scoped Sage/Mason entry on a closed bead is an explicit investigation on the same
ID. Save the prior terminal disposition in `interrupted_work`, assign a fresh
ownership operation, and open only the investigation. On finish, attach the findings receipt
and restore the prior terminal disposition unless the human explicitly requested
new implementation. Do not change “delivered” into “findings” or redispatch code
that already shipped. Entry on unfinished Warden work returns it to Warden review
through Marshal afterward, not to a new Executor implementation.

`work create` accepts `title`, `outcome`, optional `project`, `acceptance` (array of
strings), `requested_role` (default weaver for incomplete requests, executor when
explicitly supplied), `priority` (0–4, default 2), `context` (array of references),
optional `intake` (`benefit`, `uncertainties`), optional `models` keyed by role to
`{model, effort}`, and optional `children`. A graph child has a caller-local `key` and the same task
fields; `depends_on` contains child keys or existing bead IDs. Persist the planned
ID mapping on the creation receipt before writing children. Reject cycles before
mutation; a crash midway resumes missing children and dependencies under the same
receipt. Parent/children become dispatchable only after graph completion.

`work update` accepts any subset of `title`, `outcome`, `acceptance`, `summary`,
`size` (`small|medium|large|unknown`), `overlap_tags`, `context`, `intake`, and `models`. A scope change
invalidates approval for the previous source/scope combination; it does not modify
an already completed delivery. `progress` accepts `kind` (`investigation|source|
validation|blocker`), `summary`, and `evidence` references.

`report` accepts `title`, `problem`, `observed_evidence`, `required_change`,
`acceptance_checks`, optional `project`, `discovered_from`, `context`, and `intake`. The
request ID is the report retry identity. It returns the actual bead and filing
state independently of subsequent dispatch.

### Marshal, policy, and human intervention

| Command | Behavior |
| --- | --- |
| `policy show` | Read the policy section of `fulcrum.yaml` and its rationale |
| `policy set --input FILE` | Human/Vizier-only edit of the YAML policy section through the same config operation |
| `backlog list [--ready] [--include-deferred]` | Compact work summaries, dependency/wait reasons, and priority |
| `marshal brief [--bead ID] [--kind auto\|groom\|dispatch\|recover]` | Read-only decision preview with one purpose, omitted counts, and continuation commands |
| `marshal decide --input FILE` | Apply independent decisions after checking their ownership/snapshot expectations |
| `dispatch --bead ID` | Execute an already-authorized dispatch; `--authorize` records operator authorization under ordinary limits, `--human` authorizes immediate operator bypass |
| `human list` | Work assigned HUMAN and the exact action needed |
| `human resolve ID --input FILE` | Record `answer`, optional `scope_change`, and `resume_role`; return ownership to Marshal for continuation |

YAML policy fields are `automatic_capacity`, `default_project_capacity`,
`project_capacity` (override map),
`paused_projects`,
`suspended_rules` (map from named rule to scope/reason), and `rationale`.
Model, policy, and timing/log defaults all live in the same YAML file, not Beads
or a second config copy. Marshal can use fewer slots without changing these values.
Every suspension has an explicit scope and can be cleared through `policy set`.
No automatic expiry schedules a new task. Vizier/human alone may grant broad rule
suspensions; Justiciar's current takeover grants its already-defined local powers
but never authority to modify `fulcrum.yaml`.

A decision references the retained `decision_operation` described in §10 and contains `bead_id`, `expected_ownership_operation`, `expected_phase`, `action`,
`reason`, and action-specific fields. The payload is `{"decisions":[...]}`.
Actions: `dispatch` (`role`), `defer` (`reconsider_when`), `clarify` (`question`),
`duplicate` (`canonical_bead`), `reject`, `recover` (`scope`, `diagnosis`), or
`human` (`question`, `required_action`). Return per-decision applied/conflict
results. A mixed result completes the command with explicit conflicts; do not
roll back independent accepted decisions or secretly retry stale judgment.

### Native task control, handoffs, and delivery

| Command | Behavior |
| --- | --- |
| `task list` / `task show ID` | Retained and observed native state; no implicit resume |
| `task start --role ROLE --bead ID` | Create/start a task for an admitted assignment; same service used by dispatch |
| `task send ID --input FILE` | Send `text` with request correlation; ordinary messages require owner/Marshal/human authority |
| `task interrupt ID [--turn-id ID]` | Interrupt and observe the identified active turn; don't infer termination from acknowledgment |
| `task requests ID` | Pending native approvals/input requests, if any |
| `task respond ID --request REQUEST_ID --input FILE` | Return the provider's typed approval/input response from the terminal |
| `task archive ID` / `task unarchive ID` | Explicit UI lifecycle operation; unarchive suppresses automatic rearchive |
| `task release ID` | Release Fulcrum subscription and completed owned terminals; never archive as a side effect |
| `task delete ID --yes` | Explicit managed-task deletion; requires no active work or takeover scope |
| `worktree prepare --bead ID` / `worktree inspect --bead ID` | Prepare/inspect the bead's isolated source environment |
| `worktree cleanup --bead ID` | Remove settled managed worktree/branch through delivery adapter |
| `validation start --bead ID --source OID` | Submit exact committed source for provider validation without promotion authority |
| `validation show --bead ID` | Current source, provider handle, checks, and evidence |
| `review approve --bead ID --source OID --summary TEXT` | Warden approval of current source; no implicit source change |
| `promotion start --bead ID --source OID` | Authorize delivery of approved exact source |
| `source sync --bead ID` | Retry/inspect configured source publication after promotion without promoting again |
| `promotion show --bead ID` | Actual promotion, synchronization, and cleanup facts |

`task send` cannot send system messages to Vizier; an explicit human request is
the exception. Pending native input becomes a visible blocker, with one Marshal
request if human help is needed. Preserve native request IDs and methods; do not
invent answers or blanket-reject every server request. If reconnect loses an
in-memory native request, inspect/recover the task and report that gap.

`finish` outcomes and payloads:

| Role / outcome | Input and resulting responsibility |
| --- | --- |
| Weaver `answered` | `summary`, `evidence`; closes question bead |
| Weaver `planned` | `summary`, `plan_id`; retain published future plan and its explicit deferral without dispatch |
| Weaver `ready` | `summary`, nonempty `acceptance` array, optional `evidence`; implementation-ready scope awaiting Marshal review, without authorization or dispatch |
| Executor `ready_for_review` | `summary`, `source_oid`, `checks`, `evidence`; recorded handoff to Warden |
| Warden `approved` | `summary`, `source_oid`, `checks`, `evidence`; approve and promote current source |
| Sage/Mason `findings` | `summary`, `findings` (report objects); file findings and resume interrupted work via Marshal, or close standalone investigation |
| Justiciar `repaired` | `summary`, `changes`, `waived_requirements`, `known_defects`, `source_oid` when applicable, `evidence`; reconcile actual results and finish/reassign residual work |
| Any worker `blocked` | `summary`, `blocker`, `attempts`, `required_action`; transfer accountability to Marshal for a decision |
| Vizier/Marshal `completed` | `summary`; close the explicit request bead; standing leadership control record persists |

Checks are objects `{name, status, evidence}`, with status `passed|failed|not_run`
and an explanation for `not_run`. A check is evidence, not a new required ceremony.
The application verifies referenced source and provider facts; it does not trust
an assertion of promotion without observing Git/provider results.

An accepted Executor finish seals its input and stops further Executor work. It
returns `accepted` while a destination is being prepared; the owner transfer waits
for native terminal evidence. The old ownership reference cannot submit another different
finish. Failed reporting after this acceptance does not reopen implementation.

### Operations, diagnostics, and emergency repair

| Command | Behavior |
| --- | --- |
| `operation show ID` / `operation list` | Receipt, attempts, source request, next action, and evidence |
| `operation wait ID` | Wait for completed/failed/cancelled/uncertain; finite timeout |
| `operation cancel ID --reason TEXT` | Cancel if supported, then inspect actual postcondition |
| `operation reconcile ID` | Inspect external truth and settle/resume the same receipt |
| `wait --bead ID --until PHASE` | Wait for a bead phase or `closed`; finite timeout; no polling agent |
| `status [--project ID] [--bead ID]` | Ownership and forward-progress view |
| `doctor` | Component/loop health and concrete unavailable capabilities |
| `logs [--bead ID] [--operation ID] [--since TIME]` | Bounded event/evidence retrieval; `--follow` is an explicit client stream |
| `trace --bead ID` | Ordered lifecycle and operation links, with observed gaps |
| `logs prune` | Apply configured diagnostic retention |
| `recover inspect --scope SCOPE` | Best-effort read-only evidence even with controller/ledger unavailable |
| `recover takeover --scope SCOPE --reason TEXT` | Establish Justiciar takeover and stop competing managed work |
| `recover repair --scope SCOPE --input FILE` | Execute an explicit typed repair through the same adapters, offline when needed |
| `recover release --scope SCOPE --summary TEXT` | Reconcile facts, invalidate takeover ownership reference, and restore ordinary ownership/leadership |

Scopes are `bead:ID`, `beads:ID,ID,...`, `project:ID`, or `instance`. Repair actions are an ordered
array of `{action, target, arguments, reason}`. Supported actions: `interrupt`,
`release_subscription`, `terminate_owned_terminal`, `adopt_owner`,
`replace_thread`, `cancel_delivery`, `reconcile_delivery`, `remove_worktree`,
`set_disposition`, `restore_leadership`, `repair_service`, `reinstall`,
`quarantine`, `beads_update`, and `git`. Destructive actions require takeover
scope or explicit human actor. `repair_service` targets only an enumerated owned
service. `reinstall` targets the main or recovery installation with an explicit
source root. `quarantine` retains an exact owned artifact and reports its path;
it does not delete the only copy. `beads_update` uses stock `bd` against exact
IDs with explicit fields; `git` takes literal argv and a recorded repository or
worktree target inside the assigned scope. These are operator/Justiciar repair
capabilities, never normal dispatch shortcuts. Arguments and before/after facts
are retained on the receipt; no shell interpolation or private database writes.
`set_disposition` accepts new scope, waived checks,
remaining defects, and actual outcome; it cannot falsely claim a Git promotion.
Unknown actions are rejected. Justiciar may use direct Git/`bd` tools when these
repairs are insufficient, then use reconcile/release to record actual facts.

When Beads is unavailable, `recover inspect` and runtime resource-release actions
can still run against explicit configured targets. Return `degraded`, the observed
effects, and a clear statement that no durable receipt was recorded. Do not write
an emergency work queue to disk. Do not require a new model task for these repairs.

## 3. Beads representation

### Record classes

| Class | Native representation | Owner |
| --- | --- | --- |
| Work | Normal task/bug/feature/epic, `fc:work` label after adoption | Current responsible task or HUMAN |
| Installation | Reserved `fc-system`, `fc:control` label | Current Marshal, or HUMAN during bootstrap |
| Task record | Chore with `fc:task` label and `external_ref=fulcrum:thread:<threadId>` | Its native task, retained after closure |
| Memory | Chore with `fc:memory`; curated text scoped globally, by project, or by role | Vizier globally, Marshal for project/role maintenance |
| Analytics | Chore with `fc:analytics`; native turn facts, rate cards, or frozen totals | Marshal |
| Operation receipt | Chore with `fc:operation` label | Work owner, Marshal for mechanical work, or HUMAN for operator operations |

Control/task/operation/memory/analytics records have owners but are not themselves demands for
perpetual model activity. Their lifecycle tracks a resource or operation. Open
**work** has the forward-progress requirement. Closing a record retains its last
owner; terminal receipts are not picked up by `backlog list`.

Operation IDs are `fc-` plus the request UUID without hyphens. This is an identity,
not a content hash. Other new records use a preselected random `fc-` ID, eight
hexadecimal characters by default; choose another if the ledger already contains
it. Persist planned child/resource IDs in the initiating receipt before creating
them. Existing native `bd` IDs of any valid suffix length are accepted unchanged.
`fc-system` is the only reserved human-readable ID.

Normal operational transitions update assignee, native status, and the entire
`fc` object in one `bd update` call. `--metadata` merges top-level keys in the
inspected stock implementation; replacing its `fc` key preserves unrelated
metadata. Omitted nested `fc` values do not survive automatically: send the full
current object. Labels/dependency edits are separate steps reconciled by receipt,
not assumed atomic with metadata. Do not use `bd batch` for arbitrary metadata:
its supported update fields are only status, priority, title, and assignee.

### Work metadata

```json
{
  "fc": {
    "kind": "work",
    "project": "fulcrum",
    "owner": "01a-example-executor",
    "role": "executor",
    "ownership_operation": "fc-22222222222242228222222222222222",
    "phase": "working",
    "requested_role": "executor",
    "origin": {"thread_id": "01a-example-weaver", "request_id": null, "bead_id": null},
    "workflow_root": "fc-51o",
    "caused_by": null,
    "models": {},
    "plan": null,
    "completion_cost": null,
    "summary": "Return correctly ordered values without quadratic behavior.",
    "outcome": "Optimize sorting while preserving documented ordering semantics.",
    "acceptance": ["Ordering tests pass", "Representative large input avoids quadratic growth"],
    "context": [],
    "intake": {"benefit": "Keep large sorting requests responsive.", "uncertainties": []},
    "size": "small",
    "overlap_tags": ["sorting"],
    "next_action": "Implement and run the relevant sorting checks.",
    "last_progress_at": "2026-09-14T06:00:00Z",
    "last_progress": "Located the quadratic insertion loop.",
    "waiting": null,
    "dispatch": {"authorized": true, "origin": "human", "role": "executor"},
    "worktree": {"path": "/tmp/example/worktrees/fulcrum/fc-51o", "branch": "codex/fc-51o"},
    "delivery": null,
    "handoff": null,
    "interrupted_work": null,
    "active_operation": null,
    "last_transition": "fc-11111111111141118111111111111111",
    "disposition": null
  }
}
```

Native description is the compiled role context; native acceptance is the readable
rendering of `fc.acceptance`. Native priority and dependencies are authoritative,
not duplicated inside `fc`. `fc.outcome/acceptance` are the canonical authoring
inputs to compilation once adopted. A raw edit of native description/acceptance
is imported as a scope-change request; record it before recompiling so human edits
are not silently overwritten. Never let old compiled role instructions become the
new user outcome. `work update` is the direct, unambiguous scope-edit path.

Optional values use null rather than absent state inferred from old source. Times
are UTC. `waiting` is null or `{reasons: [...]}`. Each reason has `id`, `kind`,
`reason`, `depends_on`, `reconsider_when`, and `since`. `kind` is `dependency`,
`capacity`, `policy`, `publication`, `scope`, or `human`. `reconsider_when` is an
array of typed triggers `{event, subject}` where event is `dependency_closed`,
`capacity_available`, `priority_changed`, `policy_changed`, `publication_settled`,
`scope_updated`, or
`human_resolved`. Omitted subjects mean the current bead/project. No calendar
trigger is supported. Resolving one reason leaves the others intact. `dispatch`
is a durable Marshal/human decision, not an independent scheduling table. `handoff` holds destination role/task, originating
ownership operation, current source/evidence, and receipt. `interrupted_work` stores the original
implementation/review role, phase, outcome, and source/delivery facts while
Sage/Mason investigates. Further introspection switches retain that original work;
do not build an unbounded role-return stack.

`delivery` contains `{source_oid, provider_handle, validation, approved_source,
promotion, synchronization, cleanup, evidence}`. Git OIDs are native source
identifiers, not newly invented hashes. Approved source changes only through an
explicit Warden/Justiciar action. Historical obsolete findings remain receipt
evidence and are not copied into `next_action`.

### Ownership and native writes

Effective owner is admitted `fc.owner`, otherwise the current Marshal from
`fc-system`, otherwise HUMAN while the installation lacks leadership. A raw
unassigned or role-assigned issue is therefore Marshal-stewarded intake. It is
not an executable role-owned assignment until admission completes.

1. Read the work bead under the Fulcrum writer. For raw intake, derive requested
   role from assignee, project from the resolution rules, and source outcome from
   the original issue fields. Preserve the original issue text as evidence.
2. Set native assignee and `fc.owner` to the accountable existing task (normally
   Marshal), `phase=backlog`, and a new ownership operation in one regular `bd update`. Set the
   current transition ID in that update and read back the postcondition.
3. When dispatch is authorized, create/identify the destination task using a
   recorded operation. Preserve old ownership while provisioning.
4. If an old worker exists, stop and observe its active managed turns and tools before
   transferring write responsibility. Validate any in-flight delivery first.
5. Atomically update assignee, `fc.owner`, role, ownership operation, phase, and handoff context.
   Read back. Then start the destination turn with that exact ownership operation.

Stock `bd update --claim` has atomic claim semantics, but it does not atomically
transfer arbitrary operational metadata and is separate from additional update
fields. Do not build the protocol on an assumed combined claim-and-metadata CAS.
An outside agent that claims a queued bead is detected through native assignee
change and normalized through the same adoption path. Its bead instructions say
to run the short entry command before editing shared source; no prior Fulcrum
knowledge is needed.

If native assignee differs from admitted owner, retain the old owner until the
requested transfer is reconciled. Pause new side effects on that bead, observe
both tasks, and complete a controlled transfer or escalate. Do not overwrite a
native reassignment silently. Direct edits to the reserved `fc` metadata are
break-glass edits, not the ordinary native interface.

The stock API and cooperative local trust model do not guarantee exclusion
against arbitrary simultaneous direct `bd` and Git edits. The guarantee is one
authorized Fulcrum writer and stale-ownership rejection through its CLI, with explicit
recovery of observed external interference. This limitation is deliberate and
does not justify a second database or a Beads fork.

### Phase transitions

| Phase | Native status | Owner and allowed next transition |
| --- | --- | --- |
| intake | open | Derived Marshal; normalize to backlog/working/human |
| backlog | open or deferred | Marshal; dispatch to working/reviewing, reject/duplicate to done |
| working | in_progress | Vizier/Marshal/Weaver/Executor/Sage/Mason; finish to backlog/handoff/done or recovering |
| handoff | in_progress | Old owner until transfer; destination then reviewing; failure to recovering |
| reviewing | in_progress | Warden; fixes stay reviewing, approval to delivering, failure to recovering |
| delivering | in_progress | Warden/Justiciar; observed delivery to done, failed validation back to reviewing |
| recovering | blocked | Existing owner until Justiciar transfer; repair to previous valid phase/done/human |
| human | blocked | HUMAN; explicit resolve to Marshal backlog/recovery |
| done | closed | Historical last owner; explicit reopen must establish new scope/owner/ownership operation |

`work close` accepts `answered`, `delivered`, `reduced_scope`, `findings`,
`rejected`, `duplicate`, or `cancelled`. Disposition is `{outcome, summary,
waived_requirements, known_defects, canonical_bead, completed_at}` with unused
fields empty/null. Reopening through native `bd reopen` becomes a Marshal intake
request and invalidates the old ownership reference; it never silently reuses an old active turn.

### Installation and task records

`fc-system.fc` holds `kind=control`, effective owner, `vizier_thread`,
`marshal_thread`, `active_takeover`, and
`last_transition`. No task status, full backlog, or authoritative model/policy
configuration is copied into it. Effective standing policy is read from YAML.

Each task record holds `kind=task`, `owner` (its native task ID), `thread_id`, `role`, `work_bead`, `ownership_operation`,
`creation_operation`, `creation_cwd`, `model`, `effort`, `associated_beads`,
`replaced_by`, `missing_finish_reminder`, `archive_state`,
`archive_due_at`, `archive_operation`, and last observed native turn/status.
Observations are caches: live native facts win. An inactive task with a completed
bead still has its record, allowing archive-once behavior after restart.

### Operation receipts and uncertain outcomes

```json
{
  "fc": {
    "kind": "operation",
    "owner": "01a-example-warden",
    "request_id": "11111111-1111-4111-8111-111111111111",
    "command": "promotion.start",
    "input": {"bead_id": "fc-51o", "source_oid": "example-git-oid"},
    "bead_id": "fc-51o",
    "ownership_operation": "fc-22222222222242228222222222222222",
    "state": "accepted",
    "step": "authorize",
    "attempts": 0,
    "external": {"provider": "tollgate", "handle": "candidate-example"},
    "planned": {},
    "result": null,
    "error": null,
    "next_action": "Authorize the recorded candidate and inspect its state.",
    "created_at": "2026-09-14T06:00:00Z"
  }
}
```

Create the receipt before any external effect. An uncertain receipt creation is
reconciled by its predetermined ID. Each step records its locator/arguments before
sending; on restart, inspect that step's authoritative postcondition. Workflow
bead updates carry `last_transition=<receipt>` so a crash after the update but
before completing the receipt can be settled without repeating it.

Use one receipt per meaningful application operation, not one per poll, log line,
failed inspection, or transport reconnect. Steps/attempts are fields on the same
receipt. Related large operations may have child receipts listed in `planned`.
Result storage and comments are bounded summaries; command output lives in logs.
Completed receipts are closed in Beads; incomplete ones remain searchable.

## 4. Native Beads setup and formulas

For every project/worktree, use stock initialization against the existing central
server. This command is part of `project add`/worktree preparation, not an agent
ritual:

```sh
bd -C "$PROJECT_ROOT" init --server --external \
  --server-host 127.0.0.1 --server-port 3309 --database fulcrum \
  --prefix fc --non-interactive --skip-agents --skip-hooks
```

In enrolled product repositories/worktrees, keep their connection-only `.beads`
configuration local via Git's local exclude. In the brain repository, track safe
shared configuration and authored documents, while excluding live Dolt data,
credentials, locks, and other runtime files. Write project-local config with
`actor: project:<id>`, with per-project auto-push/export jobs disabled,
and the shared backend connection. Do not change Git author identity. The central
workspace also uses this database, but its actor is `fulcrum-controller`. Agent
thread environments may use `BEADS_ACTOR=project:<id>/thread:<threadId>`; parsing is
exact, not substring inference. Enrollment validates the effective backend and
actor via read-only context/config inspection. Project enrollment does not replace
unrelated Beads workspaces without explicit reset/move intent. Only the central
controller publishes this shared database to the brain GitHub remote on the
five-minute cadence or `ledger sync`. Native project shells create no competing
pushers. Remote copies are persistence, not another active local authority. Local
intake succeeds independently of a sync outage, which has its own receipt and
visible waiting reason.

Formula assets are named `fulcrum-<role>.formula.json`. The following is a minimal
valid shape; the installed role assets contain the full static role instructions
specified in design.md. Stock Beads requires its own `version` field; it is an
external format requirement, not a Fulcrum versioning scheme.

```json
{
  "formula": "fulcrum-executor",
  "version": 1,
  "type": "workflow",
  "pour": false,
  "vars": {
    "title": {
      "required": true
    },
    "outcome": {
      "required": true
    },
    "project": {
      "required": true
    },
    "workspace": {
      "required": true
    },
    "acceptance": {
      "required": true
    },
    "current_evidence": {
      "default": "No prior evidence."
    },
    "bead": {
      "required": true
    },
    "thread": {
      "required": true
    },
    "ownership_operation": {
      "required": true
    }
  },
  "steps": [
    {
      "id": "work",
      "title": "{{title}}",
      "description": "## Outcome\n{{outcome}}\n\n## Project and workspace\n{{project}}\n{{workspace}}\n\n## Acceptance\n{{acceptance}}\n\n## Current evidence\n{{current_evidence}}\n\n## Next action\nImplement the outcome in the assigned worktree, run relevant checks, and commit. Before editing, acquire this bead through fulcrum enter executor --bead {{bead}} --description 'Implement assigned task'.\n\n## Finish\nRun fulcrum finish --bead {{bead}} --thread-id {{thread}} --ownership-operation {{ownership_operation}} --outcome ready_for_review --input - with summary, source_oid, checks, and evidence as JSON. Once accepted, stop implementation. Report incidental issues with fulcrum report."
    }
  ]
}
```

Invoke `bd -C <ledger> cook <asset> --var key=value ...`; parse `steps` and require
one `id=work` step. This was verified read-only against the installed stock binary.
The normal command currently requires an accessible Beads workspace even for
compilation, which is why degraded entry uses packaged static instructions.

Role entry is idempotent for the same task/bead/role while that acquisition is
valid; an exact retry creates no new ownership operation or worktree. A different
role performs the specified transition. After entry, pass the **entire cooked
role/task description** directly as the worker turn input, together with the
concrete finish command including `--thread-id` and `--ownership-operation`.
Retain the exact input on the turn-start receipt. Add this literal correlation
line (an identity, not another prompt layer):

```text
[Fulcrum operation <START_OPERATION_ID>; ownership operation <OWNERSHIP_OPERATION_ID>]
```

The fixed read-the-bead bootstrap prompt is removed. `context` remains available
for inspection and compaction; it does not replace initial prompt delivery.
Marshal receives its bounded decision brief. Each microskill runs
`fulcrum enter <role> --description ...` with optional explicit bead; metadata
disables implicit invocation. `$bead` runs `fulcrum report` and does not call enter.

Every installed skill, including setup and reporting, has this
`agents/openai.yaml` policy:

```yaml
policy:
  allow_implicit_invocation: false
```

The registration command is the skill's substantive action. Supply descriptions
as literal arguments, read the returned role context, and do not duplicate the
role manual in `SKILL.md`. Infrastructure invokes a role explicitly through the
same CLI/application operation and supplies its returned context to Codex.

## 5. Adapter contracts

Adapters return typed facts. They may expose an operation as pending, but never
start untracked retries or claim successful promotion from a parse failure.
All methods receive instance context and an operation ID for correlation.

### Codex runtime

```python
class Runtime:
    async def capabilities(self) -> RuntimeCapabilities: ...
    async def create_task(self, spec: TaskSpec) -> TaskFacts: ...
    async def find_tasks(self, creation_cwd: str) -> list[TaskFacts]: ...
    async def inspect_task(self, thread_id: str) -> TaskFacts: ...
    async def configure_task(self, thread_id: str, spec: TaskSpec) -> TaskFacts: ...
    async def start_turn(self, thread_id: str, input: TurnInput) -> TurnFacts: ...
    async def find_turn(self, thread_id: str, operation_id: str) -> TurnFacts | None: ...
    async def interrupt(self, thread_id: str, turn_id: str) -> TurnFacts: ...
    async def respond(self, thread_id: str, request_id: str, response: dict) -> None: ...
    async def release(self, thread_id: str) -> ReleaseFacts: ...
    async def archive(self, thread_id: str) -> TaskFacts: ...
    async def unarchive(self, thread_id: str) -> TaskFacts: ...
    async def delete(self, thread_id: str) -> TaskFacts: ...
    async def resources(self) -> ResourceFacts: ...
    def events(self) -> AsyncIterator[RuntimeEvent]: ...
```

`TaskSpec` contains creation cwd, actual work cwd, project ID, allowed workspace
roots, title, model, effort, and supported tool configuration. `TaskFacts` contains
ID, title, cwd, archived/existence facts, active turn, loaded status,
pending input requests, and observation time. `TurnFacts` contains ID, lifecycle
state, input correlation, completion/error, and observed tools/usage.
`ReleaseFacts` distinguishes released/already-unsubscribed/not-loaded and any
remaining active terminals. `ResourceFacts` carries native loaded/active counts,
optional process FD limit/usage, and overload signals; missing observations are
unknown. Runtime events preserve native IDs and include approvals and input
requests as well as normal start/completion/failure events.

Map to supported app-server initialize, thread/start/read/list/resume/name/set,
turn/start/interrupt, thread/unsubscribe/archive/unarchive/delete, and native
request-response/event operations. Use turn/start's cwd for the worktree; verify
the returned task's project attachment and actual work context. Unsupported
configuration returns a capability result rather than being silently ignored.

**Lost creation response:** choose `<instance>/threads/<operation-id>` as a unique
creation cwd and persist it before `thread/start`. Create the native task there,
then configure/start its work turn in the actual project/worktree. Query native
thread inventory by that exact creation cwd if the response is lost, using the
stored index rather than requiring a rollout that may not exist before the first
turn. Do not create a second task if absence is not established. Multiple matches
or missing index evidence require recovery. Record the returned native ID before
starting work; afterward the task record is the stable locator. This technique is
for recovery correlation, not a second persistent task registry on disk.

**Lost start response:** inspect that task's recent inputs/turns for the exact
operation marker. If found, adopt it. If a definitive response proves rejection,
retry within the limit. If history is unavailable or absence inconclusive, mark
uncertain and do not blindly send another turn. The API does not promise a
client-provided exactly-once creation key; Fulcrum must not invent one.

`RuntimeCapabilities` reports supported model/effort pairs and native lifecycle
methods. Model precedence is explicit invocation, work role override, project role
default, then instance role default. Record the chosen value and its origin on
the task/turn receipt. An unavailable model is actionable input/capability failure;
do not silently substitute it. A changed default does not rewrite an active turn.

### Delivery provider

```python
class Delivery:
    async def prepare(self, project: Project, work: WorkRef) -> WorkspaceFacts: ...
    async def inspect_workspace(self, work: WorkRef) -> WorkspaceFacts: ...
    async def submit(self, source: SourceRef) -> ValidationFacts: ...
    async def inspect(self, source: SourceRef, handle: str | None) -> DeliveryFacts: ...
    async def promote(self, source: SourceRef, handle: str) -> DeliveryFacts: ...
    async def cancel(self, source: SourceRef, handle: str) -> DeliveryFacts: ...
    async def synchronize(self, source: SourceRef) -> DeliveryFacts: ...
    async def cleanup(self, work: WorkRef) -> WorkspaceFacts: ...
```

`WorkRef` contains bead ID, project, intended worktree path/branch, and operation
ID. `SourceRef` adds exact commit OID and target integration branch.
`WorkspaceFacts` contains path/branch/base OID, existence, ownership evidence, and
preparation result. `ValidationFacts` contains provider handle, exact source,
`pending|running|passed|failed`, and check/evidence references. `DeliveryFacts`
contains validation, promotion (`not_started|pending|promoted|failed|unknown`),
integration OID, synchronization (`pending|complete|failed|not_required`), cleanup
(`pending|complete|failed`), and evidence. Provider handles are opaque strings.

`synchronize` is also exposed as `source sync`. Its successful postcondition is
the configured remote branch containing the recorded promoted integration commit.
When Tollgate already performs this publication, inspect its outcome before any
Git retry. `not_required` is valid only when project configuration explicitly
disables required synchronization. Cleanup never removes the only unsynchronized
source or dirty evidence. Warden remains responsible for unresolved delivery facts.

The provider owns merge serialization and validation against the integration
base. Fulcrum owns role approval and responsibility for asking the provider to
act. A different CI/delivery system implements these operations; it need not
imitate Tollgate's internal candidate or certification model. A CI-only service
needs an adapter that also supplies Git integration/cleanup, not a stub that calls
a green check a promotion. Source is immutable per validation submission; Warden
edits produce a new submission and invalidate old approval.

Tollgate mapping:

| Interface | Native operations |
| --- | --- |
| prepare | `tg --json --no-launch --repository ID worktree create codex/fc-…`, then project preparation argv |
| inspect_workspace | Git worktree/branch inspection and Tollgate repository facts |
| submit | `tg --json --no-launch --repository ID candidate OID` in the assigned worktree |
| inspect | `tg ... status HANDLE`, queue/history lookup by exact source/worktree when handle is unknown, and Git facts |
| promote | `tg ... approve HANDLE` without a blocking stream; inspect status afterward |
| cancel | `tg ... cancel HANDLE`, followed by status inspection |
| synchronize | Provider source-publication result, or configured Git remote push followed by ancestry inspection; never a second promotion |
| cleanup | `tg ... worktree remove PATH` after provider disposition, then Git absence checks |

The adapter normalizes Tollgate's actual JSON shapes and the provider's own final
source regeneration/CI behavior. Do not assume a successful streamed `approve
--wait` output is a single JSON object. A missing response never proves rejection.
If an integration commit differs from submitted source because of provider-owned
regeneration, retain both identities and provider evidence of the relationship.
Never substitute an unrelated later candidate for the source Warden approved.

Adapter failure categories are `rejected`, `transient`, `uncertain`, `unavailable`,
and `unsupported`. Include observed output and whether an external effect is
possible. Only proved rejection permits declaring a mutation did not happen.

## 6. Reset without a parallel workflow store

Hard reset must remain resumable even while deleting its ledger. Use a **temporary
stock Beads reset workspace**, selected by `reset --hard`, solely for the reset's
receipt and enumerated targets. It is the sole workflow authority during reset:
stop the old controller first, treat the old ledger as deletion input, and never
run normal workflow against both ledgers. It is not a JSON journal or operational
database to retain alongside normal Beads. Its deterministic location is
`<instance-parent>/<instance-name>.reset/` and `reset` resumes it if present.

The reset receipt records exact old instance roots, managed native IDs, worktrees/branches, outstanding provider handles, and the completed step for
each. For the old implementation only, inventory its read-only operational store
to enumerate ownership; this is deletion inventory, not migration. Do not use
role-looking titles alone to classify unrelated user tasks as owned. New-system
inventory comes from its task/operation/work records. Capture config needed to
bootstrap as configuration, not retained historical work.

Sequence: obtain exclusive service control; stop old dispatch; observe terminated
managed tasks and owned tools; cancel or reconcile active provider work; delete managed
native tasks; remove managed worktrees and local branches; remove old ledger and
operational/log data; initialize a clean normal ledger and leadership; mark reset
complete; remove the temporary reset workspace; start normal service. If an
independent cleanup target fails, continue other targets and retain the failure
on the reset bead. Already-absent targets are successful postconditions.

Before deleting the temporary reset workspace, write a terminal receipt with the
same request ID into the clean ledger, containing only the new installation
identity and successful reset result, not the old target inventory. An exact retry
can then return completed even after the temporary workspace is gone. Restart
checks for an unfinished reset workspace before starting ordinary dispatch. A
fresh hard reset with a new request ID is a new destructive operation.

The configured old `brain` may contain unrelated source/documents. Delete only
its old Fulcrum ledger/generated state and explicitly enumerated Fulcrum assets,
not the enclosing Git repository. Do not purge historical Tollgate data unrelated
to Fulcrum. Any old remote ledger synchronized by Fulcrum must also be explicitly
enumerated and cleared through the provider's supported destructive operation;
an inaccessible remote makes reset incomplete, never silently preserved for later
resurrection. No backups or data migration are part of this operation.

If the Beads executable itself cannot initialize the temporary reset workspace,
return degraded read-only inventory and a concrete repair command. Do not begin a
multi-resource destructive reset without a durable Beads receipt. The emergency
role still investigates and repairs the dependency; this is not a refusal to
perform useful work.

## 7. Repository validation

Run `scripts/prepare-check` when dependencies change and `scripts/check` for the
complete formatting, strict types, and behavior gate. The check has a 55-second
wall-clock budget in a prepared environment. Tests use small in-memory records,
mocked external adapters, and bounded local file/socket checks. There are no
public test-provider, scenario, fixture, or smoke commands.

## 8. Contract acceptance

Focused tests verify request reuse and conflicts, current ownership, dependency
and capacity admission, exact task/turn recovery, safe cleanup, and current
validated delivery evidence. CLI parsing/dispatch and adapter error contracts are
checked in-process. No real Beads database, Git worktree, installed workflow, or
live model run is required for acceptance. See [validation](../validation.md).

## 9. Knowledge, analytics, and maintenance

These are required retained capabilities, not optional appendices to the CLI.
They share the result envelopes, operation identities, writer lock, and offline
mode above. The replacement command names are deliberate; no old aliases remain. All mutation
commands in this section accept JSON stdin as well as files. Derived queries
never create analytics or memory issues merely to display a report.

### Planning, memory, and publication commands

| Command | Input and result |
| --- | --- |
| `plan publish --bead ID --input FILE` | Publish approved plan text and reconcile its child graph; returns plan/root ID, child-key map, publication receipt |
| `plan refine --bead ID --input FILE` | Update an existing plan using stable keys; records scope differences and affected active owners before activating changes |
| `plan show ID` | Canonical text, approval/review evidence, activation, graph, and local/remote publication facts |
| `plan activate ID [--authorization ID]` | Human/Vizier authorization, or Marshal execution of that recorded authorization; clears only its future deferral |
| `memory list [--scope global\|project:ID\|role:ROLE]` | List bounded curated memory records |
| `memory show ID` | Canonical text, scope, owner, and update attribution |
| `memory set [--id ID] --input FILE` | Create/replace `{scope, title, text, references}`; retain previous text in the mutation receipt |
| `knowledge publish --bead ID` | Export current plan/memory to configured Git destination and inspect synchronization |
| `ledger sync` | Commit and push pending Beads history to the brain Git remote immediately; return observed publication results |
| `ledger status` | Local commit, last pushed state/time, pending age, cadence, operation ID, and any remote error |

`plan publish/refine` input is `{text, activation, approved_by, approval_evidence,
reviews, tasks, publication, approval_operation}`. Activation is `active` or `future`;
`approved_by` is `human` or the current Vizier. Approval fields must match the exact
retained approval receipt specified in §10; an agent cannot merely assert approval.
`reviews` has `cold_reader` and `requirements` entries, each `{thread_id, findings,
resolution, state}` with state `complete` or `waived`. Waived entries require
`waiver: {actor, reason}` from human/Vizier/Justiciar. Continued authoring never
requires successful review-task creation; an unpublished draft remains available in
its bead. Independent review tasks use the ordinary runtime and count toward capacity.

`tasks` uses the `work create` graph schema with stable keys. `publication` is
null or `{destination: knowledge|project, relative_path, require_remote_sync}`.
Explicit `publication=null` retains the complete plan solely in Beads. When the
field is omitted, a substantial plan defaults to configured knowledge publication;
the effective choice is included in its approval input, never silently added later.
Reject path escapes. `plan` on the root bead holds `{text, activation, approved_by,
approval_evidence, reviews, children_by_key, publication_operation}`; children hold
`plan={root_id, key}`. Existing keys retain IDs. Deleted keys preserve delivered
work and explicitly cancel/defer unfinished work only after stopping conflicting
writers; recorded scope changes invalidate affected approval. A failed graph
mutation resumes its receipt and does not create a second graph. Graph readiness
and remote publication readiness are separate observable facts.

The root stays Marshal-owned while children run. Future activation adds a policy
waiting reason removed by explicit activation, never by age or spare capacity.
Plan approval does not silently approve every later substantive refinement.
Publication may proceed locally with an independently recorded remote failure;
when remote sync is required, activation waits for its observed success or an
explicit exception, without blocking continued investigation.

`origin` preserves the initial task/request and optional reporting bead.
`workflow_root` identifies the originating plan or standalone work bead for cost
and completion aggregation. `caused_by` is null or `{operation_id, thread_id, turn_id}` identifying the operation/native turn
that produced a follow-up. A newly filed unrelated report has its own workflow
root and points back through `caused_by`; this does not charge all future work to
its discoverer. Same-scope recovery and delivery remain in the original workflow.
These references survive role and native task replacements.

Memory uses `fc={kind: memory, owner, scope, references, last_transition}` and
native title/description for curated text. Context loads relevant global/project/
role memory titles and at most 4,000 characters of selected text, with explicit
continuation references; Marshal's overall brief limit still applies. Never
copy full transcripts into memory or silently truncate a task's requirements.
Git export has a narrow `publish(record, destination)` / `inspect(receipt)`
interface: stage only selected paths in an isolated Git worktree, preserve remote
changes, commit, push without force, and inspect remote ancestry of the recorded
commit. Uncertain results are inspected before replay. A conflicting edit returns
a repair item with local content retained. A remote that already contains the
commit satisfies publication even if newer independent commits are present.
The export stores no ownership, queues, or replay state outside Beads.

### Usage and cost commands

| Command | Input and result |
| --- | --- |
| `usage [FILTERS]` | Raw native usage totals, role/project breakdown, missing observations, included native turn IDs |
| `cost [FILTERS]` | API-equivalent estimate, priced subtotal, currency, coverage and exclusion reasons, rate provenance |
| `rates list` / `rates show ID` | Retained immutable pricing inputs and their source/effective time |
| `rates add --input FILE` | Explicitly install documented pricing inputs; never silently reprice historical estimates |
| `usage reconcile [--bead ID] [--thread-id ID]` | Read retained native evidence without resuming tasks; fill recoverable usage gaps |

Filters are `--bead ID`, `--workflow ID`, `--operation ID`, `--thread-id ID`,
`--role ROLE`, `--project ID`, `--since RFC3339`, `--until RFC3339`, and
`--group-by bead|workflow|operation|task|role|project|model` (default workflow).
Filters intersect. IDs refer to current native/Beads identities, never old
SQLite row IDs. Reads work with the controller stopped and survive log pruning.

Use one `fc:analytics` record per observed native turn, with `external_ref` set to
`fulcrum:usage:<thread-id>:<turn-id>`. Its `fc` object holds `kind=analytics`,
`subtype=turn`, `owner`, `thread_id`, `turn_id`, `bead_id`, `workflow_root`, `role`,
`project`, `operation_id`, `related_task`, `purpose`, `attributions`, `model`, `effort`,
`usage`, `response_records`, `coverage`, and `missing_reasons`. A response record
has its native item ID (or a recorded observation ordinal when absent), configured
and effective model, service tier, disjoint token categories, tools, rate-card
reference, decimal priced components, and coverage. Store terminal totals and
price-bearing response facts, not every streaming token sample. If response
records exceed 64 entries, place each successive block in a linked analytics
record with its block ordinal; this bounds individual Beads updates. All records
remain stock issues, queried through the ledger adapter without SQL tables.

`attributions` lists `{workflow_root, operation_id, weight, reason}`. Ordinary
work and independent review tasks inherit one root with weight 1. A Marshal decision batch spanning
several roots allocates its turn equally across the distinct roots explicitly in
the recorded decision input; record that allocation as an estimate, not observed
per-bead token usage. Rows with no causal work remain leadership overhead.
Weights for a turn sum to 1, so global totals count every native turn once.
Reports expose direct/review/coordination components and unallocated overhead.
This accounting must not start extra Marshal turns to simplify attribution.

Duplicate cumulative usage observations replace the same observation; sum unique
final native turns, never successive cumulative totals. Track independent
review tasks through their Beads relationship and distinct native task/turn IDs,
including late results. Fulcrum does not discover or price native subagents as a
separate contribution. If native totals include usage without enough attribution,
retain those observed totals and mark the breakdown incomplete rather than adding
invented task rows. Unrelated native tasks are excluded. Native model-reroute
events affect the next response only:
deduplicate retransmission while pending, consume once at the next response,
and treat a later occurrence as new. Missing response/model evidence marks cost
partial instead of guessing from the originally configured model.

`rates add` input is `{model, currency, effective_at, retrieved_at, source_url,
input_per_million, cached_input_per_million, cache_write_input_per_million,
output_per_million, tiers, long_context, tools}`. Prices are nonnegative decimal
strings. `tiers` maps native tier to a decimal multiplier; `long_context` is null
or `{threshold_input_tokens, input_multiplier, output_multiplier}`; `tools` maps
native tool product to `{unit, price}`. Persist immutable rate-card analytics
issues. Unknown model/tier/tool pricing is unpriced, never implicitly free.
Setup seeds bundled rate cards verified against official published sources at
implementation time, with retrieval/effective dates and provenance retained.
Unsupported models remain unpriced. Prices are explicitly sourced inputs, not
constants copied indefinitely from the old code. No scheduled rate refresh or
model call is required.

For each response, ordinary input is total input minus cached and cache-write
input; invalid or missing disjoint counters make the corresponding estimate
partial. Reasoning tokens are already included in output and are not added again.
Apply documented long-context rules to that response, then tier/tool rules from
its retained rate card. Decimal arithmetic sums the priced components. Return
`coverage=complete|partial|unknown`, `priced_subtotal`, optional `total` (null
unless complete), `currency`, direct/review/coordination breakdowns, counts, and named
missing/excluded contributions.
Never present a partial subtotal as the full cost or an actual subscription bill.

Freeze a workflow summary when its work/plan and all observed causally included
turns are terminal. Persist the included ID set, exclusions, coverage,
completion boundary, totals, and rate references in an analytics record; no
Marshal turn is needed just to close or price work. If observation is unavailable,
freeze with the gap recorded rather than blocking delivery. Later recovery adds
an explicit correction record referencing the frozen result, and queries show
both the original and corrected total; do not silently rewrite historical facts.
A reopened bead starts a new recorded completion interval under the same ID.

### Automatic cost metadata on top-level beads

Every completed top-level work bead receives `metadata.fc.completion_cost`
automatically. A top-level work bead is the originating work/plan whose ID equals
its `workflow_root`, including Weaver question/authoring sessions and roots
created through direct role entry. An epic's deliverable children contribute to
that root; they are not charged again by summing child/root rollups. Control,
task, and operation records do not receive this annotation as top-level work.

The annotation is a field on the original bead, not a new cost-label bead:

```json
{
  "completion_cost": {
    "estimated_api_cost_usd": "12.340000",
    "priced_subtotal_usd": "12.340000",
    "currency": "USD",
    "coverage": "complete",
    "state": "finalized",
    "scope": "workflow_lifetime",
    "summary_bead": "fc-c05a1234",
    "completed_at": "2026-09-14T08:00:00Z",
    "calculated_at": "2026-09-14T08:00:02Z",
    "missing_reasons": []
  }
}
```

Amounts are decimal strings, calculated with the response-level rate rules above.
The example amount is illustrative, not a measured cost. USD rate cards are
required for this annotation; a contribution priced only in another currency is
explicitly unpriced here, with no invented exchange rate. `estimated_api_cost_usd`
is null when coverage is partial/unknown; `priced_subtotal_usd` retains any known
priced portion and is null if no priced evidence exists. A zero value requires
known zero cost, not missing telemetry. Include all causally attributed work from
Weaver planning/intake through child delivery and recovery, including independent review tasks,
shared Marshal decision allocations, and supported model-billed tool charges.
The summary record retains component/rate provenance and attribution assumptions.
This is an API-equivalent estimate, not an actual subscription charge.

Closing the root queues this mechanical finalization through its existing
completion operation. Write the available annotation with `state=pending` if
terminal usage still needs observation, then set `state=finalized` when the frozen
summary is ready. Never hold promotion/closure open while waiting for pricing or
start a Marshal/Sage turn solely to compute the total. A finalized annotation may
still have partial coverage; it records the evidence actually available.

Persist the summary record first, then update the root metadata using the normal
full-`fc` merge and transition receipt. A restart reconciles a missing root update
from that same receipt and summary ID. Duplicate completion events never add cost
again. The root's completion operation records finalization as unsettled until
its annotation is written or an explicit telemetry gap is finalized, so a closed
root with a pending annotation remains discoverable by normal reconciliation.
Unavailable Beads is a pending persistence failure, not successful annotation.
These metadata changes are included in the usual brain Git publication cadence.

Late usage/pricing recovery appends the correction record already specified above
and updates the root annotation to point to the corrected summary, with a new
`calculated_at`. Preserve the original summary and its evidence. Reopening starts
a new completion interval; the next closure refreshes the lifetime workflow
estimate from unique attributed native turns across its intervals. Do not add a
previous rollup to the same raw turns or silently include unrelated work filed
later from that conversation. While reopened, retain the last completion value as
historical metadata; `completed_at` identifies the closure it describes.

`bd show ID --json` exposes the native metadata directly. `fulcrum work show ID
--json` exposes it at `.result.fc.completion_cost`; `cost --workflow ID` returns
the same summary and coverage. `usage reconcile --bead ID` also reconciles this
annotation after gathering late evidence. Extend the existing deterministic
successful-delivery and duplicate/restart scenarios to assert a root with children
gets the expected USD estimate exactly once, and missing usage produces explicit
partial metadata. No live models or additional stress test are required.

A concise terminal example after a completed scenario:

```sh
fulcrum --instance "$FC_INSTANCE" usage --bead "$bead" --group-by role --json
fulcrum --instance "$FC_INSTANCE" cost --workflow "$bead" --json
fulcrum --instance "$FC_INSTANCE" memory set --input - --json <<'JSON'
{"scope":"global","title":"Delivery preference","text":"Use proportionate automated checks; no routine live-role smoke gate.","references":[]}
JSON
fulcrum --instance "$FC_INSTANCE" fleet replace --mode drain \
  --reason 'Replace managed conversations while retaining work and memory' --json
```

### Installation, recovery, and continuity commands

| Command | Input and result |
| --- | --- |
| `setup [--input FILE] [--non-interactive]` | Without input, ask only for missing required values; unattended missing fields produce structured errors |
| `skills reconcile` | Repair Fulcrum-owned links and disable implicit invocation; leave unrelated assets and real user directories intact |
| `runtime capabilities` | Supported models/efforts and lifecycle capabilities |
| `runtime status` | Shared endpoint health, resource facts, and Desktop attachment evidence or unknown |
| `runtime launch-desktop` | Start configured Desktop locally with `CODEX_APP_SERVER_WS_URL`, including during controller maintenance; no ledger operation or controller connection; never terminate another runtime |
| `service update [--source ABSOLUTE_PATH]` | Build, probe, and activate a quiescent installed snapshot; resumable operation |
| `fleet replace --mode drain\|interrupt [--project ID] --reason TEXT` | Replace the selected managed tasks, preserving ledger/worktrees/policy/memory |
| `hook context --input -` | Read-only compaction hook; native hook JSON response, not the general result envelope |
| `fulcrum-recover inspect\|takeover\|repair\|release [ARGS]` | Independent executable for the same repair contracts and arguments as `fulcrum recover`; supports `--instance`, `--json`, exact task/bead/operation IDs |

The hook accepts `{hook_event_name, source, thread_id}`; only
`SessionStart` with `source=compact` yields context for a current managed task.
It returns `{continue: true}` and optional
`hookSpecificOutput={hookEventName: SessionStart, additionalContext: TEXT}`.
It never mutates state, starts a turn, denies a tool, or enforces a Stop hook.
The same context provider backs the normal CLI. A read failure supplies a short
advisory, while unknown/unmanaged/inactive tasks receive no text.

A task's `missing_finish_reminder` is null or `{turn_id, operation_id, state}`.
After a terminal turn without the required outcome, create one reminder receipt
for that ownership acquisition. Send only when the task is idle, with the original
scope and decision input unchanged. If another user turn arrived, wait for its
safe boundary; do not inject a competing start. Repeated omission becomes a
recovery reason. A human's normal question-answering conversation is not required
to issue an implementation finish when it has no such active responsibility.

Fleet replacement records the exact selected managed-task inventory, old-to-new
mapping, pending ownership transfers, and replacements on one receipt. Drain has no implicit
switch to interrupt on client timeout. Pause admission for the selected
scope until all old writers stop; replace and transfer through normal ownership operations.
Preserve current work state and reload only relevant context. Replace each leader
at most once, update its control identity, and never start a Vizier turn except
for a retained explicit human request. Do not delete the old task's retained
evidence; explicit fleet replacement may archive it with a successor link.
Resume partial replacement from the recorded map without duplicate native starts.

Setup repairs changed LaunchAgent definitions with absolute executable paths and
a deterministic PATH, reusing unchanged services and existing leadership. A
failed prerequisite has an exact capability/error result in `doctor`. A ready
socket is insufficient: probe ready endpoint, protocol, model configuration,
project/provider IDs, Beads, and installed assets. Remove old SQLite/policy-seeding
health prerequisites; they are absent from this architecture.

The separate recovery runtime exposes only inspection and essential repair, not
a duplicate dispatcher. It packages the same small application/adapter modules
in its own dependency environment. Acquire the instance lock before mutations;
when the controller is wedged, identify and stop only its configured service,
then acquire the lock. If Beads is unavailable, use the previously specified
truthful no-receipt repair path, not a local workflow journal. A failed/aborted
repair never automatically lifts a takeover fence while conflicting writers or
uncertain effects remain. Once prerequisites recover, reconcile external facts
into Beads before restoring ordinary authority.

Caller-owned input/evidence files are never deleted by ordinary commands. Parse
and retain accepted payloads in receipts, keeping diagnostic references for
rejected inputs. Only explicitly recorded Fulcrum-created temporary artifacts
are eligible for cleanup; cleanup failure never revokes an accepted outcome.

### Brain Git publication and cadence

The production brain remains `~/brain`, currently backed by
`git@github.com:thurn/brain.git`. Preserve the configured repository rather than
creating a new local-only ledger. These paths/remotes are deployment configuration,
not hardcoded adapter assumptions. The Dolt server's data directory is inside the
brain's excluded `.beads/dolt`; enrolled projects all connect to that one database.

Configure stock Beads' `origin` with the Git transport form of the brain remote.
For the current deployment, the setup operation uses:

```sh
bd -C "$BRAIN_ROOT" dolt remote add origin git+ssh://git@github.com/thurn/brain.git
```

Setup inspects existing configuration before adding it. Git's ordinary `origin`
remains unchanged. Stock Beads stores its committed database/history under the
Git remote's `refs/dolt/data`, separately from the brain's normal branch. No
issue export is produced. Authored plans/knowledge use their existing ordinary Git
publication path. See [Beads' Git remote documentation](https://raw.githubusercontent.com/steveyegge/beads/6c124203e771433a3550c348771a5b5e27fd3c21/docs/DOLT.md).

Every 300 seconds while running, the controller checks for unpublished changes;
when present it runs the same application operation as `ledger sync`. The check
uses existing reconciliation plus stock `bd vc status`/issue observations, so
native `bd` writes are included. Coalesce changes into one batch; the next tick
is not postponed by new writes. On startup, inspect retained pending publication
and flush overdue work. On clean shutdown attempt one final flush with a 30-second
budget; inability to push never destroys accepted local work or prevents stopping.
No service means no background push; CLI-only users run `ledger sync` explicitly.
No cron job, recurring model invocation, or second publication daemon is needed.

Persist publication state on `fc-system.fc.publication`: `pending_since`,
`last_success_at`, `operation_id`, `last_dolt_commit`,
`remote_data_ref`, and `failure`. `remote_data_ref` is the observed Git ref OID,
not the Dolt commit ID; never equate those identities. One `ledger.sync` receipt
records each batch's target commits, completed steps, and errors. The check ignores
changes made solely to these sync bookkeeping fields and `ledger.sync` receipts
when deciding whether new work is pending; otherwise recording a successful push
would cause endless new pushes. Those records are included with the next real
batch. No newly computed content hashes or separate sync-state files are needed.

A batch performs these steps under the normal writer, releasing its short
critical section before network waits:

1. Record intent, commit the Beads working set with `bd dolt commit`, and retain
   the resulting native commit identity. No-op commits are successful no-ops.
2. Push native history with `bd dolt push --remote origin`. Do not stage live
   `.beads/dolt` files in ordinary Git or generate an issue export.
3. Inspect the outcome and record completion. For an uncertain push, inspect the
   remote history through the supported Dolt Git transport in an isolated scratch
   checkout; prove the recorded native commit is present before settling it. A
   changed `refs/dolt/data` value alone is insufficient. Preserve the same receipt
   across response loss rather than generating a second publication operation.

Native concurrent `bd` writes may appear in a later batch. The receipt identifies
the actual committed database state, not a promise to include writes that happened
after its commit boundary. The native commit is the durable recovery point for
that push. Ordinary plan/knowledge Git publication remains separately observable
through its existing receipt and may be flushed by the same maintenance tick.

A definitive transient failure uses the existing three-send retry limit. After
exhaustion, leave the operation visible and stop automatic replays; a timer tick
or another unrelated bead write is not fresh retry authorization. `ledger sync`
or `operation reconcile`, or a proved relevant connectivity/configuration
recovery, resumes the same unresolved operation with an explicitly recorded retry
budget. Local work can continue. Marshal receives one compact notice when a push
has failed through that budget; unresolved remote divergence goes to scoped
Justiciar recovery. Routine publication never force-pushes. Do not automatically
pull remote workflow ownership changes into a running fleet: divergence requires
controlled reconciliation with stopped conflicting writers.

The hard reset is a separate explicit cutover: replace the enumerated dedicated
Dolt remote history with a clean database and verify from a fresh clone that old
issues/history are no longer reachable through that ledger. Preserve all unrelated
Git refs, the brain ordinary branch history, documents, and configuration. Use
stock Beads' supported remote-discard initialization and a narrowly targeted
replacement of the dedicated ledger ref after recording the expected old ref and
stopping old writers. If the remote changed since inventory, stop for reconciliation;
reset authorization is not permission to overwrite an uninspected concurrent edit.
This exception never applies to ordinary sync. It does not promise physical
removal of unreachable objects retained by a host, and creates no backup/export.
An inaccessible remote leaves reset incomplete. Task 21 specifies the disposable
proof required before production cutover.

Publication regression tests use observed adapter results and injected clocks;
they do not build disposable Git/Dolt remotes or wait for publication intervals.

### Authoritative system configuration

The sole configuration file is `<brain.root>/fulcrum.yaml`, defaulting to
`~/brain/fulcrum.yaml`. It is ordinary Git-tracked YAML in the existing brain
repository. Do not create `config.toml`, a second JSON config, or a policy/model
mirror in Beads. Configuration discovery uses explicit `--config`, then the
selected instance's `config` symlink, then `~/brain/fulcrum.yaml`. Setup creates
only a link to the selected file, so subsequent commands with `--instance` can
find an isolated test configuration without falling back to production. A dangling
link is an error, not permission to use the default. The YAML's resolved parent
must match `brain.root` when that field is present; otherwise derive the root
from the parent. The installed service records the absolute selected path in its
launch arguments. Neither a discovery link nor generated service arguments are
another configuration authority.

These are the initial role and slot defaults in `fulcrum.yaml`; the rest of the
configuration uses the setup fields already specified above:

```yaml
models:
  vizier: {model: gpt-5.6-sol, effort: high}
  marshal: {model: gpt-5.6-sol, effort: high}
  weaver: {model: gpt-5.6-sol, effort: high}
  executor: {model: gpt-5.6-sol, effort: high}
  warden: {model: gpt-5.6-sol, effort: high}
  sage: {model: gpt-5.6-sol, effort: high}
  mason: {model: gpt-5.6-sol, effort: high}
  justiciar: {model: gpt-5.6-sol, effort: high}
policy:
  automatic_capacity: 4
  default_project_capacity: 4
  project_capacity: {}
  paused_projects: []
  suspended_rules: {}
  rationale: Initial human-approved installation defaults.
brain:
  root: /Users/dthurn/brain
  remote: origin
  push_interval_seconds: 300
```

A project's slot ceiling is its `policy.project_capacity[project_id]` override,
otherwise `default_project_capacity`, always constrained by global capacity; there is no reserved recovery slot. Per-project role model defaults remain under each enrolled
project's `models` map. Existing per-request/work overrides apply only to that
work and retain their origin; they never modify these defaults. The smoke's
Luna/low choice is an explicit invocation override, not a config edit.

Only `--actor human` or the current Vizier task may create/change this file via
`config set`, `policy set`, `project add/remove/enable/disable`, or setup input.
A managed task may not impersonate a human. Controller file writes are permitted
only to execute a retained request from one of those actors, not autonomous
policy changes. Setup is allowed to initialize the file during a human-requested
installation; unattended setup is the same human-authorized operation. Rerunning
setup without an explicit config mutation must preserve the existing file.
Other roles, including Marshal and Justiciar, may inspect configuration and
propose changes but cannot apply them, replace the discovery target to bypass the
rule, or ask an implementation worker to edit it. The recovery executable
preserves this restriction. This is enforced by CLI role checks and role/tool
instructions within the existing trusted-local-user model, not a claim that OS
permissions can distinguish agents sharing the same user account.

`config set` accepts a JSON object of known configuration fields via the common
`--input` contract and merges specified mappings while replacing supplied arrays;
`policy set` applies the same operation within `policy`. Reject unknown fields,
duplicate YAML keys, unsafe YAML tags, invalid slot counts, and unsupported model/
effort values when capabilities are available. A model capability outage reports
unverified fields without fabricating a successful capability check. Write a
validated temporary file beside the target and atomically replace it, preserving
comments and unrelated settings. Compare the current file contents with those
read before editing; a concurrent manual change returns `CONFIG_CONFLICT`, not a
silent overwrite. No configuration version field or content hash is introduced.
Receipts retain initiator, rationale, changed fields, and Git publication evidence;
they are audit evidence, never a second source for the controller's policy.
Configuration repair must not depend on the connection it is fixing: an explicit
human `config set --offline` may use the same writer lock and file operation when
Beads is unavailable. Return `degraded` with the actual file change and no claimed
Beads receipt; retain ordinary Git history when available, never a second repair
journal. A Vizier request needs verifiable current leadership identity; absent
that evidence, report the missing verification and leave direct human repair
available. This exception permits human repair, not agent impersonation.

Direct human/Vizier edits are supported. Read and validate the current file before
new dispatch decisions; source-file events can refresh the parsed in-memory view.
Apply model/slot/policy changes to subsequent decisions without changing active
turns or interrupting workers to fit a lowered limit. Endpoint/executable/backend
changes are reported as requiring controlled service restart. Malformed/missing
configuration pauses new automatic admissions with a precise diagnostic; it must
not silently restore defaults or rewrite the file. Existing owners can finish
using their retained task/delivery facts, and inspection/emergency diagnosis stays
available. Repair requiring a file edit belongs to the human or Vizier.

Authorized YAML changes are committed and pushed to the brain's normal branch
by the five-minute maintenance cycle or `config sync`. This is independent of
native Beads history publication under `refs/dolt/data`: status exposes both.
Use the existing Git publication adapter with only `fulcrum.yaml` selected; keep
unrelated staged changes untouched. The controller may publish exact authorized
contents but must not resolve a YAML merge conflict itself. Conflicting remote
configuration is returned to human/Vizier; other roles may still repair unrelated
Git/Beads problems. Remote publication failure leaves local configuration usable
and its pending status visible. Reset preserves this file and its configured
values; it does not replace them with defaults while wiping workflow data.

Add one compact deterministic CLI assertion for each boundary: a Marshal or
Justiciar config edit is rejected without changing the file; a Vizier edit changes
subsequent model/slot selection; reset preserves the YAML. No native model run is
needed for these checks.

## 10. Complete workflow and operator interfaces

This section completes the contracts above. [The implementation index](plan/README.md)
maps every command family to its application operation, implementation task, and
verification. No second workflow engine is implied by the larger CLI surface.

### Marshal decision briefs and current context

`marshal brief` and `marshal request` accept `--kind auto|groom|dispatch|recover`,
default `auto`, plus the existing optional `--bead ID`. Auto selects a purpose from
the decision actually needed. Recovery/external intervention uses `recover`;
unclear proposals, duplicates, splitting and readiness use `groom`; actionable
work needing authorization/order uses `dispatch`. An explicit kind filters the
eligible work; it does not turn an ineligible bead into an actionable candidate.
`marshal request` records/sends the same selected input as automatic judgment;
`marshal brief` creates no receipt. If no judgment is needed, return
`decision_required=false`, `kind` (null when no purpose applies), `rows=[]`,
`omitted_counts`, and the reason,
without a native turn. A mutation request may retain its own no-op receipt, but
must not manufacture a decision turn simply to produce a nonempty result.

A serialized brief contains `kind`, `decision_required`, `why_now`, relevant
`policy_capacity`, `rows`, `omitted_counts` by purpose, and `continuations` (CLI
argv arrays). Each row carries `bead_id`, `title`, `outcome`, `owner`, `phase`,
`decision_needed`, a concise `decision_context`, `unknowns`, and `evidence_refs`.
`decision_context` contains the dependencies/blockers/size/overlap/progress facts
that matter to this choice. `proposed_action` is optional and carries its source
(author proposal or explicit policy rule); Python does not invent product value,
technical confidence, or priority tradeoffs. Omit redundant status narration.
The full serialized brief, including any current-context/memory excerpt, remains
at most 6,000 characters and 12 rows. Use fewer rows if required. Keep complete
comparison facts on the decision receipt and expose full evidence on demand.

At each safe decision boundary, prioritize urgent recovery, then relevant existing
priority and waiting age; a free slot alone executes existing authorization. Batch
only one kind at a time. Count omitted work by kind and supply a continuation
query, so a short brief does not pretend to show the whole backlog. Events arriving
while Marshal runs wait as current bead facts. Recompute selection at the next
safe boundary; do not interrupt a productive turn or append unrelated recovery
history to a grooming response. These are selectors in the existing decision
operation, not separate schedulers or periodic backlog-review jobs. Kind selects
what the briefing is about, not a new authority boundary or mandatory pipeline:
a grooming answer may also authorize newly clear work if no further competing
choice remains. Do not require a second Marshal turn just to repeat that decision.

Authoring guidance lives in the Weaver/report formulas. `fc.intake` is null or
`{benefit: string|null, uncertainties: string[]|null}`; absent/null means unknown,
while `uncertainties=[]` means the author identified none. Other intake facts use
existing outcome, acceptance, native dependencies, context and size fields.
`work create/update` and `report` accept the optional `intake` object. Native `bd`
callers need no Fulcrum metadata: preserve their request, read the supplied text,
and show unknown fields in the brief rather than rejecting intake or filling them
with invented values. Guidance should improve proposals without making every
small request satisfy a formal readiness ceremony.

`clarify` retains its existing `{question}` payload and records an investigation
request on the same unstarted bead. Transfer it to Weaver through normal ownership
and admission operations, with that question and the expected clarification in
its next action. Do not ask the human when repository evidence can answer it.
Weaver's ready/blocked outcome returns it to Marshal; relevant scope/evidence
changes create a new grooming decision. The old unknown alone does not repeatedly
wake Marshal or restart Weaver. Other eligible work continues. Active delivery
problems instead use recovery; clarification must not silently bounce Warden work
back to Executor. Splitting a proposal uses Weaver's ordinary graph/refinement
path, not a new Marshal-specific split engine.

Accepted decisions write the current reason and reconsideration triggers into
existing `dispatch`, `waiting.reasons`, or `disposition`, adding
`decision_operation` there to reference the originating receipt. Dispatch also
retains its reason; a waiting reason already has `reason` and `reconsider_when`.
A replacement decision supersedes current rationale, with prior evidence retained
on the old receipt. No duplicate backlog list or full task body lives on the
leadership control bead.

`context --role marshal` returns the current-context projection for standing
leadership: relevant YAML policy, current outstanding decision if any, actionable/
blocked work references, present rationale/reconsideration conditions, and omitted
counts with continuation commands. It uses the same 6,000-character/12-row bound.
The next decision prompt combines selected current context and its batch within
that single bound, never by concatenating two independently full-sized payloads.
After compaction or leader replacement, rebuild from Beads and YAML before acting;
do not replay historical notifications. The hook returns only a short current
reminder and the context command, and never creates a decision or rewrites memory.
General `memory` records may add lasting lessons/preferences once task 14 exists;
the current-work projection must work independently of that later capability.

Completed work and superseded incidents are omitted unless directly relevant to a
current dependency/recovery decision, in which case include only the needed fact
and evidence link. Exact historical inputs remain on their receipts for diagnosis.
All this context is a derived read view; losing Marshal's conversation does not
lose backlog decisions or require another durable summary store.

### Ownership operation and decision freshness

`fc.ownership_operation` is the ID of the acquisition/transfer receipt, not its
request UUID and not the most recent ordinary mutation. The transfer receipt
records `{from_thread, from_ownership_operation, to_thread, to_role}` and planned
work/task updates before changing ownership. The new acquisition reference is
that receipt's own ID. The source work update and destination task-record update
are reconciled steps; a turn cannot start until both agree. Human takeover and
reopening use the same rule. A repeated compatible `enter` retains the existing
reference; a real role change or new ownership cycle replaces it even if the
native task ID stays the same.

Owner-restricted writes require both matching task ID and ownership operation;
return `OWNERSHIP_CONFLICT` (exit 5) before external effects on mismatch. Sealing
an Executor finish also rejects a different second finish from the same
acquisition (`FINISH_SEALED`, exit 5); an exact request retry returns its receipt.
Accepted downstream steps remain bound to their recorded handoff/source and are
not invalidated merely because the successful transfer changed the work owner.

`marshal brief` is a read-only preview. Before sending a decision turn, create a
`marshal.decision` receipt with the exact ordered input, chosen work IDs, and
per-row decision-relevant facts. `marshal decide` input adds
`decision_operation` to `{decisions:[...]}`. Compare each row's owner, ownership
operation, phase, outcome, acceptance, dependencies, priority, relevant waiting
reasons, current source/approval, intake benefit/uncertainties, overlap tags, and relevant effective policy
against that receipt. Store complete comparison facts even when the displayed
brief summarizes them. Never use a digest or revision counter. `marshal decide`
may reference a receipt from `marshal request [--bead ID] [--kind auto|groom|dispatch|recover]`, which initiates the
same coalesced decision operation as automatic dispatch judgment. Its result is
`decision_operation`, selected IDs, and task/turn facts. Human requests can use
this command without invoking a skill.

Changed relevant facts return `STALE_DECISION` for that row. Reconcile dependency
and capacity changes mechanically: recheck capacity at dispatch, queue an already
authorized start when full, and do not require fresh judgment for a routine freed
slot. A batch can apply independent unchanged rows. One outstanding Marshal turn
is the limit, including explicit requests. Full work requirements are never
clipped into the comparison facts or a worker prompt.

### Missing work and task actions

| Command | Input, result, and authority |
| --- | --- |
| `work dependencies ID --input FILE` | `{add:[bead_id], remove:[bead_id]}`; returns current dependencies and affected waiting reasons. Current owner or human; reject cycles, self-edges, missing targets, and overlapping add/remove sets before writes. |
| `task output ID [--turn-id ID]` | Bounded native messages/tool evidence with `items`, `next_cursor`, `observed_at`, and `gaps`; read without resuming. Supports `--limit`, `--cursor`, `--max-bytes` (default 262144). |
| `task wait ID [--turn-id ID] --until idle\|terminal` | Native observed condition, turn ID, pending requests, and gaps; common timeout, never implicit cancellation. A failed turn satisfies terminal observation but is returned as failed evidence. |
| `task terminals ID` | Exact owned terminal IDs, running/completed/unknown status and output references; read-only. |
| `task terminal stop ID --terminal TERMINAL_ID --reason TEXT` | Owner/human/scoped Justiciar; record intent and observe termination. If the native API only supports all-terminal cleanup, reject targeted stopping as unsupported rather than stopping additional running terminals. |
| `task terminal stop ID --all-owned --reason TEXT` | Explicitly stop all background terminals belonging to that managed task through the supported native method, then observe; mutually exclusive with `--terminal`. |
| `marshal request [--bead ID] [--kind auto\|groom\|dispatch\|recover]` | Initiate the selected recorded decision; return its receipt/work, or an explicit no-decision result without a model turn. |

`work update` additionally accepts `priority` (0–4) and `title`. Its known fields
are `title`, `outcome`, `acceptance`, `summary`, `size`, `overlap_tags`, `context`,
and `priority`; omitted fields remain unchanged. Native status changes go through
close/reopen/transfer, not an arbitrary phase assignment. Dependency writes are
separate stock operations whose planned edge set is retained and inspected after
response loss. Dependency closure is a wake trigger, not proof of successful
prerequisite completion: delivered/answered/findings satisfy only the declared
prerequisite outcome; cancelled/rejected/reduced-scope work requires judgment.
A duplicate follows its recorded canonical bead and cannot bypass that bead's
unsatisfied outcome or introduce a cycle.

`project add` accepts `codex_project_id=null`: discover an exact enrolled root or
create a native project through the supported runtime API; similarly enroll the
exact repository with Tollgate when its ID is absent. Retain planned locators and
observed provider IDs on the enrollment receipt. Unsupported creation fails that
capability with an actionable result; operators need not use the Desktop UI.
Never create a second project when lookup after a lost response is inconclusive.

### Plan authoring, reviews, and closure

| Command | Input and result |
| --- | --- |
| `plan draft --bead ID --input FILE` | `{text, tasks, summary, publication, validation}`; stores complete unpublished draft, stable keys, and proposed checks. Returns draft facts and its operation; no dispatch/publication. |
| `plan review start --bead ID --perspective cold_reader\|requirements` | Start one independent ordinary Codex task, returning `review_operation`, `thread_id`, and input evidence. Owner/authorized author or human initiates. |
| `plan review finish --task ID --input FILE` | `{review_operation, findings, summary}` from that review task; findings are `{problem, required_change, evidence}` objects. Retain output and mark the review result complete without changing plan ownership. |
| `plan approve --bead ID --input FILE` | Human/Vizier `{reason, resolutions, waivers}`; resolutions map review operation to text, waivers are `{perspective, reason}`. Returns `approval_operation` and exact approved scope. Justiciar may separately waive an unavailable review within its takeover scope via repair, but cannot impersonate plan approval. |
| `plan complete ID` | Inspect and mechanically settle root completion; returns root state, unsatisfied obligations and next commands. Same application operation runs after child/publication settlement. |

A draft's `validation` is `{summary, checks}`. Each check is
`{criterion, task_keys, evidence_required}` and identifies how planned work covers
the outcome; it need not be a separate child. Weaver decides proportionate
verification. Small plans do not acquire a validation bead by default. A large
assembled-system test may be a real deliverable child when warranted.

`plan complete` is a mechanical reconcile operation available to human, current
owner or the controller; it cannot authorize changed scope. Draft/review/approval
errors use `STALE_REVIEW`, `APPROVAL_CONFLICT`, or `ACTIVATION_NOT_AUTHORIZED`
(exit 5) with current unmet facts and next commands, never silent waiver.
An incomplete `plan complete` inspection returns `root_closed=false` and unsatisfied
obligations; successful command inspection does not imply successful root closure.
It creates no mutation receipt merely to repeat an unchanged inspection.

Review tasks use role `weaver`, `purpose=plan_review`, `related_task` and
`associated_beads` on their ordinary task record. `review_operation` records the
perspective and exact reviewed draft. They do not acquire the plan's ownership or
become materialized workflow-step issues. Cold-reader input contains only the
candidate draft and review instructions; requirements-review input also contains
the original request and retained discussion references/text. Do not reuse an
author's conversation for an independent review. Send full prepared prompts,
count these tasks toward ordinary capacity, and price their native turns once.
Only the named review task may finish its review. Task control, output, request
responses and stopping use the existing task CLI. No native-subagent API is used.
A terminal review without a result gets one reminder, then normal recovery.

The root `plan` object adds `draft`, `validation`, `approval_operation` and
`activation_authorization`. An approval receipt retains the exact text, task-key
map with outcomes/acceptance/dependencies, review inputs/results/resolutions,
validation, and publication requirements. Publish/refine takes the previously
specified plan payload plus `approval_operation` and verifies it against those
retained values. Supplied `approved_by`/`approval_evidence` must match the receipt;
merely asserting approval is insufficient. Any substantive difference requires
new approval and invalidates affected reviews; cosmetic changes can be retained
as a documented difference without invalidating unchanged reviewed requirements.
The author identifies proposed cosmetic changes; human/Vizier approval decides
any disputed classification. Draft saving requires current owner/human authority;
publication mutates only approved scope.

Human/Vizier `plan activate` records authorization for the currently approved
future plan. Marshal passes that receipt through `--authorization`; it cannot
activate based only on age, policy capacity, or its ordinary dispatcher authority.
Required remote publication must settle before actual child starts. A changed
substantive draft invalidates authorization of that new scope, not already shipped
work. Deleted keys retain delivered evidence; stopping/cancelling unfinished
children is explicit in refinement and never silently inferred.

The root remains open and Marshal-owned while approved children or required
publication/delivery obligations remain. A planned future root is open/deferred;
`finish planned` ends the authoring responsibility without closing that root.
`plan complete` re-reads current facts. Successful required children and observed
required publication/delivery permit `done/delivered`; question-only roots close
as answered without a child graph. Cancelled/rejected children leave a scope
blocker until an authorized refinement removes/replaces the obligation, or scoped
Justiciar records reduced-scope completion. No extra model turn or validation
child is required to perform the aggregation. Root reopening retains prior cost
metadata as historical and establishes a new ownership operation. Telemetry never
blocks closure.

### Bootstrap, writer exclusion, and configuration failure

Use one advisory writer lock at `<brain.root>/.fulcrum-controller.lock`, outside
the resettable ledger. `<instance>/controller.lock` is a discovery symlink to it,
not another lock authority. `fc-system` binds the resolved `instance_root` and
`brain_root`; a second distinct instance against the same root fails
`INSTANCE_CONFLICT` before dispatch. Resolve symlinks before locking; reject a
backend configuration pointing to a different database/root than the verified
binding. Remote-host ledgers and multiple machines writing one active installation
are outside the local single-user contract. Setup verifies physical server identity,
not just a user-supplied port string.

With no ledger, setup may create the declared root/configuration, dependency
runtime, and stock Beads workspace before its first receipt. These are bounded,
idempotent bootstrap primitives inspected on rerun, not a new workflow journal.
Create the setup receipt as soon as Beads is available, before native tasks,
project enrollment or other workflow effects. Reserved `fc-system` is inspected
before initialization; leadership provisioning records intent before native starts.
A failed setup returns completed steps and precise unavailable capabilities. Never
claim a receipt when it was not persisted. Nothing starts Vizier without an
explicit retained human request.

An explicitly selected instance with missing/dangling configuration fails closed;
only an invocation with no explicit instance/config selection may use the production
default. `--config` gives a human a direct repair path. `service status` and
`recover inspect` inspect owned installation artifacts and explicit targets when
YAML cannot parse; they do not reconstruct work from logs. The running controller
retains an in-memory last validated configuration for finishing known work while
pausing admission. After a restart with invalid YAML, inspection remains available
but mutations requiring missing endpoints wait for human repair; do not promise
they can execute from a nonexistent configuration cache. Offline human config
repair has the already-specified no-receipt degraded exception.

### Observable operator results

`status.result` contains `observed_at`, `instance`, `work`, `operations`, `capacity`,
`publication`, and `gaps`. Work rows extend `WorkView` with `phase`,
`ownership_operation`, `next_action`, `waiting`, `active_task`, `last_progress_at`,
`delivery`, and structured `next_commands`. Capacity reports configured global and
project limits, active managed IDs, pending start reservations, unknown managed
IDs, human bypasses, and pressure. Delivery separates submitted source, approved
source, promoted integration source, source synchronization, and cleanup.

`doctor.result` contains `components` and `loops`; each has `name`,
`healthy|unavailable|unsupported|stale|unknown`, `last_success_at`, `evidence`,
`affected_commands`, and `next_commands`. Report intake/event/reconciliation/runner
loop health separately from service/socket liveness. Default reconciliation is
stale after two missed 15-second deadlines plus one external-request budget
(60 seconds); loops expose their own configured expected interval/deadline.

`trace.result` has ordered `items`, `next_cursor`, and `gaps`; items carry time,
bead/task/turn/operation IDs, transition/effect, outcome, evidence references, and
source identities when applicable. Reconstruct the durable summary from Beads
receipts and live facts, optionally enriching it from logs. Pruned logs produce
an explicit gap, never an invented complete transcript. `task output` exposes
native output without loading idle tasks; unavailable native history is a gap.
`operation show` includes accepted input, planned IDs/steps, attempt count,
external locators, current observations, errors and next argv commands.

All ordinary JSON reads are one envelope. Only explicitly requested `logs --follow`
streams JSONL event envelopes, with a final summary on normal termination. Reads
are bounded: default 20 items, `--limit 0` explicitly requests all; task/log chunks
have a byte cap. Cursors encode ordering position, not a hash or workflow version.
Document concurrent-page limitations and stable `(timestamp, id)` tie breaking.

### Publication bookkeeping boundaries

The adapter compares native issue changes since the last real published boundary,
using supported stock Beads history/diff and current issue reads. Ignore only
`fc-system.fc.publication` fields and `ledger.sync` receipt changes when deciding
whether a new batch is needed; do not ignore the entire control issue or other
operation classes. Retain the native boundary commit identity in the publication
receipt. If an installed stock API cannot provide enough change detail, report an
unsupported publication capability during setup/probing rather than polling
commits into an endless loop or introducing a second dirty-state database.

The same tick flushes separately observed ordinary Git changes for authorized
configuration and exported knowledge. A new source/work item, native edit or YAML
edit may create real pending work; recording push success alone may not. A relevant
connectivity-recovery retry authorization is a recorded transition from failed to
successful capability observation, not every successful poll. Compare these facts
directly and retain retry grants on the existing operation.

### Provider configuration

`runtime.kind` must be `codex`; `delivery.kind` must be `tollgate`. File-backed
simulated providers and their crash/event/clock/barrier controls are removed.
Test doubles are private to the test suite and cannot be selected by configuration.

### Timing, diagnostics, and repair payload details

The remaining optional configuration maps have these known fields/defaults; omitted
fields take these defaults only in an otherwise valid file, never after invalid
configuration. Times are positive seconds unless noted; ratios are strictly between
zero and one with resume below pause. `source_watch_root` remains the optional
absolute path/null field specified in setup. Preserve existing `knowledge`, model,
project and provider maps from §2.

```yaml
timing:
  intake_busy_seconds: 2
  intake_idle_seconds: 10
  reconcile_seconds: 15
  external_timeout_seconds: 30
  event_silence_seconds: 120
  checkpoint_seconds: 600
  stalled_seconds: 1800
  archive_idle_seconds: 600
diagnostics:
  retention_days: 14
  max_bytes: 1073741824
  capture_bytes_per_stream: 262144
resources:
  fd_soft_limit: 4096
  pause_ratio: 0.85
  resume_ratio: 0.70
```

Retry policy remains the single fixed three-send/2-second/10-second policy; do not
add per-role retry configuration. `brain.push_interval_seconds` remains 300.
The managed task record also records the selected model origin, purpose, related
ordinary task, latest substantive progress and last sent checkpoint/reminder
operation. The control record includes the resolved instance/brain binding,
leadership acquisition operations and optional fixture-creation operation.

Repair uses `{actions:[{action,target,arguments,reason}]}`. `target` is an exact
recorded ID/path/service inside the supplied takeover scope. Reject unrecognized
argument keys before any action, retain input/order, and inspect each step before
advancing. Required action arguments are:

| Action | Arguments |
| --- | --- |
| `interrupt` | `{turn_id}` for the target task; inspect current turn before sending. |
| `release_subscription` | `{}` for the target task. |
| `terminate_owned_terminal` | `{terminal_id}`; unsupported precise targeting is an error, not broader termination. |
| `adopt_owner` | `{thread_id, role, expected_ownership_operation}` for the target work bead. |
| `replace_thread` | `{mode: drain\|interrupt}` for the target managed task; record successor before transfer. |
| `cancel_delivery`, `reconcile_delivery` | `{source_oid, provider_handle}` for the target bead; handle may be null only when inspecting an unknown submission. |
| `remove_worktree` | `{}` for the exact recorded path; default dirty/unsynchronized protection applies. |
| `set_disposition` | `{outcome, summary, new_scope, waived_requirements, known_defects, evidence}` for work; optional source evidence must be observed, never an asserted promotion. |
| `restore_leadership` | `{role: marshal\|vizier, thread_id}` against the control record. |
| `repair_service` | `{operation: start\|stop\|restart\|reinstall_definition}` for an enumerated owned service. |
| `reinstall` | `{installation: main\|recovery, source_root}` for the instance. |
| `quarantine` | `{}` for an exact corrupt owned artifact; choose a unique sibling destination and retain that planned path before moving. |
| `beads_update` | `{fields}` for one exact bead, using native update fields; this is scoped break-glass and must reconcile reserved metadata afterward. |
| `git` | `{argv:[literal arguments]}` for an exact recorded repository/worktree; never a shell string. |

All repairs except best-effort read/resource release require verifiable takeover
or human authority. None authorizes editing `fulcrum.yaml` as Justiciar. Essential
no-ledger service/reinstall repair returns actual effects without claiming a
receipt; it never expands into multi-resource reset. Platform permissions still
apply to every native/shell action.

## Source notes

Beads CLI/source was inspected at installed build `6c124203e771` (`bd version`
reported 1.2.2). This identifies evidence, not a new Fulcrum version pin or a promise
that future Beads interfaces are unchanged. Relevant stock implementation:
[create](https://github.com/steveyegge/beads/blob/6c124203e771433a3550c348771a5b5e27fd3c21/cmd/bd/create.go),
[update and metadata merge](https://github.com/steveyegge/beads/blob/6c124203e771433a3550c348771a5b5e27fd3c21/cmd/bd/update.go),
[configuration and actor precedence](https://github.com/steveyegge/beads/blob/6c124203e771433a3550c348771a5b5e27fd3c21/internal/config/config.go),
and [formula types](https://github.com/steveyegge/beads/blob/6c124203e771433a3550c348771a5b5e27fd3c21/internal/formula/types.go).
Native lifecycle claims use the [official Codex app-server documentation](https://learn.chatgpt.com/docs/app-server).
Tollgate mappings were checked against its local CLI and Fulcrum's current
adapter; implement response normalization against observed provider facts, not
the old adapter's assumptions.
