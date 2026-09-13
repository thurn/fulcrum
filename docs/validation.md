# Validation

`scripts/check` creates an isolated environment, installs the pinned dependency
set and editable package, then runs formatting, type checks, and unit/integration
tests. Tests use controlled adapters and injected time for state transitions,
capacity, conflict leases, event ordering, uncertain operations, crash adoption,
message batching, cadence, outcomes, worker health, source isolation, and
checkpointed reset safety. SQLite-trigger tests prove that direct writes cannot
bypass core capacity, lease-release, or recovery-progress invariants.

Claims about the shared desktop runtime, automatic Weaver activation, native
helpers, interruption, Tollgate worktrees/candidates, Beads/Dolt restoration, and
macOS service behavior require isolated checks against those native boundaries.
Mock results exercise failure behavior but are not readiness evidence.

The first assembled-product check follows the final section of
[`plans/fulcrum-python-runtime.md`](plans/fulcrum-python-runtime.md): clean setup
and rerun, shared desktop visibility, live editable reload, Archon policies,
Plan-mode Weaver naming, one small bead through independent review and native
delivery, archival, compaction, helper termination, repair, all reboot modes, and
a seeded reset that proves selected local/remote state and role numbering are
cleared without touching source repositories or unrelated tasks.
