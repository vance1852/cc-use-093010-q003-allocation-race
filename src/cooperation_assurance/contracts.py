"""校准协议和测点记录的严格数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


def _decimal(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationError(f"{path} 必须是有限数值")
    return result


@dataclass(frozen=True, slots=True)
class Stratum:
    """一个需要单独覆盖的校准环境分层。"""

    key: str
    label: str
    required_trials: int

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Stratum":
        data = _require_mapping(raw, path)
        required_trials = data.get("required_trials")
        if isinstance(required_trials, bool) or not isinstance(required_trials, int):
            raise ValidationError(f"{path}.required_trials 必须是整数")
        if required_trials <= 0:
            raise ValidationError(f"{path}.required_trials 必须大于零")
        return cls(
            key=_required_text(data.get("key"), f"{path}.key"),
            label=_required_text(data.get("label"), f"{path}.label"),
            required_trials=required_trials,
        )


@dataclass(frozen=True, slots=True)
class Metric:
    """协议中声明的一个可测点指标。"""

    key: str
    label: str
    kind: str
    unit: str | None
    direction: str

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Metric":
        data = _require_mapping(raw, path)
        kind = _required_text(data.get("kind"), f"{path}.kind")
        if kind not in {"binary", "continuous", "count"}:
            raise ValidationError(f"{path}.kind 不受支持")
        direction = _required_text(data.get("direction"), f"{path}.direction")
        if direction not in {"higher", "lower"}:
            raise ValidationError(f"{path}.direction 必须是 higher 或 lower")
        unit = _optional_text(data.get("unit"), f"{path}.unit")
        if kind == "binary" and unit is not None:
            raise ValidationError(f"{path}.unit 对二元指标必须为空")
        return cls(
            key=_required_text(data.get("key"), f"{path}.key"),
            label=_required_text(data.get("label"), f"{path}.label"),
            kind=kind,
            unit=unit,
            direction=direction,
        )


@dataclass(frozen=True, slots=True)
class Protocol:
    """一次校准所依据的不可歧义协议版本。"""

    protocol_id: str
    version: int
    title: str
    task_family: str
    strata: tuple[Stratum, ...]
    metrics: tuple[Metric, ...]
    stratum_weights: Mapping[str, Decimal]
    seed: int
    bootstrap_samples: int
    admission_rules: tuple[Mapping[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: object) -> "Protocol":
        data = _require_mapping(raw, "protocol")
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationError("protocol.version 必须是正整数")
        strata = tuple(
            Stratum.from_dict(item, f"protocol.strata[{index}]")
            for index, item in enumerate(_require_sequence(data.get("strata"), "protocol.strata"))
        )
        metrics = tuple(
            Metric.from_dict(item, f"protocol.metrics[{index}]")
            for index, item in enumerate(_require_sequence(data.get("metrics"), "protocol.metrics"))
        )
        if not strata:
            raise ValidationError("protocol.strata 不能为空")
        if not metrics:
            raise ValidationError("protocol.metrics 不能为空")
        if len({item.key for item in strata}) != len(strata):
            raise ValidationError("protocol.strata.key 不能重复")
        if len({item.key for item in metrics}) != len(metrics):
            raise ValidationError("protocol.metrics.key 不能重复")
        raw_weights = _require_mapping(data.get("stratum_weights"), "protocol.stratum_weights")
        if set(raw_weights) != {item.key for item in strata}:
            raise ValidationError("protocol.stratum_weights 必须覆盖全部且仅覆盖已声明分层")
        weights = {
            key: _decimal(value, f"protocol.stratum_weights.{key}")
            for key, value in raw_weights.items()
        }
        if any(value <= 0 for value in weights.values()):
            raise ValidationError("protocol.stratum_weights 必须全部大于零")
        if sum(weights.values(), Decimal(0)) != Decimal(1):
            raise ValidationError("protocol.stratum_weights 之和必须为 1")
        seed = data.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValidationError("protocol.seed 必须是整数")
        bootstrap_samples = data.get("bootstrap_samples")
        if (
            isinstance(bootstrap_samples, bool)
            or not isinstance(bootstrap_samples, int)
            or bootstrap_samples < 100
            or bootstrap_samples > 100000
        ):
            raise ValidationError("protocol.bootstrap_samples 必须在 100 到 100000 之间")
        rules = tuple(
            _require_mapping(item, f"protocol.admission_rules[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("admission_rules"), "protocol.admission_rules")
            )
        )
        if not rules:
            raise ValidationError("protocol.admission_rules 不能为空")
        for index, rule in enumerate(rules):
            metric = _required_text(rule.get("metric"), f"protocol.admission_rules[{index}].metric")
            if metric not in {item.key for item in metrics}:
                raise ValidationError(f"protocol.admission_rules[{index}].metric 未声明")
            operator = _required_text(rule.get("operator"), f"protocol.admission_rules[{index}].operator")
            if operator not in {"gte", "lte"}:
                raise ValidationError(f"protocol.admission_rules[{index}].operator 不受支持")
            _decimal(rule.get("threshold"), f"protocol.admission_rules[{index}].threshold")
        return cls(
            protocol_id=_required_text(data.get("protocol_id"), "protocol.protocol_id"),
            version=version,
            title=_required_text(data.get("title"), "protocol.title"),
            task_family=_required_text(data.get("task_family"), "protocol.task_family"),
            strata=strata,
            metrics=metrics,
            stratum_weights=weights,
            seed=seed,
            bootstrap_samples=bootstrap_samples,
            admission_rules=rules,
        )

    @property
    def metric_map(self) -> dict[str, Metric]:
        return {metric.key: metric for metric in self.metrics}

    @property
    def stratum_keys(self) -> frozenset[str]:
        return frozenset(item.key for item in self.strata)


@dataclass(frozen=True, slots=True)
class Observation:
    """一次已结构化的跨境合作项目任务测点。"""

    source_batch: str
    source_row: str
    program_id: str
    protocol_id: str
    protocol_version: int
    stratum_key: str
    observed_at: str
    metrics: Mapping[str, Decimal]
    excluded_reason: str | None

    @classmethod
    def from_dict(cls, raw: object, protocol: Protocol) -> "Observation":
        data = _require_mapping(raw, "observation")
        protocol_id = _required_text(data.get("protocol_id"), "observation.protocol_id")
        protocol_version = data.get("protocol_version")
        if protocol_id != protocol.protocol_id or protocol_version != protocol.version:
            raise ValidationError("测点引用的协议版本与当前协议不一致")
        stratum_key = _required_text(data.get("stratum_key"), "observation.stratum_key")
        if stratum_key not in protocol.stratum_keys:
            raise ValidationError("observation.stratum_key 未在协议中声明")
        metric_data = _require_mapping(data.get("metrics"), "observation.metrics")
        expected = protocol.metric_map
        missing = sorted(set(expected) - set(metric_data))
        extra = sorted(set(metric_data) - set(expected))
        if missing or extra:
            raise ValidationError(f"测点指标不匹配：缺少 {missing}，多出 {extra}")
        parsed: dict[str, Decimal] = {}
        for key, value in metric_data.items():
            metric = expected[key]
            number = _decimal(value, f"observation.metrics.{key}")
            if metric.kind == "binary" and number not in {Decimal(0), Decimal(1)}:
                raise ValidationError(f"observation.metrics.{key} 必须是 0 或 1")
            if metric.kind == "count" and number != number.to_integral_value():
                raise ValidationError(f"observation.metrics.{key} 必须是整数")
            parsed[key] = number
        return cls(
            source_batch=_required_text(data.get("source_batch"), "observation.source_batch"),
            source_row=_required_text(data.get("source_row"), "observation.source_row"),
            program_id=_required_text(data.get("program_id"), "observation.program_id"),
            protocol_id=protocol_id,
            protocol_version=protocol.version,
            stratum_key=stratum_key,
            observed_at=_required_text(data.get("observed_at"), "observation.observed_at"),
            metrics=parsed,
            excluded_reason=_optional_text(data.get("excluded_reason"), "observation.excluded_reason"),
        )
