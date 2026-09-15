from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fulcrum.deterministic import (
    DeterministicClock,
    DeterministicRuntime,
    ProviderState,
    advance_clock,
    arm_fault,
    emit_provider_event,
    initial_provider_state,
)
from fulcrum.runtime import AppServerError, TaskSpec, TurnInput


class DeterministicProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.endpoint = str(Path(self.temporary.name) / "provider.json")
        ProviderState(self.endpoint).initialize(initial_provider_state())

    async def test_runtime_retains_full_prompt_and_external_completion(self) -> None:
        runtime = DeterministicRuntime(self.endpoint)
        task = await runtime.create_task(
            TaskSpec(
                creation_cwd=self.temporary.name,
                cwd=self.temporary.name,
                project_id="fixture-project",
                workspace_roots=(self.temporary.name,),
                title="deterministic worker",
                model="gpt-5.6-luna",
                effort="low",
            )
        )
        turn = await runtime.start_turn(
            task.id,
            TurnInput(
                text="Preserve this complete fixture prompt.",
                cwd=self.temporary.name,
                workspace_roots=(self.temporary.name,),
                model="gpt-5.6-luna",
                effort="low",
                operation_id="fc-runtime-turn",
                ownership_operation="fc-runtime-owner",
            ),
        )
        retained = ProviderState(self.endpoint).read()["runtime"]["tasks"][task.id]
        self.assertEqual(
            retained["turns"][turn.id]["input"]["text"],
            "Preserve this complete fixture prompt.",
        )

        emit_provider_event(
            self.endpoint,
            {
                "provider": "runtime",
                "event": "runtime.turn_completed",
                "target": task.id,
                "data": {
                    "turn_id": turn.id,
                    "output": "fixture output",
                    "tools": [{"name": "shell", "status": "completed"}],
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                },
            },
        )
        observed = await DeterministicRuntime(self.endpoint).inspect_turn(
            task.id, turn.id
        )
        assert observed is not None
        self.assertTrue(observed.completed)
        self.assertEqual(observed.usage, {"input_tokens": 12, "output_tokens": 3})
        self.assertEqual((await runtime.inspect_task(task.id)).active_turn, None)

    async def test_applied_lost_create_is_discoverable_after_restart(self) -> None:
        arm_fault(
            self.endpoint,
            {
                "provider": "runtime",
                "method": "create_task",
                "occurrence": 1,
                "effect": "applied",
                "response": "timeout",
            },
        )
        runtime = DeterministicRuntime(self.endpoint)
        with self.assertRaises(AppServerError) as raised:
            await runtime.create_task(
                TaskSpec(
                    creation_cwd=self.temporary.name,
                    cwd=self.temporary.name,
                    project_id=None,
                    workspace_roots=(self.temporary.name,),
                    title="lost response",
                    model="gpt-5.6-luna",
                    effort="low",
                )
            )
        self.assertTrue(raised.exception.uncertain)
        discovered = await DeterministicRuntime(self.endpoint).find_tasks(
            self.temporary.name
        )
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].title, "lost response")

    def test_clock_advances_monotonically_in_persisted_state(self) -> None:
        before = DeterministicClock(self.endpoint).now()
        result = advance_clock(self.endpoint, 37.5)
        after = DeterministicClock(self.endpoint).now()
        self.assertEqual((after - before).total_seconds(), 37.5)
        self.assertEqual(result["seconds"], 37.5)
        with self.assertRaises(ValueError):
            advance_clock(self.endpoint, 0)


if __name__ == "__main__":
    unittest.main()
