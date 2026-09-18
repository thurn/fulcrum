# Weaver: scope, handoff, and measurement

Weaver answers questions, prepares implementation scope, and authors future plans.
It does not implement repository changes. This includes documentation, tests, and
configuration. Plan artifacts use Fulcrum's plan commands; investigation and scope
use ledger fields. Weaver is entered in the human-invoked task and is never created
by Steward or delegated to another task. The initial skill establishes this boundary
before repository investigation so the first user-facing update can accurately
promise investigation or scoping.

## Proportionate authoring

| Request | Proportionate result | Material decisions |
| --- | --- | --- |
| Question about scheduling | `answered`: short answer and specific evidence references | Investigate the relevant behavior; do not invent an implementation task. |
| Delete an obsolete document | `ready`: a short summary and an observable acceptance array | Inspect incoming references. Include removal or correction of directly affected links unless evidence justifies excluding them. |
| Small behavior change | `ready`: intended behavior, affected boundary, observable checks | Resolve routine implementation details from evidence; ask only about choices that change scope or safety. |
| Future migration plan | `planned`: summary and the published future plan's own ID | Preserve plan approval/publication and future-activation boundaries. Authored does not mean activated. |
| Ambiguous or consequential retirement | Investigate consumers, dependencies, rollback needs, and uncertainty; `blocked` if a material answer is unavailable | State the specific missing decision and attempted evidence. Do not guess which production system to remove. |

These are authoring review cases, not claims that automated tests prove a model
will always obey a prompt. Tests exercise command contracts and resulting state;
manual review checks that the skill, formula, fallback, and documentation agree.
Benefits, dependencies, uncertainty, and effort belong in scope when informative,
not as compulsory empty headings. A small request can use one sentence and one
acceptance check. Do not treat every omitted implementation detail as forbidden.

Example `ready` payload for the first test's evidence:

```json
{
  "summary": "Delete docs/hooks.md and remove its incoming links in README.md and docs/fulcrum2/audit.md; leave unrelated content intact.",
  "acceptance": ["The file is absent and no incoming Markdown link still points to it."],
  "evidence": ["README.md:12", "docs/fulcrum2/audit.md:78"]
}
```

This example is historical evidence, not an instruction to repeat that deletion.

## What each state proves

`answered` closes a resolved question with evidence. `planned` retains a published
future plan without activating it. `ready` stores implementation-ready `scope`,
replaces placeholder acceptance, proposes Executor as the next role, and returns
ownership to Steward. It proves **scope ready for admission**, not implementation
or delivery. `blocked` records the impediment, attempts, and required action.

After the Weaver turn ends, reconciliation checks dependencies, capacity, pauses,
workspace/source facts, and overlap reservations before creating an Executor. The
Executor contract is compiled only from the retained `scope`; original outcome,
context, and transcript text remain provenance and are never replayed downstream.

After transfer, Weaver can answer follow-up questions read-only. `context` reports
current ownership and does not return the prior owner's stale cooked prompt.
Material scope revisions go to the current owner; a follow-up does not authorize
reclaiming the bead or submitting another finish.

## Recovery and command results

Weaver entry accepts a literal `--description` or a JSON `description` through
`--input`. Use JSON for multiline or shell-sensitive text; never interpolate the
request into a shell command. Entry binds the invoking task and does not create or
rename a native task. Project enrollment is a separate preflight and must not be
performed as an intake side effect. Inspect a retained operation before retrying.

Entry and finish return compact results with relevant IDs, achieved step,
role-specific facts, instructions once where applicable, next action, and an
inspection command. The outer envelope retains request/operation IDs and warnings.
`operation show ID --json` still returns the full durable input, plan, checkpoints,
and timestamps. Receipt facts describe the state achieved by that operation;
`context` and `work show` inspect current state.

The retained scope identifies its finish operation. A retry after transfer can
complete the receipt without reacquiring work, even if Marshal already advanced
it. Changed input under the same request ID conflicts; a new finish from the old
owner is rejected. An interrupted CLI leaves the detached worker and source lease
intact and returns the operation locator on timeout.

Marshal attention retains failed attempts and retries with new request IDs after
bounded backoff. Before a native send, a durable uncertain checkpoint prevents
blind replay if the process disappears. A definitive rejection can retry;
an uncertain send retains the operation/thread locator and requires inspection.
The actual Marshal can still settle that retained decision with the original
brief and fresh comparison checks. An intent interrupted before the send checkpoint
can resume safely. Missing or unavailable leadership is visible rather than being
reported as a successful notification.

## Confirmed findings and limits

The first test established successful registration and a backlog transfer. It did
not establish review, dispatch, implementation, or delivery. Its initial promise
was inconsistent with the returned role, and its scope excluded directly broken
references without sufficient justification.

