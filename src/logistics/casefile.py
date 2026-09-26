"""案卷资料加载与引用完整性校验。

案卷 JSON 结构见 ``fixtures/case-007.json``（全部为虚构数据）。
加载器只做结构与引用校验，不推断任何身份结论。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .entities import (
    AddressCode,
    Authorization,
    Box,
    Contact,
    Node,
    Piece,
    Sale,
    Subject,
    VehicleBatch,
)
from .events import (
    REGISTERED_FIELDS,
    Event,
    EventError,
    event_from_dict,
)

REQUIRED_TOP = {"domain", "version", "case_id"}


class CaseFileError(ValueError):
    """案卷结构或引用不合法。"""


@dataclass
class CaseFile:
    domain: str
    version: int
    case_id: str
    subjects: dict[str, Subject]
    contacts: dict[str, Contact]
    addresses: dict[str, AddressCode]
    nodes: dict[str, Node]
    vehicle_batches: dict[str, VehicleBatch]
    pieces: dict[str, Piece]
    boxes: dict[str, Box]
    sales: dict[str, Sale]
    authorizations: dict[str, Authorization]
    # waybill_id -> 该运单的完整事件流
    waybill_events: dict[str, list[Event]]
    # 转寄关系：原运单 -> 目标运单
    forward_links: dict[str, str] = field(default_factory=dict)
    # 目标运单尚未到卷的转寄（重放时报告为缺口）
    dangling_forwards: dict[str, str] = field(default_factory=dict)

    def events_of(self, waybill_id: str) -> list[Event]:
        try:
            return self.waybill_events[waybill_id]
        except KeyError:
            raise CaseFileError(f"未知运单: {waybill_id}") from None

    def authorizations_for(self, carrier: str) -> list[Authorization]:
        return [a for a in self.authorizations.values() if a.carrier == carrier]

    # ---- 引用查找便捷方法（缺失即报错，不允许悬空引用） ----

    def ref_contact(self, contact_id: str | None) -> Contact | None:
        if contact_id is None:
            return None
        if contact_id not in self.contacts:
            raise CaseFileError(f"运单引用了不存在的联系方式: {contact_id}")
        return self.contacts[contact_id]

    def ref_address(self, address_id: str | None) -> AddressCode | None:
        if address_id is None:
            return None
        if address_id not in self.addresses:
            raise CaseFileError(f"运单引用了不存在的地址代号: {address_id}")
        return self.addresses[address_id]

    def ref_node(self, node_id: str | None) -> Node | None:
        if node_id is None:
            return None
        if node_id not in self.nodes:
            raise CaseFileError(f"事件引用了不存在的网点: {node_id}")
        return self.nodes[node_id]


def _index(items: list[dict], cls, key: str = "id") -> dict:
    result: dict[str, object] = {}
    for raw in items:
        if key not in raw:
            raise CaseFileError(f"{cls.__name__} 缺少 {key}")
        obj_id = raw[key]
        if obj_id in result:
            raise CaseFileError(f"{cls.__name__} 标识重复: {obj_id}")
        result[obj_id] = cls(**raw)
    return result


def _load_authorizations(items: list[dict]) -> dict[str, Authorization]:
    result: dict[str, Authorization] = {}
    for raw in items:
        raw = dict(raw)
        for fk in ("scope_waybill_ids", "scope_node_ids"):
            raw[fk] = frozenset(raw.get(fk, ()))
        auth = Authorization(**raw)
        if auth.id in result:
            raise CaseFileError(f"授权标识重复: {auth.id}")
        result[auth.id] = auth
    return result


def load_casefile(path: str | Path) -> CaseFile:
    """读取案卷 JSON 并完成结构、序号与引用完整性校验。"""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = REQUIRED_TOP - data.keys()
    if missing:
        raise CaseFileError(f"案卷缺少必要字段: {sorted(missing)}")
    if data["domain"] != "logistics-backtracking":
        raise CaseFileError("案卷 domain 必须为 logistics-backtracking")
    if not isinstance(data["version"], int) or data["version"] < 1:
        raise CaseFileError("案卷 version 必须为 >=1 的整数")

    subjects = _index(data.get("subjects", []), Subject)
    contacts = _index(data.get("contacts", []), Contact)
    addresses = _index(data.get("addresses", []), AddressCode)
    nodes = _index(data.get("nodes", []), Node)
    batches = _index(data.get("vehicle_batches", []), VehicleBatch)
    pieces = _index(data.get("pieces", []), Piece)
    boxes = _index(data.get("boxes", []), Box)
    sales_raw = data.get("sales", [])
    for raw in sales_raw:
        raw["piece_ids"] = tuple(raw.get("piece_ids", ()))
    sales = _index(sales_raw, Sale)
    authorizations = _load_authorizations(data.get("authorizations", []))

    waybill_events = _load_waybill_events(data.get("waybills", []))
    case = CaseFile(
        domain=data["domain"],
        version=data["version"],
        case_id=data["case_id"],
        subjects=subjects,
        contacts=contacts,
        addresses=addresses,
        nodes=nodes,
        vehicle_batches=batches,
        pieces=pieces,
        boxes=boxes,
        sales=sales,
        authorizations=authorizations,
        waybill_events=waybill_events,
    )
    _validate_references(case)
    return case


def _load_waybill_events(raw_waybills: list[dict]) -> dict[str, list[Event]]:
    streams: dict[str, list[Event]] = {}
    for wb in raw_waybills:
        wid = wb.get("id")
        if not wid:
            raise CaseFileError("运单缺少 id")
        if wid in streams:
            raise CaseFileError(f"运单标识重复: {wid}")
        raws = wb.get("events", [])
        events = [event_from_dict(e, expected_waybill=wid) for e in raws]
        seqs = [e.seq for e in events]
        if len(seqs) != len(set(seqs)):
            raise EventError(f"运单 {wid} 事件序号重复")
        if not events:
            raise EventError(f"运单 {wid} 没有任何事件")
        kinds = [e.type for e in events]
        if kinds[0] != "registered":
            raise EventError(f"运单 {wid} 首事件必须是 registered")
        if kinds.count("registered") != 1:
            raise EventError(f"运单 {wid} 只能有一条 registered")
        # 同一事件流内时间戳不得倒退（不同来源的晚到更正允许晚于业务时间，
        # 但必须以追加事件形式出现，故按录入顺序检查 at 单调不减）。
        for prev, cur in zip(events, events[1:]):
            if cur.at < prev.at:
                raise EventError(
                    f"运单 {wid} 事件时间倒流: seq {cur.seq} 早于 seq {prev.seq}"
                )
        streams[wid] = events
    return streams


def _validate_references(case: CaseFile) -> None:
    piece_seen: dict[str, str] = {}  # piece_id -> 首次装入/拆分所在运单
    for wid, events in case.waybill_events.items():
        for ev in events:
            p = ev.payload
            if ev.type == "registered":
                unknown = set(p) - REGISTERED_FIELDS
                if unknown:
                    raise CaseFileError(f"运单 {wid} registered 含未知字段: {sorted(unknown)}")
                case.ref_contact(p.get("sender_contact_id"))
                case.ref_contact(p.get("receiver_contact_id"))
                case.ref_address(p.get("sender_address_id"))
                case.ref_address(p.get("receiver_address_id"))
                case.ref_node(p.get("pickup_node_id"))
                if not p.get("carrier"):
                    raise CaseFileError(f"运单 {wid} 缺少 carrier")
                for key in ("sender_subject_id", "receiver_subject_id"):
                    sid = p.get(key)
                    if sid is not None and sid not in case.subjects:
                        raise CaseFileError(f"运单 {wid} 引用了不存在的主体: {sid}")
            elif ev.type == "correction":
                for key in ("sender_subject_id", "receiver_subject_id"):
                    if key in ev.corrected_fields:
                        sid = ev.corrected_fields[key]
                        if sid is not None and sid not in case.subjects:
                            raise CaseFileError(f"运单 {wid} 更正引用了不存在的主体: {sid}")
                for key in ("sender_contact_id", "receiver_contact_id"):
                    if key in ev.corrected_fields:
                        case.ref_contact(ev.corrected_fields[key])
                for key in ("sender_address_id", "receiver_address_id"):
                    if key in ev.corrected_fields:
                        case.ref_address(ev.corrected_fields[key])
                if "pickup_node_id" in ev.corrected_fields:
                    case.ref_node(ev.corrected_fields["pickup_node_id"])
            elif ev.type == "scan":
                node = case.ref_node(p.get("node_id"))
                if node is None:
                    raise CaseFileError(f"运单 {wid} scan 缺少 node_id")
                if p.get("kind") not in {
                    "accepted", "departed", "in_transit",
                    "arrived", "out_delivery", "signed", "failed",
                }:
                    raise CaseFileError(f"运单 {wid} scan 类型非法: {p.get('kind')}")
            elif ev.type == "vehicle_batch":
                bid = p.get("batch_id")
                if bid not in case.vehicle_batches:
                    raise CaseFileError(f"运单 {wid} 引用了不存在的车次: {bid}")
            elif ev.type in ("packed", "unpacked", "merged", "partially_signed"):
                for pid in p.get("piece_ids", []):
                    if pid not in case.pieces:
                        raise CaseFileError(f"运单 {wid} 引用了不存在的货物件: {pid}")
                    if ev.type == "packed":
                        if pid in piece_seen:
                            raise CaseFileError(
                                f"货物件 {pid} 同时装入运单 {piece_seen[pid]} 与 {wid}（须先 unpacked）"
                            )
                        piece_seen[pid] = wid
                    elif ev.type == "unpacked":
                        piece_seen.pop(pid, None)
            elif ev.type == "forwarded":
                target = p.get("to_waybill_id")
                if not target:
                    raise CaseFileError(f"运单 {wid} forwarded 缺少 to_waybill_id")
                case.forward_links[wid] = target
            elif ev.type == "query_receipt":
                if not p.get("query_id") or not p.get("status"):
                    raise CaseFileError(f"运单 {wid} 查询回执缺少 query_id/status")

    # 转寄目标可能尚未到卷：不做硬错误，重放时作为缺口报告。
    for src, target in case.forward_links.items():
        if target not in case.waybill_events:
            case.dangling_forwards[src] = target

    # 车次节点引用
    for b in case.vehicle_batches.values():
        case.ref_node(b.from_node_id)
        case.ref_node(b.to_node_id)

    # 售药记录的件必须存在
    for sale in case.sales.values():
        for pid in sale.piece_ids:
            if pid not in case.pieces:
                raise CaseFileError(f"售药记录 {sale.id} 引用了不存在的货物件: {pid}")

    # 授权范围内的引用
    for auth in case.authorizations.values():
        for wid in auth.scope_waybill_ids:
            if wid not in case.waybill_events:
                raise CaseFileError(f"授权 {auth.id} 列有不存在的运单: {wid}")
        for nid in auth.scope_node_ids:
            if nid not in case.nodes:
                raise CaseFileError(f"授权 {auth.id} 列有不存在的网点: {nid}")
