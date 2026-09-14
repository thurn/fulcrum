# Python control of the live Codex desktop runtime

Operative wind-down is the sole exception to ordinary no-steer delivery. It sends
`turn/steer` with `threadId` and the exact retained `expectedTurnId`, containing a
no-follow-up notice, before `turn/interrupt` for those same IDs. Accepted requests
do not prove termination: `thread/read` must show the parent and every nested
helper terminal before retirement and archive.

Status: completed experiment on 2026-09-12; all disposable tasks archived.

## Executive summary

Python can administer tasks in the same live Codex app-server runtime used by
the desktop app on this installation. The experiment proved all of the
following without UI automation, database edits, transcript edits, or a
detached worker:

- connect a Python process to the exact app-server process used by Codex
  desktop;
- create a new task and capture its actual `threadId`;
- read the task back by ID;
- set and verify its user-facing title;
- send initial and follow-up turns to that exact ID;
- select a model and reasoning effort and read the persisted values back;
- observe `active`, `completed`, `idle`, and `notLoaded` lifecycle evidence;
- archive, restore, verify identity and conversation history, and archive
  again; and
- rename the currently active desktop task from Python and confirm the update.

The successful topology was one app-server with two clients:

```text
Python JSON-RPC client ── WebSocket ──┐
                                      ├── codex app-server PID 91499
Codex desktop PID 92496 ─ WebSocket ──┘       127.0.0.1:4500
```

This topology matters. Starting a second app-server and creating a task there
would only prove control of a detached Codex worker. It would not prove control
of the task store and runtime visible to the live desktop.

The main qualification is operational: the official documentation describes
the app-server command and WebSocket transport as experimental and unsupported
for production workloads. The working setup therefore demonstrates a powerful
local integration technique, not a production-stability guarantee.

## Scope and safety constraints

The original experiment imposed these boundaries:

- all administration actions had to originate in Python through the Codex SDK
  or app-server protocol;
- agent-only task tools, UI automation, database changes, and transcript-file
  changes were excluded;
- existing user tasks and workspace source could not be changed;
- the desktop could not be restarted or reconfigured without approval;
- timeouts had to be bounded;
- turn completion had to come from runtime events rather than sleeps;
- an uncertain creation response had to be reconciled before any retry; and
- Part 1 had to stop with the disposable task unarchived for human inspection.

The user later performed the authorized desktop relaunch needed to establish a
shared listener. The two primary experiment tasks were disposable and ended
archived. The only existing task changed was the current task, which the user
explicitly asked Python to rename after the disposable experiments had ended.

## Environment

The successful run used:

| Component | Observed value |
| --- | --- |
| Host OS | macOS, Apple silicon |
| Python | 3.14.4 |
| Codex desktop | 26.908.40834 |
| Bundled Codex CLI/app-server | `codex-cli 0.154.0-alpha.6.2` |
| Codex binary | `/Applications/ChatGPT.app/Contents/Resources/codex` |
| Shared transport | WebSocket JSON-RPC over loopback TCP |
| Endpoint | `ws://127.0.0.1:4500` |
| Python package `openai-codex` | Not installed during the experiment |

The Python client used only the standard library. It implemented the WebSocket
HTTP Upgrade, masked client frames, JSON-RPC request correlation,
notifications, bounded waits, and connection shutdown directly. The lack of
an installed SDK was therefore not a blocker.

Official Codex documentation now describes a stable Python SDK named
`openai-codex`. Published SDK builds bring a pinned Codex CLI runtime and, by
default, control a local app-server. That is appropriate for independent jobs.
The experiment instead used the app-server protocol directly because the
question was specifically whether Python could join the desktop's existing
live server rather than launch its own runtime.

## Official protocol baseline

The [Codex app-server documentation][app-server] establishes the relevant
contract:

- app-server uses JSON-RPC 2.0 without a `jsonrpc` field on the wire;
- stdio uses JSONL, while TCP and Unix listeners use WebSocket framing;
- clients must send `initialize` and then the `initialized` notification before
  other methods;
- `thread/start` creates a task and returns its ID;
- `thread/read` and `thread/list` return task data and runtime status;
- `thread/name/set` updates the user-facing title and emits
  `thread/name/updated`;
