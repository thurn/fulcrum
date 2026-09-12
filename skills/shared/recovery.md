# Recovery Runbook

## Investigations and unavailable tasks

Keep same-project diagnosis in the original Executor task but suspend the
assignment on a LIFO stack. Capture its worktree, source/candidate identities,
review history, and failure history; create a fresh owned Tollgate worktree for
the investigation. A cross-project fault is repaired under the affected
project's own pair and scope. Resume the original assignment without erasing its
ownership, review attempts, or failure history.

For an unavailable task, first attempt to resume or message the original. If it
cannot return, never adopt its worktree. Inspect whether its candidate already
promoted before reconstructing anything. Preserve dirty work until its explicit
disposition is understood, then create a fresh replacement worktree from a
retained commit or certified base. Source push or cleanup failure after promotion
is recovery work; it is not permission to redo the implementation.

## Tollgate emergency boundary

The emergency exception exists only when Tollgate cannot create the worktree
needed to repair Tollgate. First use supported diagnosis, service restart, and
certified-installation recovery. If those fail, the Archon records the outage,
attempts, narrow Tollgate scope, owner, permitted runtime changes, and exact
certified release OID.

The repair pair may then use an ordinary Git worktree on a unique branch from
that verified commit. Retain checks, diff, rollback, and evidence. The Overseer
must review before a provisional binary is installed. Provisional means
uncertified: it may restore normal worktree creation but cannot promote itself.
Move the reviewed repair into a fresh Tollgate-owned worktree, run normal
candidate certification and source synchronization, reconcile the installed
version to that certified result, and only then clean emergency resources.

Never rewrite release, disable voting checks, alter service state to claim
success, or fabricate a certificate. If normal evidence cannot be established,
preserve the repair and escalate it.
