# Validation exercises

These exercises use disposable data and do not modify the live brain, project
registry, schedules, or production task graph.

## Task 06 — Brain issue graph and synchronization

Use Beads 1.2.2 and Dolt 2.2.0, as pinned in
[compatibility.md](compatibility.md). Create a disposable private Git remote and
a disposable Beads/Dolt remote, then initialize a brain checkout against them.

1. Create two issues labeled for different projects and connect them with
   `bd --directory <brain> dep add <dependent> <prerequisite>`.
2. Commit one Markdown plan with Git and commit the issue working set with
   `bd --directory <brain> dolt commit`. Push Git and Dolt separately.
3. Restore both into a clean disposable checkout. Verify the Markdown plan with
   Git and the two issues plus cross-project edge with `bd show` and `bd dep tree`.
4. Repeat one intake using the same stable `fulcrum-intake:<key>` external
   reference. Verify the existing bead is reused and its missing dependency is
   reconciled instead of creating a duplicate.
5. Point each remote at an unavailable endpoint in turn. Verify both local
   commits remain present and the failed command is retained as a
   `push_obligations` entry in the responsible role's progress record.

The repository test suite also checks label ambiguity, future-by-default
standalone work, plan activation inheritance, bounded command construction,
unrelated staged-content refusal, local preservation after push failure, and
single-owner recording of retry obligations.

Executed on 2026-09-11 with Beads 1.2.2 and Dolt 2.2.0: two disposable issues
(`project:fulcrum` and `project:battlement`) and their blocking edge were
committed and pushed to a local Dolt remote. A plan was pushed separately to a
bare Git remote. A clean checkout restored the Markdown, both issues, and the
one dependency edge. Temporary data was moved to Trash after verification.
Calling the installed helper twice with the same stable intake key returned the
same issue ID and kept one dependency edge, confirming interrupted intake is
idempotent against the selected Beads version.

## Task 07 — Markdown plans, memory, and NEWS

The sanitized fixture tree under `tests/fixtures/brain/` contains queued and
future plans in two projects, a cross-project prerequisite, global/role/project
memory, project-summary links, and multi-project NEWS entries. `scripts/check`
verifies discovery without file changes, file-specific diagnostics for invalid
frontmatter, duplicate IDs, unknown projects, invalid activation, cycles,
preserved NEWS Markdown and dates, concise role context, and JSON CLI output.

The readers are deterministic and bounded to 256 KB per document. They use
safe YAML loading, invoke no model, and never rewrite the source Markdown.

## Task 08 — Eligibility and plan reconciliation

The sanitized scenarios under `tests/fixtures/eligibility/` cover queued versus
future activation, two composed holds, a cross-project prerequisite, a cycle,
partial intake, a canceled prerequisite, an empty plan, and refinement of an
active assignment. Focused checks also cover unknown integration/resources,
an existing owner, closure without complete promotion/push/cleanup evidence,
and a requirement removed without a recorded scope decision.

The evaluator returns every blocking reason independently and deep-copy checks
confirm that it does not change beads, activation, assignments, mandates, or
plans. Reconciliation targets only assignments for the changed plan and
preserves existing mandates while requesting a pause when scope may have moved.

## Task 13 — Resources, holds, and pause/resume

`tests/test_resources.py` launches a controlled owned process and proves a host
cannot be reported quiet until the process exits and Tollgate queue/run counts
have drained. Other checks cover unavailable measurements, overlapping holds,
evidence and explicit human release, bounded structured recovery exceptions,
five/ten-minute checkpoint deadlines, the non-candidate status of unvalidated
work, and complete identity checks before resume.

`fulcrum resources` bounds subprocesses to three seconds and captured output to
256 KiB. Its CPU, memory-pressure, process, and Tollgate fields report missing
observations as unavailable. Raw observations remain local operational evidence.

## Task 14 — Escalation and recovery

`tests/fixtures/recovery/tabletop.json` and `tests/test_recovery.py` cover failed
handoff delivery, repeated candidate CI failure, an unavailable task, source
push failure, nested investigations, and a simulated Tollgate worktree outage.
Every path either selects concrete recovery or records the expected next actor
with its boundary, attempts, retained work, untried recovery, and evidence.

The CI path requires `tg diagnose`, permits one hypothesized unchanged retry,
and bounds further diagnosis to fifteen minutes. The Tollgate outage case is a
pure tabletop: production service is not disrupted, provisional repair remains
uncertified, and normal certification plus installed-version reconciliation
remain mandatory.

