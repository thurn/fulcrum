# Markdown task files versus SQLite: local measurements

Measured September 20, 2026. **At 1,000 tasks, Markdown files with YAML
frontmatter were fast enough for sub-second operation in every tested
fast-parser case.** SQLite was substantially faster for queries and contended
claims, but file storage was not intrinsically too slow for Hive.

- [Hive design](../../hive-design.md)
- [Per-operation results](../measurements/task-files-2026-09-20.json)
- [Reproduction script](../../scripts/measure-task-files.py)
- [Earlier Beads measurements](2026-09-19-beads-contention.md)

## What was compared

This benchmark used disposable storage prototypes. It did not implement Hive.
The final run recorded **688 timed operations**, with no operation failures.

The same task fixture was represented in three ways:

- One Markdown file per task, parsed with PyYAML's C-backed safe loader.
- The same files, parsed with PyYAML's pure-Python safe loader.
- A SQLite database with an index on status/priority/ID and a dependency table.

There were 1,000 tasks initially: 200 queued, including 50 blocked by unfinished
prerequisites, and 800 completed. Each task had about 4KB of Markdown body and
metadata for identity, title, status, priority, project, owner, assignment,
exclusivity, dependencies, and notes.

The file implementation read only YAML frontmatter for global queries. It
read and preserved the full Markdown body when updating a task. It did not
use a persistent index, watcher, resident task cache, or database.

The host was macOS 26.5.2 ARM64 with 18 logical CPUs, Python 3.12.14, PyYAML
6.0.3, and SQLite 3.53.4. Filesystem caches were warm; cold-storage latency and
network filesystems were not tested.

## Complete command latency

These numbers include **fresh Python process startup and imports for every
operation**. Entries are **p50 / p95 in milliseconds**.

| Operation | Clients | Markdown + fast YAML | SQLite |
| --- | ---: | ---: | ---: |
| Read one task | 1 | 42 / 44 | 34 / 35 |
| Read one task | 4 | 50 / 53 | 40 / 43 |
| Read one task | 16 | 63 / 74 | 56 / 75 |
| Find ready tasks | 1 | 85 / 91 | 35 / 37 |
| Find ready tasks | 4 | 104 / 109 | 40 / 42 |
| Find ready tasks | 16 | 180 / 195 | 56 / 76 |
| Update one task | 1 | 43 / 45 | 35 / 38 |
| Update one task | 4 | 46 / 47 | 38 / 44 |
| Update one task | 16 | 74 / 102 | 55 / 71 |
| Claim next task | 1 | 86 / 93 | 36 / 40 |
| Claim next task | 4 | 185 / 226 | 40 / 43 |
| Claim next task | 16 | 688 / 721 | 55 / 71 |
| Mixed reads/updates | 1 | 65 / 89 | 37 / 39 |
| Mixed reads/updates | 4 | 71 / 96 | 40 / 41 |
| Mixed reads/updates | 16 | 113 / 166 | 54 / 65 |

Queries returned the ten highest-priority tasks whose dependencies were done.
File queries scanned and parsed all 1,000 headers. SQLite queried its indexes.
Mixed cells alternated ready queries and updates while workers ran concurrently.

The small samples were eight calls per cell at one or four clients, and
thirty-two calls at sixteen clients. The p95 values are nearest-rank sample
statistics, not established production SLOs. Modes ran sequentially under
normal host load.

## Where the time goes

With imports already loaded in one process, median operation times were:

| Operation | Fast YAML files | Pure-Python YAML files | SQLite |
| --- | ---: | ---: | ---: |
| Read one known task | 0.08ms | 0.19ms | 0.35ms |
| Read/parse all 1,000 tasks | 62.34ms | 202.41ms | 1.91ms |
| Compute ready tasks | 60.57ms | 180.78ms | 0.34ms |
| Update one task | 0.41ms | 0.41ms | 0.41ms |

The initial exploratory run measured a fast YAML scan at roughly 41ms; the
final run measured 62ms. This variation is a reason to avoid treating these
small samples as precise latency guarantees. Fresh-command readiness latency
was more similar between runs: approximately 85–91ms p95 with one client.