- `turn/start` sends input to a specific `threadId` and can override model and
  effort;
- `turn/completed` reports a terminal turn state;
- `thread/status/changed` reports runtime changes;
- `thread/archive` and `thread/unarchive` manage persisted task history; and
- `model/list` reports the models and reasoning efforts available to the live
  server.

The documentation also says that a model or effort supplied on `turn/start`
becomes the default for later turns in that task. On the tested protocol,
`thread/read` exposed the current or latest persisted values as `model` and
`reasoningEffort`.

The [Codex SDK documentation][codex-sdk] differentiates the SDK from a custom
app-server client: the SDK is intended for programmatic coding jobs, while the
app-server interface is the lower-level choice for clients that need
authentication, history, approvals, and streamed events.

## Connection discovery

### Default desktop launch was not attachable

The initial read-only discovery correctly stopped as blocked. At that point:

- desktop version 26.903.71938 was running bundled CLI 0.153.4;
- the desktop-owned app-server used its default `stdio://` transport;
- stdin and stdout were already owned by the desktop parent;
- there was no `--listen` argument;
- the documented control socket at
  `~/.codex/app-server-control/app-server-control.sock` did not exist; and
- `~/.codex/ipc/ipc.sock` closed a standards-compliant WebSocket Upgrade
  attempt and was therefore not an app-server endpoint.

This established an important negative result: a process being named Codex or
owning a socket is not enough. The socket must accept the app-server transport
and protocol handshake. The Python SDK's normal behavior of starting a local
server would not have satisfied the shared-runtime requirement.

### `CODEX_APP_SERVER_USE_LOCAL_DAEMON` did not activate

Setting `CODEX_APP_SERVER_USE_LOCAL_DAEMON=1` did not produce a shared daemon
connection in the tested desktop build. Process inspection still showed a
private desktop child:

```text
/Applications/ChatGPT.app/Contents/Resources/codex ... app-server ...
```

Inspection of the installed desktop bundle showed that its daemon branch was
guarded by several conditions, including an empty configuration-override list.
The desktop launch supplied Codex configuration overrides for bundled app
features, so that branch was not selected. This is a build-specific
implementation observation, not a documented public compatibility guarantee.

There is also a general macOS environment distinction: a variable placed in
`.zshrc` affects interactive shells, but a GUI app started from the Dock does
not necessarily inherit it. Testing desktop environment variables requires
launching the app from a process that actually carries those variables and
then verifying the resulting process topology.

### Working listener topology

The successful setup ran the bundled app-server with an explicit loopback
listener:

```sh
/Applications/ChatGPT.app/Contents/Resources/codex \
  app-server --listen ws://127.0.0.1:4500
```

The desktop was then launched with:

```sh
CODEX_APP_SERVER_WS_URL=ws://127.0.0.1:4500 \
  /Applications/ChatGPT.app/Contents/MacOS/ChatGPT
```

Read-only process and socket inspection proved that:

- Codex PID 91499 was listening on `127.0.0.1:4500`;
- ChatGPT PID 92496 had an established TCP connection to that PID;
- the desktop had no separate private child app-server; and
- Python's WebSocket handshake to the same listener returned
  `HTTP/1.1 101 Switching Protocols`.

The discovery client then initialized successfully and called
`thread/loaded/list`, observing three loaded tasks. The full discovery took
74.231 ms; `initialize` took 0.381 ms and `thread/loaded/list` took 0.135 ms.

The `/readyz` endpoint returns an empty successful response body. Consequently,
this quiet command can appear to print nothing even when it succeeds:

```sh
curl --max-time 3 --fail --silent http://127.0.0.1:4500/readyz
```

Use headers or print the status code when testing it interactively:

```sh
curl --max-time 3 --silent --show-error \
  --write-out 'HTTP %{http_code}\n' \
  --output /dev/null \
  http://127.0.0.1:4500/readyz
```

## Python client behavior

The client implementation used these safeguards:

1. Validate that a TCP WebSocket URL targets loopback and has an explicit port.
2. Perform and verify the HTTP WebSocket Upgrade, including
   `Sec-WebSocket-Accept`.
