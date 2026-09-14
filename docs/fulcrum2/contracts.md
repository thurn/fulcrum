# Fulcrum contracts

This is the normative implementation contract for the replacement described in
[design.md](design.md). Commands shown as `fulcrum ...` are to be implemented.
Examples using `bd` describe stock Beads capabilities inspected during design.
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
<instance>/controller.lock     OS advisory lock; contents are not workflow truth
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
| `--claim TOKEN` | Ownership acquisition token for owner-restricted mutations |
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
task, an omitted claim may be obtained from its current Beads task record after
checking the native task ID; a terminal can always pass the claim explicitly.

This is a trusted local-user system, not an adversarial multi-tenant permission
boundary. Explicit human/operator commands are allowed from the terminal. Role
checks and claim tokens prevent accidental stale writes; they do not pretend to
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

When the caller omits a request ID, generate it before contacting the controller
and print it to stderr so a lost response is still retryable. Structured callers
should supply it themselves. Normal stdout stays one parseable JSON document.

## 2. Complete command surface

All mutation commands in this document use operation receipts and the common options unless
the bootstrap/reset exception is expressly documented. All listing commands
accept `--limit N` (default 20, `0` means all) and `--cursor CURSOR` and return
`items` plus `next_cursor`. File/log data is paginated by count or byte limit.

### Installation, projects, and service

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
error, not permission to kill that process. Test setup chooses unused ports and
records them in its own config. `kind` is `codex` or `deterministic` for runtime,
`tollgate` or `deterministic` for delivery.

Project preparation and validation are argument arrays, never arbitrary shell
fragments generated from a bead. For a shell-based repository check, configure
`["/bin/sh", "path/to/checked-in-script"]` explicitly. Empty arrays mean no
project-specific command. Setup writes absolute executable paths for services.

### Role entry and work

| Command | Behavior |
| --- | --- |
| `enter ROLE --description TEXT [--bead ID] [--origin human|dispatch]` | Create/adopt work and bind the current explicit task, or create a native task when none is supplied; default origin human |
| `leader show vizier|marshal` | Native ID, fixed title, and control-bead reference |
| `leader replace ROLE --reason TEXT` | Explicitly replace broken leadership, invalidate old leadership claims, retain policy in `fulcrum.yaml` |
| `context [--bead ID] [--role ROLE]` | Current bead, compiled instructions, ownership, actionable evidence, and pending operation references |
| `work create --input FILE` | Create an outcome or graph; returns root and child IDs |
| `work show ID` / `work list` | Work-only view, excluding control/operation issues; filters `--project`, `--role`, `--owner`, `--phase` |
| `work adopt ID --role ROLE` | Explicit adoption; existing owner must consent/stop or caller must use takeover authority |
| `work update ID --input FILE` | Update outcome, acceptance, summary, or annotations; active substantive scope changes are made visible to the owner |
| `work transfer ID --to-thread ID --role ROLE --reason TEXT` | Controlled ownership transfer; refuse to run a second writer while the old turn is active |
| `work children ID` | Deliverable children and dependency progress |
| `work close ID --outcome OUTCOME --summary TEXT` | Terminal disposition; ordinary implementation requires observed delivery, while answered/rejected/duplicate work does not |
| `work reopen ID --reason TEXT [--role ROLE]` | Explicit new ownership cycle on the same ID; invalidate old claims and return to Marshal unless human entry starts the specified role |
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
`role`, `claim_token`, `instructions`, and any outstanding operation ID.

One non-leadership task actively executes one work bead at a time. Entry into a
different bead checkpoints its prior unfinished work and returns that work to
Marshal before adopting the new one. Preserve the actual source/worktree and
stop old helpers before new conflicting edits. Leadership may steward many
backlog beads while handling one request turn. This does not create a worker pool
that reuses an unrelated task's conversation history automatically.

Scoped Sage/Mason entry on a closed bead is an explicit investigation on the same
ID. Save the prior terminal disposition in `interrupted_work`, assign a fresh
claim, and open only the investigation. On finish, attach the findings receipt
and restore the prior terminal disposition unless the human explicitly requested
new implementation. Do not change “delivered” into “findings” or redispatch code
that already shipped. Entry on unfinished Warden work returns it to Warden review
through Marshal afterward, not to a new Executor implementation.

