"""合法调取范围校验。

入库任何一条材料之前都要过三道闸：

1. 挂接的回执必须存在，且回执本身由某份授权支持；
2. 授权在调取请求时点有效（未撤销、在有效期内）；
3. 承运方、数据类别、运单号都在授权列明范围内。

倒查重放只基于通过校验的材料，缺授权/缺回执的线索以"缺口"形式列出，
而不是直接使用或静默丢弃。
"""

from __future__ import annotations

from .models import Authorization, DataCategory, QueryReceipt


class LegalAccessError(ValueError):
    """材料不在合法调取范围内。"""


def check_access(
    auths: dict[str, Authorization],
    receipts: dict[str, QueryReceipt],
    *,
    carrier: str,
    category: DataCategory,
    waybill_no: str,
    receipt_id: str,
) -> Authorization:
    """校验一条入库材料，返回支撑它的授权；不合法则抛 LegalAccessError。"""
    receipt = receipts.get(receipt_id)
    if receipt is None:
        raise LegalAccessError(f"缺少查询回执 {receipt_id}，材料不得入库：{waybill_no}/{category}")
    if receipt.carrier != carrier or receipt.waybill_no != waybill_no:
        raise LegalAccessError(
            f"回执 {receipt_id} 与材料不符："
            f"回执={receipt.carrier}/{receipt.waybill_no} 材料={carrier}/{waybill_no}"
        )
    if category not in receipt.categories:
        raise LegalAccessError(f"回执 {receipt_id} 未覆盖数据类别 {category}")

    auth = auths.get(receipt.auth_id)
    if auth is None:
        raise LegalAccessError(f"回执 {receipt_id} 引用的授权 {receipt.auth_id} 不存在")
    if auth.revoked:
        raise LegalAccessError(f"授权 {auth.auth_id} 已撤销")
    if not auth.covers_time(receipt.requested_at):
        raise LegalAccessError(
            f"授权 {auth.auth_id} 在调取请求时点 {receipt.requested_at} 不在有效期内"
        )
    if not auth.covers(carrier, category, waybill_no):
        raise LegalAccessError(
            f"授权 {auth.auth_id} 未覆盖 {carrier} 的 {category}（运单 {waybill_no}）"
        )
    return auth


def assess_gap(
    auths: dict[str, Authorization],
    receipts: dict[str, QueryReceipt],
    *,
    carrier: str,
    category: DataCategory,
    waybill_no: str,
) -> str:
    """对一个缺失的节点/材料，判断缺的是授权还是回执（或两者都缺）。"""
    receipt_exists = any(
        r.carrier == carrier and r.waybill_no == waybill_no and category in r.categories
        for r in receipts.values()
    )
    auth_exists = any(
        not a.revoked and a.covers(carrier, category, waybill_no) for a in auths.values()
    )
    if receipt_exists and not auth_exists:
        return "missing_authorization"
    if auth_exists and not receipt_exists:
        return "missing_receipt"
    if not auth_exists and not receipt_exists:
        return "missing_authorization_and_receipt"
    # 两者都有却仍缺失：说明承运方尚未回传数据
    return "awaiting_carrier_data"