3. Send masked WebSocket frames, as required for a client.
4. Correlate JSON-RPC responses by request ID while collecting asynchronous
   notifications.
5. Reject unexpected server-initiated requests rather than silently approving
   unattended interaction.
6. Bound connection, request, event, creation, and turn-completion waits.
7. Persist state atomically before non-idempotent actions.
8. Record the baseline task-ID set before creation.
9. If a creation response is uncertain, reconcile the current set against the
   baseline and refuse to create a replacement unless exactly one candidate is
   identifiable.
10. Before retrying a turn after an interrupted client run, read history and
    look for the exact prompt so it is not sent twice.
11. Treat `turn/completed` as the definitive completion signal and use
    `thread/read` as the reconciliation read.
12. Verify archive state with both archived and active API listings.

The implementation never started its own app-server. Supplying `--ws-url`
meant "connect to this existing endpoint," not "create a server here."

## Experiment 1: creation, title, prompt, and desktop visibility

Python performed this sequence:

1. `initialize`
2. `initialized`
3. `thread/list` to persist a creation baseline
4. `thread/start`
5. `thread/read` to confirm the returned ID
6. `thread/name/set`
7. wait for `thread/name/updated`
8. `thread/read` to verify the title
9. `turn/start` with read-only sandboxing
10. wait for `turn/completed`
11. `thread/read` with full history

The created task was:

| Field | Value |
| --- | --- |
| Title | `Python control experiment — awaiting inspection` |
| Thread ID | `01a095e4-6ab9-7103-a205-7d8e7798563d` |
| Turn ID | `01a095e4-6ece-7ab1-af43-934a4b14371c` |
| Prompt | `Do not use tools or change files. Reply only: PYTHON_INITIAL_OK.` |
| Response | `PYTHON_INITIAL_OK` |
| Turn status | `completed` |
| Task status after completion | `idle` |

Only `userMessage` and `agentMessage` items appeared in the turn. No tool or
file-change items were observed. The task remained unarchived while the client
disconnected. The user opened it in Codex desktop and verified the title and
conversation, proving shared desktop visibility rather than only API-local
existence.

### Part 1 timings

| Action | Time |
| --- | ---: |
| `thread/start` | 132.614 ms |
| first confirmation `thread/read` | 128.674 ms |
| `thread/name/set` | 472.821 ms |
| `turn/start` acknowledgment | 4.380 ms |
| `turn/start` to `turn/completed` | 1,871.822 ms |
| complete Part 1 | 3,122.946 ms |

### Schema-validation rejection before creation

The first creation request used `readOnly` for `thread/start.sandbox`. The
installed server definitively rejected it because this field expected the
hyphenated value `read-only`. No response was lost, and reconciliation found
zero new candidates, so no task had been created. The client reset its creation
state and made one corrected request.

The two similarly named fields use different encodings in this server version:

```json
{
  "thread/start": { "sandbox": "read-only" },
  "turn/start": { "sandboxPolicy": { "type": "readOnly" } }
}
```

Version-matched schemas generated by the installed CLI are the reliable source
for details like this. The CLI supports both TypeScript and JSON Schema output.

## Experiment 2: reconnect, follow up, archive, restore, and cleanup

After the user explicitly authorized Part 2, Python reconnected and loaded the
persisted thread ID. It read the retained title and initial history before
sending anything.

The follow-up was:

| Field | Value |
| --- | --- |
| Thread ID | `01a095e4-6ab9-7103-a205-7d8e7798563d` |
| Turn ID | `01a095e8-34fa-7920-89b7-2aba8ec23e8a` |
| Prompt | `Do not use tools or change files. Reply only: PYTHON_FOLLOWUP_OK.` |
| Actual response | `PYTHON_FOLLOWUP_OK.` |
| Turn status | `completed` |

The extra period is a useful distinction: transport and task targeting passed,
while exact textual instruction compliance failed. A protocol test should not
misclassify a model-output discrepancy as a message-delivery failure.

The client then:

- confirmed that the retained task contained the follow-up;
- found exactly one task with the experiment title;
- archived the task and found its ID in the archived API listing;
- restored it with `thread/unarchive`;
- confirmed the same ID and title;
- compared turn identity, count, prompts, responses, and statuses with the
  pre-archive history;
