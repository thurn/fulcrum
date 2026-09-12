# Fulcrum: Replace Agent Administration with Deterministic Execution

## Summary

This project plan is saved at `docs/plans/fulcrum-runtime-simplification.md`.
Saving the document is the requested deliverable; implementation is subsequent
work.

Replace standing Overseer/Executor pairs with **one live Luna Executor per
approved run, invoking a Sol/high subagent for independent review**. Python owns
routine administration, evidence collection, delivery, and execution of
Archon-approved dispatch batches.

Success means materially lower cost without sacrificing review or delivery
correctness. The cheaper workflow may take at most **1.5× the completion time of
Sol alone** on comparable tasks.

## Target behavior

### Agents and scheduling

- Archon retains scheduling judgment. Python presents one compact proposed batch containing work, priorities, dependencies, exclusions, and resource constraints. Archon approves or adjusts that batch.
- Python dispatches approved work automatically. Parallel execution is the default, including within one repository; dependencies, holds, explicit exclusions, and Tollgate admission constrain it.
- Allow **five active work slots** across implementation and scheduled specialists. A review subagent inherits its Executor’s slot while the Executor waits; it does not require another slot.
- Reuse the Executor conversation within one approved plan or batch. Retain a reviewer within a task’s correction cycle; start independent review context for the next task.
- Preserve Luna/xhigh implementation and Sol/high review initially. After two unsuccessful correction rounds, automatically upgrade implementation to Sol while retaining independent review. After two further unsuccessful correction rounds, pause the task and escalate once.
- Keep scheduled Sage and Inquisitor jobs on their existing daily cadence. Python handles scheduling and prevents duplicate overlapping runs. Sage reads retained evidence before requesting bounded interviews.
- Replace routine Watchman agent patrols with Python checks. Weaver becomes a planning/intake skill without fleet registration or identity-reporting ceremonies.

### Intake and communication

- Small tasks require a requested outcome, bounded scope, and acceptance/validation information. Remove the mandatory eight-section template. Detailed plans remain available when needed.
- Publish approved intake through one command that validates content, creates or updates Beads, reconciles dependencies, and records synchronization obligations.
- Remove direct Executor-to-Archon status chatter and peer handoffs. Executors invoke their reviewer through native subagent controls and return structured results.
- Maintain at most one pending scheduling notification and one unresolved escalation per condition. Updates replace stale information in the stored brief rather than generating additional messages.
- Normal completion updates state and the dashboard. Notify Archon when another batch decision or exception needs attention.
- Eliminate agent status polling, peer `wait_threads()` calls, and repeated handoff-reminder continuations. Native subagent waiting is permitted; it must not trigger monitoring loops.

### Verified review and promotion

A promotion mandate must derive from the actual Sol reviewer run:

1. Python prepares the review request from the approved requirements, exact source commit, relevant validation evidence, and a fixed reviewer instruction template.
2. Runtime instrumentation records the actual spawn, parent/subagent identity, model, reasoning configuration, prompt, and completion outcome.
3. Sol independently inspects the source and returns a structured approval or rejection with findings and reviewed scope.
4. The verifier reads runtime evidence directly. Parent-authored summaries or caller-supplied claims about the reviewer never establish approval.
5. Missing, malformed, interrupted, mismatched, or unverifiable evidence prevents mandate issuance.

Mandates bind the repository, assignment, approved requirements revision,
candidate/source commit, reviewer identity, and verdict. Store existing Git
identities and actual evidence; introduce no additional content hashes or format
versions.

Changed source requires renewed review. Tollgate’s internal reconstruction of an
unchanged reviewed candidate remains its certification responsibility.

For enrolled repositories, enforce this policy inside Tollgate’s service:

- Cover direct revision approval, existing candidate authorization, retries, dependency authorization, and promotion eligibility.
- Every candidate receiving authority must have its own valid mandate. Approving a dependent candidate cannot implicitly authorize unreviewed ancestors.
- Do not accept an agent-selectable “non-Fulcrum” exemption or `--human` bypass.
- Provide a separate, audited human override bound to the exact candidate and scope, requiring OS-backed user-presence authentication. An ordinary UI click or asserted caller identity is insufficient.
- Keep review approval, successful CI, certified promotion, source synchronization, and cleanup as distinct completion requirements.

These controls verify that the required review occurred and granted authority.
They do not prove that the reviewer found every defect.

## Implementation milestones

### 1. Prove runtime integration and establish measurements