Code inspection and isolated tests confirmed additional causes: the prepared
summary was absent from Marshal's brief; `ready` retained placeholder acceptance;
entry's requested Weaver role could remain the proposed dispatch role; post-transfer
receipt retries could fail against the old owner; context could return stale role
instructions; failed review attempts lost their coalesced attention state; and
native review startup lacked an explicit uncertain send checkpoint. These are
separate from hypotheses about the first test's latency.

Durable checkpoints, source leases, immutable source selection, and the resident's
native connection ownership remain in place. Remediation changed the resident's
scheduling policy without changing that ownership boundary: cheap source probes
replace unconditional update workers, and reconciliation is event-driven with a
bounded periodic fallback. No live deletion bead or unrelated active task is used
for reproduction. Tests use provider doubles; benchmarks use an isolated real
Beads database and a simulated native task-naming endpoint. Neither establishes
production end-to-end delivery reliability or model behavior.

## Timing method

The first test's approximately 13 s entry tool wait and 2.8 s receipt interval,
and 3.6 s finish tool wait and 1.9 s receipt interval, use different boundaries.
Their difference does not identify a bottleneck.

Run the checked-in benchmark with a prepared interpreter:

```sh
.venv/bin/python scripts/benchmark-weaver.py --source CHECKOUT --samples 5 --output /tmp/weaver.json
```

`--baseline` selects the original summary-only finish payload for measuring a
historical checkout. It is a benchmark option, not a production compatibility path.
The harness copies source to a temporary pinned directory, initializes a temporary
embedded Dolt/Beads database, uses real CLI parsing and detached workers, and
substitutes only native task naming/inspection. It never connects to the live
resident or creates production tasks. It measures first invocation separately
from subsequent invocations in the same fixture; every CLI and worker is a fresh
process. This is not an OS-cache-flushed or cold production-service benchmark.

Comparable probes measure source selection/lease acquisition, request construction,
CLI and detached-worker waiting, lock acquisition, configuration reads, each Beads
subprocess, runtime work, role cooking, and entry/finish handlers. External elapsed
time runs from launching the client process through collecting stdout/stderr.
Receipt timestamps are recorded independently. Setup and the separate receipt
inspection after client return are excluded. Nested stage durations overlap and
must not be added together. Client startup/import/harness/return overhead remains
separate from the handler and is not assigned to network latency.

For future diagnostics, `FULCRUM_TIMING_FILE=/absolute/path/timings.jsonl` enables
process-tagged stage spans in production commands. It records stage names, monotonic
start/duration, and PID, never descriptions or payloads. Diagnostic write failure
does not change command behavior; the selected path is caller-managed and not an
unbounded default log. Normal stdout and durable receipt formats remain unchanged
by instrumentation. Existing application diagnostics and Beads observations still
provide request-correlated outcomes and adapter durations.

## Measured results, 2026-09-16

Environment: macOS arm64, Python 3.12.14, stock `bd 1.2.2 (6c124203e)`,
embedded Dolt, native task fixture, normal concurrent host workload. Baseline
application source was `5fa7bb6`; candidate entry/finish code is delivered with
this report. Each run had **5 entry/finish pairs**, with the first sample separate
and **4 warmed-fixture samples**. Runs were sequential, baseline then candidate;
the initial two-sample harness probe is excluded. Stage totals and individual
samples are retained in [the measurement artifact](../measurements/weaver-2026-09-16.json).

| Comparable boundary | Baseline | Candidate |
| --- | ---: | ---: |
| First entry, client launch through return | 33.651 s | 37.245 s |
| Warm entry median (4 samples) | 36.854 s | 37.034 s |
| Warm entry maximum | 47.642 s | 46.816 s |
| First finish, client launch through return | 19.859 s | 17.799 s |
| Warm finish median (4 samples) | 17.586 s | 23.670 s |
| Warm finish maximum | 17.910 s | 31.775 s |
| Warm entry receipt interval median | 12.870 s | 12.524 s |
| Warm finish receipt interval median | 9.878 s | 8.547 s |
| Median entry stdout bytes | 4,724 | 3,158 |
| Median finish stdout bytes | 1,663 | 684 |
| Beads subprocesses per entry | 42 | 39 |
| Beads subprocesses per finish | 19 | 18 |

Output fell **33.1% for entry** and **58.9% for finish**, measured separately from
execution. The candidate includes explicit acceptance and more truthful handoff
facts, so this is a comparison of useful command results, not identical payloads.
Three entry reads and one finish read were removed: receipt updates reuse their
retained snapshot as the merge baseline, then still read fresh state under the
lock, reject ownership conflicts, merge unrelated changes, and verify writes.
Finish also avoids constructing/loading its ledger configuration twice.

There is **no established end-to-end latency speedup**. Warm entry was essentially
unchanged (+0.5%); warm finish was slower (+34.6%) in these runs. Lower receipt
intervals do not override that client-boundary result. The sample is too small
and variable to establish p95 or to attribute the slowdown to a particular cause.

