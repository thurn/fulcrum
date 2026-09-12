"""Evidence-backed diagnostics for the assembled local product."""

from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path
from typing import Any

from fulcrum.config import RuntimePaths, load_installation
from fulcrum.install import (
    APP_SERVER_LABEL,
    CONTROLLER_LABEL,
    HUMAN_SKILLS,
    REMOVED_SKILLS,
    package_root,
)
from fulcrum.prompts import TEMPLATES, load_template
from fulcrum.store import Store


def doctor(paths: RuntimePaths) -> dict[str, Any]:
    config = load_installation(paths.config_file)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check(
        "editable_import",
        package_root() == Path(config.source_root) / "src" / "fulcrum",
        str(package_root()),
    )
    for name, command in (
        ("codex", config.codex_bin),
        ("git", shutil.which("git")),
        ("beads", shutil.which("bd")),
        (
            "tollgate",
            shutil.which("tg") or "/Applications/Tollgate.app/Contents/MacOS/tg",
        ),
    ):
        check(
            name,
            bool(command and Path(command).exists()),
            str(command or "unavailable"),
        )
    for name in TEMPLATES:
        try:
            load_template(name)
            check(f"prompt:{name}", True, "loaded from editable source")
        except Exception as error:
            check(f"prompt:{name}", False, str(error))
    codex_root = Path.home() / ".codex" / "skills"
    for name in HUMAN_SKILLS:
        target = codex_root / name
        check(
            f"skill:{name}",
            target.is_symlink()
            and target.resolve(strict=False).is_relative_to(Path(config.source_root)),
            str(target),
        )
    for name in REMOVED_SKILLS:
        check(
            f"removed_skill:{name}",
            not (codex_root / name).exists(),
            str(codex_root / name),
        )
    agents = Path.home() / "Library" / "LaunchAgents"
    for label in (APP_SERVER_LABEL, CONTROLLER_LABEL):
        check(
            f"service:{label}",
            (agents / f"{label}.plist").is_file(),
            str(agents / f"{label}.plist"),
        )
    endpoint = (
        config.app_server_endpoint.replace("ws://", "http://", 1)
        .replace("wss://", "https://", 1)
        .rstrip("/")
        + "/readyz"
    )
    try:
        with urllib.request.urlopen(endpoint, timeout=2) as response:
            check("app_server_ready", response.status == 200, endpoint)
    except Exception as error:
        check("app_server_ready", False, str(error))
    check("controller_socket", paths.socket.exists(), str(paths.socket))
    if paths.database.is_file():
        try:
            with Store(paths.database, readonly=True) as store:
                integrity = store.row("PRAGMA integrity_check")
                foreign_keys = store.row("PRAGMA foreign_keys")
                policies = store.rows("SELECT * FROM policies WHERE active = 1")
                archon = store.row(
                    "SELECT * FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
                )
                dispatch = store.row(
                    "SELECT value FROM meta WHERE key = 'dispatch_enabled'"
                )
            check(
                "sqlite_integrity",
                bool(integrity and next(iter(integrity.values())) == "ok"),
                str(integrity),
            )
            check(
                "sqlite_foreign_keys",
                bool(foreign_keys and next(iter(foreign_keys.values())) == 1),
                str(foreign_keys),
            )
            check(
                "archon",
                archon is not None,
                archon["native_thread_id"] if archon else "missing",
            )
            check("policies", bool(policies), f"{len(policies)} active")
            check(
                "dispatch",
                bool(dispatch and dispatch["value"] == "1"),
                "enabled" if dispatch and dispatch["value"] == "1" else "disabled",
            )
        except Exception as error:
            check("controller_database", False, str(error))
    else:
        check("controller_database", False, str(paths.database))
    failures = [item for item in checks if not item["ok"]]
    return {
        "ok": not failures,
        "ready": not failures,
        "checks": checks,
        "failures": failures,
    }