Use the documented Codex app-server thread/turn APIs through a shared runtime
connected to both Python and the desktop. The installed desktop contains
shared-server connection paths; their configuration and behavior need live
verification. [App-server interface](https://learn.chatgpt.com/docs/app-server).

In a disposable project, demonstrate:

- Python-created Executor visible and directly steerable in the desktop.
- Correct model selection, resumption, interruption, completion events, and retained history.
- Sol/high subagent invocation with the prescribed prompt and inspectable result.
- Reliable correlation of spawn configuration, effective runtime settings, and final reviewer output.
- Reconnection without duplicate work or concurrent ownership.

Hooks may collect bounded event evidence, but cannot be treated as a universal
enforcement boundary. Verify actual installed coverage; do not depend on unstable
transcript scraping. [Hook contracts](https://learn.chatgpt.com/docs/hooks).

If required evidence or shared desktop control cannot be established, record the
exact integration blocker and stop dependent rollout. Do not silently substitute
detached workers, unverifiable receipts, or an agent-operated control loop.

Instrument creation, setup, implementation, review, corrections, CI, handoffs, and
cleanup separately. Also measure validation environment installation time; do not
presume it is agent overhead.

### 2. Implement one complete task path

Build the smallest functioning path before fleet scheduling:

`approved task → setup → implementation → review → correction/approval → delivery → complete`

- Provide narrow Python operations for worktree preparation, candidate submission, review preparation, receipt verification, delivery, and completion.
- Preserve Tollgate ownership of worktrees, certification, promotion, and synchronization.
- Let Python wait for ordinary CI and handle deterministic administrative recovery. Invoke an agent only when investigation or code changes are required.
- Return concise structured observations to agents. Parse external status output before exposing it; retain large logs separately.
- Completion requires verified promotion, required source push, and owned cleanup. A promoted task with pending cleanup or push resumes recovery rather than implementation.

### 3. Add durable orchestration and compact scheduling

Use a single-writer SQLite operational store through Python’s standard library.
Beads remains authoritative for issue content/dependencies; Git remains
authoritative for plans and source.

Persist assignments, approved batches, dispatch intents, returned runtime IDs,
stage transitions, review evidence references, holds, and outstanding delivery
obligations.

Expose these minimum interfaces:

- `fulcrum intake`: validate and publish concise task or plan intake.
- `fulcrum schedule propose`: return a bounded batch recommendation.
- `fulcrum schedule approve`: record Archon’s exact batch and constraints.
- `fulcrum run/status/pause/resume`: operate and inspect approved work.
- `fulcrum review prepare/verify`: prepare canonical review inputs and validate runtime evidence.
- `fulcrum deliver`: complete the certified delivery sequence.
- `fulcrum brief`: return the current scheduling decisions and actionable exceptions.

Use runtime events for stage transitions and bounded Python reconciliation after
reconnects or external-system changes. Never start a replacement after an
uncertain creation until the original attempt is reconciled.

Direct human steering pauses automatic advancement of the affected run until its
current scope and source are reconciled. Unrelated approved work continues.

### 4. Enforce mandates in Tollgate

Add repository review policy, verified mandate references, and the human override
to Tollgate’s service and authorization interfaces. The service resolves
evidence through the verifier; it does not trust approval metadata supplied by
the Executor.

Keep validation outside lengthy repository mutation locks, then recheck candidate
and policy identity before granting authority. Persist verified authorization
evidence for restart recovery.

Complete Tollgate’s required certified build, installation, restart, and health
verification before enabling enforcement on live work.

### 5. Replace the existing workflow and cut over

- Rewrite role skills, hooks, setup, readiness, and operational documentation around the new responsibilities.
- Remove the standing Overseer workflow, numbered pair registry, Weaver registration, agent-written handoff flags, and routine agent patrol.
- Update existing status surfaces to show actual stages, reviewer evidence, pending batch decisions, and recovery obligations.
- Stop new legacy dispatch, inventory active work, and drain or explicitly checkpoint it before cutover.
- Preserve unfinished work and delivery obligations. Remove obsolete runtime formats without backward-compatibility layers or parallel schedulers.
- Saving this plan does not create Beads, change schedules, interrupt live agents, or begin implementation.

## Validation and acceptance

**Correctness and recovery**

- Accept a genuine Sol/high approval with the required prompt and matching source.
- Reject wrong model/effort, incomplete prompts, fabricated parent receipts, interrupted reviews, missing evidence, stale commits, and rejected findings.
- Reject bypasses through direct approval, dependency authorization, retries, and alternate service entry points.
- Verify the human override requires actual user presence and records exact scope.
- Exercise duplicate events, uncertain creation, controller restart, disconnect, pause/resume, human steering, CI failure, push failure, and cleanup failure.
- Demonstrate that native reviewer waiting produces no status-check loop and that five occupied work slots cannot deadlock reviewer creation.

**Performance comparison**

Run the new workflow and Sol alone against identical isolated starting states and
acceptance checks. Include a tiny Markdown edit, localized bug, multi-file change,
UI change, merge conflict, and CI repair. Use two repetitions per case initially.

Report:

- Successful completion and independently checked defects.
- Total input, cached input, output/reasoning usage where available.
- Actual account cost/usage where attributable; clearly separate API-price estimates.
- Elapsed time, correction rounds, creation calls, monitoring calls, and coordination-only model turns.

Default adoption target: at least **20% lower measured cost per successful task**,
no observed correctness regression, and median completion time no greater than
**1.5× Sol alone**. Report tail latency and failed attempts separately. Treat the
small initial sample as a pilot, not proof of universal equivalence.

Administrative acceptance requires zero agent turns devoted solely to
registration or status polling, no peer waiting cycles, no duplicate dispatches,
and no recurring unchanged-condition message flood.

## Assumptions and delivery defaults

- Archon approves batches; Python performs the approved dispatches.
- Executors remain live desktop tasks; reviewers may appear as subagents.
- Existing Beads and Tollgate responsibilities remain.
- The 20% savings threshold is a proposed default; the five-slot cap and 1.5× time limit are agreed decisions.
- The trust model addresses agent mistakes and workflow bypasses, not a hostile administrator replacing the runtime or service.
- Validate each milestone before expanding it. Run repository-required checks for implementation changes, reinstall dependencies after dependency-file changes, and commit/push with Conventional Commits according to repository instructions.
