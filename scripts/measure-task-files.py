#!/usr/bin/env python3
"""Disposable task-store microbenchmark: Markdown/YAML vs SQLite, no Hive code."""

import argparse
from contextlib import closing
import concurrent.futures as cf
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

BODY = (
    "\n## Requested work\n\nInvestigate the failure, implement a focused fix, and validate the result.\n"
    * 42
)
READY_SQL = """SELECT t.* FROM tasks t WHERE t.status='queued'
AND NOT EXISTS (SELECT 1 FROM dependencies d JOIN tasks p ON p.id=d.prerequisite
WHERE d.task=t.id AND p.status!='done') ORDER BY t.priority,t.id LIMIT 10"""


def fixture(i):
    return {
        "id": f"H-{i:04d}",
        "title": f"Repair task {i}",
        "status": "queued" if i < 200 else "done",
        "priority": i % 5,
        "project": "probe",
        "owner": "",
        "assignment": 0,
        "exclusive": False,
        "dependencies": [f"H-{i-1:04d}"] if i < 200 and i % 4 == 3 else [],
        "notes": "",
        "body": BODY,
    }


def encode(task):
    import yaml

    return (
        "---\n"
        + yaml.dump(
            {k: v for k, v in task.items() if k != "body"},
            Dumper=yaml.CSafeDumper,
            sort_keys=True,
        )
        + "---\n"
        + task["body"]
    )


def parse_file(path, loader, full=False):
    import yaml

    with path.open() as f:
        if f.readline() != "---\n":
            raise ValueError("missing frontmatter")
        lines = []
        for line in f:
            if line.rstrip("\n") == "---":
                break
            lines.append(line)
        else:
            raise ValueError("unterminated frontmatter")
        task = yaml.load("".join(lines), Loader=loader)
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("id"), str)
            or not isinstance(task.get("dependencies"), list)
        ):
            raise ValueError("invalid task structure")
        if full:
            task["body"] = f.read()
    return task