`work create` accepts `title`, `outcome`, optional `project`, `acceptance` (array of
strings), `requested_role` (default weaver for incomplete requests, executor when
explicitly supplied), `priority` (0–4, default 2), `context` (array of references),
optional `models` keyed by role to `{model, effort}`, and optional `children`. A graph child has a caller-local `key` and the same task
fields; `depends_on` contains child keys or existing bead IDs. Persist the planned
ID mapping on the creation receipt before writing children. Reject cycles before
mutation; a crash midway resumes missing children and dependencies under the same
receipt. Parent/children become dispatchable only after graph completion.

`work update` accepts any subset of `title`, `outcome`, `acceptance`, `summary`,
`size` (`small|medium|large|unknown`), `overlap_tags`, `context`, and `models`. A scope change
invalidates approval for the previous source/scope combination; it does not modify
an already completed delivery. `progress` accepts `kind` (`investigation|source|
validation|blocker`), `summary`, and `evidence` references.

`report` accepts `title`, `problem`, `observed_evidence`, `required_change`,
`acceptance_checks`, optional `project`, `discovered_from`, and `context`. The
request ID is the report retry identity. It returns the actual bead and filing
state independently of subsequent dispatch.

### Marshal, policy, and human intervention

| Command | Behavior |
| --- | --- |
| `policy show` | Read the policy section of `fulcrum.yaml` and its rationale |
| `policy set --input FILE` | Human/Vizier-only edit of the YAML policy section through the same config operation |
| `backlog list [--ready] [--include-deferred]` | Compact work summaries, dependency/wait reasons, and priority |
| `marshal brief [--bead ID]` | Exact default decision input plus omitted counts and continuation commands |
| `marshal decide --input FILE` | Apply independent decisions after checking their claim/phase expectations |
| `dispatch --bead ID` | Execute an already-authorized dispatch; `--human` explicitly authorizes immediate operator dispatch |
| `human list` | Work assigned HUMAN and the exact action needed |
| `human resolve ID --input FILE` | Record `answer`, optional `scope_change`, and `resume_role`; return ownership to Marshal for continuation |

YAML policy fields are `automatic_capacity`, `default_project_capacity`,
`project_capacity` (override map),
`reserved_recovery_slots`, `worker_helper_limit` (null by default), `paused_projects`,
`suspended_rules` (map from named rule to scope/reason), and `rationale`.
Model, policy, and timing/log defaults all live in the same YAML file, not Beads
or a second config copy. Marshal can use fewer slots without changing these values.
Every suspension has an explicit scope and can be cleared through `policy set`.
No automatic expiry schedules a new task. Vizier/human alone may grant broad rule
suspensions; Justiciar's current takeover grants its already-defined local powers
but never authority to modify `fulcrum.yaml`.

A decision contains `bead_id`, `expected_claim`, `expected_phase`, `action`,
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
| Weaver `ready` | `summary`, completed scope/acceptance on bead; Marshal backlog |
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
for native terminal evidence. The old claim cannot submit another different
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
| `recover release --scope SCOPE --summary TEXT` | Reconcile facts, invalidate takeover claim, and restore ordinary ownership/leadership |

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
    "claim": "22222222-2222-4222-8222-222222222222",
    "phase": "working",
    "requested_role": "executor",
    "origin": {"thread_id": "01a-example-weaver", "request_id": null, "bead_id": null},
    "workflow_root": "fc-51o",
    "caused_by": null,
    "models": {},
    "plan": null,
    "summary": "Return correctly ordered values without quadratic behavior.",
    "outcome": "Optimize sorting while preserving documented ordering semantics.",
    "acceptance": ["Ordering tests pass", "Representative large input avoids quadratic growth"],
    "context": [],
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
claim, current source/evidence, and receipt. `interrupted_work` stores the original
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
   Marshal), `phase=backlog`, and a new claim in one regular `bd update`. Set the
   current transition ID in that update and read back the postcondition.
3. When dispatch is authorized, create/identify the destination task using a
   recorded operation. Preserve old ownership while provisioning.
4. If an old worker exists, stop and observe its active turn/helpers before
   transferring write responsibility. Validate any in-flight delivery first.
5. Atomically update assignee, `fc.owner`, role, claim, phase, and handoff context.
   Read back. Then start the destination turn with that exact claim.

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
authorized Fulcrum writer and stale-claim rejection through its CLI, with explicit
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
| done | closed | Historical last owner; explicit reopen must establish new scope/owner/claim |

