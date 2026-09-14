# Scaling Fulcrum inside Codex Desktop

Status: proposed architecture and validation plan, 2026-09-13.

## Goal and invariant

Fulcrum must scale to dozens of concurrent workers while every worker remains a
real Codex thread visible in Codex Desktop. Desktop visibility is a product
invariant, not an optional debugging convenience.

This target is plausible. A user has already run more than 30 concurrent threads
in Desktop. The recent file-descriptor failure does not establish a physical
limit in Codex or macOS. It establishes that Fulcrum's current shared-runtime
configuration combines a very small per-process descriptor limit with a client
lifecycle that kept too many tool-heavy threads loaded indefinitely.

The design must distinguish three properties that the current implementation
partly conflates:

1. **Persisted and visible:** the thread has durable history and appears in
   Desktop.
2. **Loaded:** the app-server holds the thread in memory and may retain its
   session writer, locks, tool helpers, pipes, and sockets.
3. **Active:** the thread currently has a running turn or helper.

A thread does not need to remain loaded merely to remain persisted and visible.
The official app-server protocol can list and read a stored thread without
resuming or subscribing to it. A later `thread/resume` can load that same thread
before its next turn. This separation is the central mechanism for retaining
Desktop visibility without paying the full runtime cost for every idle worker.

## What failed

The exhausted process was the Codex app-server, not the Fulcrum controller.
Fulcrum normally needs only its websocket, SQLite handles, control socket, lock,
watcher, and logs. The app-server owns the much larger resource set created on
behalf of all connected clients.

At the original failure, the app-server used 251 of its 256 allowed file
descriptors:

| Descriptor type | Count | Primary source |
| --- | ---: | --- |
| Pipes | 128 | Standard streams for 43 retained helper children |
| Regular files | 77 | Rollouts, writer locks, SQLite files, WALs, and binaries |
| IPv4 sockets | 36 | Desktop, controller, model, and tool connections |
| Other | 10 | Unix sockets, kqueues, and operating-system handles |

The 128 pipes and 43 children are an especially strong causal signal: a child
with piped stdin, stdout, and stderr leaves approximately three pipe descriptors
open in its parent. The retained children were primarily computer-use REPLs,
Node REPLs, and artifact-template helpers. Nineteen thread-writer locks were open;
15 mapped to Fulcrum tasks, including 11 idle tasks.

At 251 descriptors, the app-server had only five free slots. Starting a command
with three pipes can temporarily require at least six pipe descriptors before
the parent closes the child-side ends. The next command could therefore fail
during process creation even though a snapshot had not yet shown 256 persistent
descriptors.

The relevant lifecycle behavior is documented by OpenAI:

- `thread/start` automatically subscribes the connection to thread events.
- `thread/read` reads stored state without resuming or subscribing.
- `thread/loaded/list` distinguishes threads currently loaded in memory.
- `thread/unsubscribe` removes the current connection's subscription.
- after the final subscriber leaves, the server unloads an inactive thread after
  a documented 30-minute grace period.

Before the resource fix, Fulcrum started many threads through one permanent
controller connection and did not unsubscribe when their work became idle or
complete. The app-server therefore continued to treat the controller as an
interested subscriber and retained the corresponding thread runtimes.

## Why ordinary Desktop use can exceed 30 threads

The failure should not be interpreted as "Codex supports only 19 threads."
Several variables differ between ordinary Desktop use and the failing Fulcrum
run:

- A Desktop sidebar can contain many persisted threads while only a smaller set
  is loaded.
- Exiting Desktop closes its client connection. Fulcrum's controller websocket is
  intentionally permanent, so its subscriptions can survive for the app-server's
  entire lifetime unless Fulcrum releases them explicitly.
- Tool helpers are not necessarily identical for every thread. The failing run
  retained several helper processes per loaded worker.
- Active turns, idle loaded threads, sockets, and subprocess launches create
  different transient peaks even at the same visible thread count.
- The Fulcrum-managed app-server is a `KeepAlive` service. It does not receive the
  incidental cleanup that a private Desktop-owned app-server gets when Desktop
  exits.
- The current service inherited a soft descriptor limit of 256. That is a small
  policy limit for a long-running daemon supervising dozens of subprocesses, not
  a physical macOS limit.

The user's successful 30-thread workload is valuable acceptance evidence. The
next benchmark should reproduce that workload closely enough to identify which
differences in loading, tools, and service limits explain the divergent result.

## Responsibility boundary

Fulcrum owns semantic thread lifecycle. It must explicitly release subscriptions
that it no longer needs and resume the same thread when new work arrives.

