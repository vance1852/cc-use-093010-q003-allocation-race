"""服务额度库存覆盖、供应缺口和服务额度单价敞口计算。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping


ZERO = Decimal("0")


def _text(value: Decimal, places: str = "0.001") -> str:
    return format(value.quantize(Decimal(places), rounding=ROUND_HALF_UP), "f")


@dataclass(frozen=True, slots=True)
class DemandBucket:
    facility_id: str
    product: str
    daily_demand: Decimal
    protected_reserve: Decimal = ZERO

    @property
    def key(self) -> str:
        return f"{self.facility_id}:{self.product}"


def inventory_coverage(
    inventory: Iterable[Mapping[str, object]],
    demand: Iterable[DemandBucket],
) -> list[dict[str, object]]:
    available_by_key: dict[str, Decimal] = {}
    for row in inventory:
        key = f"{row['facility_id']}:{row['product']}"
        available_by_key[key] = available_by_key.get(key, ZERO) + Decimal(str(row["available_quota_units"]))
    result: list[dict[str, object]] = []
    for bucket in sorted(demand, key=lambda item: item.key):
        if bucket.daily_demand < ZERO or bucket.protected_reserve < ZERO:
            raise ValueError("需求和保护服务额度库存不能为负数")
        available = available_by_key.get(bucket.key, ZERO)
        usable = max(ZERO, available - bucket.protected_reserve)
        days = None if bucket.daily_demand == ZERO else usable / bucket.daily_demand
        result.append({
            "inventory_key": bucket.key,
            "available_quota_units": _text(available),
            "protected_reserve": _text(bucket.protected_reserve),
            "usable_quota_units": _text(usable),
            "daily_demand": _text(bucket.daily_demand),
            "coverage_days": None if days is None else _text(days, "0.01"),
            "below_three_days": False if days is None else days < Decimal("3"),
        })
    return result


def supply_gap(
    *,
    opening_inventory: Decimal,
    confirmed_inbound: Decimal,
    forecast_demand: Decimal,
    protected_reserve: Decimal,
) -> dict[str, object]:
    values = (opening_inventory, confirmed_inbound, forecast_demand, protected_reserve)
    if any(value < ZERO for value in values):
        raise ValueError("供应缺口输入不能为负数")
    projected_closing = opening_inventory + confirmed_inbound - forecast_demand
    gap = max(ZERO, protected_reserve - projected_closing)
    surplus = max(ZERO, projected_closing - protected_reserve)
    return {
        "opening_inventory": _text(opening_inventory),
        "confirmed_inbound": _text(confirmed_inbound),
        "forecast_demand": _text(forecast_demand),
        "projected_closing": _text(projected_closing),
        "protected_reserve": _text(protected_reserve),
        "supply_gap": _text(gap),
        "surplus_after_reserve": _text(surplus),
        "requires_action": gap > ZERO,
    }


def mark_to_market(
    positions: Iterable[Mapping[str, object]],
    market_index_prices: Mapping[str, Decimal],
) -> dict[str, object]:
    rows: list[dict[str, str]] = []
    total_cost = ZERO
    total_market = ZERO
    for position in sorted(positions, key=lambda item: str(item["position_id"])):
        market_index = str(position["market_index"]).upper()
        if market_index not in market_index_prices:
            raise ValueError(f"缺少 {market_index} 基准服务额度单价")
        quantity = Decimal(str(position["quantity_quota_units"]))
        entry = Decimal(str(position["entry_price_cny"]))
        if quantity < ZERO or entry < ZERO:
            raise ValueError("持仓数量和入场服务额度单价不能为负数")
        market = market_index_prices[market_index]
        cost = quantity * entry
        market_value = quantity * market
        pnl = market_value - cost
        total_cost += cost
        total_market += market_value
        rows.append({
            "position_id": str(position["position_id"]),
            "market_index": market_index,
            "quantity_quota_units": _text(quantity),
            "entry_price_cny": _text(entry, "0.01"),
            "market_price_cny": _text(market, "0.01"),
            "unrealized_pnl_cny": _text(pnl, "0.01"),
        })
    return {
        "positions": rows,
        "total_cost_cny": _text(total_cost, "0.01"),
        "total_market_value_cny": _text(total_market, "0.01"),
        "unrealized_pnl_cny": _text(total_market - total_cost, "0.01"),
    }