`work close` accepts `answered`, `delivered`, `reduced_scope`, `findings`,
`rejected`, `duplicate`, or `cancelled`. Disposition is `{outcome, summary,
waived_requirements, known_defects, canonical_bead, completed_at}` with unused
fields empty/null. Reopening through native `bd reopen` becomes a Marshal intake
request and invalidates the old claim; it never silently reuses an old active turn.

### Installation and task records

`fc-system.fc` holds `kind=control`, effective owner, `vizier_thread`,
`marshal_thread`, `active_takeover`, and
`last_transition`. No task status, full backlog, or authoritative model/policy
configuration is copied into it. Effective standing policy is read from YAML.

Each task record holds `kind=task`, `owner` (its native task ID), `thread_id`, `role`, `work_bead`, `claim`,
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
    "claim": "22222222-2222-4222-8222-222222222222",
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
    "title": {"required": true},
    "outcome": {"required": true},
    "project": {"required": true},
    "workspace": {"required": true},
    "acceptance": {"required": true},
    "current_evidence": {"default": "No prior evidence."},
    "bead": {"required": true}
  },
  "steps": [{
    "id": "work",
    "title": "{{title}}",
    "description": "## Outcome\n{{outcome}}\n\n## Project and workspace\n{{project}}\n{{workspace}}\n\n## Acceptance\n{{acceptance}}\n\n## Current evidence\n{{current_evidence}}\n\n## Next action\nImplement the outcome in the assigned worktree, run relevant checks, and commit. Before editing, acquire this bead through fulcrum enter executor --bead {{bead}} --description 'Implement assigned task'.\n\n## Finish\nRun fulcrum finish --bead {{bead}} --outcome ready_for_review --input - with summary, source_oid, checks, and evidence as JSON. Once accepted, stop implementation. Report incidental issues with fulcrum report."
  }]
}
```

Invoke `bd -C <ledger> cook <asset> --var key=value ...`; parse `steps` and require
one `id=work` step. This was verified read-only against the installed stock binary.
The normal command currently requires an accessible Beads workspace even for
compilation, which is why degraded entry uses packaged static instructions.

Role entry is idempotent for the same task/bead/role: the context's instruction to
enter does not create another claim or another worktree. A new invocation with a
different role performs the specified transition. After entry, the fixed worker
prompt is:

```text
Work on bead <ID>. Read it with bd show <ID> and follow its current instructions.
Use fulcrum context --bead <ID> if current ownership or evidence is unclear.
[Fulcrum operation <OPERATION_ID>; claim <CLAIM_TOKEN>]
```

The last line is a literal correlation marker retained with the sent input, not
a secret or a prompt template hierarchy. Marshal uses its compact brief instead
of the worker body. Each microskill runs `fulcrum enter <role> --description ...`
with optional explicit bead; metadata disables implicit invocation. `$bead` runs
`fulcrum report` and does not call enter.

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
ID, title, cwd, archived/existence facts, active turn, loaded status, known helpers,
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

The reset receipt records exact old instance roots, managed native IDs, descendant
IDs, worktrees/branches, outstanding provider handles, and the completed step for
each. For the old implementation only, inventory its read-only operational store
to enumerate ownership; this is deletion inventory, not migration. Do not use
role-looking titles alone to classify unrelated user tasks as owned. New-system
inventory comes from its task/operation/work records. Capture config needed to
bootstrap as configuration, not retained historical work.

Sequence: obtain exclusive service control; stop old dispatch; observe terminated
managed tasks/helpers; cancel or reconcile active provider work; delete managed
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

## 7. CLI-driven scenarios

These are executable acceptance examples for the future CLI. Use `jq` only to
extract returned identifiers. The scenario suite invokes the installed executable
as a subprocess; it does not import controller internals or patch `_request`.

### Isolated setup and intake through promotion

```sh
export FC_INSTANCE="$(mktemp -d /tmp/fulcrum-cli.XXXXXX)"
fulcrum --instance "$FC_INSTANCE" setup --input - --json <<'JSON'
{"runtime":{"kind":"deterministic"},"delivery":{"kind":"deterministic"},"beads":{"database":"fulcrum"},"brain":{"root":"/tmp/fulcrum-sample-brain","remote":"origin","branch":"main","push_interval_seconds":300},"projects":[{"id":"sample","root":"/tmp/fulcrum-sample","codex_project_id":"sample","delivery":{"kind":"deterministic"},"integration_branch":"main","prepare_argv":[],"validate_argv":[]}]}
JSON

