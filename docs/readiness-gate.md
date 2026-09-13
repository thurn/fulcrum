# Readiness

Setup enables ordinary dispatch only when the shared app-server handshake works,
the desktop/controller topology is verified, configured models and efforts are
advertised, selected Git/Codex/Tollgate project identities agree, the brain's Git
and Beads stores are reachable, checkout-backed assets are loaded, SQLite is
healthy, Archon exists, and explicit capacity and recurring policies are stored.

Readiness also requires recent reconciliation, all critical workers running, no
capacity or lease invariant violation, no progress-free recovery, and no external
operation left ambiguous after targeted observation. Both launchd jobs must be
loaded with their installed environment; an unmanaged listener cannot satisfy the
app-server topology check.

`fulcrum doctor --json` reports each actual check and never consumes checked-in
success evidence. A missing observation is unavailable, not successful. One
project's failed integration blocks that project; a runtime connection failure
disables all new managed starts.
