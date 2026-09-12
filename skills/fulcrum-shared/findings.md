# Actionable future findings

Read existing open Beads findings in the affected project before filing. Match
the underlying failure/boundary, not title wording or the date of the review.
Use a stable project + problem key across Sage/Inquisitor runs. Inspect close
reasons before treating a terminal finding as a recurrence; Archon decides its
disposition. Ambiguous matches require reconciliation, not another duplicate.

Each finding states evidence references, impact, proposed change, affected
interfaces, acceptance, validation, suggested priority, and related beads.
Label measured effects with method/window; label predictions as expected benefits
and missing metrics as unavailable. Avoid invented numeric savings.

fulcrum.findings.Finding and publish_finding create standalone activation:future
beads with stable intake references. Pass matching_issue_id after semantic review
when an older matching issue has a different key. Existing nonterminal issues
receive new evidence/proposals in notes only: preserve their scope, status,
activation, priority, owner, dependencies, and any active mandate. Report active
matches to Archon; never silently expand current execution. Identical evidence
is not appended twice. New scope is proposed future work, never self-authorized.

All commands target the configured brain. After filing/updating, explicitly
commit Beads history and immediately attempt its separate push. Retain failed
push obligations and report them to Archon. Brain reports use ordinary Markdown
Git commit/push; raw logs stay local. No findings is valid when the report explains
coverage and evidence rather than inventing work to fill a quota.
