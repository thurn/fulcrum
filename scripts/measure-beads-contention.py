#!/usr/bin/env python3
"""Disposable local Beads contention probe. Never discovers a production DB."""

import argparse
import tempfile
import concurrent.futures as cf
import fcntl
import json
import math
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import threading
import time

ROOT = Path()  # Set to a fresh disposable directory by the entry point.
ENV = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith(("BEADS_", "BD_", "DOLT_", "GIT_"))
}
ENV.update(BD_NON_INTERACTIVE="1", DO_NOT_TRACK="1")
RESULT = {
    "platform": platform.platform(),
    "cpu_count": os.cpu_count(),
    "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "runs": [],
    "checks": [],
}


def save():
    (ROOT / "results.json").write_text(json.dumps(RESULT, indent=2))


def call(repo, args, actor="bench", stdin=None, timeout=30):
    cmd = [
        "bd",
        *([] if args[0] == "init" else ["-C", str(repo)]),
        "--sandbox",
        "--dolt-auto-commit",
        "off",
        "--actor",
        actor,
        "--json",
        *args,
    ]
    start = time.perf_counter()
    try:
        p = subprocess.run(
            cmd,
            cwd=repo,
            env=ENV,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        row = {
            "ms": (time.perf_counter() - start) * 1000,
            "code": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
        }
    except subprocess.TimeoutExpired as e:
        row = {
            "ms": (time.perf_counter() - start) * 1000,
            "code": 124,
            "stdout": "",
            "stderr": "probe timeout",
        }
    return row


def must(repo, args, **kw):
    r = call(repo, args, **kw)
    if r["code"]:
        raise RuntimeError(f"{args}: {r}")
    return json.loads(r["stdout"]) if r["stdout"].strip() else None


def summary(rows):
    ms = sorted(r["ms"] for r in rows)
    ok = [r["ms"] for r in rows if r["code"] == 0]
    errors = {}
    for r in rows:
        if r["code"]:
            key = r["stderr"].strip()[:500] or r["stdout"].strip()[:500]
            errors[key] = errors.get(key, 0) + 1
    return {
        "n": len(rows),
        "successes": len(ok),
        "p50_ms": statistics.median(ms),
        "p95_ms": ms[math.ceil(0.95 * len(ms)) - 1],
        "max_ms": max(ms),
        "success_p50_ms": statistics.median(ok) if ok else None,
        "errors": errors,
    }


def run(repo, mode, size, workload, clients, op, iterations=None):
    iterations = iterations or (8 if clients == 1 else 4 if clients == 4 else 2)
    barrier = threading.Barrier(clients)

    def worker(i):
        barrier.wait()
        return [op(i, j) for j in range(iterations)]

    start = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=clients) as pool:
        rows = [row for batch in pool.map(worker, range(clients)) for row in batch]
    wall = time.perf_counter() - start
    entry = {
        "mode": mode,
        "size": size,
        "workload": workload,
        "clients": clients,
        "wall_seconds": wall,
        "success_ops_per_second": sum(r["code"] == 0 for r in rows) / wall,
        **summary(rows),
        "rows": rows,
    }
    RESULT["runs"].append(entry)
    save()
    print(
        mode,
        size,
        workload,
        clients,
        json.dumps({k: v for k, v in entry.items() if k not in ("rows", "errors")}),
        flush=True,
    )
    if entry["errors"]:
        print("ERROR EXAMPLE", next(iter(entry["errors"])), flush=True)
    return entry