Stage probes locate the dominant measured cost in Beads subprocesses: baseline
mean entry handler time was 36.626 s, with 36.607 s inside ledger calls; candidate
was 37.202 s, with 37.187 s inside ledger calls. Mean `show` duration rose from
0.866 to 0.939 s on entry and from 0.896 to 1.159 s on finish despite fewer calls.
This locates time, but does not prove whether host contention, embedded-engine
startup, filesystem activity, or another factor caused the difference. Lock waits
were below 0.01 s in aggregate per five-command group, and simulated runtime calls
averaged about 0.001 s. Those observations do not establish live lock/network costs.

Source selection/lease probes averaged 0.004 s before and 0.022 s after across
both command types; request construction averaged 0.003 s in both runs. Mean
client-minus-handler time was 1.470/0.542 s for entry/finish before and
1.444/1.416 s after; that residual includes process startup, imports, fixture
machinery, polling/waiting, and return delivery. It is intentionally not assigned
to one bottleneck. The detached client's existing `communicate` wait and temporary
output files remain unchanged.

The live first test used different storage/runtime conditions and a different
outer tool-wait boundary. These measurements therefore support smaller output
and less redundant process work, not a production speedup or an explanation of
its approximately 13 s wait. Further production-representative server-backed
measurement is needed before additional latency claims or policy changes.

## First-test remediation

The operational-test postmortem produced the following enforced workflow:

1. Weaver is never dispatchable. `$weaver` binds the human-invoked task, and every
   work-creation, adoption, selection, and worker-registration boundary rejects a
   request to create another Weaver task.
2. Executor admission prepares an owned, clean worktree before role entry.
   Executor and Warden task roots are that worktree, never the live project root.
   The live project config runs `scripts/prepare-check` during preparation, so the
   review environment is ready before either role starts.
3. Executor finish transfers to one Warden. Marshal dispatch retains the exact
   authorized Weaver scope revision; Executor and Warden titles, outcomes,
   acceptance, and scope evidence compile only from that revision, never raw
   intake. A Warden finish first enforces one task commit atop the retained base.
   Correctable topology or validation failures do not seal judgment. One accepted
   Warden finish seals judgment. The controller then waits for validation, verifies
   the task is terminal, releases its subscription, approves review, promotes,
   observes source synchronization, cleans the worktree, and closes the bead.
   After acceptance, delivery never requires a second Warden instruction or finish.
4. An older nonterminal finish receipt is cancelled as superseded when a later
   accepted finish already advanced the work. It is not reported as a sealed
   recovery failure. Terminal tasks become immediately archive-eligible after
   ownership transfers to another thread; the current owner retains the normal
   idle threshold.
5. Command diagnostics retain bounded redacted request/result projections,
   operation/thread/turn IDs, associated bead IDs, and durations. `trace --bead`
   merges those events with operation receipts and managed-task observations.
   Reconciliation spans make duplicate scheduling rounds attributable. Dispatch
   timelines correlate Marshal receipt, enqueue, worktree preparation, role entry,
   and native observation. Per-bead failures are isolated and retained as trace
   gaps and recovered health episodes while unrelated backlog continues.
6. Role prompts contain exact accepted progress, ownership-qualified stop, and
   finish schemas and state that Warden finishes once after acceptance. Formula cooking is local, successful Beads
   mutations reuse their returned record, fresh random operation IDs skip a
   redundant existence read, and empty dependency sets skip list calls.

A two-sample isolated follow-up on 2026-09-16 measured warm entry at **21.047 s**
and warm finish at **11.592 s**, versus the earlier candidate medians of 37.034 s
and 23.670 s. The follow-up used 25 Beads subprocesses per entry and 13 per finish,
down from 39 and 18. This is a small, non-concurrent sample and is evidence of the
removed process amplification, not a production latency guarantee. The remaining
25/13 subprocess boundary is intentionally visible as further batching work.

## Validation and delivery

The required `scripts/check` passes formatting, strict type checks, and the complete
128-test suite. Behavioral tests cover literal registration, CLI JSON payloads,
explicit acceptance, question/plan/blocked outcomes, compact receipt inspection,
partial and fully degraded entry, scope visibility (including long scope and
material uncertainty), Marshal attention/authorization/dispatch eligibility,
HUMAN fallback, failed-leader backoff, uncertain sends, interrupted checkpoints,
exact replay, stale ownership, and merge safety. Existing source-pinning,
connection-continuity, detached-client, and cross-process lock checks also pass.

The postmortem remediation is covered by formatting, strict type checking, and the
complete unit suite. It adds regression cases for isolated Executor/Warden roots,
workspace-preparation failure, repeated Weaver rejection, one-shot Warden judgment,
controller-owned approval/promotion/synchronization/cleanup/closure, superseded
finish replay, immediate archive eligibility after transfer, source probing, and
multi-bead diagnostic correlation. Resident changes require the explicit safe
maintenance handoff described in the live-iteration architecture; they are never
silently activated as ordinary application code.
