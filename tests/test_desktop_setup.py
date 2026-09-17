from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import uuid

from fulcrum.contracts import ActorContext, FulcrumError, InstanceContext
from fulcrum.desktop_setup import (
    DesktopSetupService,
    PRE_ACTIVATION_ACCEPTANCE,
    REQUIRED_ACCEPTANCE,
    REQUIRED_NATIVE_TOOLS,
)
from tests.support import MemoryLedger, observe_action_prompt, request


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


def acceptance(names):
    return {name: {"passed": True, "evidence": [f"observed {name}"]} for name in names}


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
        assert (
            "without inventing loop or turn IDs"
            in actions["steward"]["arguments"]["prompt"]
        )
        assert "yield_time_ms" in actions["steward"]["arguments"]["prompt"]
        assert (
            "never poll it with functions.wait"
            in actions["steward"]["arguments"]["prompt"]
        )
        assert (
            "~/fulcrum/skills/fulcrum-marshal/SKILL.md"
            in actions["marshal"]["arguments"]["prompt"]
        )
        assert (
            "~/fulcrum/skills/fulcrum-vizier/SKILL.md"
            in actions["vizier"]["arguments"]["prompt"]
        )
        for role, action in actions.items():
            observe_action_prompt(
                service._ledger_override,
                action,
                task_id=f"{role}-task",
                session_id=f"{role}-session",
                instance=root / "instance",
            )
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
        diagnostic = next(
            row
            for row in second.result["pending_actions"]
            if row["reporting"].get("purpose") == "diagnostic_escape_hatch"
        )
        service.claim_action(
            replace(
                bootstrap_request(root),
                command=("action", "claim"),
                arguments={
                    "record_id": "fc-system",
                    "action_id": diagnostic["action_id"],
                },
                input={"attempt_id": "diagnostic-attempt"},
            )
        )
        service.report_action_result(
            replace(
                bootstrap_request(root),
                command=("action", "result"),
                arguments={
                    "record_id": "fc-system",
                    "action_id": diagnostic["action_id"],
                },
                input={
                    "attempt_id": "diagnostic-attempt",
                    "outcome": "succeeded",
                    "native_result": {"threadId": "steward-task"},
                },
            )
        )
        assert not any(
            row["tool"] == "automation_update"
            for row in second.result["pending_actions"]
        )
        (root / "instance" / "broker.sock").touch()
        pre_activation = acceptance(PRE_ACTIVATION_ACCEPTANCE)
        activating = service.bootstrap(
            bootstrap_request(
                root,
                acceptance=pre_activation,
                **supplied,
            )
        )
        assert activating.result["admission"] == "paused"
        assert activating.result["prerequisites"]["acceptance"] is None
        assert activating.result["hooks"]["operational_state"] == "evidence_confirmed"
        assert activating.result["hooks"]["operator_confirmation_required"] is False
        schedule = next(
            row
            for row in activating.result["pending_actions"]
            if row["tool"] == "automation_update"
        )
        assert schedule["arguments"]["status"] == "ACTIVE"
        assert "scheduled Fulcrum heartbeat" in schedule["arguments"]["prompt"]
        assert not schedule["arguments"]["prompt"].startswith("Fulcrum-Action:")
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
                    "native_result": {
                        "automationId": "automation-1",
                        "status": "ACTIVE",
                        "targetThreadId": "marshal-task",
                    },
                },
            )
        )
        ready = service.bootstrap(
            bootstrap_request(
                root,
                acceptance=acceptance(REQUIRED_ACCEPTANCE),
                **supplied,
            )
        )
        assert ready.result["state"] == "ready"
        assert ready.result["admission"] == "running"
        assert ready.result["schedule"]["automation_id"] == "automation-1"
        assert ready.result["schedule"]["status"] == "ACTIVE"
        assert len(ready.result["standing"]) == 3

        system = service._ledger_override.show("fc-system")
        desktop = dict(system.fc["desktop"])
        standing = dict(desktop["standing"])
        standing["marshal"] = {
            **standing["marshal"],
            "state": "stopped",
            "positively_completed_at": "2026-09-17T00:00:00Z",
        }
        desktop["standing"] = standing
        desktop["marshal_schedule"] = {
            **desktop["marshal_schedule"],
            "prompt": "legacy recurring prompt",
        }
        service._ledger_override.update_fc(
            "fc-system", {**system.fc, "desktop": desktop}
        )
        repairing = service.bootstrap(
            bootstrap_request(
                root,
                acceptance=acceptance(REQUIRED_ACCEPTANCE),
                **supplied,
            )
        )
        repair = next(
            row
            for row in repairing.result["pending_actions"]
            if row["reporting"].get("purpose") == "marshal_schedule_retarget"
        )
        assert repair["arguments"]["id"] == "automation-1"
        assert "scheduled Fulcrum heartbeat" in repair["arguments"]["prompt"]
        assert not repair["arguments"]["prompt"].startswith("Fulcrum-Action:")
        assert repairing.result["standing"]["marshal"]["state"] == "registered"
        assert "positively_completed_at" not in repairing.result["standing"]["marshal"]


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
        assert str(root / "brain" / "fulcrum.yaml") in retained
        hooks = (root / "codex" / "hooks.json").read_text(encoding="utf-8")
        assert (
            f"--config {(root / 'brain' / 'fulcrum.yaml').resolve(strict=False)}"
            in hooks
        )