created=$(fulcrum --instance "$FC_INSTANCE" work create --json --input - <<'JSON'
{"project":"sample","title":"Fix ordering","outcome":"Return values in ascending order.","acceptance":["Ordering tests pass."],"requested_role":"executor"}
JSON
)
bead=$(printf '%s' "$created" | jq -r '.result.bead_id')
entry=$(fulcrum --instance "$FC_INSTANCE" enter executor --bead "$bead" \
  --description 'Fix ordering' --project sample --json --wait)
thread=$(printf '%s' "$entry" | jq -r '.result.thread_id')
claim=$(printf '%s' "$entry" | jq -r '.result.claim_token')
fulcrum --instance "$FC_INSTANCE" scenario run delivery-happy --bead "$bead" --json
fulcrum --instance "$FC_INSTANCE" wait --bead "$bead" --until closed --timeout 30 --json
fulcrum --instance "$FC_INSTANCE" trace --bead "$bead" --json
```

The test harness creates `/tmp/fulcrum-sample` as a disposable committed Git
fixture before setup; it also creates `/tmp/fulcrum-sample-brain` with an `origin`
pointing to a disposable Git remote served through the supported Git transport.
The test harness supplies this endpoint and its isolated credentials. These fixture paths must be reserved
for this isolated run; fail if they already belong to other work. No real
repository or production remote is reused. Test setup
provisions an isolated **real stock Beads** backend on a free local port.
Deterministic providers use native-looking opaque IDs only inside that instance.
They cannot attach to the production runtime or integration branch.

### Script the same role and delivery operations directly

Given a source commit in the prepared fixture worktree, these commands exercise
the same public operations as an agent. Obtain `source` with `git rev-parse HEAD`
in that worktree; the source below is a shell variable, not an invented OID.

```sh
fulcrum --instance "$FC_INSTANCE" finish --bead "$bead" \
  --thread-id "$thread" --claim "$claim" --outcome ready_for_review --json \
  --input - <<JSON
{"summary":"Ordering fixed.","source_oid":"$source","checks":[{"name":"ordering","status":"passed","evidence":"fixture test output"}],"evidence":[]}
JSON
fulcrum --instance "$FC_INSTANCE" reconcile --bead "$bead" --json
warden=$(fulcrum --instance "$FC_INSTANCE" work show "$bead" --json | jq -r '.result.fc.owner')
wclaim=$(fulcrum --instance "$FC_INSTANCE" work show "$bead" --json | jq -r '.result.fc.claim')
fulcrum --instance "$FC_INSTANCE" review approve --bead "$bead" \
  --thread-id "$warden" --claim "$wclaim" --source "$source" --summary 'Reviewed current source' --json
fulcrum --instance "$FC_INSTANCE" promotion start --bead "$bead" \
  --thread-id "$warden" --claim "$wclaim" --source "$source" --json
```

For deterministic mode, terminal-turn events and provider completion are supplied
through `scenario emit`, below. In native mode, the adapter observes real turns
and provider state. The examples are separate flows: do not close a bead in the
first example and then reuse it as open work for the second.

### Role transition, uncertain delivery, and HUMAN resolution

```sh
fulcrum --instance "$FC_INSTANCE" enter sage --bead "$bead" \
  --thread-id "$thread" --description 'Inspect workflow failures' --json
fulcrum --instance "$FC_INSTANCE" context --bead "$bead" --json

fulcrum --instance "$FC_INSTANCE" operation reconcile "$promotion_op" --json
fulcrum --instance "$FC_INSTANCE" promotion show --bead "$bead" --json

