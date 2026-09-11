# Compatibility and prerequisites

Evidence date: 2026-09-11 (America/Los_Angeles)

This report records the bootstrap interfaces used by the first Fulcrum
implementation. It contains no credentials or production task data. Re-run the
probes after upgrading any listed tool; an installed executable alone is not a
compatibility guarantee.

## Selected versions

| Component | Selected baseline | Evidence and constraint |
| --- | --- | --- |
| Python | CPython 3.12.14 | Installed and used to create an isolated virtual environment successfully. Fulcrum supports Python `>=3.12,<3.13` initially. |
| Pyre | `pyre-check==0.10.0` | Installed into that Python 3.12 environment and `pyre --version` completed. Task 02 must also run it against Fulcrum's actual sources. |
| Black | `black==26.5.1` | Installed into the same environment and reported CPython 3.12.14. |
| Beads | `bd` 1.2.2 (`6c124203e771`) | Installed CLI. The brain opens successfully in embedded mode and the server, backup, migration, dependency, sync, and Dolt commands below were inspected from this exact build. |
| Dolt | 2.3.2 | Homebrew's available stable version. It is not installed at this checkpoint. Task 05 owns installation and a real Beads server-mode migration probe before this pairing is considered operational. |
| Node.js | 24.19.0 | Installed; satisfies beads-ui's declared `node >=22` engine. |
| beads-ui | 0.12.6, upstream `b1d519a1bb7cf0a133c64dd999d74f0d02490c00` | Upstream retains Express 5.2.1 and lit-html 3.3.1. On Node 24, `npm ci` succeeded and all 384 tests passed. A read-only launch against the current brain returned `GET /healthz` as `{"ok":true}` with `bd` 1.2.2. Dashboard import remains deferred until Task 21. |
| Tollgate | `tg` 0.1.0 | Installed CLI. Repository registration, worktree, candidate, approval, queue, diagnosis, pause/resume, push, and configuration help were inspected. |
| Codex CLI | 0.153.4 | Installed with task, app-server, model selection, hook, and remote transport support described below. |

