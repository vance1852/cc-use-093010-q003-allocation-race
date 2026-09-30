"""不依赖第三方库的基础描述性统计。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class NumericSummary:
    count: int
    minimum: Decimal
    maximum: Decimal
    mean: Decimal
    median: Decimal
    sample_variance: Decimal | None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "count": self.count,
            "minimum": format(self.minimum, "f"),
            "maximum": format(self.maximum, "f"),
            "mean": format(self.mean, "f"),
            "median": format(self.median, "f"),
            "sample_variance": (
                None if self.sample_variance is None else format(self.sample_variance, "f")
            ),
        }


@dataclass(frozen=True, slots=True)
class WilsonInterval:
    successes: int
    trials: int
    lower: float
    upper: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "successes": self.successes,
            "trials": self.trials,
            "lower": round(self.lower, 12),
            "upper": round(self.upper, 12),
        }


def summarize(values: Iterable[Decimal | int | str]) -> NumericSummary:
    """计算有限数值的稳定摘要，方差使用 n-1 分母。"""

    data = sorted(Decimal(str(value)) for value in values)
    if not data:
        raise ValueError("至少需要一个数值")
    if any(not item.is_finite() for item in data):
        raise ValueError("数值必须有限")
    count = len(data)
    total = sum(data, Decimal(0))
    mean = total / count
    midpoint = count // 2
    median = (
        data[midpoint]
        if count % 2
        else (data[midpoint - 1] + data[midpoint]) / Decimal(2)
    )
    variance = None
    if count > 1:
        squared = sum(((item - mean) ** 2 for item in data), Decimal(0))
        variance = squared / Decimal(count - 1)
    return NumericSummary(
        count=count,
        minimum=data[0],
        maximum=data[-1],
        mean=mean,
        median=median,
        sample_variance=variance,
    )


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> WilsonInterval:
    """计算二项比例的双侧 Wilson 区间。"""

    if isinstance(successes, bool) or isinstance(trials, bool):
        raise ValueError("计数必须是整数")
    if not isinstance(successes, int) or not isinstance(trials, int):
        raise ValueError("计数必须是整数")
    if trials <= 0 or successes < 0 or successes > trials:
        raise ValueError("成功数与校准数不合法")
    if not math.isfinite(z) or z <= 0:
        raise ValueError("z 必须是有限正数")
    proportion = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    center = (proportion + z2 / (2.0 * trials)) / denominator
    radius = (
        z
        * math.sqrt((proportion * (1.0 - proportion) + z2 / (4.0 * trials)) / trials)
        / denominator
    )
    return WilsonInterval(
        successes=successes,
        trials=trials,
        lower=max(0.0, center - radius),
        upper=min(1.0, center + radius),
    )


def group_metric(
    rows: Sequence[object],
    metric_name: str,
    *,
    include_excluded: bool = False,
) -> dict[str, NumericSummary]:
    """按测点分层汇总一个指标，不推断业务分析准入结论。"""

    grouped: dict[str, list[Decimal]] = {}
    for row in rows:
        excluded_reason = getattr(row, "excluded_reason")
        if excluded_reason is not None and not include_excluded:
            continue
        metrics = getattr(row, "metrics")
        if metric_name not in metrics:
            raise KeyError(metric_name)
        grouped.setdefault(getattr(row, "stratum_key"), []).append(metrics[metric_name])
    return {key: summarize(values) for key, values in sorted(grouped.items())}

