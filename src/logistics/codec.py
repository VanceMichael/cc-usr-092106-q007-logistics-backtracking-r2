"""模型与 JSON 之间的序列化（卷宗持久化用）。"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum

from . import models as m

_REGISTRY: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        m.Authorization,
        m.QueryReceipt,
        m.AddressAlias,
        m.WaybillVersion,
        m.WeightObservation,
        m.ScanEvent,
        m.BoxEvent,
        m.VehicleBatch,
        m.SaleSeed,
    )
}


def to_dict(obj) -> dict:
    if not is_dataclass(obj):
        raise TypeError(f"不支持持久化的类型：{type(obj)}")
    out: dict = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        if isinstance(value, Enum):
            value = value.value
        elif isinstance(value, frozenset):
            value = sorted(value)
        elif isinstance(value, tuple):
            value = [_encode(v) for v in value]
        else:
            value = _encode(value)
        out[f.name] = value
    return out


def _encode(value):
    if isinstance(value, m.PartyRef):
        d = to_dict(value)
        d["__party__"] = True
        return d
    if isinstance(value, Enum):
        return value.value
    return value


def from_dict(tag: str, data: dict):
    cls = _REGISTRY[tag]
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = _convert(f.type, data[f.name])
    return cls(**kwargs)


def _convert(annotation, value):
    """按注解字符串把 JSON 值还原成枚举/嵌套模型/元组/冻结集合。"""
    if value is None:
        return None
    ann = annotation.replace("from __future__ import annotations", "")
    if "PartyRef" in ann and isinstance(value, dict):
        return _party_from_dict(value)
    if ann.startswith("PartyRole"):
        return m.PartyRole(value)
    if "DataCategory" in ann and isinstance(value, str):
        return value  # Literal，原样保留
    if "RevisionReason" in ann:
        return value
    if "ScanType" in ann:
        return value
    if ann.startswith("tuple"):
        return tuple(value)
    if ann.startswith("frozenset"):
        return frozenset(value)
    return value


def _party_from_dict(data: dict) -> m.PartyRef:
    data = {k: v for k, v in data.items() if k != "__party__"}
    data["role"] = m.PartyRole(data["role"])
    return m.PartyRef(**data)


def encode_record(obj) -> dict:
    return {"t": type(obj).__name__, "d": to_dict(obj)}


def decode_record(line: dict):
    return from_dict(line["t"], line["d"])