The Codex app-server owns the implementation resources behind that lifecycle:
helper processes, pipes, sockets, rollout writers, and global admission. Fulcrum
should not permanently own app-server descriptor policy through `lsof`, fixed
descriptor-type budgets, or assumptions about how many helpers a future Codex
release starts.

Process-level telemetry remains useful for diagnosis and load testing. Until the
app-server exposes a supported capacity signal, a conservative emergency circuit
breaker can also prevent a known self-exhaustion path. It should be treated as a
temporary safeguard, not as Fulcrum's scheduling model.

## Recommended path

### 1. Preserve visibility while unloading idle workers

Fulcrum should model persistence, subscription, loading, and activity separately.

- Keep every worker's native thread ID and user-facing title permanently bound to
  its Fulcrum task.
- Unsubscribe when a worker reaches a terminal turn and Fulcrum has no immediate
  follow-up to send.
- Do not archive a worker merely to reclaim resources. Archiving changes its
  user-visible organization and conflicts with the visibility invariant.
- Use non-subscribing `thread/read` for periodic reconciliation of idle workers.
- Resume and subscribe immediately before Fulcrum starts a new turn.
- Subscribe for the entire duration of an active turn so events and approvals are
  not lost.
- Make success, failure, cancellation, controller shutdown, and restart paths all
  converge on the same subscription cleanup.

The app-server's documented 30-minute no-subscriber grace period means correct
unsubscription alone does not provide immediate reclamation. Fulcrum should not
fake immediate unloading by archiving visible workers. Instead, the system needs
enough capacity for the active set plus the grace-period working set. An upstream
request for an explicit "unload but keep persisted" operation or a configurable
grace period would materially improve this design.

### 2. Raise the app-server's service limit

The app-server launch service should receive an explicit, substantially larger
soft open-file limit. An initial value such as 4,096 is reasonable for validation,
but the final value must be derived from measurements rather than treated as a
magic constant.

For a target concurrency `N`, size the limit from:

```text
stable app-server baseline
+ N * measured peak descriptors per active worker
+ grace-period idle working set
+ transient command/turn-start burst
+ safety margin
```

At the observed rough cost of 12-17 persistent descriptors per loaded thread,
50 loaded threads would consume hundreds of descriptors, not thousands. A 4,096
limit should remove the immediate artificial ceiling while leaving room for
transient tool launches. Memory, process count, CPU, and model-service throughput
must still be measured independently.

Raising the limit is necessary for the target architecture, but it is not a
substitute for releasing subscriptions. Without lifecycle cleanup, any finite
limit merely delays exhaustion.

### 3. Remove the four-worker product ceiling

The current maximum of four active conversations and four resident idle workers
was derived from the accidental 256-descriptor environment. It is not a Fulcrum
product invariant and contradicts the goal of dozens of concurrent workers.

Replace it with configured concurrency whose safe maximum is established by the
scaling benchmark. Queueing should respond to observed saturation of CPU, memory,
model capacity, or supported app-server overload signals, not a hard-coded
four-worker assumption.

### 4. Reduce per-thread helper cost

The failing workload appeared to provision multiple tool helpers for most loaded
threads. Determine which helpers are created eagerly, which are created on first
use, and which Fulcrum roles actually need them.

Potential reductions include:

- avoid enabling computer-use or artifact tools for workers that cannot use them;
- prefer shared process-level helpers where Codex supports them;
- request lazy tool-server startup and prompt teardown after the last subscriber;
- separate optional specialist capabilities from the default worker tool set;
- terminate completed background terminals explicitly.

Fulcrum must not depend on undocumented helper names or counts. These
optimizations should be expressed through supported thread configuration or
upstream app-server behavior.

### 5. Fix the current reconciliation churn

The resource patch repeatedly treats `no rollout found` for already parked or
archived tasks as a deferred cleanup failure. The live controller produced
thousands of repeated deferrals. A missing or not-loaded runtime for a task already
known to be terminal should normally settle the cleanup state rather than create
an unbounded retry loop.

This churn is not the main source of app-server descriptors, but it obscures real
failures and makes the lifecycle state machine harder to trust.

### 6. Retain only a diagnostic circuit breaker

During the transition, keep a simple last-resort admission check so Fulcrum does
not knowingly push the app-server into `EMFILE`. It should:

- block only new Fulcrum starts;
- never interrupt active or human-driven turns;
- report current loaded-thread and process-resource observations;
- recover automatically when pressure clears; and
- not claim that a specific descriptor belongs to Fulcrum.

Once the higher limit, lifecycle model, and scaling tests are established, remove
process-internal descriptor categories from ordinary Fulcrum scheduling.

## Scaling validation

