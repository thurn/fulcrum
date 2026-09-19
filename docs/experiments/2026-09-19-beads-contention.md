# Beads contention measurements for Hive

Measured locally on September 19, 2026. **Beads server mode is a credible
performance option, but its unwrapped ready-task claiming failed under
contention. Embedded mode is too slow for Hive's proposed concurrent core.**

- [Hive design](../../hive-design.md)
- [Per-call measurements and correctness observations][data]
- [Reproduction script](../../scripts/measure-beads-contention.py)

## Method

The benchmark used installed Beads 1.2.2 (`6c124203e`) and Dolt 2.2.0 on
macOS 26.5.2 ARM64, with 18 logical CPUs and Python 3.12.14. It made **1,336
timed operations**, plus setup and verification calls.

Two disposable databases were used: embedded Dolt and a private Dolt server
bound to localhost on a separate port and data directory. No production
Beads database or existing server was used. Private servers were stopped
after testing.

Configuration and workloads:

- Stock `bd` CLI, bypassing Fulcrum. Fresh process per operation.
- `--sandbox --dolt-auto-commit off --json`; no configured remotes.
- 100 and 1,000 synthetic tasks, with blocking dependencies on approximately
  one quarter of the tasks, labels, descriptions, and small metadata objects.
- 1, 4, and 16 concurrent clients. Each worker starts at a barrier and issues
  sequential commands. Distinct-write workers update different tasks.
- Ready queries return ten tasks. Task reads fetch one complete task.
- Mixed cells alternate ready queries and independent updates.
- Eight samples per single-client cell, sixteen per four-client cell, and
  thirty-two per sixteen-client cell. Three ready-query warmups per size.
- Sample p95 uses nearest rank. These small samples are descriptive, not a
  production latency guarantee.

The embedded database retained one pilot record whose fields differed from
subsequently imported fixtures. Correctness probes add records after the
1,000-task latency matrix. Modes ran sequentially under normal host load;
there was no randomized order or CPU isolation.

## Query and update latency

All numbers below are **milliseconds**, including process startup and waiting.
These are the 1,000-task results; the raw artifact also contains the
100-task results. All **896 ordinary read/update operations succeeded**.

| Operation | Clients | Embedded p50 / p95 | Server p50 / p95 |
| --- | ---: | ---: | ---: |
| Ready query | 1 | 219 / 223 | 88 / 91 |
| Ready query | 4 | 689 / 2130 | 111 / 115 |
| Ready query | 16 | 1590 / 5708 | 181 / 244 |
| Task read | 1 | 323 / 329 | 69 / 73 |
| Task read | 4 | 330 / 4599 | 89 / 93 |
| Task read | 16 | 2148 / 8582 | 136 / 222 |
| Independent updates | 1 | 302 / 310 | 88 / 92 |
| Independent updates | 4 | 305 / 4574 | 185 / 220 |
| Independent updates | 16 | 1877 / 12127 | 527 / 986 |
| Mixed reads/writes | 1 | 256 / 303 | 91 / 95 |
| Mixed reads/writes | 4 | 773 / 2682 | 110 / 125 |
| Mixed reads/writes | 16 | 1545 / 8717 | 180 / 274 |

The contrast is large:

- At four clients, server read p95 was 93–115ms and independent-update p95
  was 220ms. Those fit Hive's proposed 250ms local-command budget.
- At sixteen clients, server reads remained around 222–244ms p95, while
  independent writes reached 986ms p95.
- Embedded mode reached 5.7s p95 for ready queries, 8.6s for task reads, and
  12.1s for independent updates at sixteen clients. Its slowest update took
  19.3s. No command hit the benchmark's 30-second timeout.
- Embedded throughput for those three workloads was only 4.0, 2.7, and
  1.6 successful operations per second respectively.

This supports rejecting embedded Beads for the concurrent coordination path.
It does **not** support claiming that all Beads queries inherently take a
second, or that the historical Fulcrum overhead was purely a database issue.

## Claiming and transaction correctness

The probes distinguish exclusive ownership from ready selection and from
Hive's shared capacity requirement.

### Claiming a specified task

Five rounds per mode had sixteen distinct actors concurrently execute
`bd update ID --claim` against one task. **Every round had exactly one
successful claimant**; all other callers reported an existing claim.

There were no observed duplicate owners. These expected refusals are not
counted as infrastructure failures.

### Claiming the next ready task

With sixteen clients making thirty-two `bd ready --claim` calls:

- Embedded: 32/32 succeeded, with 32 distinct tasks claimed.
- Server: 3/32 succeeded; 29 calls returned a serialization error.
- Two additional server runs reproduced exactly 3/32 successes each.
- A four-client server probe had 3/8 successes and five serialization errors.

The server error was:

