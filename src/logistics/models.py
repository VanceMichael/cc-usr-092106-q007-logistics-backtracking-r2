"""倒查领域的不可变模型。

所有对象一律以"事件/版本"表达，不做就地覆盖：承运方晚到更正、运单注销、
转寄、拒收、部分签收都是同一运单号下的新版本，旧版本永久保留。

关键约定：面单寄件人、实际揽收网点、收药人网络昵称是三个独立主体引用
（``PartyRef``），即使手机号或地址代号相同也不合并——候选关联由
``links`` 模块单独给出，模型层不提供"认定同一"的能力。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal

# ---------------------------------------------------------------------------
# 主体：同一运单上的三种角色永远分开记录
# ---------------------------------------------------------------------------


class PartyRole(str, Enum):
    SENDER = "sender"            # 面单寄件人
    PICKUP_OUTLET = "pickup_outlet"  # 实际揽收网点
    RECEIVER_NICK = "receiver_nick"  # 收药人网络昵称


# 合法调取的数据类别
DataCategory = Literal[
    "waybill",       # 运单及版本
    "scan",          # 节点扫描
    "weight",        # 包裹重量
    "box",           # 箱盒对应
    "vehicle",       # 车辆批次
    "address_alias",  # 地址代号
    "receipt",       # 查询回执本身
]

# 版本变更原因（initial 之后都必须保留上一版）
RevisionReason = Literal[
    "initial",         # 首次调取
    "late_correction",  # 承运方晚到更正
    "cancellation",    # 运单注销
    "forward",         # 转寄
    "refusal",         # 拒收
    "partial_sign",    # 部分签收
]

ScanType = Literal[
    "accepted",        # 揽收
    "in_transit",      # 运输
    "transfer",        # 中转
    "merge",           # 外省再次合并
    "arrived",         # 到达派送网点
    "out_for_delivery",
    "signed",          # 签收
    "partial_signed",  # 部分签收
    "refused",         # 拒收
    "forwarded",       # 转寄发出
    "cancelled",       # 注销
]


def parse_ts(value: str) -> datetime:
    """解析 ISO-8601 时间戳（容忍 'Z' 结尾）。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class PartyRef:
    """一次面单记录中的主体引用。

    ``ref`` 在卷宗内全局唯一，由承运方原始标识与角色构成；不同角色即使
    手机号/地址完全一致，也是不同的 ``ref``，互不覆盖。
    """

    ref: str
    role: PartyRole
    carrier: str
    display: str = ""                 # 面单上的名字/昵称原文
    phone: str | None = None
    address_code: str | None = None   # 地址代号（见 AddressAlias）
    outlet_code: str | None = None    # 网点代号（揽收网点角色用）

    def matches(self, *, phone: str | None = None, address_code: str | None = None) -> bool:
        """属性是否相同——仅供候选关联使用，不是身份判定。"""
        if phone is not None and self.phone == phone:
            return True
        if address_code is not None and self.address_code == address_code:
            return True
        return False


# ---------------------------------------------------------------------------
# 合法调取：授权与回执
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Authorization:
    """一份法律手续所划定的可调取范围。"""

    auth_id: str
    case_id: str
    team_id: str                     # 持函地区小组
    carriers: tuple[str, ...]        # 可调取的承运方
    categories: tuple[DataCategory, ...]
    valid_from: str                  # ISO-8601
    valid_to: str
    waybill_numbers: frozenset[str] = field(default_factory=frozenset)
    # 空集合表示"本授权列明的全部运单"（即 waybill_numbers 非空时逐个匹配）；
    # 通配调取通过 categories 与 carriers 限定，运单号仍须在 waybill_numbers 中，
    # 除非案件级授权显式 scope_all=True。
    scope_all: bool = False
    revoked: bool = False

    def covers_time(self, ts: str) -> bool:
        moment = parse_ts(ts)
        return parse_ts(self.valid_from) <= moment <= parse_ts(self.valid_to)

    def covers(self, carrier: str, category: DataCategory, waybill_no: str) -> bool:
        if self.revoked:
            return False
        if carrier not in self.carriers:
            return False
        if category not in self.categories:
            return False
        if self.scope_all:
            return True
        return waybill_no in self.waybill_numbers


@dataclass(frozen=True)
class QueryReceipt:
    """承运方/平台对一次查询出具的回执。任何入库材料都必须能挂到回执上。"""

    receipt_id: str
    auth_id: str
    carrier: str
    waybill_no: str
    requested_at: str
    received_at: str
    categories: tuple[DataCategory, ...]
    responder: str = ""              # 出具单位/对接人（虚构代号）


# ---------------------------------------------------------------------------
# 地址代号
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AddressAlias:
    """地址代号 → 地址文本的一个版本（代号可能换绑，故同样保留版本）。"""

    code: str
    version: int
    address_text: str
    valid_from: str
    valid_to: str | None = None      # None 表示当前仍有效
    source_receipt_id: str = ""


# ---------------------------------------------------------------------------
# 运单版本、包裹、重量、箱盒、扫描、车次
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WaybillVersion:
    """同号运单的一个版本。永不修改；更正以更高 version_no 追加。"""

    waybill_no: str
    carrier: str
    version_no: int
    revision_reason: RevisionReason
    sender: PartyRef
    receiver: PartyRef
    pickup_outlet: PartyRef
    parcel_ids: tuple[str, ...]      # 本版本面单覆盖的包裹
    event_time: str                  # 业务事实发生时间
    recorded_at: str                 # 入卷时间（晚到更正会晚于 event_time）
    receipt_id: str
    note: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.revision_reason in ("cancellation", "refusal")

    def party_refs(self) -> tuple[PartyRef, ...]:
        return (self.sender, self.pickup_outlet, self.receiver)


@dataclass(frozen=True)
class WeightObservation:
    """某次称量记录；同一包裹多节点重量可比对，拆并箱时异常会暴露。"""

    parcel_id: str
    waybill_no: str
    node_code: str
    weight_kg: float
    event_time: str
    recorded_at: str
    receipt_id: str


@dataclass(frozen=True)
class ScanEvent:
    """节点扫描。车辆批次与省份用于还原跨省运输。"""

    scan_id: str
    waybill_no: str
    carrier: str
    parcel_id: str | None
    node_code: str
    province: str
    scan_type: ScanType
    event_time: str
    recorded_at: str
    receipt_id: str
    vehicle_batch_no: str | None = None


@dataclass(frozen=True)
class BoxEvent:
    """箱盒的装入/拆出/合并事件，连接"一箱药 ↔ 多件快递"。"""

    box_code: str
    action: Literal["packed", "split", "merged", "opened"]
    parcel_ids: tuple[str, ...]
    waybill_nos: tuple[str, ...]
    province: str
    event_time: str
    recorded_at: str
    receipt_id: str
    note: str = ""


@dataclass(frozen=True)
class VehicleBatch:
    """车辆批次：一车运单/包裹的集合与跨省区间。"""

    batch_no: str
    carrier: str
    vehicle_code: str
    origin_province: str
    dest_province: str
    departed_at: str
    arrived_at: str | None
    waybill_nos: tuple[str, ...]
    receipt_id: str


@dataclass(frozen=True)
class SaleSeed:
    """倒查起点：一次网络售药发货记录（全部虚构）。"""

    sale_id: str
    listing_nick: str                # 售药网络昵称
    buyer_nick: str
    event_time: str
    # 起点线索：可能只知道运单号，也可能只知道包裹号或箱号
    waybill_no: str | None = None
    parcel_id: str | None = None
    box_code: str | None = None
    note: str = ""
