"""材料冻结登记：跨地区小组并行扩线时去重。

冻结对象是“材料”而不是人，例如某运单的承运方底单、某网点的扫描流水、
某车次清单、某次查询回执。冻结登记：

- 以材料指纹（kind, ref）为唯一键，任何小组只能冻结一次，
  重复冻结抛 :class:`FreezeConflict`，由调用方向后申请的小组说明；
- 冻结必须落在有效授权范围内（运单/网点授权），越界直接拒绝；
- 提供释放（release）与快照，便于交接和审计。

本实现是进程内登记；真实服务应以同一唯一约束落库，
本模块的异常语义就是给存储层的去重契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .casefile import CaseFile


class MaterialKind(str, Enum):
    WAYBILL_RECORD = "waybill_record"     # 承运方保存的运单版本/底单
    NODE_SCAN_LOG = "node_scan_log"       # 网点节点扫描流水
    VEHICLE_BATCH_MANIFEST = "vehicle_batch_manifest"
    QUERY_RECEIPT = "query_receipt"


class FreezeConflict(Exception):
    """材料已被另一小组冻结。"""

    def __init__(self, fingerprint: tuple[str, str], holder_team: str, freeze_id: str):
        self.fingerprint = fingerprint
        self.holder_team = holder_team
        self.freeze_id = freeze_id
        super().__init__(
            f"材料 {fingerprint[0]}:{fingerprint[1]} 已由 {holder_team} 冻结（{freeze_id}）"
        )


class AuthorizationError(PermissionError):
    """冻结超出有效授权范围或授权已失效。"""


@dataclass(frozen=True)
class Freeze:
    id: str
    team: str
    operator: str
    at: str
    materials: frozenset[tuple[str, str]]  # (MaterialKind.value, ref_id)
    authorization_ids: frozenset[str]
    case_id: str
    note: str = ""
    released_at: str | None = None


@dataclass
class FreezeRegistry:
    case: CaseFile

    def __post_init__(self) -> None:
        self._freezes: dict[str, Freeze] = {}
        # 材料指纹 -> 持有冻结 id
        self._holders: dict[tuple[str, str], str] = {}

    def freeze(
        self,
        freeze_id: str,
        *,
        team: str,
        operator: str,
        at: str,
        materials: set[tuple[MaterialKind, str]] | list[tuple[MaterialKind, str]],
        authorization_ids: set[str] | list[str],
        note: str = "",
    ) -> Freeze:
        """登记一次冻结；任一材料已被他组持有即整体拒绝（不去重半成功）。"""

        if freeze_id in self._freezes:
            raise ValueError(f"冻结单号重复: {freeze_id}")
        if not materials:
            raise ValueError("冻结材料不能为空")

        norm = {(k.value, ref) for k, ref in materials}
        auth_ids = set(authorization_ids)
        if not auth_ids:
            raise AuthorizationError("冻结必须附带调取授权")

        # 授权校验：存在、未撤销、未过期（按 ISO 字符串比较）、承运方/范围覆盖。
        auths = []
        for aid in auth_ids:
            auth = self.case.authorizations.get(aid)
            if auth is None:
                raise AuthorizationError(f"授权不存在: {aid}")
            if auth.revoked:
                raise AuthorizationError(f"授权已撤销: {aid}")
            if auth.expires_at and auth.expires_at < at:
                raise AuthorizationError(f"授权已过期: {aid}")
            auths.append(auth)

        for kind, ref in norm:
            self._check_coverage(kind, ref, auths)

        # 去重检查放在全部校验之后；先到先得，整体原子。
        for fp in norm:
            holder = self._holders.get(fp)
            if holder is not None:
                raise FreezeConflict(fp, self._freezes[holder].team, holder)

        freeze = Freeze(
            id=freeze_id,
            team=team,
            operator=operator,
            at=at,
            materials=frozenset(norm),
            authorization_ids=frozenset(auth_ids),
            case_id=self.case.case_id,
            note=note,
        )
        self._freezes[freeze_id] = freeze
        for fp in norm:
            self._holders[fp] = freeze_id
        return freeze

    def _check_coverage(self, kind: str, ref: str, auths) -> None:
        if kind == MaterialKind.WAYBILL_RECORD.value:
            events = self.case.waybill_events.get(ref)
            if events is None:
                raise AuthorizationError(f"待冻结运单不存在: {ref}")
            carrier = events[0].payload["carrier"]
            if not any(
                a.carrier == carrier and a.covers_waybill(ref) for a in auths
            ):
                raise AuthorizationError(f"没有覆盖运单 {ref}（{carrier}）的有效授权")
        elif kind == MaterialKind.NODE_SCAN_LOG.value:
            node = self.case.nodes.get(ref)
            if node is None:
                raise AuthorizationError(f"待冻结网点不存在: {ref}")
            if not any(
                a.carrier == node.carrier and a.covers_node(ref) for a in auths
            ):
                raise AuthorizationError(f"没有覆盖网点 {ref}（{node.carrier}）的有效授权")
        elif kind == MaterialKind.VEHICLE_BATCH_MANIFEST.value:
            batch = self.case.vehicle_batches.get(ref)
            if batch is None:
                raise AuthorizationError(f"待冻结车次不存在: {ref}")
            if not any(a.carrier == batch.carrier for a in auths):
                raise AuthorizationError(f"没有承运方 {batch.carrier} 的有效授权")
        elif kind == MaterialKind.QUERY_RECEIPT.value:
            # 回执引用格式 waybill_id/query_id，授权按运单核查
            wid, _, _ = ref.partition("/")
            events = self.case.waybill_events.get(wid)
            if events is None:
                raise AuthorizationError(f"回执引用了不存在的运单: {wid}")
            carrier = events[0].payload["carrier"]
            if not any(
                a.carrier == carrier and a.covers_waybill(wid) for a in auths
            ):
                raise AuthorizationError(f"没有覆盖运单 {wid} 的有效授权")
        else:
            raise AuthorizationError(f"未知材料类型: {kind}")

    def release(self, freeze_id: str, at: str) -> Freeze:
        freeze = self._freezes.get(freeze_id)
        if freeze is None:
            raise KeyError(f"冻结不存在: {freeze_id}")
        if freeze.released_at is not None:
            raise ValueError(f"冻结已释放: {freeze_id}")
        released = Freeze(
            id=freeze.id, team=freeze.team, operator=freeze.operator, at=freeze.at,
            materials=freeze.materials, authorization_ids=freeze.authorization_ids,
            case_id=freeze.case_id, note=freeze.note, released_at=at,
        )
        self._freezes[freeze_id] = released
        for fp in freeze.materials:
            if self._holders.get(fp) == freeze_id:
                del self._holders[fp]
        return released

    def holder_of(self, kind: MaterialKind, ref: str) -> str | None:
        return self._holders.get((kind.value, ref))

    def freezes_by_team(self, team: str) -> list[Freeze]:
        return [f for f in self._freezes.values() if f.team == team]

    def active_freezes(self) -> list[Freeze]:
        return [f for f in self._freezes.values() if f.released_at is None]