def atomic_write(path, task):
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(encode(task))
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def files_op(root, backend, op, worker, iteration):
    import yaml

    loader = yaml.CSafeLoader if backend == "files-c" else yaml.SafeLoader
    tasks = root / backend

    def scan():
        return [parse_file(p, loader) for p in sorted(tasks.glob("*.md"))]

    def ready(rows):
        index = {t["id"]: t for t in rows}
        return sorted(
            [
                t
                for t in rows
                if t["status"] == "queued"
                and all(index[d]["status"] == "done" for d in t["dependencies"])
            ],
            key=lambda t: (t["priority"], t["id"]),
        )[:10]

    if op == "show":
        return parse_file(tasks / f"H-{worker*4:04d}.md", loader, True)["id"]
    if op in ("ready", "scan"):
        rows = scan()
        return [t["id"] for t in ready(rows)] if op == "ready" else len(rows)
    # Updates and admissions use the same permanent directory-level lock so
    # scans driving admission cannot mix states from simultaneous writers.
    with (tasks / ".admission.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if op == "update":
            path = tasks / f"H-{worker*4:04d}.md"
            t = parse_file(path, loader, True)
            t["notes"] = f"{worker}/{iteration}"
        else:
            rows = scan()
            if op == "claim-cap" and sum(t["status"] == "assigned" for t in rows) >= 4:
                return "capacity-full"
            candidates = ready(rows)
            if not candidates:
                return "no-ready-work"
            path = tasks / (candidates[0]["id"] + ".md")
            t = parse_file(path, loader, True)
            t.update(
                status="assigned",
                owner=f"worker-{worker}-{iteration}",
                assignment=t["assignment"] + 1,
            )
        atomic_write(path, t)
        return t["id"]


def sqlite_op(root, op, worker, iteration):
    with closing(
        sqlite3.connect(root / "tasks.sqlite", timeout=5, isolation_level=None)
    ) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA synchronous=FULL")
        if op == "show":
            return con.execute(
                "SELECT * FROM tasks WHERE id=?", (f"H-{worker*4:04d}",)
            ).fetchone()["id"]
        if op == "ready":
            return [r["id"] for r in con.execute(READY_SQL)]
        if op == "scan":
            return len(con.execute("SELECT * FROM tasks").fetchall())
        con.execute("BEGIN IMMEDIATE")
        try:
            if op == "update":
                ident = f"H-{worker*4:04d}"
                con.execute(
                    "UPDATE tasks SET notes=? WHERE id=?",
                    (f"{worker}/{iteration}", ident),
                )
            else:
                if (
                    op == "claim-cap"
                    and con.execute(
                        "SELECT count(*) FROM tasks WHERE status='assigned'"
                    ).fetchone()[0]
                    >= 4
                ):
                    con.commit()
                    return "capacity-full"
                row = con.execute(READY_SQL).fetchone()
                if row is None:
                    con.commit()
                    return "no-ready-work"
                ident = row["id"]
                con.execute(
                    "UPDATE tasks SET status='assigned',owner=?,assignment=assignment+1 WHERE id=?",
                    (f"worker-{worker}-{iteration}", ident),
                )
            con.commit()
            return ident
        except BaseException:
            con.rollback()
            raise


def operation(root, backend, op, w, i):
    return (
        sqlite_op(root, op, w, i)
        if backend == "sqlite"
        else files_op(root, backend, op, w, i)
    )


def stats(rows):
    ms = sorted(x["ms"] for x in rows)
    return {
        "n": len(rows),
        "p50_ms": statistics.median(ms),
        "p95_ms": ms[math.ceil(0.95 * len(ms)) - 1],
        "max_ms": max(ms),
        "errors": sum(x.get("code", 0) != 0 for x in rows),
    }


def setup(root):
    root.mkdir(parents=True, exist_ok=True)
    for backend in ("files-c", "files-python"):
        directory = root / backend
        directory.mkdir()
        for i in range(1000):
            (directory / f"H-{i:04d}.md").write_text(encode(fixture(i)))
    with sqlite3.connect(root / "tasks.sqlite") as con:
        con.executescript("""PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
CREATE TABLE tasks(id TEXT PRIMARY KEY,title TEXT,status TEXT,priority INTEGER,project TEXT,owner TEXT,assignment INTEGER,exclusive INTEGER,notes TEXT,body TEXT);
CREATE TABLE dependencies(task TEXT,prerequisite TEXT,PRIMARY KEY(task,prerequisite));
CREATE INDEX task_ready ON tasks(status,priority,id);
""")
        for i in range(1000):
            t = fixture(i)
            con.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    t[k]
                    for k in (
                        "id",
                        "title",
                        "status",
                        "priority",
                        "project",
                        "owner",
                        "assignment",
                        "exclusive",
                        "notes",
                        "body",
                    )
                ),
            )
            con.executemany(
                "INSERT INTO dependencies VALUES(?,?)",
                [(t["id"], d) for d in t["dependencies"]],
            )


def reset_claims(root, backend):
    if backend == "sqlite":
        with sqlite3.connect(root / "tasks.sqlite") as con:
            con.execute(
                "UPDATE tasks SET status='queued',owner='' WHERE status='assigned'"
            )
    else:
        import yaml

        for p in (root / backend).glob("*.md"):
            t = parse_file(p, yaml.CSafeLoader, True)
            if t["status"] == "assigned":
                t.update(status="queued", owner="")
                atomic_write(p, t)


