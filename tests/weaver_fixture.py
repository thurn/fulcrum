"""Isolated provider doubles for real Weaver/Marshal command handlers."""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

from fulcrum.configuration import ConfigurationManager, default_config
from fulcrum.contracts import ActorContext
from fulcrum.runtime import TaskFacts, TurnFacts
from tests.support import MemoryLedger, record, request


class FormulaLedger(MemoryLedger):
    def cook(self, formula, variables):
        # The external formula provider is replaced; use the actual packaged
        # formula and one-pass substitution so literal {{...}} stays literal.
        step = json.loads(Path(formula).read_text())["steps"][0]
        return {
            key: re.sub(r"\{\{(\w+)\}\}", lambda m: variables[m[1]], step[key])
            for key in ("title", "description")
        }


class NativeFixture:
    def __init__(self):
        self.transport = self
        self.titles = {}
        self.turns = []

    async def set_name(self, thread, title):
        self.titles[thread] = title

    async def inspect_task(self, thread):
        return TaskFacts(
            thread,
            self.titles.get(thread),
            None,
            "toy",
            (),
            False,
            True,
            True,
            "idle",
            None,
            None,
            (),
            "2026-09-16T00:00:00Z",
        )

    async def start_turn(self, thread, turn):
        self.turns.append((thread, turn))
        return TurnFacts(
            "turn",
            thread,
            "running",
            turn.operation_id,
            False,
            None,
            (),
            None,
            "2026-09-16T00:00:00Z",
        )


class WeaverFixture:
    def __enter__(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.config = default_config(self.root)
        self.config["projects"] = {"toy": {"root": str(self.root), "enabled": True}}
        self.config["timing"]["marshal_coalesce_seconds"] = 0
        self.native = NativeFixture()
        self.ledger = FormulaLedger(
            record("fc-system", kind="system", marshal_thread="marshal"),
            record(
                "fc-marshal",
                kind="task",
                thread_id="marshal",
                role="marshal",
                creation_cwd=str(self.root),
                model="fixture",
                effort="low",
                last_observed={"runtime_status": "idle"},
            ),
        )
        self.ledger.workspace = self.root
        for module in ("roles", "work", "completion", "leadership"):
            self.stack.enter_context(
                patch(f"fulcrum.{module}._ledger", return_value=self.ledger)
            )
        self.loads = self.stack.enter_context(
            patch.object(ConfigurationManager, "load", return_value=(self.config, b""))
        )
        self.base = request()
        self.base = replace(
            self.base,
            instance=replace(
                self.base.instance,
                instance_root=self.root,
                brain_root=self.root,
                config_path=self.root / "fulcrum.yaml",
            ),
            runtime_submit=lambda action, timeout: asyncio.run(action(self.native)),
        )
        return self

    def __exit__(self, *args):
        self.stack.__exit__(*args)

    def entry(self, description="Please delete docs/hooks.md"):
        return replace(
            self.base,
            command=("enter",),
            arguments={"role": "weaver"},
            input={"description": description},
            thread_id="weaver",
            actor=ActorContext(kind="task", task_id="weaver"),
        )

    def finish(self, entry_result):
        import uuid

        # Used against both baseline and candidate in the measurement harness.
        payload = entry_result.result
        if "result" in payload:
            payload = payload["result"]
        return replace(
            self.base,
            command=("finish",),
            arguments={"bead": payload["bead_id"], "outcome": "ready"},
            input={
                "summary": "Delete docs/hooks.md and remove its two incoming links in README.md and docs/fulcrum2/audit.md; leave unrelated text intact.",
                "acceptance": [
                    "The file is absent and neither incoming link is broken."
                ],
                "evidence": ["README.md:12", "docs/fulcrum2/audit.md:78"],
            },
            thread_id="weaver",
            actor=ActorContext(kind="task", task_id="weaver"),
            ownership_operation=payload["ownership_operation"],
            request_id=str(uuid.uuid4()),
        )