The existing regression proves some reclamation behavior but does not represent
the target workload. Its repeated native operations are thread reads rather than
real tool subprocess launches, and its app-server is isolated from Desktop and
unrelated human conversations.

Add a disposable shared-runtime benchmark with stages at 1, 10, 25, 30, and 50
workers:

1. Start from a stable app-server baseline and record descriptors, child count,
   resident memory, CPU, loaded threads, sessions, and writer locks.
2. Create named Fulcrum workers and verify that every one appears in Desktop.
3. Start real turns concurrently. Include representative shell commands, tool
   calls, review work, and at least one heavier specialist workload.
4. Keep some turns active while others finish, then start replacement work to
   exercise transient peaks.
5. Unsubscribe terminal workers without archiving them.
6. Verify that unsubscribed workers remain visible and that `thread/read` does not
   reload them.
7. Wait for documented unloading or observed `thread/closed`, then verify helper,
   session, lock, socket, and descriptor reclamation.
8. Resume a sample of unloaded workers and confirm exact identity and history.
9. Repeat several complete waves without restarting the app-server.
10. Run the same benchmark with unrelated human Desktop threads present.

Acceptance criteria for the first supported scale should include:

- at least 30 simultaneous active workers, followed by a 50-worker target;
- every worker visible in Desktop throughout its retained lifetime;
- no `EMFILE`, app-server crash, lost event, lost history, or duplicate thread;
- no monotonically increasing descriptor, child-process, or memory baseline across
  repeated waves;
- bounded recovery after controller disconnect and reconnect;
- Desktop remains responsive while Fulcrum is at maximum concurrency; and
- load limits are expressed as configuration backed by measured evidence.

The test should also compare the user's known-good 30-thread Desktop workload with
30 Fulcrum workers. That comparison is more useful than assuming that all visible
threads have identical runtime cost.

## Architecture alternatives

### One shared app-server

This is the recommended near-term architecture because it directly preserves
simultaneous Desktop visibility. Increase the service limit, fix subscription
lifecycle, reduce helper cost, and establish the supported concurrency through
measurement.

### Multiple app-server shards

Sharding would give each server its own descriptor table, but Desktop currently
attaches to one server at a time in Fulcrum's verified topology. Sharding therefore
does not satisfy simultaneous visibility unless Desktop gains supported multi-
server aggregation. It should not be the primary plan.

### Independent SDK workers

The stable Codex SDK is the official recommendation for automated coding jobs,
but independent SDK-owned runtimes do not currently satisfy Fulcrum's verified
same-Desktop visibility requirement. The SDK is useful for isolated tests and may
become an execution option if Codex later provides a supported way for Desktop to
aggregate or attach to those worker runtimes. It is not a replacement for the
current invariant today.

## Upstream app-server requests

Fulcrum can make the current design substantially safer, but several capabilities
belong in Codex:

- a supported persisted-but-unloaded thread operation;
- configurable or shorter no-subscriber unload grace;
- lazy or pooled per-thread tool helpers;
- prompt reaping of helper children, pipes, sockets, writers, and locks;
- process-level capacity and resource-pressure telemetry;
- global admission that returns a supported overload error before `EMFILE`;
- documented scale expectations for loaded and active threads; and
- a supported Desktop attachment mechanism for long-running custom clients.

OpenAI currently documents websocket app-server transport as experimental and
unsupported for production workloads. It also documents bounded websocket ingress
queues and an overload error, but that request-level protection did not prevent
the observed process-level descriptor exhaustion. Fulcrum should report a minimal
reproduction upstream while keeping its own behavior correct and bounded.

## Recommended implementation order

1. Fix the reconciliation retry storm and audit every thread-start path for a
   matching unsubscribe path.
2. Change resource parking from archive-based hiding to unsubscribe-based unloading
   while retaining Desktop visibility.
3. Add an explicit app-server soft descriptor limit and perform a controlled
   restart after active work drains.
4. Replace the four-active/four-idle constants with temporary configurable limits.
5. Build the shared Desktop benchmark and establish a supported 30-worker result.
6. Measure and remove unnecessary per-thread helpers.
7. Raise the tested target to 50 simultaneous active workers.
8. File upstream app-server issues with the benchmark and resource evidence.
9. Remove Fulcrum's process-internal admission logic once supported app-server
   capacity behavior exists.

## References

- [Official Codex app-server documentation](https://learn.chatgpt.com/docs/app-server)
- [Official Codex SDK documentation](https://learn.chatgpt.com/docs/codex-sdk)
- [Python control of the live Codex desktop runtime](codex-desktop-python-control.md)
- [Fulcrum technical design](technical-design.md)
- [Fulcrum reliability architecture](reliability-architecture.md)
