from __future__ import annotations

import json
import unittest
from pathlib import Path

from fulcrum.concurrency_smoke import _capacity_policy, _json_default


class Fulcrum2ConcurrencyClientTest(unittest.TestCase):
    def test_capacity_payload_uses_the_authoritative_policy_shape(self) -> None:
        self.assertEqual(
            _capacity_policy(30),
            {
                "automatic_capacity": 30,
                "default_project_capacity": 30,
                "project_capacity": {"fixture": 30},
                "rationale": "Explicit disposable native concurrency smoke.",
            },
        )

    def test_report_encoder_preserves_paths_as_strings(self) -> None:
        rendered = json.dumps(
            {"evidence": Path("/tmp/fixture/bin/fulcrum")}, default=_json_default
        )

        self.assertEqual(json.loads(rendered), {"evidence": "/tmp/fixture/bin/fulcrum"})


if __name__ == "__main__":
    unittest.main()