- archived it again; and
- confirmed the ID was present in the archived listing and absent from the
  active listing.

The archive requests took 7.398 ms and 5.044 ms. Restore took 655.372 ms. The
28.462-second cleanup run was dominated by three exhaustive scans through more
than 1,800 archived tasks, not by archive or restore themselves.

### Recovery after an over-broad identity check

The first Part 2 client run completed the follow-up and then stopped because it
compared the complete result of a filtered active-task listing before and after
`thread/resume`. The target no longer appeared under the original
`sourceKinds=["appServer"]` plus `cwd` filter even though an unfiltered API read
still returned the exact task and history.

Recovery did not send the prompt again. It found exactly one matching follow-up
turn, verified that its ID matched persisted state, recorded
`duplicateSendAvoided=true`, and continued with archive/restore cleanup. The
lesson is to reconcile non-idempotent operations through the retained
`threadId` and task history. A broad collection equality check is fragile in a
live UI where unrelated tasks and source classification can change.

## Experiment 3: model, effort, and runtime status

A second disposable task closed the remaining configuration and status gaps.
Python first called `model/list` against the live shared server. The observed
visible catalog included:

| Model | Default effort | Supported efforts |
| --- | --- | --- |
| `gpt-6-astra` | `medium` | low, medium, high, xhigh, max, ultra |
| `gpt-5.6-sol` | `low` | low, medium, high, xhigh, max, ultra |
| `gpt-5.6-terra` | `medium` | low, medium, high, xhigh, max, ultra |
| `gpt-5.6-luna` | `medium` | low, medium, high, xhigh, max |
| `gpt-5.5` | `medium` | low, medium, high, xhigh |
| `gpt-5.3-codex-spark` | `high` | low, medium, high, xhigh |

The test selected non-default model `gpt-5.6-luna` and explicitly selected
effort `low`. `thread/start` received the model. The first `turn/start` received
both model and effort:

```json
{
  "method": "turn/start",
  "params": {
    "threadId": "01a095f1-f8bd-7270-b785-52e9172a8f39",
    "input": [{
      "type": "text",
      "text": "Do not use tools or change files. Reply only: STATUS_MODEL_EFFORT_OK"
    }],
    "approvalPolicy": "never",
    "sandboxPolicy": { "type": "readOnly" },
    "model": "gpt-5.6-luna",
    "effort": "low"
  }
}
```

The test task was:

| Field | Value |
| --- | --- |
| Title | `Python status/model/effort verification` |
| Thread ID | `01a095f1-f8bd-7270-b785-52e9172a8f39` |
| Turn ID | `01a095f1-fc54-7653-9bec-0cc293c79898` |
| Response | `STATUS_MODEL_EFFORT_OK` |
| Final state | archived |

Before the turn, `thread/read` reported model `gpt-5.6-luna`, reasoning effort
`high`, and task status `idle`. The pre-turn `high` came from the effective
configuration rather than the catalog's `medium` suggestion, demonstrating
that catalog defaults and runtime defaults are distinct. After the explicit
turn override, a fresh `thread/read` reported model `gpt-5.6-luna`, reasoning
effort `low`, and status `idle`. This proves the requested effort was persisted;
it was not merely accepted syntactically.

The runtime event sequence included:

```text
turn/start response:       inProgress
thread/status/changed:     active, activeFlags=[]
turn/completed:            completed
thread/status/changed:     idle
archive transition:        notLoaded
thread/archived:           emitted
```

The `idle` notification arrived immediately before `turn/completed` in the
observed stream. Clients must correlate events by `threadId` and `turnId`
rather than assuming a fixed cross-method notification order.

### Model/status experiment timings

| Action | Time |
| --- | ---: |
| `model/list` | 0.729 ms |
| `thread/start` | 121.180 ms |
| `thread/name/set` | 471.240 ms |
| first `thread/read` | 326.673 ms |
| `turn/start` acknowledgment | 5.451 ms |
| `turn/start` to `idle` | 2,962.737 ms |
| final `thread/read` | 2.001 ms |
| `thread/archive` | 10.931 ms |
| complete experiment | 5,950.129 ms |