## Task 15 — Night Watchman and recurring work

`tests/fixtures/watchman/patrol.json` and `tests/test_watchman.py` exercise a
registered Archon/Watchman with an injectable clock. Coverage includes the Sage
24-hour cadence, per-project Inquisitor twelve-hour offset, overlap prevention,
one catch-up after downtime, next-future advancement, and disabled projects.

Patrol checks distinguish a healthy external review wait from an unexplained or
deadline-overdue stop, retain uncertainty for unavailable observations, and
report failed source pushes. Stable identity plus condition fingerprints silence
unchanged repeats while still reporting changes and resolutions. A real stopped
fixture targets the registered Archon; a quiet patrol produces no Archon message
and no human-facing noise. Automation planning creates, updates, or reuses one
hourly heartbeat without duplicate schedules.

## Task 09 — Identity and portable handoffs

`tests/test_roles.py` exercises pending and ambiguous creation responses,
nonroutable client IDs, duplicate resolution, retained pair numbering, Plan-mode
activation refusal, and uncertain delivery reconciliation. These are controlled
tool-response fixtures; the live create tool returned an immediate actual ID.
On 2026-09-11 a disposable Codex task received a real handoff through
`send_message_to_thread`; `read_thread` verified the requested acknowledgment,
then the task was archived. No production registry or brain data was changed.

The wheel installs shared references under `share/fulcrum/skills/shared`.
Install the complete skills tree together, preserving relative references.
The helper only constructs records; the role uses the existing atomic writer
and the supported Codex tools. It does not claim that a send caused action.

## Task 10 — Cooperative Archon and enrollment

`tests/test_coordination.py` transfers the current coordinator after verified
relinquishment, rejects stale writers/acknowledgments, and preserves the exact
assignment bytes including its mandate. Enrollment checks reject missing health,
wrong repository paths, wrong project IDs, wrong hosts, and non-Git contexts.
These checks run against disposable state, not the human's current fleet.

Read-only live reconciliation on 2026-09-11 found existing saved local Git
projects and active configured Tollgate registrations at matching Git roots for
Fulcrum, Tollgate, and Battlement. No duplicate project was created and no
production role was activated. IDs differ from the earlier compatibility audit;
resolve them at enrollment rather than copying historical report values. Current
Archon enrollment remains a Task 19 setup operation.

## Task 11 — Weaver intake and refinement

`tests/test_weaver.py` generates a standalone bug contract, parses approved
future/queued plan files, checks eligibility without dispatch, reuses a bead
after an interrupted create, and routes an active refinement to reconciliation
without changing its mandate. The Beads responses are controlled fixtures; no
production work is published. Existing Task 06 checks cover independent pushes
and Task 09 covers Plan-mode registration refusal. Skill frontmatter is validated
with skill-creator's quick_validate.py. Manual contract review confirms that
substantial plans require two distinct reviews, whereas direct intake does not,
and approval to save future work does not authorize implementation.

## Task 12 — Review, replacement, and certified recovery

`tests/test_delivery.py` uses immutable commits in a disposable Git repository
for an accepted review after one rejected version, missing/duplicate evidence,
third-rejection escalation, an Archon-directed next approach, permitted versus
out-of-scope replacement, and closure refusal while push/cleanup is pending.
The completion facts in that unit exercise are explicitly simulated observations,
not certificates. Review URL decoding verifies both encoding layers.

A separate live disposable Tollgate repository on 2026-09-11 used a voting file
check: removing the required file produced a failed candidate with no certificate.
A corrected single-commit replacement passed and was certified/promoted. The
probe exposed and verified Tollgate's unpromoted-ancestor rejection, now covered
in the portable delivery contract. A retained worktree demonstrated outstanding
cleanup; an unavailable disposable remote exercised source synchronization
failure and recovery. No production release or source branch was manually changed.

The assignment schema adds optional reviewed source OID, allowed replacement
categories, and predecessor candidate ID. Existing version-1 records remain
readable; absent replacement permissions grant none. Tollgate still owns all
certificates, tested commits, queue reconstruction, and promotion authority checks.

A clean wheel installation into an isolated prefix outside the developer home
resolved every relative skill reference and imported the installed delivery
helper. Black, Pyre, and all 71 focused tests pass. The live remote outage was
reported as Tollgate preflight-pending (not a falsely claimed promoted change);
post-promotion push/cleanup-pending closure is covered by controlled observations.

### Tasks 09–12 delivery evidence

