"""确定性的 JSON 与 JSONL 输入输出。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

from .contracts import Observation, Protocol, ValidationError


class JsonDataError(ValueError):
    """JSON 文件缺失、损坏或不符合契约。"""


def _reject_constant(value: str) -> None:
    raise JsonDataError(f"JSON 不允许非有限数值 {value}")


def load_json(path: Path) -> Any:
    """读取 UTF-8 JSON，并拒绝重复键和非标准常量。"""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise JsonDataError(f"{path} 含重复键 {key}")
            result[key] = value
        return result

    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=pairs_hook,
        )
    except OSError as exc:
        raise JsonDataError(f"无法读取 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise JsonDataError(f"{path} 不是有效 JSON: {exc.msg}") from exc


def iter_jsonl(path: Path) -> Iterator[Any]:
    """逐行读取 JSONL；空行允许存在，但每条记录必须完整占一行。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise JsonDataError(f"无法读取 {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            yield json.loads(
                line,
                parse_float=Decimal,
                parse_constant=_reject_constant,
            )
        except json.JSONDecodeError as exc:
            raise JsonDataError(f"{path}:{line_number} 不是有效 JSON: {exc.msg}") from exc


def load_protocol(path: Path) -> Protocol:
    try:
        return Protocol.from_dict(load_json(path))
    except ValidationError as exc:
        raise JsonDataError(f"协议校验失败: {exc}") from exc


def load_observations(path: Path, protocol: Protocol) -> tuple[Observation, ...]:
    observations: list[Observation] = []
    seen: set[tuple[str, str]] = set()
    for line_number, raw in enumerate(iter_jsonl(path), start=1):
        try:
            item = Observation.from_dict(raw, protocol)
        except ValidationError as exc:
            raise JsonDataError(f"第 {line_number} 条测点校验失败: {exc}") from exc
        identity = (item.source_batch, item.source_row)
        if identity in seen:
            raise JsonDataError(f"测点来源身份重复: {identity[0]}/{identity[1]}")
        seen.add(identity)
        observations.append(item)
    if not observations:
        raise JsonDataError("测点文件不能为空")
    return tuple(observations)


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"不能序列化 {type(value).__name__}")


def canonical_json(value: object) -> str:
    """生成跨平台一致的紧凑 JSON 文本。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
