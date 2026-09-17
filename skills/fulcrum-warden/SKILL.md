---
name: fulcrum-warden
description: Review, validate, and deliver one exact registered candidate.
---

Register through `register_worker` before review and include the assignment token
with every later Fulcrum MCP mutation. Inspect the exact Executor source
in the retained Tollgate workspace. Fix only bounded review findings, invalidate
stale evidence when source changes, submit the exact candidate through
`submit_candidate`, and call `wait_for_ci_results` once. The call remains pending
until terminal evidence; do not poll. On passing exact-source CI, complete the
retained promotion/synchronization obligations and call `finish`. A failed result
uses the same task and repair allowance.
