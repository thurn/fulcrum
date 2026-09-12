# Worktree, review, and certified delivery

Use the configured repository and supported installed Tollgate help. Capture
the certified release OID from `tg --repository <id> status --json --no-launch`,
verify healthy configuration, then `tg --repository <id> worktree create <name>`.
Verify returned base/path/branch. Record ownership and use that tree for work.
Commit proportionately validated changes with explicit paths; candidate submission
requires an exact clean immutable commit. Check native repository instructions.

Submit `tg candidate <source-oid> --json --no-launch` without approval. Retain
candidate ID, full source OID, tested OID (null if unavailable), captured base,
branch/worktree identity, and observed queue revision. These are distinct:
Tollgate may test a reconstruction atop earlier queued work. Retain log handles
and a concise check result; never substitute a local test for certification.

Immediately send Overseer those identities, contract/approved-plan revision,
and review evidence. Include UI walkthrough and retained screenshots when
appearance changed; track and clean up demo services. Reviewer inspects only;
it never edits or builds inside the Executor's tree.

Review approval names exact candidate, assignment, reviewed scope and allowed
replacement categories. Missing authority is a wait, not a request to bypass.
Three unsuccessful substantive reviews escalate to Archon. A duplicate source
or evidence request does not increment the count. Archon's next approach is
recorded without erasing history; material scope change needs appropriate approval.

Executor verifies the immutable source is still clean, stops owned runtime
processes, then `tg approve <candidate-id> --json --no-launch`. Observe status,
wait, logs, and diagnose through native commands. Tollgate alone owns queue
reconstruction, CI evidence reuse, certification and release advancement.
The installed Tollgate requires exactly one task commit based on promoted
release. Preserve prior source OIDs in retained candidates, then consolidate
local replacement edits into one task commit atop the captured certified base;
do not submit a failed/unpromoted ancestor or incorporate speculative queue
prefixes. Recheck current release when Tollgate requests rebasing.
In-scope repair yields a new clean commit and unauthorized candidate; record
which reviewed candidate it replaces and ask Overseer to record the permitted
replacement mandate. Ordinary conflicts/CI repairs are allowed only within that
mandate. A changed product contract returns to review. Do not manually advance
release, cherry-pick onto it, push worktree branches, or update the primary tree.

After terminal promotion, verify the certificate's tested/promoted identity,
configured remote tip, and cleanup state. If automatic pushing is disabled, use
`tg --repository <id> push --json --no-launch`, then check the configured remote.
Record source push pending/failed until verified. Verify worktree/branch cleanup
through Tollgate and actual process exit; retained or failed cleanup is outstanding
work. Do not delete another owner's tree or stop the shared Beads server.

Only certified promotion + required source push + owned cleanup permit code-bead
closure. Close through installed Beads' native terminal state, commit and attempt
Beads history push promptly, report outcome/evidence to Overseer, then archive.
A promoted candidate with push/cleanup outstanding remains recovery work and
must not trigger fresh implementation. Brain push failures are separate retries.
