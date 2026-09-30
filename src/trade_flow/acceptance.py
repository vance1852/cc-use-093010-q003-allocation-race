"""贯通服务额度单价、合作通道、服务额度库存、额度申请和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "cluster-a", "name": "北部数贸运营节点", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_quota_units": "500000"})
    service.create_facility("plan", {"facility_id": "pool-b", "name": "东部推理池", "kind": "inference-pool", "timezone": "Asia/Shanghai", "capacity_quota_units": "800000"})
    service.create_route("plan", {"route_id": "fabric-a-b", "origin_id": "cluster-a", "destination_id": "pool-b", "product": "cross-border-data", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "cluster-a", "product": "cross-border-data", "grade": "PEAK_VALLEY", "quantity_quota_units": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "fabric-a-b", "shipper_id": "tenant-east", "service_date": "2026-09-25", "requested_quota_units": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "fabric-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "fabric-recovery", "name": "关键服务节点检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"fabric-a-b": "20"}, "demand_changes": {"cluster-a:cross-border-data": "-5"}})
    service.approve_scenario("risk", "fabric-recovery", 1)
    scenario = service.run_scenario("plan", "fabric-recovery", "2026-09-23")
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行数贸运营节点调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
