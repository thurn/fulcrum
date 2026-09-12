# Synthetic postmortem: repeated context reads

Scope: fixture fleet interval 2026-09-10 through 2026-09-11, since the prior
fixture postmortem. Reviewed role entry points, context/handoff boundaries,
resource observations, and Tollgate evidence. Hook runtime delivery and token
metrics are unavailable in this fixture; they are not assumed zero.

Project: fulcrum. Underlying problem: redundant-context. Suggested priority: P2.
Evidence: the controlled finding input in tests/test_findings.py describes three
repeated full-context reads. This is synthetic evidence, not a production timing.
Expected benefit: fewer repeated reads and less context churn. No measured
speedup or token savings are claimed. Compare matched runs before claiming gains.
Proposed change: reuse bounded context until an assignment or hold changes.
Affected interfaces: role entry points and the context reader.
Acceptance: unchanged input avoids repeated reads; changed assignment/hold input
refreshes promptly. Validate call counts and correctness on both scenarios.

Update the existing issue by underlying problem; preserve active scope and
mandates. Any implementation remains future work for Archon scheduling.
Missing interviews are reported with evidence limits; archive restoration stays
an explicit obligation until verified. Raw logs remain in the originating tools.