fulcrum --instance "$FC_INSTANCE" human list --json
fulcrum --instance "$FC_INSTANCE" human resolve "$blocked_bead" --input - --json <<'JSON'
{"answer":"The unavailable external service is restored.","resume_role":"warden"}
JSON
```

Each command uses IDs from the corresponding scenario's prior output, not IDs
hardcoded from a live installation. The integration tests supply an unfinished
Executor for the role-transition case, an uncertain promotion receipt for the
second case, and a genuinely HUMAN-owned bead for the third.

### Deterministic scenario controls

These commands are enabled only when the instance explicitly selects deterministic
adapters. They inject **external-provider events/facts**, never arbitrary work-bead
state or privileged transition bypasses.

| Command | Contract |
| --- | --- |
| `scenario list` | Show supported named scenarios |
| `scenario run NAME [--bead ID]` | Execute a small scripted sequence using public application commands and deterministic provider events |
| `scenario emit --input FILE` | Inject `{provider, event, target, data}` into the configured deterministic adapter |
| `scenario advance --seconds N` | Advance the injected test clock so timeout/archive behavior needs no real sleep |
| `scenario fault --input FILE` | Set `{provider, method, occurrence, effect, response}` on the deterministic provider |
| `smoke concurrency --workers 30 --model gpt-5.6-luna --effort low --timeout 600` | Explicit real-runtime smoke; requires an isolated native test instance |

Events are `runtime.turn_completed`, `runtime.turn_failed`,
`runtime.input_requested`, `runtime.unarchived`, `delivery.validation_passed`,
`delivery.validation_failed`, and `delivery.promoted`. `target` is the actual task
or provider handle returned by the fake adapter. Fault responses are `timeout`,
`disconnect`, `reject`, or `malformed`; `effect` is `applied` or `not_applied`.
Provider observations after an applied-but-lost response expose the real fake
side effect so production reconciliation can discover it. Deterministic adapter
state may persist as **external test-provider state** across controller restarts;
it is not an additional Fulcrum workflow ledger.

Named scenarios: `delivery-happy`, `retry-same-request`, `handoff-crash`,
`promotion-response-lost`, `controller-restart`, `archive-once`, `role-transition`,
and `human-resolution`. A scenario returns assertions and trace references. Keep
the implementation small; this is not a general simulation language.

The concurrency smoke starts 30 distinct native tasks through the normal adapter,
with a shared start barrier to observe overlapping active turns. Each performs one
trivial shell operation in a disposable fixture and finishes. Observe native
start/completion and release Fulcrum subscriptions. The whole command is capped
at ten minutes; on timeout, interrupt only its own tasks and report incomplete
coverage. Delete its disposable artifacts through the same cleanup operations.
Do not wait 30 minutes for the runtime's unload grace or turn this into a repeated
soak suite. An explicit smoke command authorizes its finite native model calls;
ordinary automated tests make none.

## 8. Contract acceptance

Implementation is ready when every command family in this document works from a terminal,
structured results are consistent, and the compact scenarios prove these
behaviors: exact retries do not duplicate work; old claims cannot finish new
ownership; destination work waits for old writers to stop; successful external
effects survive response loss/restart; manual unarchive remains visible; degraded
investigation returns usable evidence; and a complete CLI-driven delivery closes
the correct bead. Keep provider integration probes proportionate and use real
stock Beads for the core scenarios. Do not replace this with snapshot tests of
generated prompts or a large manual validation program.

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
| `plan activate ID` | Human/Vizier/Marshal-authorized activation of a future plan; clears only its future deferral |
| `memory list [--scope global\|project:ID\|role:ROLE]` | List bounded curated memory records |
| `memory show ID` | Canonical text, scope, owner, and update attribution |
| `memory set [--id ID] --input FILE` | Create/replace `{scope, title, text, references}`; retain previous text in the mutation receipt |
| `knowledge publish --bead ID` | Export current plan/memory to configured Git destination and inspect synchronization |
| `ledger sync` | Commit and push pending Beads history to the brain Git remote immediately; return observed publication results |
| `ledger status` | Local commit, last pushed state/time, pending age, cadence, operation ID, and any remote error |

`plan publish/refine` input is `{text, activation, approved_by, approval_evidence,
reviews, tasks, publication}`. Activation is `active` or `future`; `approved_by`
is `human` or an explicitly authorized leader task. The evidence references an
actual approval message or explicit CLI action; an agent must not invent it.
`reviews` has `cold_reader` and `requirements` entries, each `{thread_id, findings,
resolution, state}` with state `complete` or `waived`. Waived entries require
`waiver: {actor, reason}` from human/Vizier/Justiciar. Continued authoring never
requires successful helper creation; an unpublished draft remains available in
its bead. Review helpers use the ordinary runtime and count toward capacity.

`tasks` uses the `work create` graph schema with stable keys. `publication` is
null or `{destination: knowledge|project, relative_path, require_remote_sync}`.
An absent publication destination retains the complete plan solely in Beads;
substantial plans default to the configured knowledge destination when available.
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
`project`, `operation_id`, `parent_turn`, `helper_call_id`, `attributions`, `model`, `effort`,
`usage`, `response_records`, `coverage`, and `missing_reasons`. A response record
has its native item ID (or a recorded observation ordinal when absent), configured
and effective model, service tier, disjoint token categories, tools, rate-card
reference, decimal priced components, and coverage. Store terminal totals and
price-bearing response facts, not every streaming token sample. If response
records exceed 64 entries, place each successive block in a linked analytics
record with its block ordinal; this bounds individual Beads updates. All records
remain stock issues, queried through the ledger adapter without SQL tables.

`attributions` lists `{workflow_root, operation_id, weight, reason}`. Ordinary
work and helpers inherit one root with weight 1. A Marshal decision batch spanning
several roots allocates its turn equally across the distinct roots explicitly in
the recorded decision input; record that allocation as an estimate, not observed
per-bead token usage. Rows with no causal work remain leadership overhead.
Weights for a turn sum to 1, so global totals count every native turn once.
Reports expose direct/helper/coordination components and unallocated overhead.
This accounting must not start extra Marshal turns to simplify attribution.

Duplicate cumulative usage observations replace the same observation; sum unique
final native turns, never successive cumulative totals. Track discovered helper
turns through explicit parent turn and helper-call identity, including nested and
late-finishing helpers. Repeated helper calls remain distinct; unrelated native
tasks are excluded. Native model-reroute events affect the next response only:
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
unless complete), `currency`, direct/helper/coordination breakdowns, counts, and named
missing/excluded contributions.
Never present a partial subtotal as the full cost or an actual subscription bill.

Freeze a workflow summary when its work/plan and all observed causally included
turns/helpers are terminal. Persist the included ID set, exclusions, coverage,
completion boundary, totals, and rate references in an analytics record; no
Marshal turn is needed just to close or price work. If observation is unavailable,
freeze with the gap recorded rather than blocking delivery. Later recovery adds
an explicit correction record referencing the frozen result, and queries show
both the original and corrected total; do not silently rewrite historical facts.
A reopened bead starts a new recorded completion interval under the same ID.

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
| `runtime launch-desktop` | Start configured Desktop with `CODEX_APP_SERVER_WS_URL`; never terminate another runtime |
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
for that ownership claim. Send only when the task is idle, with the original
scope and decision input unchanged. If another user turn arrived, wait for its
safe boundary; do not inject a competing start. Repeated omission becomes a
recovery reason. A human's normal question-answering conversation is not required
to issue an implementation finish when it has no such active responsibility.

Fleet replacement records the exact selected task/helper inventory, old-to-new
mapping, pending claims, and replacements on one receipt. Drain has no implicit
switch to interrupt on client timeout. Pause admission for the selected
scope until all old writers stop; replace and transfer through normal claims.
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

The hard reset remains a separate explicit cutover: clear only enumerated old
Fulcrum Beads data on the brain remote and publish the clean native database.
Preserve the repository and unrelated documents; never resurrect old data from
a historical issue export during startup. Existing Git history is not automatically
rewritten by a normal push or this maintenance cadence. A historical purge, if
needed for the separately authorized reset inventory, must be a narrowly scoped
reset action, never inferred by ordinary synchronization.

Extend the deterministic restart/duplicate scenario with a disposable Git
remote fixture and the same stock Beads Git transport. With an injected clock, prove a
native `bd` change is pushed by the next five-minute tick, an idle tick produces
no new commit, and an applied-but-unacknowledged push is reconciled without a
duplicate operation. This requires no native model calls or wall-clock five-minute wait.

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
  automatic_capacity: 30
  default_project_capacity: 30
  project_capacity: {}
  reserved_recovery_slots: 1
  worker_helper_limit: null
  paused_projects: []
  suspended_rules: {}
  rationale: Initial human-approved installation defaults.
brain:
  root: /Users/dthurn/brain
  remote: origin
  push_interval_seconds: 300
```

A project's slot ceiling is its `policy.project_capacity[project_id]` override,
otherwise `default_project_capacity`, always constrained by global capacity and
the recovery reserve. Per-project role model defaults remain under each enrolled
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
