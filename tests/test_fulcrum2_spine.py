from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from fulcrum.contracts import ActorContext, ParsedRequest
from fulcrum.instance import WriterLock, resolve_instance


class Fulcrum2SpineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.brain = self.root / "brain"
        self.instance = self.root / "instance"
        self.brain.mkdir()
        self.instance.mkdir()
        self.config = self.brain / "fulcrum.yaml"
        self.config.write_text(f"brain:\n  root: {self.brain}\n", encoding="utf-8")
        (self.instance / "config").symlink_to(self.config)
        self.executable = Path(os.sys.executable).with_name("fulcrum")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(
        self, *arguments: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.executable), *arguments],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_help_is_local_and_common_flags_work_at_each_command_level(self) -> None:
        help_result = self.invoke("--instance", str(self.root / "missing"), "--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("work", help_result.stdout)

        result = self.invoke(
            "work",
            "--json",
            "show",
            "fc-example",
            "--instance",
            str(self.instance),
            "--timeout",
            "5",
        )
        self.assertEqual(result.returncode, 4)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(envelope["error"]["details"]["command"], ["work", "show"])

    def test_input_errors_are_clean_envelopes_and_name_fields(self) -> None:
        malformed = self.invoke(
            "--instance",
            str(self.instance),
            "work",
            "create",
            "--input",
            "-",
            "--json",
            input_text="{not json",
        )
        self.assertEqual(malformed.returncode, 2)
        self.assertEqual(json.loads(malformed.stdout)["error"]["code"], "INVALID_JSON")
        self.assertEqual(malformed.stderr, "")

        unknown = self.invoke(
            "work",
            "create",
            "--instance",
            str(self.instance),
            "--input",
            "-",
            "--json",
            input_text=json.dumps({"title": "literal", "surprise": True}),
        )
        self.assertEqual(unknown.returncode, 2)
        self.assertEqual(
            json.loads(unknown.stdout)["error"]["details"]["fields"], ["surprise"]
        )

        duplicate = self.invoke(
            "enter",
            "weaver",
            "--description",
            "direct",
            "--instance",
            str(self.instance),
            "--input",
            "-",
            "--json",
            input_text=json.dumps({"description": "file"}),
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertEqual(
            json.loads(duplicate.stdout)["error"]["details"]["fields"], ["description"]
        )

    def test_request_identity_is_emitted_and_exact_payload_survives_wire_roundtrip(
        self,
    ) -> None:
        request_id = str(uuid.uuid4())
        instance = resolve_instance(instance=str(self.instance), config=None)
        payload = {
            "title": "quotes '$()'\nand newlines",
            "acceptance": ["literal ; | &"],
        }
        request = ParsedRequest(
            command=("work", "create"),
            arguments={},
            input=payload,
            actor=ActorContext.parse("human"),
            instance=instance,
            request_id=request_id,
        )
        restored = ParsedRequest.from_wire(request.to_wire())
        self.assertEqual(restored.input, payload)
        self.assertEqual(restored.request_id, request_id)

        generated = self.invoke(
            "work", "create", "--instance", str(self.instance), "--offline", "--json"
        )
        self.assertEqual(generated.returncode, 4)
        emitted = generated.stderr.strip().removeprefix("request_id=")
        uuid.UUID(emitted)
        self.assertEqual(json.loads(generated.stdout)["request_id"], emitted)

    def test_writer_lock_is_shared_by_instance_aliases(self) -> None:
        alias = self.root / "alias"
        alias.mkdir()
        (alias / "config").symlink_to(self.config)
        first = resolve_instance(instance=str(self.instance), config=None)
        second = resolve_instance(instance=str(alias), config=None)
        self.assertEqual(first.lock_path, second.lock_path)
        assert first.lock_path is not None and second.lock_path is not None
        with WriterLock(first.lock_path):
            with self.assertRaisesRegex(Exception, "another Fulcrum writer"):
                WriterLock(second.lock_path).acquire()

    def test_served_and_offline_paths_use_the_same_application_result(self) -> None:
        process = subprocess.Popen(
            [str(self.executable), "serve", "--instance", str(self.instance)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _ in range(100):
                if (self.instance / "controller.sock").exists():
                    break
                if process.poll() is not None:
                    self.fail(f"controller exited early: {process.stderr.read()}")
                time.sleep(0.02)
            served = self.invoke("status", "--instance", str(self.instance), "--json")
            self.assertEqual(served.returncode, 0)

            busy = self.invoke(
                "reconcile", "--instance", str(self.instance), "--offline", "--json"
            )
            self.assertEqual(busy.returncode, 4)
            self.assertEqual(json.loads(busy.stdout)["error"]["code"], "WRITER_BUSY")
        finally:
            process.terminate()
            process.communicate(timeout=5)

        offline = self.invoke(
            "status", "--instance", str(self.instance), "--offline", "--json"
        )
        self.assertEqual(offline.returncode, 0)
        served_result = json.loads(served.stdout)["result"]
        offline_result = json.loads(offline.stdout)["result"]
        for field in (
            "instance",
            "work",
            "operations",
            "capacity",
            "publication",
            "gaps",
        ):
            self.assertEqual(served_result[field], offline_result[field])


if __name__ == "__main__":
    unittest.main()