SQLite's full-scan row includes fetching its stored task bodies; its readiness
query uses an index and dependency lookup instead of scanning all task bodies.
The file scan stops at the end of each YAML header. The SQL query and the file
query returned the same initial ready-task IDs and ordering.

**Parser selection matters.** With the pure-Python YAML loader, readiness-query
p95 was 226ms at one client and 239ms at four clients. Contended claim p95 was
776ms at four clients, versus 226ms with the C-backed loader. The pure-Python
variant was not tested at sixteen clients. Both variants used the C-backed
dumper for writes; neither preserved YAML comments or original header layout.

## Locking and updates

All file mutations used a permanent, process-shared admission lock. A claim
held that lock while scanning task state, selecting ready work, and replacing
one task file. Slot occupancy was derived from task state, not a separate
counter.

A file update performed:

1. Acquire the shared mutation lock and read the current task.
2. Write a complete replacement into a temporary file in the same directory.
3. Flush and `fsync` that temporary file.
4. Atomically replace the task file.
5. `fsync` the directory, then release the lock.

SQLite mutations used `BEGIN IMMEDIATE`, WAL mode, and `synchronous=FULL`.
The measured operation opened and closed its connection on every call.

There were no duplicate claims in the tested bursts. A separate sixteen-client
capacity probe admitted exactly four tasks and rejected twelve for both the
fast-parser file implementation and SQLite. That probe's p95 was **749ms for
files and 77ms for SQLite**.

The main source of file admission latency is holding a shared lock during a
whole-directory scan. More simultaneous claimants queue behind those scans.
Reading or replacing one known task is cheap; selecting the next task is the
operation that scales with the number of files.

## What the file approach does not automatically provide

An atomic replacement protects one file from readers observing a partially
written replacement. It does not make several file replacements atomic as a
group. Likewise, an unlocked scan can observe files from different moments
when other workers update them during the scan.

For Hive, that suggests a deliberate distinction:

- Dashboard and exploratory queries may tolerate a slightly mixed view.
- Admission must validate its decision while holding the shared lock.
- Related state that must change together should fit in one task file where
  possible. Otherwise the design needs an explicit multi-file transition or
  recovery rule.
- All workers must use the same authoritative task directory, independent of
  their isolated code worktrees.
- Direct editor writes, Git checkout/pull, and other tools do not automatically
  honor Hive's lock. Live metadata editing needs a defined coordination rule.

Git makes text history and publication convenient, but does not itself supply
an atomic update of several files in the live working directory.

No process-kill, power-loss, full-disk, malformed-file, or multi-file recovery
experiments were performed. The flush calls were measured, but neither backend
was configured with macOS `F_FULLFSYNC`, and this benchmark does not establish
identical crash-durability guarantees.

## Implications for Hive

At this scale, **do not introduce an index or database just to avoid reading
1,000 small YAML headers**. A fast parser and a straightforward scan were enough
for the tested workload. File storage naturally provides editable task bodies,
readable Git diffs, and a portable persistence format.

SQLite buys much faster indexed queries, consistent database snapshots, and
multi-record transactions. Those are reasons to choose it even when both
options meet a sub-second usability target. It also becomes more attractive
as backlog size and the rate of concurrent mutations grow.

The file algorithm's scan work grows approximately with task count, plus
sorting; SQLite need not read every historical task for a ready query. No
10,000-task result is implied by these measurements.

If most Hive transitions update one task and cross-task operations can safely
be split, **Markdown/YAML deserves consideration as the primary store**.
If Hive requires many all-or-nothing changes across tasks, implementing that
on files could erase the simplicity benefit.

The earlier Beads benchmark measured a complete third-party CLI with audit and
version-history work. This prototype does not implement those responsibilities,
so its lower latency is not a feature-equivalent comparison with Beads.

## Reproduction

Run with Python that has PyYAML and its C extension available:

```sh
.venv/bin/python scripts/measure-task-files.py
```

The script creates a disposable directory and prints its location. Results and
fixtures remain there for inspection. It tests warm-process operations, fresh
CLI calls, concurrent queries and writes, unique claims, and capacity admission.
It does not create or modify a live Hive task store.