def seed(repo, start, end):
    lines = []
    for i in range(start, end):
        item = {
            "id": f"hb-{i:06d}",
            "title": f"Fixture task {i}",
            "description": "Synthetic local benchmark task; no production data. " * 4,
            "issue_type": "task",
            "status": "open",
            "priority": i % 5,
            "created_at": "2026-09-01T00:00:00Z",
            "updated_at": "2026-09-19T00:00:00Z",
            "labels": ["fixture"],
            "metadata": {"project": "hive-probe", "index": i},
        }
        if i % 4 == 3:
            item["dependencies"] = [
                {
                    "issue_id": item["id"],
                    "depends_on_id": f"hb-{i-1:06d}",
                    "type": "blocks",
                }
            ]
        lines.append(json.dumps(item))
    r = call(repo, ["import", "-"], stdin="\n".join(lines) + "\n", timeout=120)
    RESULT["checks"].append(
        {"mode": repo.name, "check": "seed", "start": start, "end": end, **r}
    )
    save()
    if r["code"]:
        raise RuntimeError(r)
    print(repo.name, "seeded", end, "ms", r["ms"], flush=True)


def create(repo, ident, label):
    return must(
        repo,
        [
            "create",
            f"Probe {ident}",
            "--id",
            ident,
            "--labels",
            label,
            "--description",
            "Disposable concurrency test",
        ],
    )


def correctness(repo, mode):
    # Repeated many-actor competition for exactly one task.
    for trial in range(5):
        ident = f"hb-race{trial}"
        create(repo, ident, "race")
        r = run(
            repo,
            mode,
            1000,
            "same_task_claim",
            16,
            lambda i, j: call(repo, ["update", ident, "--claim"], f"claim-{trial}-{i}"),
            1,
        )
        state = must(repo, ["show", ident])
        RESULT["checks"].append(
            {
                "mode": mode,
                "check": "single_claim_winner",
                "trial": trial,
                "successes": r["successes"],
                "state": state,
            }
        )
        save()
    # Claims chosen from the same ready queue, not pre-partitioned IDs.
    r = run(
        repo,
        mode,
        1000,
        "ready_claim",
        16,
        lambda i, j: call(
            repo, ["ready", "--claim", "--label", "fixture"], f"ready-{i}-{j}"
        ),
        2,
    )
    claimed = []
    for row in r["rows"]:
        if row["code"] == 0:
            value = json.loads(row["stdout"])
            if isinstance(value, dict) and "id" in value:
                claimed.append(value["id"])
            elif isinstance(value, list):
                claimed.extend(
                    v["id"] for v in value if isinstance(v, dict) and "id" in v
                )
    RESULT["checks"].append(
        {
            "mode": mode,
            "check": "ready_unique",
            "claims": claimed,
            "duplicates": len(claimed) - len(set(claimed)),
        }
    )
    # A failed second operation must roll back the first operation.
    ident = "hb-rollback"
    create(repo, ident, "rollback")
    before = must(repo, ["show", ident])
    r = call(
        repo,
        ["batch"],
        stdin=f'update {ident} title="should roll back"\nupdate hb-doesnotexist status=closed\n',
    )
    after = must(repo, ["show", ident])
    RESULT["checks"].append(
        {
            "mode": mode,
            "check": "batch_rollback",
            "result": r,
            "before": before,
            "after": after,
        }
    )
    # Dependency semantics for ready selection vs direct claiming.
    create(repo, "hb-prereq", "dependency")
    create(repo, "hb-dependent", "blockedprobe")
    must(repo, ["dep", "add", "hb-dependent", "hb-prereq"])
    RESULT["checks"].append(
        {
            "mode": mode,
            "check": "blocked_claim",
            "ready": call(repo, ["ready", "--claim", "--label", "blockedprobe"]),
            "direct": call(
                repo, ["update", "hb-dependent", "--claim"], actor="blocked-claimer"
            ),
        }
    )
    save()
    # Bare count-then-claim is not a capacity transaction. Synchronize snapshots.
    for i in range(16):
        create(repo, f"hb-cap{i}", "capacity")
    barrier = threading.Barrier(16)

    def naive(i, j):
        read = call(
            repo,
            ["list", "--status", "in_progress", "--label", "capacity", "--limit", "0"],
        )
        barrier.wait()
        if read["code"] or len(json.loads(read["stdout"])) >= 4:
            return read
        return call(repo, ["ready", "--claim", "--label", "capacity"], f"capacity-{i}")

    r = run(repo, mode, 1000, "capacity_without_lock", 16, naive, 1)
    state = must(
        repo, ["list", "--status", "in_progress", "--label", "capacity", "--limit", "0"]
    )
    RESULT["checks"].append(
        {
            "mode": mode,
            "check": "capacity_without_lock",
            "cap": 4,
            "admitted": len(state),
        }
    )
    for item in state:
        must(repo, ["update", item["id"], "--status", "open", "--assignee", ""])

    def locked(i, j):
        start = time.perf_counter()
        with (repo / "admission.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            read = call(
                repo,
                [
                    "list",
                    "--status",
                    "in_progress",
                    "--label",
                    "capacity",
                    "--limit",
                    "0",
                ],
            )
            if read["code"] or len(json.loads(read["stdout"])) >= 4:
                result = read
            else:
                result = call(
                    repo,
                    ["ready", "--claim", "--label", "capacity"],
                    f"locked-capacity-{i}",
                )
        result["ms"] = (time.perf_counter() - start) * 1000
        return result

    run(repo, mode, 1000, "capacity_with_flock", 16, locked, 1)
    state = must(
        repo, ["list", "--status", "in_progress", "--label", "capacity", "--limit", "0"]
    )
    RESULT["checks"].append(
        {"mode": mode, "check": "capacity_with_flock", "cap": 4, "admitted": len(state)}
    )
    save()


