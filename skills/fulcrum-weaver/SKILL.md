---
name: weaver
description: Clarify intent and author a small Fulcrum task or substantial plan.
---

Immediately run `fulcrum weaver register`, passing `--plan-mode` in read-only
Plan Mode. Follow the returned mode-specific authoring instructions. Planning
registration establishes identity only; do not publish or call finish in Plan Mode.
After approval in writable mode, register again to establish the authoring action.

For a small understood request, including an imperative such as "fix this" or
"change this file," author and file one task; do not implement the requested
project change. Use `fulcrum intake --input -` with the JSON and single-quoted
heredoc form supplied by registration. Small tasks need no plan or helper reviews.
Substantial plans require separate cold-reader and requirements-verifier helpers
as described in the returned guidance. Resolve model preferences conversationally;
both implementation roles default to Sol/high.

The registration response supplies authoring guidance, exact completion commands,
and intake examples directly. Python owns registration, issue
publication, retries, scheduling, and archival. Finish after authored intake is
retained, without waiting for Archon or remote synchronization.
