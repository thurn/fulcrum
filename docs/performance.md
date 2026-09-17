# Performance tracing

Fulcrum has opt-in, correlated spans for finding operation latency rather than
guessing from the duration of a whole command. Instrumentation is inert unless
`FULCRUM_TIMING_FILE` is set and never records request payloads, paths, results,
or descriptions.

## Profile an operation

Run a representative command through the repository tool:

```sh
scripts/profile-operation record --output /tmp/status-timing.jsonl \
  --label status -- fulcrum status --json
```

The command keeps its normal stdout and stderr. The profiler adds one wall-clock
root span and prints an analysis after the command completes. The timing file
contains nested spans with a trace ID, span ID, parent span ID, process IDs,
monotonic start, duration, outcome, label, and sample number.

Use multiple samples only for safe, repeatable commands. Every sample executes the
command again; do not repeat a mutation unless its normal idempotency contract is
intentional.

```sh
scripts/profile-operation record --output /tmp/status-timing.jsonl \
  --label status --samples 10 -- fulcrum status --json
```

Append separately measured variants to compare labels in one report:

```sh
scripts/profile-operation record --append --output /tmp/comparison.jsonl \
  --label before --samples 5 -- fulcrum status --json
scripts/profile-operation record --append --output /tmp/comparison.jsonl \
  --label after --samples 5 -- fulcrum status --json
```

## Analyze and visualize

Analyze one or more existing timing streams without running an operation:

```sh
scripts/profile-operation analyze /tmp/status-timing.jsonl
scripts/profile-operation analyze /tmp/status-timing.jsonl --json
```

The report ranks stages by exclusive (self) time, which avoids double-counting
nested spans, and shows p50, p95, maximum, call count, wall-time share, and the
slowest inclusive spans. `unattributed command.wall` is measured wall time not
covered by child spans; a large value is a concrete prompt to instrument startup,
imports, process handoff, or another missing boundary.

Export the same events for a timeline/flame view in Perfetto or Chrome tracing:

```sh
scripts/profile-operation analyze /tmp/status-timing.jsonl \
  --chrome-trace /tmp/status-trace.json
```

## Coverage

Current spans distinguish source selection and preparation, CLI request building,
application dispatch and exact command handlers, operation coordination, lock
categories and wait time, external-effect boundaries, configuration loads, Beads
process-slot waits, individual Beads verbs, output capture, receipt operations,
and complete command wall time. Spans propagate through the source-following
launcher and share one trace across its processes without changing source leases
or restarting the resident.

For ad hoc collection without the wrapper, point `FULCRUM_TIMING_FILE` at an
absolute caller-managed file before invoking `fulcrum`. Those rows remain
analyzable, though the stream has no `command.wall` root unless it was recorded by
the profiler.