def main(root):
    import yaml, platform

    print(f"Output: {root}", flush=True)
    if root.exists() and any(root.iterdir()):
        raise ValueError("Benchmark requires an empty disposable directory")
    setup(root)
    report = {
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "pyyaml": yaml.__version__,
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "task_count": 1000,
        "open_tasks": 200,
        "blocked_tasks": 50,
        "body_bytes": len(BODY.encode()),
        "runs": [],
        "checks": [],
    }

    def save():
        (root / "results.json").write_text(json.dumps(report, indent=2))

    expected = operation(root, "sqlite", "ready", 0, 0)
    for backend in ("files-c", "files-python"):
        assert operation(root, backend, "ready", 0, 0) == expected
    # Separate algorithm time in a warm process from one fresh process per CLI.
    for backend in ("files-c", "files-python", "sqlite"):
        for op in ("show", "scan", "ready", "update"):
            operation(root, backend, op, 0, 0)
            rows = []
            for j in range(8):
                start = time.perf_counter()
                val = operation(root, backend, op, 0, j)
                rows.append({"ms": (time.perf_counter() - start) * 1000, "value": val})
            entry = {
                "mode": "warm-process",
                "backend": backend,
                "op": op,
                "clients": 1,
                **stats(rows),
                "rows": rows,
            }
            report["runs"].append(entry)
            save()
            print({k: v for k, v in entry.items() if k != "rows"}, flush=True)
    # OS caches warm, but each command imports its libraries afresh.
    for backend in ("files-c", "files-python", "sqlite"):
        for clients in (1, 4, 16):
            for op in ("show", "ready", "update", "claim", "mixed"):
                if backend == "files-python" and clients == 16:
                    continue
                if op == "claim":
                    reset_claims(root, backend)
                barrier = threading.Barrier(clients)

                def worker(w):
                    barrier.wait()
                    rows = []
                    for j in range(8 if clients == 1 else 2):
                        start = time.perf_counter()
                        try:
                            p = subprocess.run(
                                [
                                    sys.executable,
                                    __file__,
                                    "--worker",
                                    backend,
                                    op,
                                    str(w),
                                    str(j),
                                    "--root",
                                    str(root),
                                ],
                                capture_output=True,
                                text=True,
                                timeout=30,
                            )
                            rows.append(
                                {
                                    "ms": (time.perf_counter() - start) * 1000,
                                    "code": p.returncode,
                                    "stdout": p.stdout,
                                    "stderr": p.stderr,
                                }
                            )
                        except subprocess.TimeoutExpired:
                            rows.append(
                                {
                                    "ms": (time.perf_counter() - start) * 1000,
                                    "code": 124,
                                    "stderr": "timeout",
                                }
                            )
                    return rows

                start = time.perf_counter()
                with cf.ThreadPoolExecutor(max_workers=clients) as pool:
                    rows = [
                        r for batch in pool.map(worker, range(clients)) for r in batch
                    ]
                entry = {
                    "mode": "fresh-cli",
                    "backend": backend,
                    "op": op,
                    "clients": clients,
                    "wall_seconds": time.perf_counter() - start,
                    **stats(rows),
                    "rows": rows,
                }
                report["runs"].append(entry)
                save()
                print({k: v for k, v in entry.items() if k != "rows"}, flush=True)
                assert entry["errors"] == 0, entry
                if op == "claim":
                    ids = [json.loads(r["stdout"])["value"] for r in rows]
                    assert len(set(ids)) == len(ids), (backend, ids)
                    report["checks"].append(
                        {
                            "backend": backend,
                            "clients": clients,
                            "unique_claims": len(ids),
                        }
                    )
    # Capacity in a burst is distinct from unlimited claims.
    for backend in ("files-c", "sqlite"):
        reset_claims(root, backend)
        barrier = threading.Barrier(16)

        def worker(w):
            barrier.wait()
            start = time.perf_counter()
            p = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--worker",
                    backend,
                    "claim-cap",
                    str(w),
                    "0",
                    "--root",
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {
                "ms": (time.perf_counter() - start) * 1000,
                "code": p.returncode,
                "stdout": p.stdout,
                "stderr": p.stderr,
            }

        with cf.ThreadPoolExecutor(max_workers=16) as pool:
            rows = list(pool.map(worker, range(16)))
        assert all(r["code"] == 0 for r in rows)
        ids = [json.loads(r["stdout"])["value"] for r in rows]
        assert ids.count("capacity-full") == 12
        report["runs"].append(
            {
                "mode": "fresh-cli",
                "backend": backend,
                "op": "claim-cap",
                "clients": 16,
                **stats(rows),
                "rows": rows,
            }
        )
        report["checks"].append(
            {
                "backend": backend,
                "capacity": 4,
                "admitted": 16 - ids.count("capacity-full"),
            }
        )
        save()
    print("Completed", root, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--worker", nargs=4)
    args = parser.parse_args()
    if args.worker:
        backend, op, w, i = args.worker
        start = time.perf_counter()
        value = operation(
            args.root,
            backend,
            ("ready" if (int(w) + int(i)) % 2 else "update") if op == "mixed" else op,
            int(w),
            int(i),
        )
        print(
            json.dumps(
                {"value": value, "operation_ms": (time.perf_counter() - start) * 1000}
            )
        )
    else:
        main(args.root or Path(tempfile.mkdtemp(prefix="hive-files-bench-")))
