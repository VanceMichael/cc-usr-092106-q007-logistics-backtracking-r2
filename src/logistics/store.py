"""仅追加（append-only）卷宗存储。

- 每条材料序列化为一行 JSONL，永不更新、永不删除；
- 运单版本按 ``(waybill_no, version_no)`` 去重，后到的更高版本只能追加，
  旧版本始终可读；
- 所有写入经 ``legal.check_access`` 三闸校验，必须挂回执、落在授权范围；
- 跨进程用 ``fcntl`` 锁保护，适合不同地区小组并行扩线。
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from . import codec
from .legal import LegalAccessError, check_access
from .models import (
    AddressAlias,
    Authorization,
    BoxEvent,
    QueryReceipt,
    SaleSeed,
    ScanEvent,
    VehicleBatch,
    WaybillVersion,
    WeightObservation,
)

try:  # 平台为 Linux；保留无 fcntl 环境的退化路径
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class Dossier:
    """内存索引 + JSONL 仅追加日志。

    Parameters
    ----------
    path:
        卷宗日志路径（JSONL）。同名 ``*.lock`` 用于跨进程互斥。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._fh = None

        # 已通过校验的材料索引
        self.authorizations: dict[str, Authorization] = {}
        self.receipts: dict[str, QueryReceipt] = {}
        self.address_aliases: dict[str, list] = defaultdict(list)
        self.waybill_versions: dict[str, list[WaybillVersion]] = defaultdict(list)
        self.weights: list[WeightObservation] = []
        self.scans: list[ScanEvent] = []
        self.box_events: list[BoxEvent] = []
        self.vehicle_batches: dict[str, VehicleBatch] = {}
        self.seeds: dict[str, SaleSeed] = {}

        if self.path.exists():
            self._reload()

    # ------------------------------------------------------------------ 锁

    def __enter__(self) -> "Dossier":
        self._fh = open(self._lock_path, "w")
        if fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        if fcntl is not None and self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # ------------------------------------------------------------ 追加写入

    def _append(self, obj, *, category: str | None = None) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(codec.encode_record(obj), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._index(obj)

    def _index(self, obj) -> None:
        if isinstance(obj, Authorization):
            self.authorizations[obj.auth_id] = obj
        elif isinstance(obj, QueryReceipt):
            self.receipts[obj.receipt_id] = obj
        elif isinstance(obj, AddressAlias):
            self.address_aliases[obj.code].append(obj)
        elif isinstance(obj, WaybillVersion):
            self.waybill_versions[obj.waybill_no].append(obj)
        elif isinstance(obj, WeightObservation):
            self.weights.append(obj)
        elif isinstance(obj, ScanEvent):
            self.scans.append(obj)
        elif isinstance(obj, BoxEvent):
            self.box_events.append(obj)
        elif isinstance(obj, VehicleBatch):
            self.vehicle_batches[obj.batch_no] = obj
        elif isinstance(obj, SaleSeed):
            self.seeds[obj.sale_id] = obj

    def _reload(self) -> None:
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                self._index(codec.decode_record(json.loads(line)))

    # ------------------------------------------------------- 授权 / 回执

    def add_authorization(self, auth: Authorization) -> None:
        if auth.auth_id in self.authorizations:
            raise ValueError(f"授权 {auth.auth_id} 已存在（授权记录不可覆盖，如变更请出新授权）")
        self._append(auth)

    def revoke_authorization(self, auth_id: str, *, recorded_note: str = "") -> None:
        """撤销不删旧记录：写一条 revoked=True 的新版本，原授权留在日志里。"""
        old = self.authorizations.get(auth_id)
        if old is None:
            raise KeyError(auth_id)
        revoked = Authorization(
            auth_id=old.auth_id,
            case_id=old.case_id,
            team_id=old.team_id,
            carriers=old.carriers,
            categories=old.categories,
            valid_from=old.valid_from,
            valid_to=old.valid_to,
            waybill_numbers=old.waybill_numbers,
            scope_all=old.scope_all,
            revoked=True,
        )
        self._append(revoked)

    def add_receipt(self, receipt: QueryReceipt) -> None:
        if receipt.receipt_id in self.receipts:
            raise ValueError(f"回执 {receipt.receipt_id} 已存在")
        auth = self.authorizations.get(receipt.auth_id)
        if auth is None:
            raise LegalAccessError(f"回执 {receipt.receipt_id} 引用的授权 {receipt.auth_id} 不存在")
        if auth.revoked or not auth.covers_time(receipt.requested_at):
            raise LegalAccessError(f"授权 {auth.auth_id} 已撤销或在请求时点失效")
        for category in receipt.categories:
            if not auth.covers(receipt.carrier, category, receipt.waybill_no):
                raise LegalAccessError(
                    f"授权 {auth.auth_id} 未覆盖 {receipt.carrier} 的 {category}"
                    f"（运单 {receipt.waybill_no}），回执不得登记"
                )
        self._append(receipt)

    # ------------------------------------------------------------- 业务材料

    def add_address_alias(self, alias) -> None:
        check_access(
            self.authorizations, self.receipts,
            carrier=_carrier_of_receipt(self.receipts, alias.source_receipt_id),
            category="address_alias",
            waybill_no=_waybill_of_receipt(self.receipts, alias.source_receipt_id),
            receipt_id=alias.source_receipt_id,
        )
        self._append(alias)

    def add_waybill_version(self, wv: WaybillVersion) -> None:
        check_access(
            self.authorizations, self.receipts,
            carrier=wv.carrier, category="waybill",
            waybill_no=wv.waybill_no, receipt_id=wv.receipt_id,
        )
        existing = self.waybill_versions.get(wv.waybill_no, [])
        if any(v.version_no == wv.version_no for v in existing):
            raise ValueError(
                f"运单 {wv.waybill_no} 版本 {wv.version_no} 已存在；"
                "历史版本不可覆盖，更正请使用更高版本号"
            )
        if wv.version_no != 1 and not existing:
            raise ValueError(f"运单 {wv.waybill_no} 缺少 version_no=1 的初始版本")
        if existing and wv.version_no <= max(v.version_no for v in existing):
            raise ValueError(
                f"运单 {wv.waybill_no} 已有更新版本；晚到材料只能以更高 version_no 追加，"
                "原轨迹保持不变"
            )
        self._append(wv)

    def add_weight(self, w: WeightObservation) -> None:
        check_access(
            self.authorizations, self.receipts,
            carrier=_carrier_of_receipt(self.receipts, w.receipt_id),
            category="weight", waybill_no=w.waybill_no, receipt_id=w.receipt_id,
        )
        self._append(w)

    def add_scan(self, s: ScanEvent) -> None:
        check_access(
            self.authorizations, self.receipts,
            carrier=s.carrier, category="scan",
            waybill_no=s.waybill_no, receipt_id=s.receipt_id,
        )
        self._append(s)

    def add_box_event(self, b: BoxEvent) -> None:
        # 箱盒事件常跨多份运单（归集/再合并）：回执只需证明其中一单的箱盒
        # 材料经合法调取即可；回执列明的运单必须出现在事件涉及的运单中。
        receipt = self.receipts.get(b.receipt_id)
        if receipt is None:
            raise LegalAccessError(f"回执 {b.receipt_id} 不存在")
        if b.waybill_nos and receipt.waybill_no not in b.waybill_nos:
            raise LegalAccessError(
                f"箱盒事件 {b.box_code} 涉及 {list(b.waybill_nos)}，"
                f"但回执 {receipt.receipt_id} 只对应 {receipt.waybill_no}，无法佐证"
            )
        check_access(
            self.authorizations, self.receipts,
            carrier=receipt.carrier, category="box",
            waybill_no=receipt.waybill_no, receipt_id=b.receipt_id,
        )
        self._append(b)

    def add_vehicle_batch(self, v: VehicleBatch) -> None:
        check_access(
            self.authorizations, self.receipts,
            carrier=v.carrier, category="vehicle",
            waybill_no=v.waybill_nos[0], receipt_id=v.receipt_id,
        )
        self._append(v)

    def add_seed(self, seed: SaleSeed) -> None:
        if seed.sale_id in self.seeds:
            raise ValueError(f"售药种子记录 {seed.sale_id} 已存在")
        self._append(seed)

    # ------------------------------------------------------------- 查询

    def latest_version(self, waybill_no: str) -> WaybillVersion | None:
        versions = self.waybill_versions.get(waybill_no)
        return max(versions, key=lambda v: v.version_no) if versions else None

    def version_history(self, waybill_no: str) -> list[WaybillVersion]:
        return sorted(self.waybill_versions.get(waybill_no, []), key=lambda v: v.version_no)

    def scans_of(self, waybill_no: str) -> list[ScanEvent]:
        return sorted(
            (s for s in self.scans if s.waybill_no == waybill_no),
            key=lambda s: s.event_time,
        )


def _carrier_of_receipt(receipts: dict[str, QueryReceipt], receipt_id: str) -> str:
    r = receipts.get(receipt_id)
    if r is None:
        raise LegalAccessError(f"回执 {receipt_id} 不存在")
    return r.carrier


def _waybill_of_receipt(receipts: dict[str, QueryReceipt], receipt_id: str) -> str:
    r = receipts.get(receipt_id)
    if r is None:
        raise LegalAccessError(f"回执 {receipt_id} 不存在")
    return r.waybill_no