def benchmark(repo, mode):
    for size in (100, 1000):
        seed(repo, 0 if size == 100 else 100, size)
        for _ in range(3):
            must(repo, ["ready", "--limit", "10"])
        for clients in (1, 4, 16):
            run(
                repo,
                mode,
                size,
                "ready",
                clients,
                lambda i, j: call(repo, ["ready", "--limit", "10"]),
            )
            run(
                repo,
                mode,
                size,
                "show",
                clients,
                lambda i, j: call(repo, ["show", f"hb-{i:06d}"]),
            )
            run(
                repo,
                mode,
                size,
                "distinct_updates",
                clients,
                lambda i, j: call(
                    repo,
                    ["update", f"hb-{i*4:06d}", "--notes", f"{size}/{clients}/{i}/{j}"],
                ),
            )
            run(
                repo,
                mode,
                size,
                "mixed_ready_updates",
                clients,
                lambda i, j: call(
                    repo,
                    (
                        ["ready", "--limit", "10"]
                        if (i + j) % 2
                        else [
                            "update",
                            f"hb-{i*4:06d}",
                            "--notes",
                            f"mixed/{size}/{clients}/{i}/{j}",
                        ]
                    ),
                ),
            )
    correctness(repo, mode)


def additional_checks(repo, mode):
    states = must(
        repo, ["list", "--status", "in_progress", "--label", "fixture", "--limit", "0"]
    )
    RESULT["checks"].append(
        {
            "mode": mode,
            "check": "ready_claim_persisted",
            "states": [v for v in states if v.get("assignee", "").startswith("ready-")],
        }
    )
    if mode == "server":
        import fcntl

        def serialized_claim(i, j):
            start = time.perf_counter()
            with (repo / "ready-claim.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = call(
                    repo,
                    ["ready", "--claim", "--label", "fixture"],
                    f"serialized-ready-{i}-{j}",
                )
            result["ms"] = (time.perf_counter() - start) * 1000
            return result

        run(repo, mode, 1000, "ready_claim_with_flock", 16, serialized_claim, 2)
    # Same-record JSON patch merge correctness.
    create(repo, "hb-metadata", "metadata-probe")
    run(
        repo,
        mode,
        1000,
        "same_task_metadata_patch",
        16,
        lambda i, j: call(
            repo,
            ["update", "hb-metadata", "--set-metadata", f"worker{i}={i}"],
            f"metadata-{i}",
        ),
        1,
    )
    state = must(repo, ["show", "hb-metadata"])
    RESULT["checks"].append(
        {"mode": mode, "check": "metadata_preserved", "state": state}
    )
    # An intentionally long read reveals whether unrelated reads can overlap.
    cmd = [
        "bd",
        "-C",
        str(repo),
        "--sandbox",
        "--dolt-auto-commit",
        "off",
        "--json",
        "sql",
        "SELECT SLEEP(2) AS waited",
    ]
    sleeper = subprocess.Popen(
        cmd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        time.sleep(0.5)
        running = sleeper.poll() is None
        query = call(repo, ["ready", "--limit", "10"])
        out, err = sleeper.communicate(timeout=10)
        RESULT["checks"].append(
            {
                "mode": mode,
                "check": "read_during_sleep",
                "sleeper_running_at_start": running,
                "sleeper_code": sleeper.returncode,
                "sleeper_stdout": out,
                "sleeper_stderr": err,
                "query": query,
            }
        )
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.communicate()

    save()


def repeat_server_claims(repo):
    for clients in (4, 16, 16):
        label = f'repeat-{clients}-{len(RESULT["runs"])}'
        r = run(
            repo,
            "server",
            1000,
            "ready_claim_repeat",
            clients,
            lambda i, j: call(
                repo, ["ready", "--claim", "--label", "fixture"], f"{label}-{i}-{j}"
            ),
            2,
        )
        states = must(
            repo,
            ["list", "--status", "in_progress", "--label", "fixture", "--limit", "0"],
        )
        RESULT["checks"].append(
            {
                "mode": "server",
                "check": "repeat_claim_persisted",
                "clients": clients,
                "successes": r["successes"],
                "persisted": len(
                    [v for v in states if v.get("assignee", "").startswith(label)]
                ),
            }
        )

    def locked(i, j):
        start = time.perf_counter()
        with (repo / "ready-claim.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = call(
                repo, ["ready", "--claim", "--label", "fixture"], f"locked-four-{i}-{j}"
            )
        result["ms"] = (time.perf_counter() - start) * 1000
        return result

    run(repo, "server", 1000, "ready_claim_with_flock", 4, locked, 4)
    save()


def main(modes=("embedded", "server")):
    RESULT["bd_version"] = subprocess.check_output(["bd", "version"], text=True).strip()
    RESULT["dolt_version"] = subprocess.check_output(
        ["dolt", "version"], text=True
    ).splitlines()[0]
    for mode in modes:
        repo = ROOT / mode
        repo.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        server = None
        log = None
        try:
            if mode == "server":
                data = ROOT / "server-data"
                data.mkdir(exist_ok=True)
                with socket.socket() as s:
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                log = (ROOT / "server.log").open("w")
                server = subprocess.Popen(
                    [
                        "dolt",
                        "sql-server",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--data-dir",
                        str(data),
                        "--loglevel",
                        "warning",
                    ],
                    cwd=data,
                    env=ENV,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError(
                            "private server failed: "
                            + (ROOT / "server.log").read_text()
                        )
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.1)
                extra = [
                    "--server",
                    "--external",
                    "--server-host",
                    "127.0.0.1",
                    "--server-port",
                    str(port),
                ]
            else:
                extra = []
            if not (repo / ".beads/metadata.json").exists():
                init = call(
                    repo,
                    [
                        "init",
                        "--prefix",
                        "hb",
                        "--skip-hooks",
                        "--skip-agents",
                        "--non-interactive",
                        *extra,
                    ],
                    timeout=60,
                )
                if init["code"]:
                    raise RuntimeError(init)
            RESULT[mode + "_metadata"] = json.loads(
                (repo / ".beads/metadata.json").read_text()
            )
            save()
            benchmark(repo, mode)
            additional_checks(repo, mode)
            if mode == "server":
                repeat_server_claims(repo)
        finally:
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
            if log:
                log.close()
    RESULT["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="New directory for disposable databases and results",
    )
    args = parser.parse_args()
    if args.output_dir is None:
        ROOT = Path(tempfile.mkdtemp(prefix="hive-beads-contention-")).resolve()
    else:
        ROOT = args.output_dir.resolve()
        ROOT.mkdir(parents=True, exist_ok=False)
    print(f"All databases and results: {ROOT}", flush=True)
    main()
