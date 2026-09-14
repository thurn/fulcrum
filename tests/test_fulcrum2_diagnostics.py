from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fulcrum.diagnostics import DiagnosticLog


class DiagnosticLogTest(unittest.TestCase):
    def test_redaction_stream_caps_rotation_and_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = os.environ.get("FULCRUM_TEST_API_TOKEN")
            os.environ["FULCRUM_TEST_API_TOKEN"] = "very-secret-value"
            try:
                log = DiagnosticLog(
                    root, retention_days=1, max_bytes=8192, capture_bytes=32
                )
                retained = log.append(
                    {
                        "event": "adapter",
                        "authorization": "Bearer abcdefghijklmnop",
                        "error": "value=very-secret-value",
                        "stdout": "x" * 100,
                    }
                )
            finally:
                if previous is None:
                    os.environ.pop("FULCRUM_TEST_API_TOKEN", None)
                else:
                    os.environ["FULCRUM_TEST_API_TOKEN"] = previous
            serialized = json.dumps(retained)
            self.assertNotIn("very-secret-value", serialized)
            self.assertNotIn("abcdefghijklmnop", serialized)
            self.assertEqual(retained["authorization"], "[REDACTED]")
            self.assertTrue(retained["stdout"]["truncated"])
            old = root / "events-20000101-00000.jsonl"
            old.write_text("{}\n", encoding="utf-8")
            timestamp = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
            os.utime(old, (timestamp, timestamp))
            result = log.prune()
            self.assertIn(old.name, result["removed_files"])
            self.assertFalse(old.exists())


class Fulcrum2DiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.brain = cls.root / "brain"
        cls.instance = cls.root / "instance"
        cls.project = cls.root / "project"
        for path in (cls.brain, cls.instance, cls.project):
            path.mkdir()
        for path in (cls.brain, cls.project):
            subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"], cwd=path, check=True
            )
            subprocess.run(
                ["git", "config", "beads.role", "maintainer"], cwd=path, check=True
            )
        subprocess.run(
            [
                "bd",
                "init",
                "--prefix",
                "fc",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
            ],
            cwd=cls.brain,
            capture_output=True,
            check=True,
            timeout=60,
        )
        cls.config = cls.brain / "fulcrum.yaml"
        cls.config.write_text(
            f"brain:\n  root: {cls.brain}\nprojects:\n  toy:\n    root: {cls.project}\n    enabled: true\n",
            encoding="utf-8",
        )
        (cls.instance / "config").symlink_to(cls.config)
        cls.executable = Path(os.sys.executable).with_name("fulcrum")

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["bd", "-C", str(cls.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        cls.temporary.cleanup()

    def invoke(
        self, *arguments: str, payload: dict[str, object] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.executable), *arguments],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def create_work(self, request_id: str) -> tuple[str, str]:
        created = self.invoke(
            "work",
            "create",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--request-id",
            request_id,
            "--input",
            "-",
            "--json",
            payload={
                "title": "Observable work",
                "outcome": "Expose durable operator facts",
                "project": "toy",
                "acceptance": ["Status is sufficient to choose a next command"],
            },
        )
        self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
        result = json.loads(created.stdout)
        return result["result"]["result"]["bead_id"], result["operation_id"]

    def test_status_logs_trace_and_wait_are_bounded_offline_reads(self) -> None:
        request_id = str(uuid.uuid4())
        bead_id, operation_id = self.create_work(request_id)
        status = self.invoke(
            "status",
            "--bead",
            bead_id,
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
        status_result = json.loads(status.stdout)["result"]
        self.assertEqual(status_result["work"][0]["id"], bead_id)
        self.assertTrue(status_result["work"][0]["native_observation"]["available"])
        self.assertEqual(status_result["work"][0]["ownership_operation"], operation_id)
        self.assertIsInstance(status_result["work"][0]["next_commands"], list)

        logs = self.invoke(
            "logs",
            "--operation",
            operation_id,
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(logs.returncode, 0, logs.stderr + logs.stdout)
        self.assertTrue(json.loads(logs.stdout)["result"]["items"])
        followed = self.invoke(
            "logs",
            "--follow",
            "--timeout",
            "0.2",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(followed.returncode, 0, followed.stderr + followed.stdout)
        stream = [json.loads(line) for line in followed.stdout.splitlines()]
        self.assertGreaterEqual(len(stream), 2)
        self.assertEqual(stream[-1]["state"], "completed")

        first = self.invoke(
            "trace",
            "--bead",
            bead_id,
            "--limit",
            "1",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        first_result = json.loads(first.stdout)["result"]
        self.assertEqual(len(first_result["items"]), 1)
        self.assertIsNotNone(first_result["next_cursor"])
        second = self.invoke(
            "trace",
            "--bead",
            bead_id,
            "--cursor",
            first_result["next_cursor"],
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        second_result = json.loads(second.stdout)["result"]
        self.assertNotEqual(
            first_result["items"][0]["id"], second_result["items"][0]["id"]
        )

        ready = self.invoke(
            "wait",
            "--bead",
            bead_id,
            "--until",
            "backlog",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(ready.returncode, 0, ready.stderr + ready.stdout)
        expired = self.invoke(
            "wait",
            "--bead",
            bead_id,
            "--until",
            "closed",
            "--timeout",
            "2",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(expired.returncode, 3, expired.stderr + expired.stdout)
        details = json.loads(expired.stdout)["error"]["details"]
        self.assertEqual(details["bead_id"], bead_id)
        self.assertEqual(details["work"]["phase"], "backlog")

    def test_doctor_separates_components_from_stale_loops(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        (self.instance / "service-health.json").write_text(
            json.dumps(
                {
                    name: {"last_success_at": old}
                    for name in ("intake", "event", "reconciliation", "runner")
                }
            ),
            encoding="utf-8",
        )
        server = subprocess.Popen(
            [
                str(self.executable),
                "serve",
                "--once",
                "--instance",
                str(self.instance),
                "--json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(100):
            if (self.instance / "controller.sock").exists():
                break
            time.sleep(0.02)
        try:
            doctor = self.invoke("doctor", "--instance", str(self.instance), "--json")
        finally:
            try:
                server.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                server.terminate()
                server.communicate(timeout=10)
        self.assertEqual(doctor.returncode, 0, doctor.stderr + doctor.stdout)
        result = json.loads(doctor.stdout)["result"]
        components = {item["name"]: item for item in result["components"]}
        loops = {item["name"]: item for item in result["loops"]}
        self.assertEqual(components["ledger"]["state"], "healthy")
        self.assertEqual(components["service_socket"]["state"], "healthy")
        self.assertEqual(loops["reconciliation"]["state"], "stale")
        self.assertTrue(loops["reconciliation"]["next_commands"])

    def test_status_retains_partial_evidence_when_ledger_is_unavailable(self) -> None:
        broken_instance = self.root / "broken-instance"
        broken_brain = self.root / "missing-ledger"
        broken_instance.mkdir()
        broken_config = self.root / "broken.yaml"
        broken_config.write_text(
            f"brain:\n  root: {broken_brain}\nprojects: {{}}\n", encoding="utf-8"
        )
        (broken_instance / "config").symlink_to(broken_config)
        status = self.invoke(
            "status",
            "--instance",
            str(broken_instance),
            "--offline",
            "--json",
        )
        self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
        result = json.loads(status.stdout)["result"]
        self.assertEqual(result["work"], [])
        self.assertEqual(result["gaps"][0]["projection"], "work")
        self.assertEqual(result["gaps"][0]["availability"], "unavailable")


if __name__ == "__main__":
    unittest.main()
