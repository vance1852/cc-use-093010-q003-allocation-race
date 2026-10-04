from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from trade_flow.api import JsonApplication
from trade_flow import service as service_module
from trade_flow import storage as storage_module
from trade_flow.clock import FrozenClock
from trade_flow.errors import Conflict, Forbidden, InvalidState, SupplyError
from trade_flow.planning import AllocationRequest, PricePoint, allocate_capacity, latest_streak
from trade_flow.service import SupplyService
from trade_flow.risk import DemandBucket, inventory_coverage, mark_to_market, supply_gap


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_uses_first_close_as_base(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("108")),
            PricePoint("2026-09-19", Decimal("105")),
            PricePoint("2026-09-20", Decimal("102")),
            PricePoint("2026-09-21", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 4)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["nomination_id"], "first")
        self.assertEqual(rows[0]["allocated_quota_units"], "70.000")
        self.assertEqual(rows[1]["allocated_quota_units"], "30.000")

    def test_inventory_coverage_and_supply_gap(self) -> None:
        coverage = inventory_coverage(
            [{"facility_id": "inference-pool", "product": "platform-capacity", "available_quota_units": "250"}],
            [DemandBucket("inference-pool", "platform-capacity", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = supply_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["supply_gap"], "30.000")

    def test_mark_to_market_groups_deterministically(self) -> None:
        result = mark_to_market(
            [{"position_id": "p1", "market_index": "PEAK_VALLEY", "quantity_quota_units": "100", "entry_price_cny": "105"}],
            {"PEAK_VALLEY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class SupplyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部数贸运营节点", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_quota_units": "500000"})
        self.service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_quota_units": "800000"})
        self.service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "cross-border-data", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def quote(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{day}", "close_cny": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_quote_revisions_preserve_history(self) -> None:
        first = self.quote(23, "98")
        second = self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-23", "close_cny": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["quote_id"], second["quote_id"])
        rows = self.connection.execute("SELECT * FROM market_index_quotes ORDER BY quote_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_quote_id"], rows[0]["quote_id"])

    def test_nomination_replay_and_payload_conflict(self) -> None:
        payload = {"nomination_id": "nom-1", "route_id": "fabric-a-b", "shipper_id": "tenant", "service_date": "2026-09-25", "requested_quota_units": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_nomination("dispatch", payload)
        self.assertEqual(first, self.service.submit_nomination("dispatch", payload))
        changed = dict(payload, requested_quota_units="81000")
        with self.assertRaises(Conflict):
            self.service.submit_nomination("dispatch", changed)

    def test_outage_reduces_allocation_and_transfer_consumes_inventory(self) -> None:
        self.service.announce_outage("risk", "fabric-a-b", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "fabric-a-b", "shipper_id": f"shipper-{number}", "service_date": "2026-09-25", "requested_quota_units": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "fabric-a-b", "2026-09-25")
        self.assertEqual(allocation["available_capacity"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_quota_units"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "cross-border-data", "grade": "PEAK_VALLEY", "quantity_quota_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        transfer = self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
        self.assertEqual(transfer["loaded_quota_units"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_quota_units"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.quote(23, "98")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "cross-border-data", "grade": "PEAK_VALLEY", "quantity_quota_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "服务节点检修恢复", "market_index_drop_percent": "9", "route_capacity_changes": {"fabric-a-b": "20"}, "demand_changes": {"cluster-a:cross-border-data": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE supply_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/quotes/summary/PEAK_VALLEY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


class DispatchAdjudicationTests(unittest.TestCase):
    """并发确认同一份已获批申请时，必须得到稳定可解释的业务裁决。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "dispatch.sqlite3")
        self.service = self._service()
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部节点", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_quota_units": "500000"})
        self.service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_quota_units": "800000"})
        self.service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "cross-border-data", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "cross-border-data", "grade": "A", "quantity_quota_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.submit_nomination("dispatch", {"nomination_id": "nom-1", "route_id": "fabric-a-b", "shipper_id": "tenant", "service_date": "2026-09-25", "requested_quota_units": "40000", "priority": 10, "idempotency_key": "key-1"})
        self.service.allocate("dispatch", "fabric-a-b", "2026-09-25")
        self.service.connection.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _service(self) -> SupplyService:
        connection = storage_module.connect(self.db_path)
        return SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))

    def _dispatch_concurrently(self, seats):
        barrier = threading.Barrier(len(seats))
        original = service_module.transaction
        results: dict[str, object] = {}

        @contextlib.contextmanager
        def synchronized(connection, *, immediate=False):
            barrier.wait(timeout=10)
            with original(connection, immediate=immediate):
                yield

        def worker(seat: str, kwargs: dict) -> None:
            service = self._service()
            try:
                results[seat] = service.dispatch_transfer("dispatch", **kwargs)
            except BaseException as exc:
                results[seat] = exc
            finally:
                service.connection.close()

        service_module.transaction = synchronized
        try:
            threads = [threading.Thread(target=worker, args=item) for item in seats.items()]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            service_module.transaction = original
        return results

    def _assert_single_fact(self) -> None:
        connection = storage_module.connect(self.db_path)
        try:
            transfers = connection.execute("SELECT transfer_id, nomination_id FROM transfers").fetchall()
            decisions = connection.execute("SELECT transfer_id, nomination_id FROM transfer_decisions").fetchall()
            lot = connection.execute("SELECT available_quota_units, revision FROM inventory_lots WHERE lot_id='lot-1'").fetchone()
            nomination = connection.execute("SELECT state, revision FROM nominations WHERE nomination_id='nom-1'").fetchone()
            audits = connection.execute(
                "SELECT count(*) FROM supply_audit_events WHERE event_type='transfer.dispatched'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual([tuple(row) for row in transfers], [("transfer-1", "nom-1")])
        self.assertEqual([tuple(row) for row in decisions], [("transfer-1", "nom-1")])
        self.assertEqual((lot["available_quota_units"], lot["revision"]), ("20000.000", 2))
        self.assertEqual((nomination["state"], nomination["revision"]), ("in_transit", 3))
        self.assertEqual(audits, 1)

    def request(self, **overrides) -> dict:
        params = {
            "transfer_id": "transfer-1",
            "nomination_id": "nom-1",
            "lot_id": "lot-1",
            "expected_revision": 2,
        }
        params.update(overrides)
        return params

    def test_identical_concurrent_confirms_share_one_fact(self) -> None:
        for round_no in range(20):
            with self.subTest(round=round_no):
                seats = {"A": self.request(), "B": self.request()}
                results = self._dispatch_concurrently(seats)
                for result in results.values():
                    self.assertNotIsInstance(result, BaseException)
                self.assertEqual(results["A"], results["B"])
                self.assertEqual(results["A"]["transfer_id"], "transfer-1")
                self.assertEqual(results["A"]["loaded_quota_units"], "40000.000")
        self._assert_single_fact()

    def test_loser_gets_business_conflict_not_storage_error(self) -> None:
        # 席位 A 先完成首次裁决；席位 B 随后用不同发放编号并发重试。
        first = self._service()
        try:
            winner = first.dispatch_transfer("dispatch", **self.request())
        finally:
            first.connection.close()
        seats = {"B": self.request(transfer_id="transfer-2")}
        results = self._dispatch_concurrently(seats)
        loser = results["B"]
        self.assertIsInstance(loser, Conflict)
        self.assertNotIsInstance(loser, sqlite3.IntegrityError)
        self.assertIsInstance(loser, SupplyError)
        self.assertEqual(loser.details["reason"], "decision_payload_conflict")
        self.assertIn("transfer_id", loser.details["conflicting_fields"])
        self.assertEqual(loser.details["current"]["transfer_id"], "transfer-1")
        self.assertEqual(loser.details["current"]["nomination_revision"], 3)
        self.assertEqual(loser.details["submitted"]["transfer_id"], "transfer-2")
        self.assertEqual(winner["transfer_id"], "transfer-1")
        self._assert_single_fact()

    def test_stale_expected_revision_is_explained_with_current_version(self) -> None:
        # 先由席位 A 完成首次发放，B 使用过期依据（旧版本）再确认。
        first = self._service()
        try:
            response = first.dispatch_transfer("dispatch", **self.request())
        finally:
            first.connection.close()
        service = self._service()
        try:
            with self.assertRaises(Conflict) as caught:
                service.dispatch_transfer("dispatch", **self.request(transfer_id="transfer-2", expected_revision=2))
        finally:
            service.connection.close()
        conflict = caught.exception
        self.assertEqual(conflict.details["reason"], "decision_payload_conflict")
        self.assertEqual(conflict.details["current"]["nomination_state"], "in_transit")
        self.assertEqual(conflict.details["current"]["nomination_revision"], 3)
        self.assertEqual(response["state"], "in_transit")
        self._assert_single_fact()

    def test_identical_request_replays_first_result_after_restart(self) -> None:
        before = self._service()
        try:
            first = before.dispatch_transfer("dispatch", **self.request())
        finally:
            before.connection.close()
        restarted = self._service()
        try:
            replayed = restarted.dispatch_transfer("dispatch", **self.request())
        finally:
            restarted.connection.close()
        self.assertEqual(first, replayed)
        self._assert_single_fact()

    def test_replay_never_deducts_inventory_or_writes_audit_again(self) -> None:
        first = self._service()
        try:
            first.dispatch_transfer("dispatch", **self.request())
        finally:
            first.connection.close()
        for _ in range(5):
            again = self._service()
            try:
                again.dispatch_transfer("dispatch", **self.request())
            finally:
                again.connection.close()
        self._assert_single_fact()

    def test_no_storage_exception_escapes_when_lot_is_insufficient(self) -> None:
        # 用一份 40000 申请之外的小额批次耗尽场景：库存不足必须是业务冲突。
        service = self._service()
        try:
            service.add_inventory_lot("dispatch", {"lot_id": "lot-2", "facility_id": "cluster-a", "product": "cross-border-data", "grade": "A", "quantity_quota_units": "10", "unit_cost_cny": "91", "received_at": "2026-09-24T07:00:00Z"})
            with self.assertRaises(Conflict) as caught:
                service.dispatch_transfer("dispatch", **self.request(transfer_id="transfer-x", lot_id="lot-2"))
        finally:
            service.connection.close()
        self.assertEqual(caught.exception.details["reason"], "insufficient_inventory")
        self.assertEqual(caught.exception.details["current"]["available_quota_units"], "10")

    def test_unknown_application_and_lot_are_business_not_found(self) -> None:
        service = self._service()
        try:
            with self.assertRaises(SupplyError):
                service.dispatch_transfer("dispatch", **self.request(nomination_id="missing"))
        finally:
            service.connection.close()


class DispatchHttpAdjudicationTests(unittest.TestCase):
    """真实线程化 HTTP 服务上两个运营席位并发确认同一申请。"""

    def setUp(self) -> None:
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        from trade_flow.api import JsonApplication, make_handler

        self.urllib = urllib.request
        self.urllib_error = urllib.error
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "http.sqlite3")
        connection = storage_module.connect(self.db_path)
        service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            service.create_user(user_id, user_id, role)
        service.create_facility("plan", {"facility_id": "cluster-a", "name": "节点", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_quota_units": "500000"})
        service.create_facility("plan", {"facility_id": "pool-b", "name": "池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_quota_units": "800000"})
        service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "cross-border-data", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
        service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "cluster-a", "product": "cross-border-data", "grade": "A", "quantity_quota_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        service.submit_nomination("dispatch", {"nomination_id": "nom-1", "route_id": "fabric-a-b", "shipper_id": "tenant", "service_date": "2026-09-25", "requested_quota_units": "40000", "priority": 10, "idempotency_key": "key-1"})
        service.allocate("dispatch", "fabric-a-b", "2026-09-25")
        connection.close()

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(JsonApplication(database=self.db_path))
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _post(self, payload: dict) -> tuple[int, dict]:
        request = self.urllib.Request(
            f"http://127.0.0.1:{self.port}/transfers",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Actor-Id": "dispatch"},
            method="POST",
        )
        try:
            with self.urllib.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except self.urllib_error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_two_seats_see_one_fact_and_conflict_is_explainable(self) -> None:
        base = {"transfer_id": "transfer-1", "nomination_id": "nom-1", "lot_id": "lot-1", "expected_revision": 2}
        results: dict[str, tuple[int, dict]] = {}

        def seat(name: str) -> None:
            results[name] = self._post(base)

        threads = [threading.Thread(target=seat, args=(name,)) for name in ("A", "B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual({status for status, _ in results.values()}, {201})
        self.assertEqual(results["A"][1], results["B"][1])
        self.assertEqual(results["A"][1]["transfer_id"], "transfer-1")

        status, body = self._post({**base, "transfer_id": "transfer-OTHER"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "conflict")
        self.assertEqual(body["error"]["details"]["reason"], "decision_payload_conflict")
        self.assertEqual(body["error"]["details"]["current"]["nomination_revision"], 3)
        self.assertNotIn("sqlite", json.dumps(body, ensure_ascii=False).lower())


if __name__ == "__main__":
    unittest.main()
