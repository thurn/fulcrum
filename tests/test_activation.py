from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.activation import BROKER_HANDOFF_REASON, activate
from fulcrum.bootstrap import main as bootstrap_main


class ActivationTests(unittest.TestCase):
    def test_launcher_returns_structured_source_failure_for_json_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_id = "7861cf31-0c5f-4081-ab95-8dcdf79cff3c"
            output = StringIO()
            with (
                patch(
                    "fulcrum.bootstrap.fresh_selection",
                    side_effect=RuntimeError(BROKER_HANDOFF_REASON),
                ),
                redirect_stdout(output),
            ):
                code = bootstrap_main(
                    arguments=[
                        "bootstrap",
                        "--request-id",
                        request_id,
                        "--json",
                    ],
                    instance=root / "instance",
                    config=root / "config.yaml",
                )

            result = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["request_id"], request_id)
            self.assertEqual(result["error"]["code"], "SOURCE_UNAVAILABLE")
            self.assertIn(BROKER_HANDOFF_REASON, result["error"]["message"])

    def test_changed_broker_does_not_require_handoff_without_connection_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = root / "instance"
            old_source = root / "old"
            old_broker = old_source / "src" / "fulcrum" / "broker.py"
            old_broker.parent.mkdir(parents=True)
            old_broker.write_text("old\n", encoding="utf-8")
            previous = {
                "source": str(old_source),
                "python": sys.executable,
                "commit": "old-commit",
            }

            def snapshot(_repo: Path, _commit: str, destination: Path) -> None:
                broker = destination / "src" / "fulcrum" / "broker.py"
                broker.parent.mkdir(parents=True)
                broker.write_text("new\n", encoding="utf-8")

            with (
                patch("fulcrum.activation.selection", return_value=previous),
                patch(
                    "fulcrum.activation.git", side_effect=["new-commit", "new-commit"]
                ),
                patch("fulcrum.activation.snapshot", side_effect=snapshot),
                patch("fulcrum.activation.prepare_python", return_value=sys.executable),
                patch("fulcrum.activation.preflight"),
                patch("fulcrum.activation.select_candidate") as select_candidate,
                patch("fulcrum.activation.cleanup_sources"),
            ):
                result = activate(
                    instance,
                    root / "missing-config.yaml",
                    {"repository": str(root / "repo")},
                )

            self.assertEqual(result["state"], "activated")
            self.assertIsNone(result["broker_maintenance"])
            select_candidate.assert_called_once()

    def test_cached_broker_rejection_is_retried_after_connection_owner_is_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = root / "instance"
            instance.mkdir()
            (instance / "activation.json").write_text(
                json.dumps(
                    {
                        "observed_commit": "new-commit",
                        "state": "rejected",
                        "reason": BROKER_HANDOFF_REASON,
                        "broker_maintenance": {"changed": ["src/fulcrum/broker.py"]},
                    }
                ),
                encoding="utf-8",
            )
            old_source = root / "old"
            old_broker = old_source / "src" / "fulcrum" / "broker.py"
            old_broker.parent.mkdir(parents=True)
            old_broker.write_text("old\n", encoding="utf-8")
            previous = {
                "source": str(old_source),
                "python": sys.executable,
                "commit": "old-commit",
            }

            def snapshot(_repo: Path, _commit: str, destination: Path) -> None:
                broker = destination / "src" / "fulcrum" / "broker.py"
                broker.parent.mkdir(parents=True)
                broker.write_text("new\n", encoding="utf-8")

            with (
                patch("fulcrum.activation.selection", return_value=previous),
                patch(
                    "fulcrum.activation.git", side_effect=["new-commit", "new-commit"]
                ),
                patch("fulcrum.activation.snapshot", side_effect=snapshot),
                patch("fulcrum.activation.prepare_python", return_value=sys.executable),
                patch("fulcrum.activation.preflight"),
                patch("fulcrum.activation.select_candidate") as select_candidate,
                patch("fulcrum.activation.cleanup_sources"),
            ):
                result = activate(
                    instance,
                    root / "missing-config.yaml",
                    {"repository": str(root / "repo")},
                )

            self.assertEqual(result["state"], "activated")
            select_candidate.assert_called_once()

    def test_changed_broker_still_requires_handoff_when_service_is_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = root / "instance"
            service = instance / "services" / "broker.plist"
            service.parent.mkdir(parents=True)
            service.touch()
            old_source = root / "old"
            old_broker = old_source / "src" / "fulcrum" / "broker.py"
            old_broker.parent.mkdir(parents=True)
            old_broker.write_text("old\n", encoding="utf-8")
            previous = {
                "source": str(old_source),
                "python": sys.executable,
                "commit": "old-commit",
            }

            def snapshot(_repo: Path, _commit: str, destination: Path) -> None:
                broker = destination / "src" / "fulcrum" / "broker.py"
                broker.parent.mkdir(parents=True)
                broker.write_text("new\n", encoding="utf-8")

            with (
                patch("fulcrum.activation.selection", return_value=previous),
                patch("fulcrum.activation.git", return_value="new-commit"),
                patch("fulcrum.activation.snapshot", side_effect=snapshot),
                patch("fulcrum.activation.prepare_python", return_value=sys.executable),
                patch("fulcrum.activation.preflight"),
            ):
                with self.assertRaisesRegex(RuntimeError, BROKER_HANDOFF_REASON):
                    activate(
                        instance,
                        root / "config.yaml",
                        {"repository": str(root / "repo")},
                    )


if __name__ == "__main__":
    unittest.main()