```text
dolt commit: Error 1213 (40001): serialization failure:
this transaction conflicts with a committed transaction from another client,
try restarting transaction.
```

Only acknowledged claims appeared in the persisted assignment state in
these runs. There were no extra assignments left by failed calls and no
observed duplicate claims. This is a contention/liveness failure, not evidence
of duplicate ownership or corruption.

A process-shared `flock` around the claim operation removed the errors:

| Clients | Successful calls | p50 | p95 |
| ---: | ---: | ---: | ---: |
| 4 | 16/16 | 461ms | 471ms |
| 16 | 32/32 | 1,905ms | 1,929ms |

Lock wait is included. The sixteen-client burst completed in 3.82 seconds,
about 8.4 claims/second. This is a usable workaround for infrequent admission,
but misses a strict 250ms end-to-end admission target under contention.

### Shared capacity

The deliberately naive algorithm was: count active tasks, then claim a ready
task if the count is below four. A barrier forced all sixteen callers to read
the count before any claimed.

- Embedded admitted **16 tasks despite a cap of 4**.
- Server admitted only 2, because 14 claims failed with serialization errors.
  That failure is not capacity enforcement.
- A shared `flock` spanning count and claim admitted exactly 4 in both modes.
- The locked server burst had 952ms median and 1,534ms p95, including denied
  callers waiting their turn. Embedded p95 was 5,903ms.

An atomic claim is insufficient for Hive's full admission predicate. A Beads
integration needs every admission route to participate in the same protocol
for global/project capacity and repository exclusivity. This probe tests a
capacity cap only, not a complete implementation of Hive admission.

### Dependencies, rollback, and metadata

Both modes showed the same useful behavior and important boundary:

- `ready --claim` excluded a task with an unfinished prerequisite.
- `update ID --claim` nevertheless allowed that blocked task to be claimed.
  Hive's start-here path cannot assume direct claiming enforces dependencies.
- A two-operation `bd batch` with a valid update followed by an invalid task
  ID rolled back the valid update; the task's before/after state matched.
- Sixteen concurrent `--set-metadata workerN=N` operations on one task all
  succeeded, and all sixteen keys survived. No lost metadata patches were
  observed.

`bd batch` is a genuine improvement over independent write commands, but its
advertised grammar does not include querying capacity and conditionally
claiming within the batch. These results do not establish a native atomic
operation covering all of Hive's admission requirements.

## Locking limits and untested failures

A server-side two-second `SELECT SLEEP(2)` did not prevent a simultaneous
ready query from finishing in 113ms. That demonstrates an unrelated read can
proceed while that query is pending; it does not characterize every SQL lock.

The corresponding embedded SQL probe could not run: this installed build
reports that `bd sql` is unsupported in embedded mode. Its attempted kill
probe therefore did not kill an active lock holder. **No crash-release or
crash-durability conclusion is drawn from that probe.** The raw artifact marks
those observations invalid.

No long-duration soak, power-loss test, server-restart recovery test, SQLite
comparison, or whole-agent time/cost comparison was performed. Host load,
small samples, synthetic data, disabled Dolt auto-commit policy, and the
particular installed versions limit generalization.

## Consequence for Hive

**Beads could be used if Hive accepts a local Dolt service and a small,
shared admission wrapper.** The query measurements are encouraging enough
that performance alone should not eliminate that option.

However, the wrapper is real work:

- Serialize admission around dependencies, capacity, exclusivity, and claim.
- Use dependency-aware ready selection; check direct-start dependencies too.
- Keep reads and ordinary task edits outside that admission lock.
- Handle serialization failures explicitly; do not create retry storms or
  assume that an error always implies no effect beyond the tested cases.
- Keep task transitions to a few commands, and use real batches where their
  supported operations fit.

SQLite remains the simpler fit for the currently agreed daemon-free core
and a transaction spanning the complete admission predicate. That is still
an architectural recommendation, **not a measured SQLite performance win**.
Choosing Beads would exchange some custom task-storage code for a database
service and an admission/recovery integration. The measurements make that
tradeoff concrete rather than ruling Beads out categorically.

## Reproduction

The script consolidates the executed matrix and follow-up probes. It creates
only disposable repositories and a private server, and refuses an existing
explicit output directory. It requires `bd`, `dolt`, Git, and POSIX `flock`.

```sh
python3 scripts/measure-beads-contention.py
# Or choose a new output directory:
python3 scripts/measure-beads-contention.py --output-dir /tmp/hive-beads-new
```

Results and temporary databases remain in the printed directory for inspection.
Successful command payloads are omitted from the checked-in timing rows to
keep the artifact small; correctness observations and per-call timing/error
records remain. Follow-up probes in the recorded run restarted the private
server; the consolidated script runs them within the same server lifetime.

[data]: ../measurements/beads-contention-2026-09-19.json
