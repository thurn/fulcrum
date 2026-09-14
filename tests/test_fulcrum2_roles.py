from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve

from fulcrum.ledger import Ledger
from fulcrum.roles import fallback_instructions, role_title

ROLES = (
    "vizier",
    "marshal",
    "weaver",
    "executor",
    "warden",
    "sage",
    "mason",
    "justiciar",
)


class Fulcrum2RoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_degraded_entry_returns_truthful_unregistered_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brain, instance, project = (
                root / "brain",
                root / "instance",
                root / "project",
            )
            for path in (brain, instance, project):
                path.mkdir()
            config = brain / "fulcrum.yaml"
            config.write_text(
                "\n".join(
                    (
                        "beads:",
                        "  executable: /definitely-missing/fulcrum-bd",
                        "runtime:",
                        "  kind: codex",
                        "  endpoint: ws://127.0.0.1:1",
                        "brain:",
                        f"  root: {brain}",
                        "models:",
                        "  executor:",
                        "    model: luna",
                        "    effort: high",
                        "projects:",
                        "  toy:",
                        f"    root: {project}",
                        "    enabled: true",
                        "    delivery:",
                        "      id: delivery-project",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            (instance / "config").symlink_to(config)
            code, stdout, stderr = await self.invoke(
                "enter",
                "executor",
                "--description",
                "Do useful work even if registration is unavailable.",
                "--project",
                "toy",
                "--instance",
                str(instance),
                "--offline",
                "--actor",
                "human",
                "--request-id",
                str(uuid.uuid4()),
                "--json",
            )
            self.assertEqual(code, 6, stderr + stdout)
            envelope = json.loads(stdout)
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["state"], "degraded")
            self.assertFalse(envelope["result"]["registered"])
            self.assertIsNone(envelope["result"]["bead_id"])
            self.assertIn("Fulcrum Executor", envelope["result"]["instructions"])
            self.assertIn("no replay was queued", envelope["warnings"][0])

            subprocess.run(["git", "init", "--quiet"], cwd=brain, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=brain,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"], cwd=brain, check=True
            )
            subprocess.run(
                ["git", "config", "beads.role", "maintainer"],
                cwd=brain,
                check=True,
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
                cwd=brain,
                capture_output=True,
                check=True,
                timeout=30,
            )
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "beads:\n  executable: /definitely-missing/fulcrum-bd\n", ""
                ),
                encoding="utf-8",
            )
            code, stdout, stderr = await self.invoke(
                "enter",
                "executor",
                "--description",
                "Retain the actual work identity when the runtime is unavailable.",
                "--project",
                "toy",
                "--instance",
                str(instance),
                "--offline",
                "--actor",
                "human",
                "--timeout",
                "1",
                "--request-id",
                str(uuid.uuid4()),
                "--json",
            )
            self.assertEqual(code, 6, stderr + stdout)
            runtime_degraded = json.loads(stdout)
            bead_id = runtime_degraded["result"]["bead_id"]
            operation_id = runtime_degraded["result"]["operation"]
            self.assertIsInstance(bead_id, str)
            self.assertIsInstance(operation_id, str)
            self.assertIsNotNone(Ledger(brain).show(bead_id))
            receipt = Ledger(brain).show(operation_id)
            assert receipt is not None and receipt.fc
            self.assertIn(receipt.fc["state"], {"failed", "uncertain"})
            subprocess.run(
                ["bd", "-C", str(brain), "dolt", "stop"],
                capture_output=True,
                check=False,
                timeout=20,
            )

    async def test_all_entries_cook_full_prompts_titles_reentry_and_hook(self) -> None:
        threads: dict[str, dict[str, Any]] = {}
        sent: dict[str, str] = {}

        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                identifier = message.get("id")
                method = message.get("method")
                params = message.get("params", {})
                if method == "initialize":
                    result: dict[str, Any] = {}
                elif method == "model/list":
                    result = {
                        "data": [
                            {
                                "model": "luna",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "high"}
                                ],
                            }
                        ]
                    }
                elif method == "thread/list":
                    requested = params.get("cwd")
                    requested = (
                        requested if isinstance(requested, list) else [requested]
                    )
                    result = {
                        "data": [
                            thread
                            for thread in threads.values()
                            if thread["cwd"] in requested
                            and bool(thread.get("archived"))
                            == bool(params.get("archived"))
                        ],
                        "nextCursor": None,
                    }
                elif method == "thread/loaded/list":
                    result = {"data": list(threads)}
                elif method == "thread/start":
                    thread_id = f"native-{len(threads) + 1}"
                    thread = {
                        "id": thread_id,
                        "name": None,
                        "cwd": params["cwd"],
                        "environments": [
                            {
                                "environmentId": "local",
                                "cwd": params["cwd"],
                                "runtimeWorkspaceRoots": params[
                                    "runtimeWorkspaceRoots"
                                ],
                            }
                        ],
                        "projectId": params["projectId"],
                        "status": {"type": "idle"},
                        "archived": False,
                        "turns": [],
                    }
                    threads[thread_id] = thread
                    result = {"thread": thread}
                elif method == "thread/name/set":
                    threads[params["threadId"]]["name"] = params["name"]
                    result = {}
                elif method == "thread/settings/update":
                    thread = threads[params["threadId"]]
                    thread["environments"][0]["cwd"] = params["cwd"]
                    thread["model"] = params["model"]
                    thread["reasoningEffort"] = params["effort"]
                    result = {}
                elif method == "thread/read":
                    result = {"thread": threads[params["threadId"]]}
                elif method == "turn/start":
                    thread = threads[params["threadId"]]
                    prompt = params["input"][0]["text"]
                    sent[thread["id"]] = prompt
                    turn = {
                        "id": f"turn-{thread['id']}",
                        "status": "completed",
                        "items": [{"type": "userMessage", "text": prompt}],
                    }
                    thread["turns"] = [turn]
                    result = {"turn": turn}
                else:
                    continue
                await connection.send(json.dumps({"id": identifier, "result": result}))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                brain, instance, project = (
                    root / "brain",
                    root / "instance",
                    root / "project",
                )
                for path in (brain, instance, project):
                    path.mkdir()
                for path in (brain, project):
                    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
                    subprocess.run(
                        ["git", "config", "user.email", "fixture@example.com"],
                        cwd=path,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "config", "user.name", "Fixture"],
                        cwd=path,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "config", "beads.role", "maintainer"],
                        cwd=path,
                        check=True,
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
                    cwd=brain,
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                model_lines = []
                for role in ROLES:
                    model_lines.extend(
                        (f"  {role}:", "    model: luna", "    effort: high")
                    )
                config = brain / "fulcrum.yaml"
                config.write_text(
                    "\n".join(
                        (
                            "runtime:",
                            "  kind: codex",
                            f"  endpoint: ws://127.0.0.1:{port}",
                            "brain:",
                            f"  root: {brain}",
                            "models:",
                            *model_lines,
                            "projects:",
                            "  toy:",
                            f"    root: {project}",
                            "    enabled: true",
                            "    codex_project_id: native-project",
                            "    delivery:",
                            "      id: delivery-project",
                            "",
                        )
                    ),
                    encoding="utf-8",
                )
                (instance / "config").symlink_to(config)
                ledger = Ledger(brain)
                leader_threads = {
                    "vizier": "native-vizier-leader",
                    "marshal": "native-marshal-leader",
                }
                for role, thread_id in leader_threads.items():
                    creation_cwd = instance / "threads" / f"standing-{role}"
                    creation_cwd.mkdir(parents=True)
                    threads[thread_id] = {
                        "id": thread_id,
                        "name": role_title(role, "fc-leader", "ignored"),
                        "cwd": str(creation_cwd),
                        "environments": [
                            {
                                "environmentId": "local",
                                "cwd": str(project),
                                "runtimeWorkspaceRoots": [str(project)],
                            }
                        ],
                        "projectId": "native-project",
                        "status": {"type": "idle"},
                        "archived": False,
                        "turns": [],
                    }
                    ledger.create_record(
                        record_id=f"fc-standing-{role}",
                        kind="task",
                        title=f"Standing {role}",
                        description=f"Native standing {role} task.",
                        owner=thread_id,
                        fc={
                            "kind": "task",
                            "owner": thread_id,
                            "thread_id": thread_id,
                            "role": role,
                            "work_bead": None,
                            "ownership_operation": f"standing-{role}",
                            "creation_operation": f"standing-{role}",
                            "creation_cwd": str(creation_cwd),
                            "model": "luna",
                            "effort": "high",
                            "model_origin": "fixture",
                            "associated_beads": [],
                        },
                    )
                ledger.create_record(
                    record_id="fc-system",
                    kind="control",
                    title="Fulcrum control",
                    description="Standing leadership identity.",
                    owner=leader_threads["marshal"],
                    fc={
                        "kind": "control",
                        "owner": leader_threads["marshal"],
                        "vizier_thread": leader_threads["vizier"],
                        "marshal_thread": leader_threads["marshal"],
                        "active_takeover": None,
                        "last_transition": "fixture",
                    },
                )
                entries: dict[str, dict[str, Any]] = {}
                description = (
                    "Literal first line\nSecond line with `code` and all scope."
                )
                for role in ROLES:
                    code, stdout, stderr = await self.invoke(
                        "enter",
                        role,
                        "--description",
                        description,
                        "--project",
                        "toy",
                        "--model",
                        "luna",
                        "--effort",
                        "high",
                        "--instance",
                        str(instance),
                        "--offline",
                        "--actor",
                        "human",
                        "--request-id",
                        str(uuid.uuid4()),
                        "--json",
                    )
                    self.assertEqual(code, 0, stderr + stdout)
                    envelope = json.loads(stdout)
                    result = envelope["result"]["result"]
                    entries[role] = result
                    thread_id = result["thread_id"]
                    bead_id = result["bead_id"]
                    instructions = result["instructions"]
                    self.assertEqual(
                        result["model_origin"], "explicit:model,explicit:effort"
                    )
                    self.assertEqual(
                        threads[thread_id]["name"],
                        role_title(role, bead_id, "Literal first line"),
                    )
                    self.assertIn(description, instructions)
                    self.assertIn("Acceptance is not yet specified", instructions)
                    self.assertIn(f"--thread-id {thread_id}", instructions)
                    self.assertIn(
                        f"--ownership-operation {envelope['operation_id']}",
                        instructions,
                    )
                    self.assertIn(
                        f"[Fulcrum operation {envelope['operation_id']}; ownership operation {envelope['operation_id']}]",
                        instructions,
                    )
                    self.assertEqual(
                        sent[thread_id],
                        (
                            f"FULCRUM_OPERATION={envelope['operation_id']} "
                            f"FULCRUM_OWNERSHIP_OPERATION={envelope['operation_id']}"
                            f"\n\n{instructions}"
                        ),
                    )
                    self.assertNotIn("read the bead", instructions.lower())
                    record = Ledger(brain).show(bead_id)
                    assert record is not None and record.fc
                    self.assertEqual(record.fc["outcome"], description)
                    self.assertEqual(
                        record.native["description"],
                        instructions.rsplit("\n\n[Fulcrum operation", 1)[0],
                    )

                executor = entries["executor"]
                before_threads = len(threads)
                before_turns = len(sent)
                code, stdout, stderr = await self.invoke(
                    "enter",
                    "executor",
                    "--description",
                    description,
                    "--bead",
                    executor["bead_id"],
                    "--project",
                    "toy",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--actor",
                    "human",
                    "--request-id",
                    str(uuid.uuid4()),
                    "--json",
                )
                self.assertEqual(code, 0, stderr + stdout)
                repeated = json.loads(stdout)["result"]
                self.assertTrue(repeated["reused"])
                self.assertEqual(len(threads), before_threads)
                self.assertEqual(len(sent), before_turns)

                code, stdout, stderr = await self.invoke(
                    "context",
                    "--bead",
                    executor["bead_id"],
                    "--instance",
                    str(instance),
                    "--offline",
                    "--json",
                )
                self.assertEqual(code, 0, stderr + stdout)
                context = json.loads(stdout)["result"]
                self.assertEqual(
                    context["instructions"],
                    executor["instructions"].rsplit("\n\n[Fulcrum operation", 1)[0],
                )
                self.assertEqual(
                    context["ownership_operation"], executor["ownership_operation"]
                )

                before_threads = len(threads)
                before_turns = len(sent)
                code, stdout, stderr = await self.invoke(
                    "enter",
                    "executor",
                    "--description",
                    "Caller-bound work\nKeep the second line literal.",
                    "--thread-id",
                    executor["thread_id"],
                    "--project",
                    "toy",
                    "--model",
                    "luna",
                    "--effort",
                    "high",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--actor",
                    "human",
                    "--request-id",
                    str(uuid.uuid4()),
                    "--json",
                )
                self.assertEqual(code, 0, stderr + stdout)
                bound_envelope = json.loads(stdout)
                bound = bound_envelope["result"]["result"]
                self.assertEqual(bound["thread_id"], executor["thread_id"])
                self.assertIsNone(bound["turn"])
                self.assertEqual(len(threads), before_threads)
                self.assertEqual(len(sent), before_turns)
                self.assertEqual(
                    threads[executor["thread_id"]]["name"],
                    role_title("executor", bound["bead_id"], "Caller-bound work"),
                )
                prior = Ledger(brain).show(executor["bead_id"])
                assert prior is not None and prior.fc
                self.assertEqual(prior.fc["phase"], "backlog")
                self.assertEqual(
                    prior.fc["last_transition"], bound_envelope["operation_id"]
                )

                hook_payload = {
                    "hook_event_name": "SessionStart",
                    "source": "compact",
                    "session_id": bound["thread_id"],
                }
                before = Ledger(brain).list_records(limit=0)
                code, stdout, stderr = await self.invoke(
                    "hook",
                    "context",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--input",
                    "-",
                    payload=hook_payload,
                )
                self.assertEqual(code, 0, stderr + stdout)
                hook = json.loads(stdout)
                self.assertTrue(hook["continue"])
                self.assertIn("additionalContext", hook["hookSpecificOutput"])
                self.assertIn(
                    "Caller-bound work",
                    hook["hookSpecificOutput"]["additionalContext"],
                )
                after = Ledger(brain).list_records(limit=0)
                self.assertEqual(
                    [item.id for item in before], [item.id for item in after]
                )

                hook_payload["session_id"] = "unrelated"
                code, stdout, stderr = await self.invoke(
                    "hook",
                    "context",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--input",
                    "-",
                    payload=hook_payload,
                )
                self.assertEqual(code, 0, stderr + stdout)
                self.assertEqual(json.loads(stdout), {"continue": True})

                closed_id = entries["warden"]["bead_id"]
                ledger = Ledger(brain)
                closed = ledger.show(closed_id)
                assert closed is not None and closed.fc
                delivered = {"source_oid": "abc123", "promotion": "observed"}
                disposition = {
                    "outcome": "delivered",
                    "summary": "The original delivery remains authoritative.",
                    "waived_requirements": [],
                    "known_defects": [],
                    "canonical_bead": None,
                    "completed_at": "2026-09-14T16:00:00Z",
                }
                ledger.update_fc(
                    closed_id,
                    {
                        **dict(closed.fc),
                        "delivery": delivered,
                        "disposition": disposition,
                    },
                )
                ledger.run(
                    ("close", closed_id, "--reason", "fixture delivered"),
                    mutating=True,
                )
                code, stdout, stderr = await self.invoke(
                    "enter",
                    "sage",
                    "--description",
                    "Investigate the delivered work without changing its identity.",
                    "--bead",
                    closed_id,
                    "--project",
                    "toy",
                    "--model",
                    "luna",
                    "--effort",
                    "high",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--actor",
                    "human",
                    "--request-id",
                    str(uuid.uuid4()),
                    "--json",
                )
                self.assertEqual(code, 0, stderr + stdout)
                investigation = json.loads(stdout)["result"]["result"]
                self.assertEqual(investigation["bead_id"], closed_id)
                reopened = Ledger(brain).show(closed_id)
                assert reopened is not None and reopened.fc
                self.assertEqual(reopened.status, "in_progress")
                self.assertEqual(reopened.fc["role"], "sage")
                self.assertEqual(reopened.fc["delivery"], delivered)
                self.assertEqual(
                    reopened.fc["interrupted_work"]["disposition"], disposition
                )
                subprocess.run(
                    ["bd", "-C", str(brain), "dolt", "stop"],
                    capture_output=True,
                    check=False,
                    timeout=20,
                )

    async def invoke(
        self, *arguments: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, str, str]:
        executable = Path(os.sys.executable).with_name("fulcrum")
        environment = os.environ.copy()
        environment.pop("CODEX_THREAD_ID", None)
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *arguments,
            env=environment,
            stdin=asyncio.subprocess.PIPE if payload is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(
                json.dumps(payload).encode() if payload is not None else None
            ),
            60,
        )
        return process.returncode or 0, stdout.decode(), stderr.decode()

    def test_titles_fallbacks_and_skill_metadata(self) -> None:
        self.assertEqual(role_title("vizier", "fc-51o", "ignored"), "🔮 VIZIER 🔮")
        self.assertEqual(role_title("marshal", "fc-51o", "ignored"), "🧭 MARSHAL 🧭")
        self.assertEqual(
            role_title("executor", "fc-51o.more", "Design search indexing"),
            "🛠️[exe-51o.more] Design search indexing",
        )
        for role in ROLES:
            self.assertTrue(fallback_instructions(role))
            metadata = (
                Path(__file__).parents[1]
                / "skills"
                / f"fulcrum-{role}"
                / "agents"
                / "openai.yaml"
            ).read_text(encoding="utf-8")
            self.assertIn("allow_implicit_invocation: false", metadata)
        bead_metadata = (
            Path(__file__).parents[1]
            / "skills"
            / "fulcrum-bead"
            / "agents"
            / "openai.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("allow_implicit_invocation: false", bead_metadata)


if __name__ == "__main__":
    unittest.main()
