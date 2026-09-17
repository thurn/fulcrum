from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import uuid

from fulcrum.contracts import InstanceContext
from fulcrum.desktop_setup import (
    DesktopSetupService,
    REQUIRED_ACCEPTANCE,
    REQUIRED_NATIVE_TOOLS,
)
from tests.support import MemoryLedger, request


def bootstrap_request(root: Path, **payload):
    instance = root / "instance"
    instance.mkdir(exist_ok=True)
    codex = root / "codex"
    context = InstanceContext(
        instance_root=instance,
        config_path=root / "brain" / "fulcrum.yaml",
        brain_root=root / "brain",
        socket_path=instance / "broker.sock",
        lock_path=root / "brain" / ".lock",
        explicit_selection=True,
    )
    return replace(
        request(("bootstrap",)),
        instance=context,
        input={"codex_root": str(codex), **payload},
        request_id=str(uuid.uuid4()),
    )


def test_bootstrap_reuses_standing_tasks_and_opens_only_after_acceptance():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = DesktopSetupService(MemoryLedger())
        support = {
            "gpt-5.6-luna": ["high"],
            "gpt-5.6-sol": ["high"],
        }
        supplied = {
            "native_tools": sorted(REQUIRED_NATIVE_TOOLS),
            "model_support": support,
        }
        first = service.bootstrap(bootstrap_request(root, **supplied))
        assert first.result["admission"] == "paused"
        actions = {
            row["reporting"]["role"]: row
            for row in first.result["pending_actions"]
            if "role" in row["reporting"]
        }
        assert set(actions) == {"steward", "marshal", "vizier"}
        for role, action in actions.items():
            service.register_standing(
                replace(
                    bootstrap_request(root),
                    command=("register", "standing"),
                    input={
                        "role": role,
                        "task_id": f"{role}-task",
                        "session_id": f"{role}-session",
                        "action_id": action["action_id"],
                    },
                )
            )

        second = service.bootstrap(bootstrap_request(root, **supplied))
        schedule = next(
            row
            for row in second.result["pending_actions"]
            if row["tool"] == "automation_update"
        )
        claim = service.claim_action(
            replace(
                bootstrap_request(root),
                command=("action", "claim"),
                arguments={
                    "record_id": "fc-system",
                    "action_id": schedule["action_id"],
                },
                input={"attempt_id": "schedule-attempt"},
            )
        )
        assert claim.result["invoke"] is True
        service.report_action_result(
            replace(
                bootstrap_request(root),
                command=("action", "result"),
                arguments={
                    "record_id": "fc-system",
                    "action_id": schedule["action_id"],
                },
                input={
                    "attempt_id": "schedule-attempt",
                    "outcome": "succeeded",
                    "native_result": {"automationId": "automation-1"},
                },
            )
        )
        (root / "instance" / "broker.sock").touch()
        activating = service.bootstrap(
            bootstrap_request(
                root,
                acceptance={name: True for name in REQUIRED_ACCEPTANCE},
                **supplied,
            )
        )
        activation = next(
            row
            for row in activating.result["pending_actions"]
            if row["reporting"].get("purpose") == "marshal_schedule_activation"
        )
        service.claim_action(
            replace(
                bootstrap_request(root),
                command=("action", "claim"),
                arguments={
                    "record_id": "fc-system",
                    "action_id": activation["action_id"],
                },
                input={"attempt_id": "activation-attempt"},
            )
        )
        service.report_action_result(
            replace(
                bootstrap_request(root),
                command=("action", "result"),
                arguments={
                    "record_id": "fc-system",
                    "action_id": activation["action_id"],
                },
                input={
                    "attempt_id": "activation-attempt",
                    "outcome": "succeeded",
                    "native_result": {"automationId": "automation-1"},
                },
            )
        )
        ready = service.bootstrap(
            bootstrap_request(
                root,
                acceptance={name: True for name in REQUIRED_ACCEPTANCE},
                **supplied,
            )
        )
        assert ready.result["state"] == "ready"
        assert ready.result["admission"] == "running"
        assert ready.result["schedule"]["automation_id"] == "automation-1"
        assert ready.result["schedule"]["status"] == "ACTIVE"
        assert len(ready.result["standing"]) == 3


def test_bootstrap_preserves_unrelated_codex_configuration():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / "codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('model = "gpt-6-astra"\n', encoding="utf-8")
        DesktopSetupService(MemoryLedger()).bootstrap(bootstrap_request(root))
        retained = config.read_text(encoding="utf-8")
        assert 'model = "gpt-6-astra"' in retained
        assert "[mcp_servers.fulcrum]" in retained
        assert "tool_timeout_sec = 3900" in retained