The upstream beads-ui baseline is [mantoni/beads-ui 0.12.6](https://github.com/mantoni/beads-ui/blob/b1d519a1bb7cf0a133c64dd999d74f0d02490c00/package.json).
Its unit suite and health check establish a suitable import baseline, not full
Fulcrum Dashboard compatibility. Task 21 owns the import and Task 22 owns its
read-model integration.

## Observed installation state

- Fulcrum is a clean `master` checkout of
  `git@github.com:thurn/fulcrum.git`. Before this report its HEAD was
  `4e47918f9bb159e046f5fef1dc76cf395edab727` and matched `origin/master`.
- Saved Codex projects resolve to stable local IDs:
  Fulcrum `6cabdf2e-d919-4a9f-a329-b73237a49e97`, Tollgate
  `0a9a3915-3530-4904-ae4a-1e56f6175487`, and Battlement
  `b7695810-dfa7-4dad-bd90-8311e615e77b`.
- Tollgate registers Tollgate (`01a0267b-b143-7a50-ba6c-33e6e55c08ff`),
  Battlement (`01a0348b-7538-7bb1-a666-65ac689f3716`), and the unrelated
  Quest Prototype repository. Fulcrum is not registered yet. Battlement reports
  `configuration-pending`; Task 02 must not change another project's policy.
- The brain is `/Users/dthurn/brain`, a clean Git checkout whose private source
  remote is `git@github.com:thurn/brain.git`. Beads resolves
  `/Users/dthurn/brain/.beads`, prefix/database `brain`, with an embedded store
  at `.beads/embeddeddolt`. It currently contains zero issues and its configured
  Dolt sync remote is the corresponding private Git SSH URL.
- Project instructions require every Fulcrum repository change to be committed
  immediately with a Conventional Commit and pushed. The numbered plan further
  requires each task to be a separate pushed commit.

Absolute home paths above are audit evidence only. Installed code and skills
must obtain them from configuration and never embed `/Users/dthurn`.

## Supported Beads interfaces

All commands must be scoped to the brain with `--directory <brain>` (or `-C`):

```text
bd --directory <brain> where
bd --directory <brain> status [--json]
bd --directory <brain> context [--json]
bd --directory <brain> dolt show|test|status|start|stop
bd --directory <brain> dolt set database|host|port|user|data-dir <value>
bd --directory <brain> dolt remote list|add|remove
bd --directory <brain> dolt commit|pull|push
bd --directory <brain> backup init|status|sync|restore
bd --directory <brain> migrate --inspect|--dry-run
bd --directory <brain> migrate schema
bd --directory <brain> dep add|list|remove|tree|cycles
```

Beads 1.2.2 documents the local server as a `dolt sql-server`. For a
Beads-managed local server, `bd dolt start` records PID, derived port, log, and
data directory beneath `.beads`; `status` reports those values; `stop` performs
a graceful shutdown, and the next client operation automatically starts the
server again. Embedded mode instead runs Dolt in the client process. An
externally managed host can be tested, but Fulcrum does not create another
supervisor. Local automatic startup requires loopback TCP, not a Unix socket.

`bd backup` is the supported full-history backup facility. The unrelated
`bd restore <issue-id>` command restores compacted issue content and must not be
used as database recovery. `bd export` is an interchange fallback, not the
primary migration backup.

Task 05 must take and verify a backup before changing the current embedded
store, inspect the automatically selected port before use, run pre/post
migration diagnostics, and prove stop/automatic-restart/recovery in a
disposable fixture. No inspection command in Task 01 changed the database.

## Supported Tollgate interfaces

Use stable JSON output and suppress UI launches in automation:

```text
tg repo list --json --no-launch
tg init <path> --run <command> --json --no-launch
tg --repository <id> config validate|explain --json --no-launch
tg --repository <id> worktree create|remove --json --no-launch
tg --repository <id> candidate <revision> [--wait] --json --no-launch
tg --repository <id> approve <candidate> --json --no-launch
tg --repository <id> status|queue|wait --json --no-launch
tg --repository <id> diagnose <candidate> --json --no-launch
tg --repository <id> pause|resume --json --no-launch
tg --repository <id> push --json --no-launch
```

Task 02 starts from this report's clean pushed commit, registers Fulcrum with
`scripts/check`, and records the expected bootstrap failure/unvalidated state
because that script does not exist at the registration anchor. A later clean
worktree commit must pass normal validation before being called certified. A
repository registration is not a certificate.

## Codex task and hook capabilities

The installed desktop integration exposes these separate contracts:

| Capability | Supported interface | Required? |
| --- | --- | --- |
| Resolve projects | `list_projects` returns `projectId`, path, host, and whether the path is a Git repository. | Required for dispatch setup. |
| Create work | `create_thread` accepts a project/projectless target, local or worktree environment, optional exact model/reasoning, title, and prompt; it returns a task ID or a queued client ID. | Required for dispatch. |
| Select models | Task creation and follow-up accept only listed model/reasoning combinations; preserve returned task/host IDs rather than inferring them from titles. | Required for authorized dispatch. |
| List and identify | `list_threads` returns task ID, host, status, project context, title, and retrieval summary. | Required for identity recovery. |
| Message | `send_message_to_thread` accepts exact task ID, optional host, prompt, and optional model/reasoning override. | Required for handoffs. |
| Read and wait | `read_thread` reads recent turns; `wait_threads` waits on exact task IDs and returns cursors/status. | Required for coordination; not transcript scraping. |
| Archive | `set_thread_archived` archives or restores an exact task. | Required for lifecycle cleanup. |
| Scheduled wakes | Desktop `automation_update` supports an in-thread heartbeat or a standalone project cron task. | Required only when recurring work is enabled. |
| Observe existing desktop work | Task listing/reading and, during active voice only, foreground screen capture can observe some runtime state. Coverage is partial and availability may change. | Optional observation; never a dispatch prerequisite. |
| Hooks | `SessionStart` can match source `compact` and add bounded context; `Stop` receives `stop_hook_active` and can request one continuation with `decision: block`. | Planned, but delivery must be tested in Task 14. |
| Remote viewing | The CLI accepts authenticated `ws://`/`wss://` app-server transports, but no non-loopback endpoint or remote authentication is configured here. | Optional and unsupported in this baseline. |

Official OpenAI documentation confirms that hooks are enabled by default,
`SessionStart` after automatic compaction runs before the immediate continuation,
and `Stop` has no matcher and can continue a turn once. See
[Hooks](https://learn.chatgpt.com/docs/hooks) and
[Scheduled tasks](https://learn.chatgpt.com/docs/automations?surface=app).
The installed CLI and desktop tool schemas are the authority for the exact
fields recorded above.

Desktop hook loading and delivery were not changed or exercised during this
read-only audit. Until Task 14 verifies real `SessionStart` and `Stop` events,
skills and persisted progress—not hooks—remain the workflow protection.

## Setup checklist and owned follow-up probes

- [x] Clean Fulcrum repository and GitHub remote verified.
- [x] Fulcrum, Tollgate, and Battlement saved Codex project IDs resolved without
  creating duplicates.
- [x] Tollgate registrations inspected; Fulcrum absence and Battlement's
  unrelated configuration warning recorded.
- [x] Existing brain Git state, Beads backend, issue count, and private remotes
  inspected without mutation.
- [x] Python/Pyre/Black and Node/beads-ui baselines exercised in disposable
  environments.
- [ ] **Task 02:** register Fulcrum, create `scripts/check`, prove that the
  bootstrap anchor cannot pass, and obtain a normal passing Tollgate result for
  the package scaffold.
- [ ] **Task 05:** install Dolt 2.3.2, verify it with Beads 1.2.2, back up and
  migrate the brain, then exercise two-client visibility and controlled
  recovery. If the pairing fails, stop before migration and select a version
  from Beads' own diagnostics rather than guessing.
- [ ] **Task 14:** install the hook source through Codex review/trust and verify
  real desktop delivery, bounded output, Plan-mode suppression, and latency.
- [ ] **Tasks 21–22:** import the pinned beads-ui source, rerun its Node checks,
  and validate every Fulcrum read model against the migrated Beads CLI.
- [ ] **Task 29:** validate optional remote viewing with authentication before
  presenting it as available. No remote endpoint is configured now.
