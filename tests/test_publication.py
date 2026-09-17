from __future__ import annotations

import unittest

from fulcrum.configuration import default_config
from fulcrum.publication import LedgerPublicationService, NativeStatus
from tests.support import BRAIN, MemoryLedger, record


class Adapter:
    remote = "origin"

    def status(self):
        return NativeStatus(branch="main", commit="native-1")

    def inspect_remote(self):
        return {"name": "origin"}

    def remote_data_ref(self):
        return "remote-1"


class PublicationTests(unittest.TestCase):
    def test_status_uses_explicit_mode_without_retired_cadence_setting(self):
        ledger = MemoryLedger(
            record(
                "fc-system",
                kind="control",
                publication={"last_dolt_commit": "native-1"},
            )
        )
        result = LedgerPublicationService().inspect(
            ledger, default_config(BRAIN), Adapter()
        )
        self.assertEqual(result["mode"], "explicit")
        self.assertNotIn("cadence_seconds", result)


if __name__ == "__main__":
    unittest.main()
