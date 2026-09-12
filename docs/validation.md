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
