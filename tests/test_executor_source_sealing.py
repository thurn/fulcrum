from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

from fulcrum.completion import _commit_executor_source, _executor_finish_payload
from fulcrum.contracts import FulcrumError
from fulcrum.delivery import WorkspaceFacts
from tests.support import record, request


def test_executor_finish_payload_rejects_worker_supplied_source() -> None:
    call = request(
        ("finish",),
        input={
            "summary": "changed the fixture",
            "source_oid": "a" * 40,
            "checks": [
                {"name": "fixture", "status": "passed", "evidence": "value=new"}
            ],
            "evidence": ["fixture updated"],
        },
    )

    try:
        _executor_finish_payload(call)
    except FulcrumError as error:
        assert error.code == "INVALID_INPUT"
        assert "must omit source_oid" in str(error)
    else:
        raise AssertionError("worker-supplied source_oid was accepted")


def test_fulcrum_commits_dirty_executor_worktree_once_and_replays() -> None:
    base_oid = "a" * 40
    source_oid = "b" * 40
    dirty = WorkspaceFacts(
        path="/managed/worker",
        branch="fulcrum/fc-source",
        base_oid=base_oid,
        head_oid=base_oid,
        exists=True,
        owned=True,
        dirty=True,
        dirty_entries=(" M fixture.txt",),
        preparation="observed",
        ownership_evidence={"matches": 1},
        observed_at="2026-09-18T00:00:00Z",
    )
    clean = WorkspaceFacts(
        **{
            **dirty.__dict__,
            "head_oid": source_oid,
            "dirty": False,
            "dirty_entries": (),
        }
    )
    provider = MagicMock()
    provider.inspect_workspace = AsyncMock(side_effect=[dirty, clean, clean])
    work = record("fc-source", ownership_operation="workspace-operation")
    call = request(("finish",), arguments={"bead": "fc-source"})

    def completed(arguments, **_kwargs):
        if "diff" in arguments:
            return subprocess.CompletedProcess(arguments, 1, "", "")
        if "rev-list" in arguments:
            return subprocess.CompletedProcess(arguments, 0, "1\n", "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    with (
        patch(
            "fulcrum.completion._context",
            return_value=(object(), work, object(), provider),
        ),
        patch("fulcrum.delivery_service._work_ref", return_value=object()),
        patch("fulcrum.completion.subprocess.run", side_effect=completed) as run,
    ):
        first = _commit_executor_source(call, work)
        replay = _commit_executor_source(call, work)

    assert first == replay
    assert first["source_oid"] == source_oid
    commit_calls = [item for item in run.call_args_list if "commit" in item.args[0]]
    assert len(commit_calls) == 1
    assert commit_calls[0].args[0][-2:] == ["-m", "chore: complete fc-source"]
