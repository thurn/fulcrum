# Fulcrum2 replacement validation results

Historical observations from retired harnesses. These reports are not current
acceptance gates and must not be rerun as repository checks. The current gate is
[`scripts/check`](../validation.md), with a 55-second prepared-environment budget.

Updated 2026-09-15. These are observed reports, not inferred status. Report paths
are retained local artifacts from isolated fixtures; each JSON report has a matching
Markdown rendering.

## Deterministic installed CLI — passed

- Invocation: `scripts/validate-fulcrum2-cli`
- Installed source commit: `c30be197c15e6454d5c492d804c0f1b6cbd86039`
- JSON: `/private/tmp/fulcrum2-task22/committed/fulcrum2-cli-20260915T081300Z-a7d97070.json`
- Markdown: `/private/tmp/fulcrum2-task22/committed/fulcrum2-cli-20260915T081300Z-a7d97070.md`
- Result: all eight deterministic cases passed; fixture cleanup removed the exact
  owned services, provider registrations, tasks, runtime, and fixture root.

## Live Luna eight-role workflow — passed

- Invocation: `scripts/validate-fulcrum2-live --model gpt-5.6-luna --effort low`
- Installed source commit: `d663d1dd39ff8da6c8dcab431e5672571aded0af`
- JSON: `/private/tmp/fulcrum2-live-task23-run34.json`
- Markdown: `/private/tmp/fulcrum2-live-task23-run34.md`
- Result: passed in 1900.004 seconds with 65/65 assertions. Real Vizier, Marshal,
  Weaver, Executor, Warden, Sage, Mason, and Justiciar turns were observed. The
  delivery path produced actual Tollgate/Git source evidence. Cleanup had no
  failures and removed the fixture root.
- Additional focused Justiciar evidence:
  `/private/tmp/fulcrum2-live-justiciar-focused.json`.

## Thirty-task concurrency — acceptance closeout pending

Run 9 (`/private/tmp/fulcrum2-concurrency-task24-run9.json`, installed commit
`8a186e8d2fbf14f42e44d6c33e803f05f8a47888`) observed all core concurrency facts:
30 distinct native tasks and turns active together, 30 public barrier arrivals,
responsive status, no supported overload condition, and barrier release. All 14
assertions reached in that phase passed. The run exhausted its budget during a
redundant terminal polling phase and therefore correctly remained failed; its
fixture-root cleanup was incomplete.

Run 11 (`/private/tmp/fulcrum2-concurrency-task24-run11.json`, installed commit
`5bedf48dab32108420fd4c11ac9145c2a9f37442`) verified exact cleanup with no cleanup
failures, stopped the owned runtime normally, removed owned provider registrations
and 27 created tasks, and removed the fixture root. It failed before the peak probe
because that control request queued behind long-held IPC requests. Commit `7809b13`
raises bounded IPC handler headroom for the three expected 30-wide request classes
plus control traffic and adds a focused regression. A full passing run after that
fix is still required; neither prior partial run is labeled success.

## Current acceptance policy

The harnesses that produced these reports have been removed. Their former pending
closeout requirements are retired. Use the complete repository check described in
[validation](../validation.md); these historical reports make no claim about the
current suite's coverage or live provider compatibility.
