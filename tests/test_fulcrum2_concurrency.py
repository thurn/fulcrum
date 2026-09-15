from __future__ import annotations

import json
import unittest
from pathlib import Path

from fulcrum.concurrency_smoke import _json_default


class Fulcrum2ConcurrencyClientTest(unittest.TestCase):
    def test_report_encoder_preserves_paths_as_strings(self) -> None:
        rendered = json.dumps(
            {"evidence": Path("/tmp/fixture/bin/fulcrum")}, default=_json_default
        )

        self.assertEqual(json.loads(rendered), {"evidence": "/tmp/fixture/bin/fulcrum"})


if __name__ == "__main__":
    unittest.main()
