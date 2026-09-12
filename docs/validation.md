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