def test_bootstrap_rejects_boolean_acceptance_without_evidence():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = DesktopSetupService(MemoryLedger())
        with unittest.TestCase().assertRaisesRegex(
            FulcrumError, "must include passed and evidence"
        ):
            service.bootstrap(
                bootstrap_request(
                    root,
                    acceptance={"workspace_access": True},
                )
            )


def _verify_pending_bootstrap_actions_rebind_to_the_latest_bootstrap_task():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        service = DesktopSetupService(MemoryLedger())
        first = service.bootstrap(
            replace(bootstrap_request(root), thread_id="first-bootstrap-task")
        )
        action_id = first.result["pending_actions"][0]["action_id"]

        second = service.bootstrap(
            replace(bootstrap_request(root), thread_id="resumed-bootstrap-task")
        )
        assert any(
            action["action_id"] == action_id
            for action in second.result["pending_actions"]
        )

        def claim(task_id: str, attempt_id: str):
            return service.claim_action(
                replace(
                    bootstrap_request(root),
                    command=("action", "claim"),
                    arguments={"record_id": "fc-system", "action_id": action_id},
                    input={"attempt_id": attempt_id},
                    actor=ActorContext.parse(f"task:{task_id}"),
                    thread_id=task_id,
                )
            )

        try:
            claim("first-bootstrap-task", "stale-attempt")
        except FulcrumError as error:
            assert error.code == "AUTHORITY_MISMATCH"
        else:
            raise AssertionError("stale bootstrap task retained action authority")

        retained = claim("resumed-bootstrap-task", "authorized-attempt")
        assert retained.result["invoke"] is True
        completed = service.report_action_result(
            replace(
                bootstrap_request(root),
                command=("action", "result"),
                arguments={"record_id": "fc-system", "action_id": action_id},
                input={
                    "attempt_id": "authorized-attempt",
                    "outcome": "succeeded",
                    "native_result": {"threadId": "created-standing-task"},
                },
                actor=ActorContext.parse("task:resumed-bootstrap-task"),
                thread_id="resumed-bootstrap-task",
            )
        )
        assert completed.result["state"] == "succeeded"


class DesktopSetupAuthorizationTests(unittest.TestCase):
    def test_pending_bootstrap_actions_rebind_to_the_latest_bootstrap_task(self):
        _verify_pending_bootstrap_actions_rebind_to_the_latest_bootstrap_task()
