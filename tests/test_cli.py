from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.cli import main
from fulcrum.config import RuntimePaths


class CliTest(unittest.TestCase):
    def test_context_uses_only_the_calling_thread_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root, root, root / "config.json", root / "control")
            with (
                patch("fulcrum.cli.resolve_paths", return_value=paths),
                patch("fulcrum.cli._thread_id", return_value="managed-thread"),
                patch(
                    "fulcrum.cli.request_sync",
                    return_value={"data": {"context": "current action"}},
                ) as request,
                patch("sys.stdout", new=io.StringIO()),
            ):
                result = main(["context"])

            self.assertEqual(result, 0)
            request.assert_called_once_with(
                paths.socket,
                {"command": "context", "thread_id": "managed-thread"},
            )

    def test_finish_rejects_missing_input_before_controller_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root, root, root / "config.json", root / "control")
            missing = root / "decisions.json"
            stderr = io.StringIO()
            with (
                patch("fulcrum.cli.resolve_paths", return_value=paths),
                patch("fulcrum.cli._request") as request,
                patch("sys.stderr", new=stderr),
            ):
                result = main(["finish", "decisions", "--input", str(missing)])

            self.assertEqual(result, 2)
            request.assert_not_called()
            self.assertIn("cannot read valid JSON", stderr.getvalue())
            self.assertIn(str(missing), stderr.getvalue())

    def test_finish_sends_complete_input_snapshot_to_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root, root, root / "config.json", root / "control")
            input_path = root / "decisions.json"
            decision = {
                "decisions": [],
                "handled_update_ids": [1],
            }
            input_path.write_text(json.dumps(decision), encoding="utf-8")

            def receive(
                _paths: RuntimePaths, request: dict[str, object]
            ) -> dict[str, object]:
                input_path.write_text("{", encoding="utf-8")
                self.assertEqual(request["options"], {"input": decision})
                return {"ok": True}

            with (
                patch("fulcrum.cli.resolve_paths", return_value=paths),
                patch("fulcrum.cli._request", side_effect=receive) as request,
                patch("sys.stdout", new=io.StringIO()),
            ):
                result = main(["finish", "decisions", "--input", str(input_path)])

            self.assertEqual(result, 0)
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
