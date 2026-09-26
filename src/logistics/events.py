"""运单事件流与字段版本留痕。

设计要点：

- 事件只能追加（append-only）。承运方晚到的“更正”不会覆盖原值，
  而是追加一条 ``correction`` 事件，旧值作为一个字段版本永久保留。
- 注销、转寄、拒收、部分签收均是轨迹上的事件，原轨迹不删除、不改写。
- 节点扫描（scan）带承运方、网点代号、扫描类型与时间戳；
  车辆批次（vehicle_batch）记录运单搭乘的车次，跨省由节点省份推导。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

# 运单注册时可录入的字段。
REGISTERED_FIELDS = frozenset(
    {
        "carrier",            # 承运方标识
        "sender_name",        # 面单寄件人（可能是昵称/代号）
        "sender_subject_id",  # 该面单角色登记到的主体 id（非身份认定）
        "sender_contact_id",  # 寄件联系方式 id，指向 contacts
        "sender_address_id",  # 寄件地址代号 id，指向 addresses
        "pickup_node_id",     # 实际揽收网点 id，可能与面单寄件地不一致
        "receiver_name",      # 收药人网络昵称
        "receiver_subject_id",
        "receiver_contact_id",
        "receiver_address_id",
        "item_description",
        "declared_weight_g",  # 申报重量（克）
    }
)

# 生命周期事件：注销 / 拒收 / 签收类，决定运单最终状态。
LIFECYCLE_TERMINAL = frozenset(
    {"cancelled", "rejected", "signed", "partially_signed"}
)

SCAN_KINDS = frozenset(
    {
        "accepted",     # 揽收
        "departed",     # 发出
        "in_transit",   # 运输中
        "arrived",      # 到达
        "out_delivery", # 派送
        "signed",       # 签收
        "failed",       # 派送失败（非拒收）
    }
)

EVENT_TYPES = frozenset(
    {
        "registered",
        "correction",
        "cancelled",
        "forwarded",        # 转寄：携带新运单号，轨迹互相指向
        "rejected",         # 拒收
        "signed",           # 整单签收
        "partially_signed", # 部分签收：signed_piece_ids 记录已签收件
        "scan",
        "vehicle_batch",
        "packed",           # 货物件装入箱/盒
        "unpacked",         # 拆箱（一箱拆成多件）
        "merged",           # 多件在外省再次合并为一批
        "query_receipt",    # 向承运方查询的回执（含晚到更正）
    }
)


class EventError(ValueError):
    """事件流不合法（缺字段、更正未知字段、时间倒流等）。"""


@dataclass(frozen=True)
class Event:
    """一条不可变轨迹事件。"""

    seq: int
    waybill_id: str
    type: str
    at: str  # ISO-8601，统一字符串便于留痕与排序
    payload: dict[str, Any]
    source: str  # 资料来源：承运方代号 / 证据编号
    corrected_fields: dict[str, Any]  # 仅 correction 使用

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "seq": self.seq,
            "waybill_id": self.waybill_id,
            "type": self.type,
            "at": self.at,
            "source": self.source,
        }
        if self.payload:
            data["payload"] = self.payload
        if self.corrected_fields:
            data["fields"] = self.corrected_fields
        return data


def _require_keys(obj: dict[str, Any], keys: Iterable[str], where: str) -> None:
    missing = [k for k in keys if k not in obj]
    if missing:
        raise EventError(f"{where} 缺少字段: {', '.join(missing)}")


def event_from_dict(data: dict[str, Any], *, expected_waybill: str | None = None) -> Event:
    """从案卷 JSON 构造事件并做基本校验。"""

    _require_keys(data, ("seq", "type", "at", "source"), "事件")
    etype = data["type"]
    if etype not in EVENT_TYPES:
        raise EventError(f"未知事件类型: {etype}")
    waybill_id = data.get("waybill_id", expected_waybill)
    if not waybill_id:
        raise EventError("事件缺少 waybill_id")
    if expected_waybill and waybill_id != expected_waybill:
        raise EventError(
            f"事件归属运单 {waybill_id} 与事件流 {expected_waybill} 不一致"
        )
    fields = data.get("fields", {})
    if etype == "correction":
        if not fields:
            raise EventError("correction 事件必须携带 fields")
        unknown = set(fields) - REGISTERED_FIELDS
        if unknown:
            raise EventError(f"更正了不可更正的字段: {sorted(unknown)}")
    elif fields:
        raise EventError(f"{etype} 事件不得携带 fields（更正请使用 correction）")
    return Event(
        seq=int(data["seq"]),
        waybill_id=waybill_id,
        type=etype,
        at=str(data["at"]),
        payload=dict(data.get("payload", {})),
        source=str(data["source"]),
        corrected_fields=dict(fields),
    )


def ordered_events(events: Iterable[Event]) -> list[Event]:
    """按 (时间, 序号) 排序的事件副本。"""

    return sorted(events, key=lambda e: (e.at, e.seq))


def field_versions(events: Iterable[Event]) -> dict[str, list[dict[str, Any]]]:
    """返回每个字段的全部版本（含原始值与每次更正），旧值不删除。

    版本条目形如 ``{"value": ..., "at": ..., "source": ..., "seq": ...}，
    按时间先后排列。
    """

    versions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in ordered_events(events):
        if ev.type == "registered":
            for key, value in ev.payload.items():
                if key in REGISTERED_FIELDS:
                    versions[key].append(
                        {"value": value, "at": ev.at, "source": ev.source, "seq": ev.seq}
                    )
        elif ev.type == "correction":
            for key, value in ev.corrected_fields.items():
                versions[key].append(
                    {"value": value, "at": ev.at, "source": ev.source, "seq": ev.seq}
                )
    return dict(versions)


def latest_fields(events: Iterable[Event]) -> dict[str, Any]:
    """折叠事件流，得到运单各字段的当前值（最新版本）。"""

    return {
        key: versions[-1]["value"]
        for key, versions in field_versions(events).items()
    }


def waybill_status(events: Iterable[Event]) -> dict[str, Any]:
    """折叠整条事件流，给出运单当前状态与关键轨迹指针。

    返回内容包括：

    - ``state``：active / cancelled / forwarded / rejected /
      signed / partially_signed；
    - ``forward_to``：转寄目标运单号（保留转寄关系，原轨迹不变）；
    - ``signed_piece_ids`` / ``unsigned_piece_ids``：部分签收明细；
    - ``piece_ids``：该运单当前承载的货物件 id 列表（装拆箱推导）；
    - ``last_event``：最近一条事件。
    """

    timeline = ordered_events(events)
    if not timeline:
        raise EventError("空事件流无法确定运单状态")

    state = "active"
    forward_to: str | None = None
    piece_ids: list[str] = []
    signed_pieces: set[str] = set()

    for ev in timeline:
        if ev.type == "cancelled":
            state = "cancelled"
        elif ev.type == "rejected":
            state = "rejected"
        elif ev.type == "signed":
            state = "signed"
        elif ev.type == "partially_signed":
            state = "partially_signed"
            signed_pieces.update(ev.payload.get("signed_piece_ids", []))
        elif ev.type == "forwarded":
            # 转寄不终结原轨迹：原运单标记 forwarded，目标运单另行建流。
            state = "forwarded"
            forward_to = ev.payload.get("to_waybill_id")
        elif ev.type in ("packed", "merged"):
            for pid in ev.payload.get("piece_ids", []):
                if pid not in piece_ids:
                    piece_ids.append(pid)
        elif ev.type == "unpacked":
            for pid in ev.payload.get("piece_ids", []):
                if pid in piece_ids:
                    piece_ids.remove(pid)

    last = timeline[-1]
    return {
        "state": state,
        "forward_to": forward_to,
        "piece_ids": list(piece_ids),
        "signed_piece_ids": sorted(signed_pieces),
        "unsigned_piece_ids": [p for p in piece_ids if p not in signed_pieces],
        "last_event": last.to_dict(),
    }
