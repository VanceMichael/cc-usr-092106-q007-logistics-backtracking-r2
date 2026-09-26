"""案卷中的基础实体。

标识一律由案卷资料给定（字符串 id），加载器校验引用完整性。
联系方式与地址代号独立于“主体”存在：系统不保存
“某手机号必然属于某人”这类结论，归属关系只以候选关联的形式呈现。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Subject:
    """案件中出现的人或角色（面单寄件人昵称、收药人昵称等）。"""

    id: str
    label: str
    note: str = ""


@dataclass(frozen=True)
class Contact:
    """联系方式（手机号等），只保存脱敏值。"""

    id: str
    phone_masked: str
    platform: str = ""
    note: str = ""


@dataclass(frozen=True)
class AddressCode:
    """地址代号：面单上的地址以代号引用，不直接存明文地址。"""

    id: str
    code: str
    province: str
    city: str
    detail_masked: str = ""


@dataclass(frozen=True)
class Node:
    """承运方扫描网点。"""

    id: str
    carrier: str
    code: str
    name: str
    province: str
    city: str


@dataclass(frozen=True)
class VehicleBatch:
    """车辆批次（车次）：运单通过 vehicle_batch 事件搭乘。"""

    id: str
    carrier: str
    batch_no: str
    depart_at: str
    from_node_id: str
    to_node_id: str
    crosses_province: bool = False


@dataclass(frozen=True)
class Piece:
    """货物件：最小流转单位，重量用于归集/拆分核对。"""

    id: str
    description: str
    weight_g: int
    medicine_lot: str = ""
    from_sale_id: str | None = None


@dataclass(frozen=True)
class Box:
    """箱/盒：若干货物件可装入一个箱后交运，也可拆箱。"""

    id: str
    weight_g: int | None = None
    note: str = ""


@dataclass(frozen=True)
class Sale:
    """网络售药发货记录：倒查起点。"""

    id: str
    at: str
    buyer_nick: str
    piece_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Authorization:
    """调取授权：按承运方与范围（运单/网点）限定合法调取边界。"""

    id: str
    carrier: str
    granted_by: str
    granted_at: str
    scope_waybill_ids: frozenset[str] = field(default_factory=frozenset)
    scope_node_ids: frozenset[str] = field(default_factory=frozenset)
    expires_at: str | None = None
    revoked: bool = False

    def covers_waybill(self, waybill_id: str) -> bool:
        # 空范围表示暂不支持“全量授权”，必须显式列明运单，避免越界调取。
        return waybill_id in self.scope_waybill_ids

    def covers_node(self, node_id: str) -> bool:
        return node_id in self.scope_node_ids
