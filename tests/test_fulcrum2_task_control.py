from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock

from fulcrum.runtime import AppServerRuntime, TaskFacts


def facts(
    thread_id: str,
    *,
    status: str = "idle",
    active_turn: str | None = None,
    last_turn: dict[str, Any] | None = None,
    pending: tuple[dict[str, Any], ...] = (),
    cwd: str = "/work",
) -> TaskFacts:
    return TaskFacts(
        id=thread_id,
        title=f"Task {thread_id}",
        cwd=cwd,
        project_id="project-toy",
        workspace_roots=(cwd,),
        archived=False,
        exists=True,
        loaded=True,
        runtime_status=status,
        active_turn=active_turn,
        last_turn=last_turn,
        pending_requests=pending,
        observed_at="2026-09-14T19:00:00Z",
    )


class NativeOutputBoundTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_output_defaults_to_the_latest_observed_turn(self) -> None:
        runtime = AppServerRuntime("fake://runtime")
        runtime.inspect_task = AsyncMock(
            return_value=facts(
                "thread-1",
                active_turn=None,
                last_turn={
                    "id": "turn-latest",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": "retained answer"}],
                },
            )
        )
        runtime.transport.thread_items_page = AsyncMock(
            return_value={"items": [], "next_cursor": None}
        )

        result = await runtime.output(
            "thread-1", turn_id=None, limit=20, cursor=None, max_bytes=128
        )

        self.assertEqual(result["turn_id"], "turn-latest")
        self.assertEqual(
            result["items"][0]["item"]["text"],
            "retained answer",
        )
        self.assertIn("lossy thread history", result["gaps"][0])
        self.assertEqual(
            runtime.transport.thread_items_page.await_args.kwargs["turn_id"],
            "turn-latest",
        )

    async def test_runtime_output_pages_without_resuming_and_truncates_one_item(
        self,
    ) -> None:
        runtime = AppServerRuntime("fake://runtime")
        runtime.transport.thread_items_page = AsyncMock(
            side_effect=[
                {
                    "items": [{"turnId": "turn-1", "item": {"text": "x" * 400}}],
                    "next_cursor": "after-large",
                }
            ]
        )
        result = await runtime.output(
            "thread-1",
            turn_id="turn-1",
            limit=20,
            cursor=None,
            max_bytes=128,
        )
        self.assertTrue(result["items"][0]["truncated"])
        self.assertEqual(result["next_cursor"], "after-large")
        self.assertTrue(result["gaps"])
        self.assertLessEqual(result["bytes"], 128)
        runtime.transport.thread_items_page.assert_awaited_once()

    async def test_limit_zero_reads_all_native_terminal_pages(self) -> None:
        runtime = AppServerRuntime("fake://runtime")
        runtime.transport.background_terminals_page = AsyncMock(
            side_effect=[
                {
                    "items": [{"processId": "process-1", "itemId": "item-1"}],
                    "next_cursor": "page-2",
                },
                {
                    "items": [{"processId": "process-2", "itemId": "item-2"}],
                    "next_cursor": None,
                },
            ]
        )
        result = await runtime.terminals("thread-1", limit=0, cursor=None)
        self.assertEqual(
            [item["terminal_id"] for item in result["items"]],
            ["process-1", "process-2"],
        )
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(
            runtime.transport.background_terminals_page.await_args_list[1].kwargs[
                "cursor"
            ],
            "page-2",
        )

    async def test_release_never_cleans_a_still_running_terminal(self) -> None:
        runtime = AppServerRuntime("fake://runtime")
        runtime.inspect_task = AsyncMock(return_value=facts("thread-1"))
        runtime.transport.background_terminals = AsyncMock(
            return_value=[{"processId": "process-1", "itemId": "item-1"}]
        )
        runtime.transport.clean_background_terminals = AsyncMock()
        runtime.transport.unsubscribe = AsyncMock(return_value="unsubscribed")
        released = await runtime.release("thread-1")
        self.assertEqual(released.active_terminals[0]["processId"], "process-1")
        runtime.transport.clean_background_terminals.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
