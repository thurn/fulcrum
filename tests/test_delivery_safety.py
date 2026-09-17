from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fulcrum.contracts import FulcrumError
from fulcrum.completion import _settled_delivery_for_source
from fulcrum.delivery import (
    DeliveryFacts,
    DeliveryProviderError,
    SourceRef,
    TollgateDelivery,
    WorkRef,
    WorkspaceFacts,
)
from fulcrum.delivery_service import DeliveryService
from tests.support import MemoryLedger, record, request


class DeliverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.ref = WorkRef(
            "fc-work",
            "toy",
            "/unused/project",
            "repo",
            "/unused/worktree",
            "work",
            "main",
            "fc-prepare",
        )
        self.source = SourceRef(self.ref, "current-source")
        self.clean = WorkspaceFacts(
            "/unused/worktree",
            "work",
            "base",
            "current-source",
            True,
            True,
            False,
            (),
            "ready",
            {},
            "2026-09-15T00:00:00Z",
        )
        self.delivery = {
            "source_oid": "current-source",
            "provider_handle": "handle",
            "validation": {"state": "passed"},
            "approved_source": {"oid": "current-source"},
        }

    def invoke(self, method, delivery, workspace, *, source="current-source"):
        work = record(delivery=delivery)
        ledger = MemoryLedger(work)
        provider = Mock()
        provider.inspect_workspace = AsyncMock(return_value=workspace)
        provider.promote = AsyncMock(
            return_value=DeliveryFacts(
                "handle",
                "current-source",
                "passed",
                "promoted",
                "integration",
                "pending",
                "pending",
                {},
                "2026-09-15T00:00:00Z",
            )
        )
        with (
            patch(
                "fulcrum.delivery_service._context",
                return_value=(ledger, work, {}, provider),
            ),
            patch(
                "fulcrum.delivery_service._retained_delivery_source",
                return_value=(self.source, "handle"),
            ),
        ):
            result = getattr(DeliveryService(), method)(
                request(
                    tuple(method.split("_")),
                    arguments={
                        "bead": "fc-work",
                        "source": source,
                        "summary": "reviewed",
                    },
                )
            )
        return result, provider, ledger

    def test_approval_and_promotion_reject_stale_or_unvalidated_source(self):
        for method in ["review_approve", "promotion_start"]:
            for invalid in ["stale", "unvalidated"]:
                delivery = deepcopy(self.delivery)
                if invalid == "unvalidated":
                    delivery["validation"]["state"] = "pending"
                with (
                    self.subTest(method=method, invalid=invalid),
                    self.assertRaises(FulcrumError) as error,
                ):
                    self.invoke(
                        method,
                        delivery,
                        self.clean,
                        source="old-source" if invalid == "stale" else "current-source",
                    )
                self.assertEqual(
                    error.exception.code,
                    "STALE_SOURCE" if invalid == "stale" else "VALIDATION_NOT_PASSED",
                )
        delivery = deepcopy(self.delivery)
        delivery["approved_source"]["oid"] = "old-source"
        with self.assertRaises(FulcrumError) as error:
            self.invoke("promotion_start", delivery, self.clean)
        self.assertEqual(error.exception.code, "STALE_APPROVAL")

    def test_changed_dirty_or_unowned_workspace_cannot_be_approved_or_promoted(self):
        for method in ["review_approve", "promotion_start"]:
            for change in [
                {"head_oid": "changed"},
                {"dirty": True},
                {"owned": False},
                {"exists": False},
            ]:
                with self.subTest(method=method, change=change):
                    result, provider, ledger = self.invoke(
                        method, self.delivery, replace(self.clean, **change)
                    )
                    self.assertFalse(result.ok)
                    self.assertEqual(result.result["error"]["code"], "STALE_SOURCE")
                    provider.promote.assert_not_called()
                    self.assertEqual(
                        ledger.show("fc-work").fc["delivery"], self.delivery
                    )

    def test_clean_current_workspace_can_be_approved(self):
        delivery = deepcopy(self.delivery)
        del delivery["approved_source"]
        result, _, ledger = self.invoke("review_approve", delivery, self.clean)
        self.assertTrue(result.ok)
        approved = ledger.show("fc-work").fc["delivery"]["approved_source"]
        self.assertEqual(approved["oid"], "current-source")
        self.assertEqual(approved["provider_handle"], "handle")

    def test_promotion_uses_only_the_current_approved_source_and_handle(self):
        result, provider, ledger = self.invoke(
            "promotion_start", self.delivery, self.clean
        )
        self.assertTrue(result.ok)
        provider.promote.assert_awaited_once_with(self.source, "handle")
        self.assertEqual(
            ledger.show("fc-work").fc["delivery"]["promotion"]["integration_oid"],
            "integration",
        )

    def test_completed_source_sync_records_publication_and_signals_broker(self):
        work = record(delivery=self.delivery)
        ledger = MemoryLedger(work)
        provider = Mock()
        provider.synchronize = AsyncMock(
            return_value=DeliveryFacts(
                "handle",
                "current-source",
                "passed",
                "promoted",
                "integration",
                "complete",
                "pending",
                {},
                "2026-09-15T00:00:00Z",
            )
        )
        with (
            patch(
                "fulcrum.delivery_service._context",
                return_value=(ledger, work, {}, provider),
            ),
            patch(
                "fulcrum.delivery_service._retained_delivery_source",
                return_value=(self.source, "handle"),
            ),
            patch("fulcrum.broker.broker_request", new=AsyncMock()) as signal,
        ):
            result = DeliveryService().source_sync(
                request(("source", "sync"), arguments={"bead": "fc-work"})
            )
        self.assertTrue(result.ok)
        provider.synchronize.assert_awaited_once_with(self.source, "handle")
        signal.assert_awaited_once()
        synchronization = ledger.show("fc-work").fc["delivery"]["synchronization"]
        self.assertEqual(synchronization["state"], "observed")
        self.assertEqual(synchronization["provider_state"], "complete")

    def test_unowned_or_dirty_workspace_is_never_deleted(self):
        tollgate = Mock()
        adapter = TollgateDelivery(tollgate)
        for change in [{"owned": False}, {"dirty": True}]:
            with (
                self.subTest(change=change),
                patch.object(
                    adapter,
                    "_inspect_workspace",
                    return_value=replace(self.clean, **change),
                ),
                self.assertRaises(DeliveryProviderError),
            ):
                adapter._cleanup(self.ref)
        tollgate.remove_worktree.assert_not_called()

    def test_terminal_delivery_can_finish_after_provider_cleanup(self):
        delivery = {
            **self.delivery,
            "promotion": {
                "state": "observed",
                "integration_oid": "integration",
            },
            "synchronization": {
                "state": "observed",
                "integration_oid": "integration",
            },
            "cleanup": {"state": "observed"},
        }
        self.assertEqual(
            _settled_delivery_for_source(record(delivery=delivery), "current-source"),
            delivery,
        )
        self.assertIsNone(
            _settled_delivery_for_source(record(delivery=delivery), "stale-source")
        )
