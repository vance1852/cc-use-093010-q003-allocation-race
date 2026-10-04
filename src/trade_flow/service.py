"""服务额度单价、服务额度库存、合作通道和额度申请的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import IndexQuote, Facility, InventoryLot, NominationRequest, Route, SupplyScenario
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write"},
    "risk": {"outage.write", "scenario.approve", "report.read"},
    "auditor": {"report.read", "audit.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("服务额度单价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准服务额度单价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_quota_units,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_quota_units),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("合作通道编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("合作通道不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_quota_units,available_quota_units,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_quota_units),
                        decimal_text(lot.quantity_quota_units),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("服务资源批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("服务资源批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同额度申请内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("合作通道当前不可额度申请")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_quota_units,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_quota_units),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("额度申请编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("合作通道不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配额度申请")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_quota_units"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_quota_units"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_quota_units=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_quota_units"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def _transfer_conflict(
        self,
        reason: str,
        message: str,
        decision: sqlite3.Row | None,
        nomination: sqlite3.Row,
        lot: sqlite3.Row | None,
        transfer_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> Conflict:
        """构造携带当前版本与差异依据的业务冲突，绝不透传存储层信息。"""
        current: dict[str, Any] = {
            "nomination_id": nomination["nomination_id"],
            "nomination_state": nomination["state"],
            "nomination_revision": nomination["revision"],
        }
        if decision is not None:
            current.update(
                {
                    "transfer_id": decision["transfer_id"],
                    "inventory_lot_id": decision["inventory_lot_id"],
                    "decided_at": decision["decided_at"],
                }
            )
        if lot is not None:
            current["lot_revision"] = lot["revision"]
        return Conflict(
            message,
            details={
                "reason": reason,
                "current": current,
                "submitted": {
                    "transfer_id": transfer_id,
                    "inventory_lot_id": lot_id,
                    "expected_revision": expected_revision,
                },
            },
        )

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        request_digest = digest(
            {
                "transfer_id": transfer_id,
                "nomination_id": nomination_id,
                "inventory_lot_id": lot_id,
                "expected_revision": expected_revision,
            }
        )
        # 全部核对、裁决与写入都在同一个立即写事务内完成：持锁后重新读取的
        # 申请状态、资源批次和版本即裁决依据，并发请求在此串行化，不会再
        # 出现“一条成功、一条撞上唯一约束”的存储层竞争。
        try:
            with transaction(self.connection, immediate=True):
                nomination = self.connection.execute(
                    "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id,r.product "
                    "FROM nominations n JOIN routes r ON r.route_id=n.route_id "
                    "WHERE n.nomination_id=?",
                    (nomination_id,),
                ).fetchone()
                if nomination is None:
                    raise NotFound("额度申请不存在")
                lot = self.connection.execute(
                    "SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)
                ).fetchone()
                if lot is None:
                    raise NotFound("服务资源批次不存在")

                decision = self.connection.execute(
                    "SELECT * FROM transfer_decisions WHERE nomination_id=?",
                    (nomination_id,),
                ).fetchone()
                if decision is not None:
                    # 已有首次成功裁决：内容一致的重放（含重启后）直接返回
                    # 首次结果，不扣减额度、不写第二条发放、不追加审计事件；
                    # 不同发放编号或依据的请求得到当前版本与明确冲突。
                    if decision["request_sha256"] == request_digest:
                        return json.loads(decision["response_json"])
                    mismatches = []
                    if decision["transfer_id"] != transfer_id:
                        mismatches.append("transfer_id")
                    if decision["inventory_lot_id"] != lot_id:
                        mismatches.append("inventory_lot_id")
                    if decision["expected_revision"] != expected_revision:
                        mismatches.append("expected_revision")
                    raise Conflict(
                        "该申请已有不同发放编号或依据的首次裁决，不能重复发放",
                        details={
                            "reason": "decision_payload_conflict",
                            "conflicting_fields": mismatches,
                            "current": {
                                "nomination_id": nomination_id,
                                "nomination_state": nomination["state"],
                                "nomination_revision": nomination["revision"],
                                "transfer_id": decision["transfer_id"],
                                "inventory_lot_id": decision["inventory_lot_id"],
                                "lot_revision": lot["revision"],
                                "decided_by": decision["decided_by"],
                                "decided_at": decision["decided_at"],
                            },
                            "submitted": {
                                "transfer_id": transfer_id,
                                "inventory_lot_id": lot_id,
                                "expected_revision": expected_revision,
                            },
                        },
                    )

                # 首次裁决：在写锁内复核申请状态、版本、资源批次与库存。
                if nomination["state"] != "allocated":
                    raise InvalidState(f"额度申请当前状态为 {nomination['state']}，不可发放服务额度")
                if nomination["revision"] != expected_revision:
                    raise self._transfer_conflict(
                        "nomination_revision_stale",
                        "发放依据的申请版本已过期，请按当前版本重新确认",
                        None,
                        nomination,
                        lot,
                        transfer_id,
                        lot_id,
                        expected_revision,
                    )
                if lot["facility_id"] != nomination["origin_id"] or lot["product"] != nomination["product"]:
                    raise Conflict(
                        "服务资源批次与合作通道起点或资源类型不匹配",
                        details={
                            "reason": "lot_mismatch",
                            "current": {"lot_revision": lot["revision"]},
                            "submitted": {"inventory_lot_id": lot_id},
                        },
                    )
                allocated = Decimal(nomination["allocated_quota_units"])
                available = Decimal(lot["available_quota_units"])
                if available < allocated:
                    raise Conflict(
                        "服务额度库存不足以完成分配",
                        details={
                            "reason": "insufficient_inventory",
                            "current": {
                                "available_quota_units": decimal_text(available),
                                "lot_revision": lot["revision"],
                            },
                            "required_quota_units": decimal_text(allocated),
                        },
                    )

                expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
                departed_at = self._now()
                loaded_text = decimal_text(allocated)
                expected_delivery_text = decimal_text(expected_delivery)
                response = {
                    "transfer_id": transfer_id,
                    "state": "in_transit",
                    "loaded_quota_units": loaded_text,
                    "expected_delivered_quota_units": expected_delivery_text,
                    "expected_arrival": utc_text(
                        parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))
                    ),
                }

                lot_cursor = self.connection.execute(
                    "UPDATE inventory_lots SET available_quota_units=?,revision=revision+1 "
                    "WHERE lot_id=? AND revision=?",
                    (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
                )
                if lot_cursor.rowcount != 1:
                    raise self._transfer_conflict(
                        "lot_revision_stale",
                        "资源批次版本已变化，请读取当前批次余量后重试",
                        None,
                        nomination,
                        lot,
                        transfer_id,
                        lot_id,
                        expected_revision,
                    )
                nomination_cursor = self.connection.execute(
                    "UPDATE nominations SET state='in_transit',revision=revision+1 "
                    "WHERE nomination_id=? AND state='allocated' AND revision=?",
                    (nomination_id, expected_revision),
                )
                if nomination_cursor.rowcount != 1:
                    raise InvalidState("额度申请状态或版本已变化，发放未执行")
                self.connection.execute(
                    "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_quota_units,"
                    "expected_delivered_quota_units,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        transfer_id,
                        nomination_id,
                        lot_id,
                        loaded_text,
                        expected_delivery_text,
                        departed_at,
                        actor_id,
                        departed_at,
                    ),
                )
                # 裁决事实与请求指纹持久化：这是重启后仍能按原请求找回
                # 首次结果、并区分内容冲突的唯一依据。
                self.connection.execute(
                    "INSERT INTO transfer_decisions(nomination_id,transfer_id,inventory_lot_id,"
                    "request_sha256,expected_revision,response_json,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        nomination_id,
                        transfer_id,
                        lot_id,
                        request_digest,
                        expected_revision,
                        canonical_json(response),
                        actor_id,
                        departed_at,
                    ),
                )
                self._audit(
                    "transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id}
                )
                return response
        except sqlite3.IntegrityError as exc:
            # 兜底：事务已回滚，此处依据已提交事实再次裁决，确保任何写入
            # 竞争都转译为稳定业务结果，存储异常绝不外泄。
            existing = self.connection.execute(
                "SELECT request_sha256,response_json FROM transfer_decisions WHERE nomination_id=?",
                (nomination_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] == request_digest:
                    return json.loads(existing["response_json"])
                raise Conflict(
                    "该申请已有不同发放编号或依据的首次裁决，不能重复发放",
                    details={"reason": "decision_payload_conflict", "nomination_id": nomination_id},
                ) from exc
            other = self.connection.execute(
                "SELECT nomination_id FROM transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            if other is not None and other["nomination_id"] != nomination_id:
                raise Conflict(
                    "发放编号已用于另一份申请，不能重复使用",
                    details={
                        "reason": "transfer_id_taken",
                        "transfer_id": transfer_id,
                        "nomination_id": other["nomination_id"],
                    },
                ) from exc
            raise Conflict(
                "发放裁决写入冲突，请使用完全相同的请求重试以获取首次结果",
                details={"reason": "decision_write_conflict", "nomination_id": nomination_id},
            ) from exc

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用服务额度单价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_quota_units AS REAL)) available_quota_units "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