## Experiment 4: rename the current task from Python

The runtime exported the current task identity as both `CODEX_THREAD_ID` and
`CODEX_SESSION_ID`. At the user's explicit request, Python read
`CODEX_THREAD_ID` and connected to the same shared listener. It then:

1. read the current task;
2. called `thread/name/set` with `Task renamed`;
3. received the matching `thread/name/updated` notification; and
4. read the task again and confirmed the new name.

| Field | Value |
| --- | --- |
| Thread ID | `01a095b9-5b2f-7b92-bb11-6b4966f3e793` |
| Previous title | `Test Python live Codex control` |
| New title | `Task renamed` |
| `thread/name/set` | 1.776 ms |
| Total verified operation | 6.562 ms |

This final check established that the title operation was not limited to tasks
created by the Python client. Given an authorized `threadId`, Python could
rename the current desktop task as well.

## Experiment 5: name a task configured for Plan Mode

A follow-up check used the existing shared listener and one disposable task,
`01a09757-aab0-7602-a3a3-a28669bf5f9e`. The client first verified that the
listener could read the current desktop task's exact runtime ID. It then used
the installed binary's generated schemas to configure the disposable task with
`thread/settings/update`, selecting `collaborationMode.mode: plan` and the
built-in mode instructions.

The `thread/settings/updated` event confirmed `collaborationMode.mode: plan`.
Python called `thread/name/set`, and `thread/read` confirmed the name
`Weaver Plan Mode naming verification`. The task had zero turns and was archived
after the check. Repeated checks reused that same task; no model turn was started.

This proves an external Python client can rename a task configured for Plan
Mode. It does not establish that a Plan-mode agent should execute mutating
commands, or that Fulcrum already receives an automatic `$weaver` activation
callback. Early naming can be a controller-owned metadata action; the activation
trigger and desktop presentation remain integration checks for implementation.

## Capability verdict

| Capability | Result | Strongest evidence |
| --- | --- | --- |
| Join the desktop's live runtime | Passed | Python and desktop connected to the same listener PID |
| Create a task | Passed | `thread/start` returned a new ID; `thread/read` confirmed it |
| Obtain a reusable `threadId` | Passed | Same ID used across two client connections and both experiment parts |
| Set a title | Passed | update event, API read, and Part 1 human desktop inspection |
| Select a model | Passed | explicit non-default model returned by start and subsequent reads |
| Select reasoning effort | Passed | explicit `low` persisted over an initial effective `high` |
| Send by task ID | Passed | initial and follow-up turns completed on the retained ID |
| Detect active/idle state | Passed | status events plus `thread/read` reconciliation |
| Archive by task ID | Passed | archive event, archived inclusion, active exclusion |
| Restore without identity loss | Passed | same ID, title, turn IDs, prompts, and responses after unarchive |
| Rename current task by ID | Passed | current task update event and final API read |
| Rename a task configured for Plan Mode | Passed | Plan-mode settings event, verified name, zero model turns |

The end-to-end answer for this installation is therefore **yes**: Python can
replace agent-driven creation, identification, naming, model/effort selection,
messaging, status observation, archiving, and restoration when it is connected
to the same app-server used by Codex desktop.

## Operational recommendations

### Prefer IDs and events over inference

- Persist the exact `thread.id` returned by `thread/start` before proceeding.
- Use `thread/read` for targeted reconciliation.
- Use `turn/completed` as the terminal signal for one turn.
- Use `thread/status/changed` for the broader task lifecycle.
- Treat `idle` as loaded and ready. Treat `notLoaded` as quiescent but not
  literally idle; resume it before starting another turn when necessary.
- Do not decide that a response was lost merely because a UI did not update
  immediately.
- Never create a replacement after a timeout without reconciling task lists and
  history.

### Separate delivery from reply compliance

A completed turn on the intended ID proves message delivery and execution. It
does not prove that the model followed an exact-output instruction. Record the
actual response and score exact compliance separately, as Part 2 demonstrated.

### Discover configuration dynamically

