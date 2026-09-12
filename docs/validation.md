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