Each task was committed separately, validated by Fulcrum's configured Tollgate
gate (`scripts/check`), promoted with a certificate, pushed to configured
`origin/master`, and reported with completed worktree cleanup:

| Task | Source commit | Result |
| --- | --- | --- |
| 09 | `17ccfb0` | Certified, synchronized, cleaned |
| 10 | `794a499` | Certified, synchronized, cleaned |
| 11 | `8041896` | Certified, synchronized, cleaned |
| 12 | `4a6771f` | Certified, synchronized, cleaned |

The disposable remote-outage candidate remained blocked in preflight and was
canceled after inspection; it was never described as promoted. Both disposable
worktrees and the temporary Tollgate registration were removed. The earlier
corrected candidate had a real certificate and its restored remote matched its
release. Full candidate/log identities remain in local Tollgate evidence.

Status updates follow verified push and cleanup, so Task 12's final `done` label
is recorded in this separate documentation commit rather than claimed before
its delivery completed.

## Task 16 — Sage postmortems and bounded interviews

`tests/test_interviews.py` covers distinct subjects within one postmortem,
write/reload before reopening, uncertain tool outcome, previously unarchived
subjects, one later reminder (including failure), skipped patrols, deadline
completion with missing evidence, restoration before closure, and condition-aware
Watchman reporting/resolution. Legacy records remain readable and request
inspection rather than inventing deadlines or prior archival state.

`tests/test_findings.py` verifies project-scoped underlying-problem matches,
future-only new intake, repeated evidence deduplication, and evidence-only updates
that preserve active scope/ownership/activation. A synthetic Fulcrum finding in
`tests/fixtures/sage/postmortem.md` separates expected benefit from unavailable
timing/token metrics and claims no measured speedup.

On 2026-09-11 a real disposable Codex subject was archived, then its interview
record and unarchive intent were saved before reopening. A debrief-only request
was delivered. Codex reported a completed turn but exposed no readable reply;
the exercise retained missing-response evidence. An injected finish clock
exercised the deadline without waiting two hours. The actual task was restored
to archived state before the interview was closed. This does not claim a live
measured interview response or elapsed timeout. Production assignments and the
brain registry were unchanged.

A separate live disposable Beads repository received two observations of the
same underlying finding through `publish_finding`: both returned one actual
issue ID, the issue remained future work, and the second observation appeared
in its notes. Its local Beads history was committed; the fixture has no remote
and was not represented as production-pushed work.

## Task 17 — Whole-codebase Inquisitor review

`tests/fixtures/inquisitor/review.md` records a complete review of the small
synthetic shop. It selects an older duplicated normalization/domain boundary
in checkout and invoicing over a trivial recent README change. The proposed
direction preserves public outputs and coercion/error behavior, identifies
affected interfaces, and calls for contract comparisons rather than arbitrary
file splitting. It makes no claim to review production project architecture.

`tests/test_inquisitor.py` copies the fixture into a disposable Git repository,
creates older source and newer documentation commits, exercises the cited
accepted/rejected input contracts, constructs a future finding referencing the
older source, rejects a cross-project match without writing, and verifies no
reviewed or out-of-scope files changed. The shared Task 16 checks cover repeated
finding updates and preservation of active assignments. No findings remains a
valid outcome when supported by the recorded scope and evidence.

Black, Pyre, and all 101 focused tests pass. A clean wheel installation into
an isolated prefix outside the developer home imported the interview/finding
helpers and resolved all 42 relative skill links, including Sage, Inquisitor,
and the updated Watchman interview guidance.

### Tasks 16–17 delivery evidence

| Task | Source commit | Verified result |
| --- | --- | --- |
| 16 | `b803b1d` | Tollgate certified, origin/master synchronized, worktree cleanup completed |
| 17 | `7137050` | Tollgate certified, origin/master synchronized, worktree cleanup completed |

Both source commits passed the configured `scripts/check` gate. Task 17's final
`done` status is recorded after verification in this documentation follow-up.
The live interview subject was restored to archived state; the disposable Beads
probe used its own embedded database and left no shared database process running.
Full tool and candidate evidence remains local; the synthetic reviews do not
claim measured production speedups or grant implementation authority.

## Task 18 — Bounded lifecycle hooks