Call `model/list` before exposing model and reasoning selectors. Validate the
requested effort against `supportedReasoningEfforts`; do not hard-code the
catalog. Record the effective values returned by start/resume and verify the
persisted `model` and `reasoningEffort` after the first turn.

### Generate version-matched schemas

Use the installed binary to generate schemas when building or upgrading a
client:

```sh
/Applications/ChatGPT.app/Contents/Resources/codex \
  app-server generate-json-schema --out /safe/output/directory
```

The schemas are specific to that Codex version. This protects the client from
subtle mismatches such as `read-only` versus `readOnly`.

### Keep listeners local or authenticate them

Plain WebSocket is appropriate only for loopback or an SSH-forwarded local
connection. The official documentation warns that non-loopback listeners may
allow unauthenticated connections by default during rollout. For remote use,
use `wss://` and one of the documented token mechanisms. Never put a bearer
token directly on the command line or in an evidence report.

Even on loopback, treat this endpoint as privileged: it can read and administer
Codex task history and initiate agent work. Bind narrowly, validate target IDs,
use least-privilege sandbox settings, and do not expose it as a general local
HTTP service.

### Account for experimental support

The demonstrated WebSocket listener is explicitly experimental and unsupported
for production workloads. A durable Fulcrum integration should isolate the
transport behind a small adapter, generate schemas in compatibility tests,
fail closed when the desktop is not connected to the expected server, and
retain the existing agent-driven task tools as the supported fallback until
OpenAI documents a production-stable shared-desktop attachment contract.

## Reproduction and evidence

The host-local client and machine-readable reports are outside the source
checkout so the experiment did not alter workspace source while it was
running:

| Artifact | Path |
| --- | --- |
| Main client | `/Users/dthurn/.codex/experiments/python-live-desktop-control/codex_live_control_experiment.py` |
| Discovery report | `/Users/dthurn/.codex/experiments/python-live-desktop-control/tcp-discovery-results.json` |
| Part 1 report | `/Users/dthurn/.codex/experiments/python-live-desktop-control/tcp-part1-results.json` |
| Part 2 report | `/Users/dthurn/.codex/experiments/python-live-desktop-control/tcp-part2-results.json` |
| Retained Part 1/2 state | `/Users/dthurn/.codex/experiments/python-live-desktop-control/tcp-experiment-state.json` |
| Model/status client | `/Users/dthurn/.codex/experiments/python-live-desktop-control/quick_status_model_effort_experiment.py` |
| Model/status report | `/Users/dthurn/.codex/experiments/python-live-desktop-control/quick-status-model-effort-results.json` |
| Model/status state | `/Users/dthurn/.codex/experiments/python-live-desktop-control/quick-status-model-effort-state.json` |

Part 1 was invoked as:

```sh
python3 /Users/dthurn/.codex/experiments/python-live-desktop-control/codex_live_control_experiment.py \
  part1 \
  --ws-url ws://127.0.0.1:4500 \
  --state /Users/dthurn/.codex/experiments/python-live-desktop-control/tcp-experiment-state.json \
  --report /Users/dthurn/.codex/experiments/python-live-desktop-control/tcp-part1-results.json \
  --request-timeout 15 \
  --create-timeout 30 \
  --event-timeout 15 \
  --turn-timeout 180
```

Part 2 used the same options with command `part2` and report
`tcp-part2-results.json`. The model/status test was invoked as:

```sh
python3 /Users/dthurn/.codex/experiments/python-live-desktop-control/quick_status_model_effort_experiment.py \
  --ws-url ws://127.0.0.1:4500 \
  --model gpt-5.6-luna \
  --effort low \
  --state /Users/dthurn/.codex/experiments/python-live-desktop-control/quick-status-model-effort-state.json \
  --report /Users/dthurn/.codex/experiments/python-live-desktop-control/quick-status-model-effort-results.json \
  --request-timeout 15 \
  --event-timeout 15 \
  --turn-timeout 180
```

No credentials were embedded in the scripts or reports. No source file in this
repository was changed by the experiment itself; this report is the deliberate
post-experiment documentation change.

[app-server]: https://learn.chatgpt.com/docs/app-server
[codex-sdk]: https://learn.chatgpt.com/docs/codex-sdk
