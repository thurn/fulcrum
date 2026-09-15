# 24 — Thirty-task concurrency

Status: implemented; final closeout validation pending.

Dependencies: [06](06-codex-runtime-adapter.md), [08](08-controller-supervision.md), [09](09-leadership-and-admission.md), [10](10-task-control-and-reviews.md), [19](19-installation-and-service.md), [22](22-deterministic-cli-validation.md)

Normative reading: [concurrency goal](../design.md#5-marshal-context-and-dispatch), [smoke contract](../contracts.md#deterministic-provider-controls). Also follow the [index conventions](README.md#worker-contract) and [completed interface contracts](../contracts.md#10-complete-workflow-and-operator-interfaces).

## Outcome and public utility

Implement the explicit `fulcrum smoke concurrency --workers 30 --model gpt-5.6-luna
--effort low --timeout 600` client utility and its checked-in driver. It uses public
fixture/work/task operations and reports measured native execution. Thirty is a
capability target; production defaults remain four and there is no reserved slot.
This task is independent of the functional test's case order but both are required
for replacement acceptance.

## Implementation sequence

1. Create an isolated native fixture with explicit shared endpoint and disposable
   workspace. Raise its global/project limits to 30 through authorized config CLI;
   never change production YAML. Use ordinary automatic start/admission operations
   to exercise the configured capacity, rather than hiding an admission bug with
   30 human bypasses. Keep leaders idle during the barrier measurement.
2. Create 30 tiny answered-work requests with requested role Weaver and recorded dispatch authorizations via
   `dispatch --authorize` operator decisions, then admit them normally. Each task receives
   the full role prompt, ownership operation and a shell command operating only in
   its fixture. Use Weaver answered tasks so the smoke does not create 30 delivery
   worktrees/candidates or require extra Warden turns.
3. The shell operation writes its distinct readiness marker and waits at a bounded
   fixture-only barrier. The driver calls `fixture barrier prepare` with the recorded participant IDs
   before starting work, then releases it when the CLI
   reports all 30 native turns active and all 30 tools ready. Keep tool waits/output
   bounded. The barrier coordinates an observation; it does not synthesize native
   task status. Expose fixture barrier status/release through the public fixture CLI.
4. Measure a simultaneous intersection of all 30 active native turn intervals, not
   30 sequential starts. After release, each task completes its trivial work and
   required answered finish. Observe all terminal turns, closed work and release of
   Fulcrum subscriptions using `task show/wait/release` and `runtime status`.
5. Record baseline/peak/final managed active counts, subscriptions, available FD/
   pressure facts, command latency and native tool evidence. Missing FD data is
   unknown; task counts and release postconditions still need observed evidence.
   Supported pressure/overload is a test failure with exact diagnostic facts.
6. Enforce a 600-second total budget including setup, execution and bounded cleanup
   attempt. Stop new starts and begin cleanup by second 540. On timeout release the
   fixture barrier, interrupt only owned tasks, inspect terminal state and preserve
   an incomplete report. No extra model turns are authorized just to fix the smoke.

## Acceptance and limitations

Report 30 distinct task IDs, overlapping active interval, one observed shell tool
operation each, terminal outcomes, no unresolved owned starts and successful
subscription release. A lower achieved concurrency is incomplete, not a passing
30-task test. Use one native turn per task, with normal missing-finish failures
reported if the task omits its outcome; do not hide them with manual fake finishes.
Check machine-readable status remains responsive during the barrier; the utility
must manage the whole run without Desktop interaction or a private runtime query.
Do not wait for the runtime's 30-minute unload grace, restart the shared runtime,
or equate unsubscription with immediate process/memory reclamation. Functional
role behavior and prolonged stress reliability are outside this short smoke.
