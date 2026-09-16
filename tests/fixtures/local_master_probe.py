"""Local Git/interpreter fixture; invoked in one narrowly permitted subprocess."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from fulcrum.bootstrap import fresh_selection
from fulcrum.activation import cleanup_sources

project = Path(sys.argv[1])
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    repo, instance = root / "repo", root / "instance"
    shutil.copytree(
        project / "src", repo / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    package = repo / "src/fulcrum"
    (repo / "pyproject.toml").write_text(
        '[project]\nrequires-python=">=3.12"\ndependencies=[]\n'
    )
    (package / "application.py").write_text("def default_application(): return None\n")
    (package / "configuration.py").write_text(
        "class ConfigurationManager:\n def __init__(self, path): pass\n"
    )
    (package / "install.py").write_text(
        "def reconcile_fulcrum2_skills(*args, **kwargs): pass\n"
    )
    (package / "cli.py").write_text(
        "from pathlib import Path\nfrom fulcrum import behavior\n"
        "def main():\n print(behavior.VALUE, (Path(__file__).parent / 'asset').read_text()); return 0\n"
    )
    instance.mkdir()
    (instance / "resident.json").write_text(
        json.dumps({"source": {"repository": str(repo)}})
    )

    def git(*args):
        return (
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *args,
                ],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    git("init", "-b", "master")

    def commit(value):
        (package / "behavior.py").write_text("VALUE = " + repr(value) + "\n")
        (package / "asset").write_text(value)
        git("add", ".")
        git("commit", "-m", "test: " + value)
        return git("rev-parse", "master")

    old_commit = commit("before")
    old, lease = fresh_selection(instance, instance / "missing-config")
    # The operation starts before committing and delays imports/assets until after.
    delayed = (
        "import pathlib,sys; sys.path.insert(0,sys.argv[1]); input(); "
        "from fulcrum import behavior; print(behavior.VALUE, "
        "(pathlib.Path(sys.argv[1])/'fulcrum/asset').read_text())"
    )
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", delayed, old["source"] + "/src"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    new_commit = commit("after")
    launch = [
        sys.executable,
        "-B",
        str(package / "bootstrap.py"),
        "--instance",
        str(instance),
    ]
    started = time.monotonic()
    new = subprocess.run(launch, capture_output=True, text=True, check=True)
    elapsed = time.monotonic() - started
    assert new.stdout.strip() == "after after", new
    selected = json.loads((instance / "selected.json").read_text())
    assert selected["commit"] == new_commit
    assert child.communicate("\n", timeout=5)[0].strip() == "before before"
    inherited = dict(
        os.environ, FULCRUM_OPERATION_SOURCE=old["source"], FULCRUM_COMMIT=old_commit
    )
    assert (
        subprocess.check_output(launch, text=True, env=inherited).strip()
        == "before before"
    )
    (package / "behavior.py").write_text("this is invalid python!")
    (package / "asset").write_text("dirty")
    assert subprocess.check_output(launch, text=True).strip() == "after after"
    os.close(lease)
    cleanup_sources(instance, selected)
    assert not Path(old["source"]).exists()
    new_commit = commit("concurrent")
    clients = [
        subprocess.Popen(
            launch, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(4)
    ]
    for client in clients:
        stdout, stderr = client.communicate(timeout=10)
        assert client.returncode == 0, stderr
        assert stdout.strip() == "concurrent concurrent", stdout
    (package / "application.py").write_text(
        "raise RuntimeError('broken committed import')\n"
    )
    git("add", ".")
    git("commit", "-m", "test: rejected candidate")
    failed = subprocess.run(launch, capture_output=True, text=True)
    assert failed.returncode != 0 and "broken committed import" in failed.stderr, failed
    assert "concurrent concurrent" not in failed.stdout
    assert json.loads((instance / "selected.json").read_text())["commit"] == new_commit
    diagnostic = subprocess.run(
        [*launch, "service", "status"], capture_output=True, text=True, check=True
    )
    assert diagnostic.stdout.strip() == "concurrent concurrent"
    assert "retained diagnostics" in diagnostic.stderr
    print(json.dumps({"commit_to_command_seconds": elapsed}))
