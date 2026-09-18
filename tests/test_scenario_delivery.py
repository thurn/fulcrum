import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fulcrum.delivery import SourceRef, WorkRef
from fulcrum.scenario_delivery import CONTROL_NAME, wait_for_scenario_admission
from tests.support import MemoryLedger, record, request


def test_scenario_barrier_releases_same_base_candidates_in_promotion_order():
    with tempfile.TemporaryDirectory(prefix="fulcrum-scenario-barrier-") as raw:
        root = Path(raw)
        repository = root / "repository"
        instance = root / "instance"
        repository.mkdir()
        instance.mkdir()
        base = "0" * 40
        alpha_oid = "a" * 40
        beta_oid = "b" * 40
        control = {
            "id": "scenario-2-test",
            "enabled": True,
            "repository_id": "repo",
            "fixture_path": "fixture.md",
            "expected_base_oid": base,
            "candidate_lines": {
                "alpha": "Entries: [Alpha]",
                "beta": "Entries: [Beta]",
            },
            "release_order": ["alpha", "beta"],
            "timeout_seconds": 5,
            "poll_seconds": 0.01,
            "state": {},
        }
        (instance / CONTROL_NAME).write_text(json.dumps(control), encoding="utf-8")
        alpha_record = record("fc-alpha")
        beta_record = record("fc-beta")
        ledger = MemoryLedger(alpha_record, beta_record)
        base_request = request()
        barrier_request = replace(
            base_request,
            instance=replace(base_request.instance, instance_root=instance),
        )

        def source(bead_id: str, oid: str) -> SourceRef:
            return SourceRef(
                WorkRef(
                    bead_id,
                    "project",
                    str(repository),
                    "repo",
                    str(repository),
                    bead_id,
                    "main",
                    "prepare",
                    actual_path=str(repository),
                    base_oid=base,
                ),
                oid,
            )

        labels = {alpha_oid: "alpha", beta_oid: "beta"}
        with (
            patch(
                "fulcrum.scenario_delivery._candidate_label",
                side_effect=lambda _document, candidate: labels[candidate.oid],
            ),
            patch("fulcrum.scenario_delivery._source_parent", return_value=base),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            alpha = pool.submit(
                wait_for_scenario_admission,
                barrier_request,
                ledger,
                alpha_record,
                source("fc-alpha", alpha_oid),
            )
            time.sleep(0.05)
            assert not alpha.done()
            beta = pool.submit(
                wait_for_scenario_admission,
                barrier_request,
                ledger,
                beta_record,
                source("fc-beta", beta_oid),
            )
            alpha_evidence = alpha.result(timeout=2)
            assert alpha_evidence is not None
            assert alpha_evidence["release"]["label"] == "alpha"
            try:
                beta.result(timeout=0.05)
            except TimeoutError:
                pass
            else:
                raise AssertionError("Beta was released before Alpha promotion")
            promoted = ledger.show("fc-alpha")
            assert promoted is not None
            ledger.update_fc(
                "fc-alpha",
                {
                    **dict(promoted.fc or {}),
                    "delivery": {
                        "promotion": {
                            "state": "observed",
                            "integration_oid": "integration",
                        }
                    },
                },
            )
            beta_evidence = beta.result(timeout=2)
            assert beta_evidence is not None
            assert beta_evidence["release"]["label"] == "beta"
            assert (
                beta_evidence["release"]["first_candidate"]["promotion"][
                    "integration_oid"
                ]
                == "integration"
            )

        retained = json.loads((instance / CONTROL_NAME).read_text(encoding="utf-8"))
        assert list(retained["state"]["arrivals"]) == ["alpha", "beta"]
        assert [item["label"] for item in retained["state"]["releases"]] == [
            "alpha",
            "beta",
        ]
