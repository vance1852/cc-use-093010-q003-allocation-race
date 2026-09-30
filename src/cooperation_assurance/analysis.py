"""按预注册协议执行确定性统计分析。"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Iterable, Mapping

from .contracts import Metric, Observation, Protocol
from .numeric import summarize, wilson_interval


ALGORITHM_VERSION = "cooperation-assurance-analysis/1"


def _quantile(values: list[Decimal], probability: Decimal) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("分位数输入不能为空")
    position = probability * Decimal(len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return ordered[lower] * (Decimal(1) - fraction) + ordered[upper] * fraction


def bootstrap_mean_interval(
    values: Iterable[Decimal], *, seed: int, samples: int
) -> tuple[Decimal, Decimal]:
    data = tuple(values)
    if not data:
        raise ValueError("bootstrap 至少需要一个样本")
    generator = random.Random(seed)
    means: list[Decimal] = []
    for _ in range(samples):
        total = sum((data[generator.randrange(len(data))] for _ in data), Decimal(0))
        means.append(total / Decimal(len(data)))
    return _quantile(means, Decimal("0.025")), _quantile(means, Decimal("0.975"))


def _metric_result(metric: Metric, values: list[Decimal], seed: int, samples: int) -> dict[str, object]:
    summary = summarize(values)
    result: dict[str, object] = summary.as_dict()
    if metric.kind == "binary":
        successes = sum(int(value) for value in values)
        interval = wilson_interval(successes, len(values))
        result.update({
            "successes": successes,
            "proportion": successes / len(values),
            "wilson_lower": interval.lower,
            "wilson_upper": interval.upper,
        })
    else:
        lower, upper = bootstrap_mean_interval(values, seed=seed, samples=samples)
        result.update({
            "bootstrap_lower": format(lower, "f"),
            "bootstrap_upper": format(upper, "f"),
        })
    return result


def analyze(protocol: Protocol, observations: Iterable[Observation]) -> dict[str, object]:
    all_observations = tuple(observations)
    included = tuple(item for item in all_observations if item.excluded_reason is None)
    strata: dict[str, dict[str, object]] = {}
    insufficient: list[dict[str, object]] = []
    for stratum_index, stratum in enumerate(protocol.strata):
        rows = [item for item in included if item.stratum_key == stratum.key]
        coverage = {"actual": len(rows), "required": stratum.required_trials, "complete": len(rows) >= stratum.required_trials}
        if not coverage["complete"]:
            insufficient.append({"stratum": stratum.key, **coverage})
        metrics: dict[str, object] = {}
        for metric_index, metric in enumerate(protocol.metrics):
            values = [item.metrics[metric.key] for item in rows]
            if values:
                metrics[metric.key] = _metric_result(
                    metric,
                    values,
                    protocol.seed + stratum_index * 1009 + metric_index,
                    protocol.bootstrap_samples,
                )
        strata[stratum.key] = {"coverage": coverage, "metrics": metrics}

    aggregate: dict[str, object] = {}
    for metric in protocol.metrics:
        available = [
            (stratum.key, strata[stratum.key]["metrics"].get(metric.key))
            for stratum in protocol.strata
        ]
        if any(value is None for _, value in available):
            aggregate[metric.key] = {"available": False, "reason": "至少一个预注册分层无有效样本"}
            continue
        if metric.kind == "binary":
            weighted = sum(
                protocol.stratum_weights[key] * Decimal(str(value["proportion"]))
                for key, value in available
            )
            lower = sum(
                protocol.stratum_weights[key] * Decimal(str(value["wilson_lower"]))
                for key, value in available
            )
            aggregate[metric.key] = {
                "available": True,
                "weighted_mean": format(weighted, "f"),
                "wilson_lower": format(lower, "f"),
            }
        else:
            weighted = sum(
                protocol.stratum_weights[key] * Decimal(str(value["mean"]))
                for key, value in available
            )
            aggregate[metric.key] = {"available": True, "weighted_mean": format(weighted, "f")}

    rule_results: list[dict[str, object]] = []
    for raw_rule in protocol.admission_rules:
        rule = dict(raw_rule)
        metric_key = str(rule["metric"])
        statistic = str(rule.get("statistic", "weighted_mean"))
        metric_result = aggregate.get(metric_key, {})
        raw_value = metric_result.get(statistic) if isinstance(metric_result, Mapping) else None
        threshold = Decimal(str(rule["threshold"]))
        passed = False
        if raw_value is not None:
            value = Decimal(str(raw_value))
            passed = value >= threshold if rule["operator"] == "gte" else value <= threshold
        rule_results.append({
            "metric": metric_key,
            "statistic": statistic,
            "operator": rule["operator"],
            "threshold": format(threshold, "f"),
            "actual": None if raw_value is None else str(raw_value),
            "passed": passed,
        })
    conclusion = "insufficient" if insufficient else ("pass" if all(item["passed"] for item in rule_results) else "fail")
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "seed": protocol.seed,
        "bootstrap_samples": protocol.bootstrap_samples,
        "included_count": len(included),
        "excluded_count": len(all_observations) - len(included),
        "strata": strata,
        "aggregate": aggregate,
        "rules": rule_results,
        "insufficient": insufficient,
        "conclusion": conclusion,
    }
