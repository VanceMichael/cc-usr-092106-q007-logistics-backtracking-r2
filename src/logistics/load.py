"""从 JSON 样例（或正式调取材料包）装载卷宗。

顺序很关键：授权 → 回执 → 其余材料（每条入库都会校验回执与授权）。
所有样例数据均为虚构。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import models as m
from .store import Dossier


def load_bundle(path: str | Path, dossier: Dossier) -> Dossier:
    """把一个 JSON 材料包装入卷宗。"""
    bundle = json.loads(Path(path).read_text(encoding="utf-8"))

    for d in bundle.get("authorizations", []):
        dossier.add_authorization(_authorization(d))
    for d in bundle.get("receipts", []):
        dossier.add_receipt(_receipt(d))
    for d in bundle.get("address_aliases", []):
        dossier.add_address_alias(_address_alias(d))
    for d in bundle.get("waybill_versions", []):
        dossier.add_waybill_version(_waybill(d))
    for d in bundle.get("weights", []):
        dossier.add_weight(m.WeightObservation(**d))
    for d in bundle.get("scans", []):
        dossier.add_scan(m.ScanEvent(**d))
    for d in bundle.get("box_events", []):
        dossier.add_box_event(m.BoxEvent(**d))
    for d in bundle.get("vehicle_batches", []):
        dossier.add_vehicle_batch(m.VehicleBatch(**_tupleize(d, ("waybill_nos",))))
    for d in bundle.get("seeds", []):
        dossier.add_seed(m.SaleSeed(**d))
    return dossier


def _authorization(d: dict) -> m.Authorization:
    return m.Authorization(
        auth_id=d["auth_id"],
        case_id=d["case_id"],
        team_id=d["team_id"],
        carriers=tuple(d["carriers"]),
        categories=tuple(d["categories"]),
        valid_from=d["valid_from"],
        valid_to=d["valid_to"],
        waybill_numbers=frozenset(d.get("waybill_numbers", [])),
        scope_all=d.get("scope_all", False),
        revoked=d.get("revoked", False),
    )


def _receipt(d: dict) -> m.QueryReceipt:
    return m.QueryReceipt(
        receipt_id=d["receipt_id"],
        auth_id=d["auth_id"],
        carrier=d["carrier"],
        waybill_no=d["waybill_no"],
        requested_at=d["requested_at"],
        received_at=d["received_at"],
        categories=tuple(d["categories"]),
        responder=d.get("responder", ""),
    )


def _address_alias(d: dict) -> m.AddressAlias:
    return m.AddressAlias(
        code=d["code"],
        version=d["version"],
        address_text=d["address_text"],
        valid_from=d["valid_from"],
        valid_to=d.get("valid_to"),
        source_receipt_id=d["source_receipt_id"],
    )


def _party(d: dict) -> m.PartyRef:
    return m.PartyRef(
        ref=d["ref"],
        role=m.PartyRole(d["role"]),
        carrier=d["carrier"],
        display=d.get("display", ""),
        phone=d.get("phone"),
        address_code=d.get("address_code"),
        outlet_code=d.get("outlet_code"),
    )


def _waybill(d: dict) -> m.WaybillVersion:
    return m.WaybillVersion(
        waybill_no=d["waybill_no"],
        carrier=d["carrier"],
        version_no=d["version_no"],
        revision_reason=d["revision_reason"],
        sender=_party(d["sender"]),
        receiver=_party(d["receiver"]),
        pickup_outlet=_party(d["pickup_outlet"]),
        parcel_ids=tuple(d["parcel_ids"]),
        event_time=d["event_time"],
        recorded_at=d["recorded_at"],
        receipt_id=d["receipt_id"],
        note=d.get("note", ""),
    )


def _tupleize(d: dict, keys) -> dict:
    out = dict(d)
    for k in keys:
        if k in out:
            out[k] = tuple(out[k])
    return out
