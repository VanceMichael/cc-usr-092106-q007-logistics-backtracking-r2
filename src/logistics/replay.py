"""从某次网络售药发货记录出发，重放货物流转并报告缺口。

重放只依据已留痕的事件推导货物所在：

    归集（packed/merged）→ 跨省运输（vehicle_batch，经 scan 省份印证）
    → 拆分（unpacked）→ 外省合并（merged）→ 签收 / 部分签收 / 拒收 / 转寄。

重放结果明确区分“已证实的节点”和“仍缺失的内容”：

- 缺扫描：两个已知节点之间没有任何 scan，或车次跨省时缺到达省扫描；
- 缺重量核对：箱重/件重与运单申报重量缺失或无法勾稽；
- 缺查询回执：运单有晚到更正（correction）但无对应 query_receipt；
- 缺授权：参与重放的运单/网点没有有效授权覆盖；
- 断点：转寄目标未到卷、货物件最后去向不明（无终态事件）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .casefile import CaseFile
from .events import latest_fields, ordered_events
from .freezes import MaterialKind


class GapKind(str, Enum):
    MISSING_SCAN = "missing_scan"
    MISSING_WEIGHT = "missing_weight"
    MISSING_RECEIPT = "missing_query_receipt"
    MISSING_AUTHORIZATION = "missing_authorization"
    DANGLING_FORWARD = "dangling_forward"
    UNRESOLVED_PIECE = "unresolved_piece"
    UNEVENT_WEIGHT_MISMATCH = "weight_mismatch"


@dataclass(frozen=True)
class Gap:
    kind: str
    ref: str          # 相关运单/车次/件 id
    detail: str


@dataclass
class ReplayStep:
    sale_id: str
    piece_ids: list[str]
    waybill_id: str
    phase: str        # collected / split / cross_province / merged / delivered / ...
    at: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayResult:
    sale_id: str
    # piece_id -> 按时间排列的轨迹步骤
    piece_journey: dict[str, list[ReplayStep]]
    # 参与重放的运单（按首次出现排序）
    waybill_ids: list[str]
    # piece_id -> 终态 signed / partially_signed / rejected / forwarded / unknown
    piece_outcomes: dict[str, str]
    gaps: list[Gap]

    def phases(self) -> list[ReplayStep]:
        """全部件的步骤合并时间线。"""

        steps = [s for js in self.piece_journey.values() for s in js]
        return sorted(steps, key=lambda s: (s.at, s.waybill_id))


def replay_sale(case: CaseFile, sale_id: str, *, at: str | None = None) -> ReplayResult:
    """重放某条网售发货记录对应货物的全链路。

    ``at`` 用于授权有效性判断的时点，缺省取案卷中最晚事件时间。
    """

    sale = case.sales.get(sale_id)
    if sale is None:
        raise KeyError(f"未知售药记录: {sale_id}")

    target_pieces = set(sale.piece_ids)
    # piece_id -> 承载过它的运单序列（按 packed/unpacked/merged 事件推导）
    carriers: dict[str, list[str]] = {pid: [] for pid in target_pieces}

    # 找出事件中直接触碰目标件的运单。
    relevant: set[str] = set()
    for wid, events in case.waybill_events.items():
        for ev in events:
            pids: set[str] = set()
            if ev.type in ("packed", "unpacked", "merged"):
                pids = set(ev.payload.get("piece_ids", []))
            elif ev.type == "partially_signed":
                pids = set(ev.payload.get("signed_piece_ids", []))
            if pids & target_pieces:
                relevant.add(wid)
                break

    # 沿转寄链闭包：转寄目标运单即使未重新装箱也属于同票货。
    queue = list(relevant)
    while queue:
        wid = queue.pop(0)
        target = case.forward_links.get(wid)
        if target and target in case.waybill_events and target not in relevant:
            relevant.add(target)
            queue.append(target)

    # 重新按事件时间顺序推导每件的承载运单序列
    all_events: list = []
    for wid in relevant:
        for ev in case.waybill_events[wid]:
            all_events.append((ev.at, ev.seq, wid, ev))
    all_events.sort(key=lambda x: (x[0], x[1]))

    piece_journey: dict[str, list[ReplayStep]] = {pid: [] for pid in sale.piece_ids}
    active_waybill: dict[str, str] = {}
    province_seen: dict[str, set[str]] = {pid: set() for pid in sale.piece_ids}

    for _, _, wid, ev in all_events:
        pids = set(ev.payload.get("piece_ids", [])) if ev.type in (
            "packed", "unpacked", "merged"
        ) else set()
        signed_pids = set(ev.payload.get("signed_piece_ids", [])) if ev.type == "partially_signed" else set()
        touched = (pids | signed_pids) & set(sale.piece_ids)
        if not touched and ev.type not in (
            "packed", "unpacked", "merged", "partially_signed", "forwarded"
        ):
            continue
        if ev.type == "packed":
            for pid in touched:
                carriers.setdefault(pid, []).append(wid)
                active_waybill[pid] = wid
                piece_journey[pid].append(ReplayStep(
                    sale_id, [pid], wid, "collected", ev.at,
                    {"source": ev.source},
                ))
        elif ev.type == "merged":
            for pid in touched:
                carriers.setdefault(pid, []).append(wid)
                active_waybill[pid] = wid
                piece_journey[pid].append(ReplayStep(
                    sale_id, [pid], wid, "merged", ev.at,
                    {"source": ev.source},
                ))
        elif ev.type == "unpacked":
            for pid in touched:
                piece_journey[pid].append(ReplayStep(
                    sale_id, [pid], wid, "split", ev.at,
                    {"source": ev.source},
                ))
                active_waybill.pop(pid, None)
        elif ev.type == "partially_signed":
            for pid in touched:
                piece_journey[pid].append(ReplayStep(
                    sale_id, [pid], wid, "partially_signed", ev.at,
                ))
        elif ev.type == "forwarded":
            # 当前挂在本原运单上的目标件随转寄转到目标运单，原轨迹保留。
            target_wid = ev.payload["to_waybill_id"]
            moved = [
                pid for pid, holder in active_waybill.items()
                if holder == wid and pid in sale.piece_ids
            ]
            for pid in moved:
                carriers.setdefault(pid, []).append(target_wid)
                active_waybill[pid] = target_wid
                piece_journey[pid].append(ReplayStep(
                    sale_id, [pid], wid, "forwarded", ev.at,
                    {"to_waybill_id": target_wid},
                ))

    # 沿扫描、车次、生命周期事件补全每件旅程（用其承载运单）。
    for pid in sale.piece_ids:
        for wid in dict.fromkeys(carriers.get(pid, [])):
            if wid not in case.waybill_events:
                continue  # 转寄目标未到卷：已作为缺口报告
            for ev in case.waybill_events[wid]:
                if ev.type == "vehicle_batch":
                    batch = case.vehicle_batches[ev.payload["batch_id"]]
                    piece_journey[pid].append(ReplayStep(
                        sale_id, [pid], wid,
                        "cross_province" if batch.crosses_province else "in_province_move",
                        ev.at, {"batch_id": batch.id},
                    ))
                    if batch.crosses_province:
                        province_seen[pid].add(
                            case.nodes[batch.from_node_id].province
                        )
                        province_seen[pid].add(
                            case.nodes[batch.to_node_id].province
                        )
                elif ev.type == "scan":
                    node = case.nodes[ev.payload["node_id"]]
                    province_seen[pid].add(node.province)
                    piece_journey[pid].append(ReplayStep(
                        sale_id, [pid], wid, f"scan:{ev.payload['kind']}", ev.at,
                        {"node_id": node.id, "province": node.province},
                    ))
                elif ev.type in ("signed", "rejected"):
                    piece_journey[pid].append(ReplayStep(
                        sale_id, [pid], wid, ev.type, ev.at,
                    ))
                elif ev.type == "cancelled":
                    piece_journey[pid].append(ReplayStep(
                        sale_id, [pid], wid, "cancelled", ev.at,
                    ))
        piece_journey[pid].sort(key=lambda s: (s.at, s.waybill_id))

    # ---- 缺口分析 ----
    gaps: list[Gap] = []
    used_waybills = sorted({w for ws in carriers.values() for w in ws})
    check_at = at or _latest_at(case)

    for wid in used_waybills:
        if wid not in case.waybill_events:
            continue  # 转寄目标未到卷：断点缺口已在下面统一报告
        events = ordered_events(case.waybill_events[wid])

        # 1) 缺扫描：相邻节点扫描之间或运单有车次但完全没有 scan
        scan_nodes = [
            (ev.at, case.nodes[ev.payload["node_id"]], ev.payload["kind"])
            for ev in events if ev.type == "scan"
        ]
        if not scan_nodes:
            gaps.append(Gap(GapKind.MISSING_SCAN, wid, "运单无任何节点扫描记录"))
        else:
            batch_between = _batch_intervals(events)
            for (a_at, a_node, _), (b_at, b_node, _) in zip(scan_nodes, scan_nodes[1:]):
                if a_node.id == b_node.id:
                    continue
                if a_node.city == b_node.city and a_node.province == b_node.province:
                    continue
                # 跨城相邻扫描之间应有车次事件落在该时间窗内。
                if not any(a_at <= t <= b_at for t in batch_between):
                    gaps.append(Gap(
                        GapKind.MISSING_SCAN, wid,
                        f"{a_node.code}({a_at}) 到 {b_node.code}({b_at}) 之间缺运输车次记录",
                    ))

        # 2) 跨省车次缺到达省扫描
        for ev in events:
            if ev.type == "vehicle_batch":
                batch = case.vehicle_batches[ev.payload["batch_id"]]
                if batch.crosses_province:
                    dest_prov = case.nodes[batch.to_node_id].province
                    if not any(
                        s.type == "scan"
                        and case.nodes[s.payload["node_id"]].province == dest_prov
                        for s in events
                    ):
                        gaps.append(Gap(
                            GapKind.MISSING_SCAN, batch.id,
                            f"跨省车次缺到达省 {dest_prov} 的扫描",
                        ))

        # 3) 重量核对：运单装入/合并件重之和 vs 最新申报重量（晚到更正后）。
        #    转寄承接单不重新称重，无装箱事件则跳过（重量已在上游单核对）。
        carried_here = set()
        for ev in events:
            if ev.type in ("packed", "merged"):
                carried_here.update(ev.payload.get("piece_ids", []))
        declared = latest_fields(events).get("declared_weight_g")
        if carried_here:
            total = sum(case.pieces[p].weight_g for p in carried_here if p in case.pieces)
            if declared is None:
                gaps.append(Gap(GapKind.MISSING_WEIGHT, wid, f"运单缺申报重量，件重合计 {total}g"))
            elif abs(total - declared) / max(declared, 1) > 0.05:
                gaps.append(Gap(
                    GapKind.UNEVENT_WEIGHT_MISMATCH, wid,
                    f"件重合计 {total}g 与申报 {declared}g 不符",
                ))

        # 4) 晚到更正缺查询回执出处
        corrections = [ev for ev in events if ev.type == "correction"]
        receipts = {ev.payload.get("query_id") for ev in events if ev.type == "query_receipt"}
        for corr in corrections:
            qid = corr.payload.get("query_id") if corr.payload else None
            if not qid or qid not in receipts:
                gaps.append(Gap(
                    GapKind.MISSING_RECEIPT, wid,
                    f"seq {corr.seq} 的晚到更正缺少承运方查询回执",
                ))

        # 5) 授权缺口
        carrier = events[0].payload["carrier"]
        if not any(
            a.carrier == carrier and a.covers_waybill(wid)
            and not a.revoked and (not a.expires_at or a.expires_at >= check_at)
            for a in case.authorizations.values()
        ):
            gaps.append(Gap(
                GapKind.MISSING_AUTHORIZATION, wid,
                f"承运方 {carrier} 运单 {wid} 无有效授权覆盖",
            ))
        scanned_node_ids = {
            ev.payload["node_id"] for ev in events if ev.type == "scan"
        }
        for nid in scanned_node_ids:
            node = case.nodes[nid]
            if not any(
                a.carrier == node.carrier and a.covers_node(nid)
                and not a.revoked and (not a.expires_at or a.expires_at >= check_at)
                for a in case.authorizations.values()
            ):
                gaps.append(Gap(
                    GapKind.MISSING_AUTHORIZATION, nid,
                    f"网点 {node.code}（{node.carrier}）扫描流水无有效授权",
                ))

    # 6) 转寄断点
    for src, target in case.dangling_forwards.items():
        if src in used_waybills:
            gaps.append(Gap(GapKind.DANGLING_FORWARD, src, f"转寄目标运单 {target} 尚未到卷"))

    # 7) 件终态
    outcomes: dict[str, str] = {}
    for pid in sale.piece_ids:
        tail_phases = [s.phase for s in piece_journey[pid]]
        if "signed" in tail_phases:
            outcomes[pid] = "signed"
        elif "partially_signed" in tail_phases:
            outcomes[pid] = "partially_signed"
        elif "rejected" in tail_phases:
            outcomes[pid] = "rejected"
        elif "forwarded" in tail_phases:
            outcomes[pid] = "forwarded"
        elif "cancelled" in tail_phases:
            outcomes[pid] = "cancelled"
        else:
            outcomes[pid] = "unknown"
            gaps.append(Gap(GapKind.UNRESOLVED_PIECE, pid, "货物件无签收/拒收等终态事件，去向不明"))

    return ReplayResult(
        sale_id=sale_id,
        piece_journey=piece_journey,
        waybill_ids=used_waybills,
        piece_outcomes=outcomes,
        gaps=gaps,
    )


def _batch_intervals(events) -> list[str]:
    return [ev.at for ev in events if ev.type == "vehicle_batch"]


def _latest_at(case: CaseFile) -> str:
    return max(
        ev.at for events in case.waybill_events.values() for ev in events
    )


def material_fingerprints_for_replay(result: ReplayResult, case: CaseFile):
    """重放涉及的全部材料指纹，供冻结登记直接使用。"""

    materials: set[tuple[MaterialKind, str]] = set()
    for wid in result.waybill_ids:
        events = case.waybill_events.get(wid)
        if events is None:
            continue  # 转寄目标未到卷：无材料可冻结，缺口已在结果中列出
        materials.add((MaterialKind.WAYBILL_RECORD, wid))
        for ev in events:
            if ev.type == "scan":
                materials.add((MaterialKind.NODE_SCAN_LOG, ev.payload["node_id"]))
            elif ev.type == "vehicle_batch":
                materials.add((MaterialKind.VEHICLE_BATCH_MANIFEST, ev.payload["batch_id"]))
            elif ev.type == "query_receipt":
                materials.add((MaterialKind.QUERY_RECEIPT, f"{wid}/{ev.payload['query_id']}"))
    return materials
