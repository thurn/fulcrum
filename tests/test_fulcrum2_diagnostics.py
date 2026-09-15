from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fulcrum.contracts import CommandResult
from fulcrum.diagnostics import DiagnosticLog, _runtime_component
from tests.support import request


class DiagnosticLogTest(unittest.TestCase):
    def test_runtime_component_uses_live_capabilities(self) -> None:
        capabilities = {
            "available": True,
            "endpoint": "ws://127.0.0.1:4500",
            "methods": ["thread/list", "turn/start"],
            "models": {"gpt-test": ["medium"]},
            "gaps": [],
        }
        with patch(
            "fulcrum.runtime_service.RuntimeService.capabilities",
            return_value=CommandResult.query(capabilities),
        ):
            component = _runtime_component(request(("doctor",)))

        self.assertEqual(component["state"], "healthy")
        self.assertEqual(component["affected_commands"], [])
        self.assertEqual(component["next_commands"], [])
        self.assertEqual(component["evidence"]["models"], ["gpt-test"])

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


if __name__ == "__main__":
    unittest.main()
