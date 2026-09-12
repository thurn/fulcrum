# Escalation Contract

Escalation preserves work and names a decision; it is not permission to stop
silently. Executors escalate to their Overseer, and Overseers escalate to the
current Archon. Only the Archon decides that execution genuinely needs human
input. Weaver planning interviews are a separate human decision flow.

Before sending, update owned progress with:

- The exact failing boundary and unedited error or evidence reference.
- Every attempted recovery, its hypothesis, action, outcome, and timestamp.
- Retained dirty paths, worktree, commits, candidates, process IDs, and push or
  cleanup obligations.
- Plausible recovery that remains untried and the specific decision requested.
- The real expected next actor and action. If delivery fails, retain the error,
  mark the handoff unsent, and let patrol/recovery observe it before retrying.

The receiver inspects the retained state before accepting a claim of blockage.
Try corrected identity/tool use, documented recovery, a scoped investigation,
or a changed in-scope approach before human escalation. Never invent authority,
credentials, success, or evidence to make the escalation disappear.

For candidate failures, invoke `tg diagnose` first. At most one unchanged retry
is allowed, and only with a stated hypothesis. After that, spend at most fifteen
further minutes diagnosing the same boundary before making a concrete repair,
rolling back, or escalating with evidence. Source changes produce a new exact
candidate under the existing mandate's permitted replacement scope.
