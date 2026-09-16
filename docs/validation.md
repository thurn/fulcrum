# Validation

## Prepare dependencies

Run once in each checkout, and again after changing dependency or packaging inputs:

```sh
scripts/prepare-check
```

This installs `requirements-dev.lock` and the editable package into `.venv` without
starting Fulcrum services. Set `FULCRUM_CHECK_VENV` to select a different prepared
environment or `FULCRUM_PYTHON` to select Python 3.12 for environment creation.
Dependency provisioning is outside the check's runtime budget.

## Complete repository check

```sh
scripts/check
```

The command checks formatting, performs full strict type checking, and discovers
all tests. It prints phase durations and the five slowest tests. Normal execution
should finish within 30 seconds; the hard deadline is 55 seconds, including child
process termination. Failed, skipped, empty, or timed-out tests cannot produce a
passing check. Dependencies are never installed or environments rebuilt here.

Tests exercise real Fulcrum decision logic with small in-memory record stores and
mocked external adapter results. Coverage prioritizes request reuse and conflicts,
ownership, dependencies, capacity and admission, exact recovery matching, delivery
evidence, runtime protocols, CLI contracts, and resource cleanup boundaries.
Small temporary-file and local-socket tests remain where they test those interfaces.
Unexpected process execution fails immediately; exceptions are the launcher
stub and exact local Python commands for source-pinning and process-lock tests.

Beads, Dolt, Git, Tollgate, Codex, remote services, and model calls are not required
to run the tests. These checks do not establish live provider compatibility or
prove complete production workflows. New regressions should be expressed through
the narrowest relevant production boundary, using clocks/events instead of long
waits and adapter results instead of external installations.

## Retired validation

The former deterministic CLI, eight-role live workflow, and thirty-task smoke
harnesses were deleted, together with their expensive integration suites. There
is no optional or nightly copy. The `fixture`, `scenario`, and `smoke concurrency`
commands, file-backed simulated providers, and crash-injection hooks were removed.
Runtime configuration now requires `codex`; delivery configuration requires
`tollgate`. Invalid retired kinds are rejected rather than migrated.

[Historical replacement reports](fulcrum2/validation-results.md) describe earlier
observations only. They impose no rerun or shipping requirement. The repository
acceptance gate is the complete check above.

## Measured acceptance

On 2026-09-15, the complete check passed locally in **3.55 seconds with cold
formatter/type-check caches**, then **3.14 and 3.44 seconds** on repeated runs.
Each run included formatting, full strict type checking, and all **79 tests**.
The tests also passed with Beads, Dolt, Git, Tollgate, and Codex unavailable on PATH.

An intentionally failing test returned exit 1. An intentionally stalled test was
terminated after 53.11 seconds with exit 124; a separate process-tree probe verified
that timeout cleanup terminates grandchildren too. Those temporary acceptance
probes were removed. These timings exclude dependency provisioning and describe
this local execution environment, not an unmeasured remote runner.