`tests/test_hook.py` exercises exact `session_id` lookup, compact-only context,
the 3,600-character hard cap (below the configured 1,000-token spill threshold),
one missing-handoff correction, and nonintervention for a second Stop pass,
Plan mode, unknown tasks, healthy reported handoffs, inactive runs, active
debrief interviews, unrelated roles, and missing or invalid progress. It also
installs twice into a disposable `hooks.json` and proves unrelated handlers and
top-level metadata remain byte-equivalent in meaning without duplicate Fulcrum
handlers. The helper performs no subprocess, network, model, Beads, or transcript
work; failed input/read paths return `{\"continue\":true}` and do not write state.

Run the focused check with:

```sh
python -m unittest tests.test_hook
```

This coding task had no supported way to compel the current desktop thread to
compact or to open its interactive `/hooks` trust reviewer. No trust bypass was
used, and CLI invocation is not represented as desktop delivery. Consequently,
desktop latency, delivered context size, and correction-turn evidence remain
`unavailable`, not passing. Until a human reviews the installed hash and performs
the desktop exercise in [hooks.md](hooks.md), the installed role skills and the
Night Watchman patrol are the verified fallback for context and missed handoffs.

## Task 19 — Repeatable installation and doctor

`tests/test_install_doctor.py` creates a retained synthetic source with matching
release and remote refs, installs twice, and confirms the active assignment and
recurring-job bytes are unchanged, services and hooks are not duplicated, all
seven skills are present, and no database restart is reported. It updates the
recorded package to 0.2.0 while retaining active role skill revisions for doctor
reconciliation. A schema-0 config is backed up before its explicit conversion;
an unknown schema remains byte-for-byte unchanged and errors. A `.worktrees`
source is rejected.

The passing doctor fixture uses three Git/Codex/Tollgate mappings, human-marked
resolved Archon and Watchman identities, one externally observed hourly
schedule, Beads-returned connectivity, matching package/source/skill revisions,
and no push obligations. Failure output is partitioned into
`required_failures`, `optional_gaps`, and `push_failures`.

Read-only live checks on 2026-09-11 observed Beads 1.2.2, Dolt 2.2.0, a running
loopback Beads-managed server with a successful connection test, and Tollgate
0.1.0. The actual saved Codex project IDs and active Tollgate registrations were
resolved for Fulcrum, Tollgate, and Battlement; each Tollgate repository had its
expected Git root, remote push enabled, and no repository block reason. No
human-created Archon or Watchman task was present in the supported task listing,
so no role registry or automation was invented. Those are required failures for
Task 20, not optional gaps.

The promoted 0.2.0 package was then installed three times from the retained
`/Users/dthurn/fulcrum` checkout at certified revision `543da4b`. Each result
reported the same seven roles and skill revision, one hook source, no migration,
and `database_restarted: false`. The first repeated invocation accidentally
recorded a misspelled expected brain remote; the next invocation corrected that
configuration through the same safe update path before doctor ran. No brain,
role, project, assignment, or schedule record was changed.

## Task 20 — Infrastructure readiness gate

[`readiness-evidence.json`](readiness-evidence.json) is the concise canonical
pass/fail/unsupported matrix, and [readiness-gate.md](readiness-gate.md) records
commands, expected exit, exact live project mappings, evidence routes, and the
completion procedure. `tests/test_readiness.py` proves required failures block
readiness, required rows cannot be disguised as unsupported, duplicate rows are
rejected, and optional unsupported coverage is accepted only as a named
fallback.

The evaluated gate is **FAIL**, as expected from live doctor output. Human role
enrollment/hourly scheduling and the Archon-owned three-project registry are
required blockers. Desktop hook delivery and runtime visibility are optional
unsupported rows with explicit skill/patrol and reported-state fallbacks. Every
other infrastructure row passes. No separate production pilot, Dashboard code,
Dashboard UI fork, service, or production Dashboard bead was created; Task 21
remains blocked.

## Guided Fulcrum setup skill

The `fulcrum-setup` skill is installed alongside the seven role skills. Its
bootstrap command accepts only two distinct explicitly human-created roles,
preserves an existing current Archon, validates exactly three fresh project
mappings through the enrollment helper, retains unhealthy projects as disabled,
and initializes only the new Archon's owned progress. Repeating the same input
does not duplicate roles or projects.

Schedule and hook observations are recorded separately after the skill has used
supported Codex automation inspection and the user has reviewed the exact hook
hash in `/hooks`. The evidence writer refuses a non-current Archon or a registry
without exactly one resolved human Watchman. Live readiness overlays only the
four runtime-dependent rows of the historical matrix and fails closed when a
doctor check is missing or failing.
