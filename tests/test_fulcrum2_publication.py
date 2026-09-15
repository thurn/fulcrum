from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fulcrum.contracts import ActorContext, InstanceContext, ParsedRequest
from fulcrum.ledger import Ledger
from fulcrum.publication import (
    DoltPublicationAdapter,
    LedgerPublicationService,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class Fulcrum2PublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.remote = self.root / "brain.git"
        self.seed = self.root / "seed"
        self.brain = self.root / "brain"
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self._run("git", "init", "--bare", str(self.remote))
        self._run("git", "clone", str(self.remote), str(self.seed))
        self._configure_git(self.seed)
        (self.seed / ".gitkeep").touch()
        self._run("git", "-C", str(self.seed), "add", ".gitkeep")
        self._run("git", "-C", str(self.seed), "commit", "-m", "chore: seed")
        self._run("git", "-C", str(self.seed), "push", "origin", "HEAD:main")
        self.brain.mkdir()
        self._run("git", "-C", str(self.brain), "init")
        self._configure_git(self.brain)
        self._run(
            "git", "-C", str(self.brain), "remote", "add", "origin", str(self.remote)
        )
        self._run(
            "bd",
            "init",
            "--prefix",
            "fc",
            "--non-interactive",
            "--skip-hooks",
            "--skip-agents",
            cwd=self.brain,
        )
        self.config = self.brain / "fulcrum.yaml"
        self.config.write_text(
            f"brain:\n  root: {self.brain}\n  remote: origin\n"
            "  push_interval_seconds: 300\n"
            f"beads:\n  executable: {shutil.which('bd')}\n"
            "delivery:\n  kind: deterministic\n"
            "knowledge:\n  require_remote_sync: false\n",
            encoding="utf-8",
        )
        self._run("git", "-C", str(self.brain), "add", "fulcrum.yaml")
        self._run(
            "git", "-C", str(self.brain), "commit", "-m", "chore: configure fixture"
        )
        config_commit = self._run(
            "git", "-C", str(self.brain), "rev-parse", "HEAD"
        ).stdout.strip()
        (self.instance / "config").symlink_to(self.config)
        self.context = InstanceContext(
            instance_root=self.instance,
            config_path=self.config,
            brain_root=self.brain,
            socket_path=self.instance / "controller.sock",
            lock_path=self.instance / "writer.lock",
            explicit_selection=True,
        )
        self.ledger = Ledger(self.brain)
        self.ledger.create_record(
            record_id="fc-system",
            kind="control",
            title="Fulcrum control",
            description="Publication fixture control.",
            owner="HUMAN",
            fc={
                "kind": "control",
                "owner": "HUMAN",
                "vizier_thread": None,
                "marshal_thread": None,
                "last_transition": None,
                "config_publication": {
                    "local": {"state": "observed", "commit": config_commit},
                    "remote": {"state": "not_required", "contains_local": False},
                },
            },
        )
        self.clock = FakeClock()

    def tearDown(self) -> None:
        subprocess.run(
            ["bd", "-C", str(self.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.temporary.cleanup()

    def request(self, command: tuple[str, ...] = ("ledger", "sync")) -> ParsedRequest:
        return ParsedRequest(
            command=command,
            arguments={},
            input={},
            actor=ActorContext(kind="human"),
            instance=self.context,
            request_id=str(uuid.uuid4()) if command != ("ledger", "status") else None,
            timeout=30,
            offline=True,
        )

    def service(self, *, after_push: Any = None) -> LedgerPublicationService:
        def factory(ledger: Ledger, **kwargs: Any) -> DoltPublicationAdapter:
            return DoltPublicationAdapter(ledger, after_push=after_push, **kwargs)

        return LedgerPublicationService(now=self.clock.now, adapter_factory=factory)

    def test_native_commit_is_proved_remotely_and_bookkeeping_does_not_loop(
        self,
    ) -> None:
        service = self.service()
        result = service.sync(self.request())
        self.assertTrue(result.ok)
        self.assertEqual(result.state.value, "completed")
        receipt = self.ledger.show(str(result.operation_id))
        self.assertIsNotNone(receipt)
        assert receipt is not None and receipt.fc is not None
        native = receipt.fc["result"]["target_dolt_commit"]
        remote_oid = receipt.fc["result"]["remote_data_ref"]
        self.assertNotEqual(native, remote_oid)
        self.assertTrue(receipt.fc["result"]["remote_contains_target"])
        before = self._remote_ref()

        status = service.status(self.request(("ledger", "status")))
        self.assertFalse(status.result["pending"])
        no_op = service.sync(self.request())
        self.assertEqual(no_op.result["step"], "no_native_changes")
        self.assertEqual(self._remote_ref(), before)
        self.assertFalse(
            service.status(self.request(("ledger", "status"))).result["pending"]
        )
        cli = subprocess.run(
            [
                str(Path(os.sys.executable).with_name("fulcrum")),
                "ledger",
                "status",
                "--instance",
                str(self.instance),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=40,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        envelope = json.loads(cli.stdout)
        self.assertEqual(envelope["result"]["local"]["commit"], native)
        self.assertEqual(envelope["result"]["remote"]["data_ref"], remote_oid)
        cli_sync = subprocess.run(
            [
                str(Path(os.sys.executable).with_name("fulcrum")),
                "ledger",
                "sync",
                "--instance",
                str(self.instance),
                "--offline",
                "--actor",
                "human",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=40,
        )
        self.assertEqual(cli_sync.returncode, 0, cli_sync.stderr)
        self.assertEqual(
            json.loads(cli_sync.stdout)["result"]["step"], "no_native_changes"
        )

    def test_fixed_deadline_coalesces_native_and_control_changes(self) -> None:
        service = self.service()
        service.sync(self.request())
        self.ledger.create_record(
            record_id="fc-native",
            kind="work",
            title="Native edit",
            description="Created outside the publication service.",
            owner="HUMAN",
            fc={"kind": "work", "owner": "HUMAN", "phase": "backlog"},
        )
        self.config.write_text(
            self.config.read_text(encoding="utf-8")
            + "policy:\n  rationale: Cadence fixture edit.\n",
            encoding="utf-8",
        )
        scheduled = service.tick(self.request())
        self.assertEqual(scheduled["action"], "scheduled")
        first_deadline = scheduled["status"]["publication"]["pending_since"]
        self.clock.advance(299)
        control = self.ledger.show("fc-system")
        assert control is not None and control.fc is not None
        fc = dict(control.fc)
        fc["marshal_thread"] = "marshal-new"
        self.ledger.update_fc(control.id, fc)
        still_scheduled = service.tick(self.request())
        self.assertEqual(still_scheduled["action"], "scheduled")
        self.assertEqual(
            still_scheduled["status"]["publication"]["pending_since"],
            first_deadline,
        )
        self.clock.advance(1)
        flushed = service.tick(self.request())
        self.assertEqual(flushed["action"], "flush")
        self.assertTrue(flushed["selected_git"]["ok"])
        self.assertTrue(flushed["native"]["ok"])
        self.assertFalse(
            service.status(self.request(("ledger", "status"))).result["pending"]
        )
        self.assertFalse(
            service.status(self.request(("ledger", "status"))).result["ordinary_git"][
                "configuration_pending"
            ]
        )

    def test_applied_push_response_loss_reuses_one_receipt(self) -> None:
        calls = 0

        def lose_once(_: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("fixture response loss")

        service = self.service(after_push=lose_once)
        result = service.sync(self.request())
        self.assertTrue(result.ok)
        self.assertEqual(result.result["step"], "remote_native_commit_observed")
        self.assertEqual(calls, 1)
        operations = [
            row
            for row in self.ledger.list_records(kind="operation", limit=0)
            if (row.fc or {}).get("command") == "ledger.sync"
        ]
        self.assertEqual([row.id for row in operations], [result.operation_id])

    def test_outage_exhaustion_survives_restart_and_explicit_sync_grants_retry(
        self,
    ) -> None:
        service = self.service()
        service.sync(self.request())
        self.ledger.create_record(
            record_id="fc-outage",
            kind="work",
            title="Outage work",
            description="Must remain durable locally.",
            owner="HUMAN",
            fc={"kind": "work", "owner": "HUMAN", "phase": "backlog"},
        )
        unavailable = self.root / "brain-unavailable.git"
        self.remote.rename(unavailable)
        first = service.sync(self.request())
        operation_id = first.operation_id
        self.assertEqual(first.state.value, "accepted")
        self.clock.advance(1)
        second = service.tick(self.request())
        self.assertEqual(second["action"], "retry")
        self.clock.advance(5)
        third = service.tick(self.request())
        self.assertEqual(third["result"]["state"], "failed")

        restarted = self.service()
        retained = restarted.status(self.request(("ledger", "status"))).result
        self.assertTrue(retained["operation"]["exhausted"])
        self.assertEqual(retained["operation"]["total_sends"], 3)
        self.assertIsNotNone(retained["pending_age_seconds"])
        self.ledger.create_record(
            record_id="fc-later",
            kind="work",
            title="Later work",
            description="Does not grant publication retries.",
            owner="HUMAN",
            fc={"kind": "work", "owner": "HUMAN", "phase": "backlog"},
        )
        self.clock.advance(300)
        self.assertEqual(restarted.tick(self.request())["action"], "exhausted")
        unavailable.rename(self.remote)
        recovered = restarted.sync(self.request())
        self.assertTrue(recovered.ok)
        self.assertEqual(recovered.operation_id, operation_id)
        receipt = self.ledger.show(str(operation_id))
        assert receipt is not None and receipt.fc is not None
        self.assertEqual(receipt.fc["planned"]["grant"], 2)
        self.assertEqual(receipt.fc["planned"]["total_sends"], 4)

    def test_remote_divergence_is_retained_without_force_or_pull(self) -> None:
        service = self.service()
        service.sync(self.request())
        peer = self.root / "peer"
        peer.mkdir()
        self._run("git", "-C", str(peer), "init")
        self._configure_git(peer)
        self._run(
            "bd",
            "init",
            "--remote",
            f"git+file://{self.remote}",
            "--prefix",
            "fc",
            "--non-interactive",
            "--skip-hooks",
            "--skip-agents",
            cwd=peer,
        )
        peer_ledger = Ledger(peer)
        peer_ledger.create_record(
            record_id="fc-peer",
            kind="work",
            title="Peer work",
            description="Creates remote divergence.",
            owner="HUMAN",
            fc={"kind": "work", "owner": "HUMAN", "phase": "backlog"},
        )
        self._run("bd", "-C", str(peer), "dolt", "push", "--remote", "origin")
        peer_ref = self._remote_ref()
        self.ledger.create_record(
            record_id="fc-local",
            kind="work",
            title="Local work",
            description="Must not overwrite the peer.",
            owner="HUMAN",
            fc={"kind": "work", "owner": "HUMAN", "phase": "backlog"},
        )
        result = service.sync(self.request())
        self.assertFalse(result.ok)
        self.assertEqual(result.state.value, "failed")
        self.assertEqual(result.result["error"]["code"], "divergence")
        self.assertEqual(self._remote_ref(), peer_ref)

    def _remote_ref(self) -> str:
        return self._run(
            "git", "--git-dir", str(self.remote), "rev-parse", "refs/dolt/data"
        ).stdout.strip()

    @staticmethod
    def _configure_git(path: Path) -> None:
        for key, value in (
            ("user.email", "fixture@example.invalid"),
            ("user.name", "Fixture"),
            ("beads.role", "maintainer"),
        ):
            Fulcrum2PublicationTest._run("git", "-C", str(path), "config", key, value)

    @staticmethod
    def _run(
        *command: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )


if __name__ == "__main__":
    unittest.main()
