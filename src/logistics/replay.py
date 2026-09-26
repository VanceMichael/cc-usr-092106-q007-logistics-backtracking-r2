"""从网络售药发货记录重放货物轨迹。

重放回答四件事：

1. 货物怎样被**归集**（箱盒 packed 事件、同批揽收）；
2. 怎样被**拆分**成多件快递（box split、同箱多运单）；
3. 怎样**跨省运输**并在外省**再次合并**（车次、merge 扫描、box merged）；
4. 怎样**签收**（signed / partial_signed / refused / cancelled）。

同时输出"仍缺什么"：

- 缺失的扫描节点（揽收、跨省车次、终态等）；
- 部分签收后未见下落的残件；
- 缺称量或称量异常（无拆并箱事件却明显变重/变轻）；
- 每个缺口到底是**缺授权**、**缺回执**，还是承运方数据未到（legal.assess_gap）。

重放基于全部历史版本：晚到更正、注销、转寄、拒收、部分签收的原轨迹都在
时间轴上，不被新版本抹掉。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .legal import assess_gap
from .models import SaleSeed
from .store import Dossier

# 称量比对的默认容差：相对差 20% 或绝对差 0.05kg 以内视为正常
DEFAULT_WEIGHT_REL_TOL = 0.20
DEFAULT_WEIGHT_ABS_TOL = 0.05

# 一条完整链条期望出现的终态扫描
TERMINAL_SCANS = {"signed", "partial_signed", "refused", "cancelled"}


@dataclass(frozen=True)
class TimelineEntry:
    event_time: str
    stage: str            # seed / consolidation / split / cross_province / merge / delivery / revision
    kind: str             # 事件类型
    province: str
    summary: str
    evidence: tuple[str, ...]
    # 晚到材料：业务发生时间与入卷时间相差显著时置位，原轨迹仍保留
    late: bool = False


@dataclass(frozen=True)
class MissingNode:
    subject: str                 # parcel:xxx / waybill:yyy
    node: str                    # 缺什么（accepted / terminal_scan / cross_province_vehicle …）
    detail: str
    carrier: str
    waybill_no: str
    category: str
    legal_status: str            # missing_authorization / missing_receipt / awaiting_carrier_data / …


@dataclass(frozen=True)
class WeightAnomaly:
    parcel_id: str
    detail: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ReplayResult:
    seed_id: str
    timeline: list[TimelineEntry]
    discovered_waybills: tuple[str, ...]
    discovered_parcels: tuple[str, ...]
    discovered_boxes: tuple[str, ...]
    discovered_batches: tuple[str, ...]
    missing_nodes: list[MissingNode]
    unsigned_parcels: list[str]
    weight_anomalies: list[WeightAnomaly]
    legal_gaps: list[MissingNode]
    version_notes: tuple[str, ...]

    @property
    def fully_resolved(self) -> bool:
        """无任何缺口且无残件，才算链条闭合。"""
        return not (self.missing_nodes or self.unsigned_parcels or self.weight_anomalies)

    def narrative(self) -> str:
        """便于负责人通读的文字版重放。"""
        lines = [f"售药种子 {self.seed_id} 的货物重放："]
        for e in self.timeline:
            if e.late and e.stage == "revision":
                tag = "（晚到补录，原轨迹保留）"
            elif e.late:
                tag = "（事实发生后补录入卷）"
            else:
                tag = ""
            lines.append(f"  {e.event_time} [{e.stage}] {e.summary}{tag}")
        if self.unsigned_parcels:
            lines.append(f"  残件：{', '.join(self.unsigned_parcels)} 终态不明")
        for g in self.missing_nodes:
            lines.append(f"  缺口：{g.subject} 缺 {g.node}（{g.legal_status}）")
        for w in self.weight_anomalies:
            lines.append(f"  重量异常：{w.parcel_id} {w.detail}")
        if self.fully_resolved:
            lines.append("  链条闭合，未见缺口。")
        return "\n".join(lines)


class Replayer:
    def __init__(
        self,
        dossier: Dossier,
        *,
        weight_rel_tol: float = DEFAULT_WEIGHT_REL_TOL,
        weight_abs_tol: float = DEFAULT_WEIGHT_ABS_TOL,
        late_record_days: float = 3.0,
    ):
        self.d = dossier
        self.weight_rel_tol = weight_rel_tol
        self.weight_abs_tol = weight_abs_tol
        self.late_record_days = late_record_days

    def replay(self, seed: SaleSeed) -> ReplayResult:
        waybills, parcels, boxes, batches = self._expand(seed)

        timeline = self._build_timeline(seed, waybills, parcels, boxes, batches)
        version_notes = self._version_notes(waybills)
        missing, unsigned = self._scan_gaps(waybills, parcels)
        legal_gaps = self._legal_gaps(waybills, missing)
        missing.extend(legal_gaps)
        weight_anomalies = self._weight_checks(parcels, waybills)

        return ReplayResult(
            seed_id=seed.sale_id,
            timeline=timeline,
            discovered_waybills=tuple(sorted(waybills)),
            discovered_parcels=tuple(sorted(parcels)),
            discovered_boxes=tuple(sorted(boxes)),
            discovered_batches=tuple(sorted(batches)),
            missing_nodes=missing,
            unsigned_parcels=sorted(unsigned),
            weight_anomalies=weight_anomalies,
            legal_gaps=legal_gaps,
            version_notes=tuple(version_notes),
        )

    # ------------------------------------------------------- 关联扩展

    def _expand(self, seed: SaleSeed):
        waybills: set[str] = set()
        parcels: set[str] = set()
        boxes: set[str] = set()
        batches: set[str] = set()

        if seed.waybill_no:
            waybills.add(seed.waybill_no)
        if seed.parcel_id:
            parcels.add(seed.parcel_id)
        if seed.box_code:
            boxes.add(seed.box_code)

        changed = True
        while changed:
            changed = False

            # 运单 → 其全部版本上的包裹、扫描所在车次
            for no in list(waybills):
                for wv in self.d.waybill_versions.get(no, []):
                    for pid in wv.parcel_ids:
                        if pid not in parcels:
                            parcels.add(pid)
                            changed = True
                for s in self.d.scans_of(no):
                    if s.parcel_id and s.parcel_id not in parcels:
                        parcels.add(s.parcel_id)
                        changed = True
                    if s.vehicle_batch_no and s.vehicle_batch_no not in batches:
                        batches.add(s.vehicle_batch_no)
                        changed = True

            # 包裹 → 挂过它的所有运单（转寄/再合并会跨运单）
            for versions in self.d.waybill_versions.values():
                pids = {p for wv in versions for p in wv.parcel_ids}
                if pids & parcels and versions[0].waybill_no not in waybills:
                    waybills.add(versions[0].waybill_no)
                    changed = True

            # 箱盒事件 ↔ 运单/包裹
            for ev in self.d.box_events:
                touch = ev.box_code in boxes or bool(set(ev.parcel_ids) & parcels) \
                    or bool(set(ev.waybill_nos) & waybills)
                if touch:
                    if ev.box_code not in boxes:
                        boxes.add(ev.box_code)
                        changed = True
                    for pid in ev.parcel_ids:
                        if pid not in parcels:
                            parcels.add(pid)
                            changed = True
                    for no in ev.waybill_nos:
                        if no not in waybills:
                            waybills.add(no)
                            changed = True

            # 车次 → 运单
            for b in self.d.vehicle_batches.values():
                if b.batch_no in batches:
                    for no in b.waybill_nos:
                        if no not in waybills:
                            waybills.add(no)
                            changed = True
                elif set(b.waybill_nos) & waybills:
                    if b.batch_no not in batches:
                        batches.add(b.batch_no)
                        changed = True

        return waybills, parcels, boxes, batches

    # ------------------------------------------------------- 时间轴

    def _build_timeline(self, seed, waybills, parcels, boxes, batches) -> list[TimelineEntry]:
        entries: list[TimelineEntry] = []

        entries.append(TimelineEntry(
            event_time=seed.event_time, stage="seed", kind="sale_record",
            province="", summary=f"网络售药发货记录（售方 {seed.listing_nick} / 买方 {seed.buyer_nick}）",
            evidence=(f"sale:{seed.sale_id}",),
        ))

        stage_of_box = {"packed": "consolidation", "split": "split",
                        "merged": "merge", "opened": "delivery"}
        box_verb = {"packed": "归集装箱", "split": "拆箱拆分", "merged": "再次合箱", "opened": "开箱"}

        for ev in self.d.box_events:
            if ev.box_code not in boxes:
                continue
            entries.append(TimelineEntry(
                event_time=ev.event_time,
                stage=stage_of_box[ev.action],
                kind=f"box_{ev.action}",
                province=ev.province,
                summary=(
                    f"{box_verb[ev.action]} {ev.box_code}：{len(ev.parcel_ids)} 件包裹 / "
                    f"{len(ev.waybill_nos)} 份运单{('：' + ev.note) if ev.note else ''}"
                ),
                evidence=(f"box:{ev.box_code}", *ev.waybill_nos),
            ))

        for no in sorted(waybills):
            for wv in self.d.version_history(no):
                reason = wv.revision_reason
                if reason == "initial":
                    stage, verb = "consolidation", "运单签发（首版轨迹）"
                elif reason == "late_correction":
                    stage, verb = "revision", "承运方晚到更正（首版轨迹保留）"
                elif reason == "cancellation":
                    stage, verb = "revision", "运单注销（原轨迹保留）"
                elif reason == "forward":
                    stage, verb = "merge", "转寄，换开新段"
                elif reason == "refusal":
                    stage, verb = "delivery", "拒收"
                else:  # partial_sign
                    stage, verb = "delivery", "部分签收"
                entries.append(TimelineEntry(
                    event_time=wv.event_time,
                    stage=stage,
                    kind=f"waybill_{reason}",
                    province="",
                    summary=(
                        f"{verb}：{wv.carrier} {no} v{wv.version_no}；"
                        f"寄件人 {wv.sender.display or wv.sender.ref} / "
                        f"揽收网点 {wv.pickup_outlet.outlet_code or wv.pickup_outlet.ref} / "
                        f"收件昵称 {wv.receiver.display or wv.receiver.ref}；"
                        f"包裹 {len(wv.parcel_ids)} 件"
                    ),
                    evidence=(f"waybill:{no}#v{wv.version_no}", f"receipt:{wv.receipt_id}"),
                    late=self._is_late(wv.event_time, wv.recorded_at),
                ))

        for s in sorted(
            (s for s in self.d.scans if s.waybill_no in waybills),
            key=lambda s: s.event_time,
        ):
            stage = {
                "accepted": "consolidation",
                "in_transit": "cross_province",
                "transfer": "cross_province",
                "merge": "merge",
                "arrived": "cross_province",
                "out_for_delivery": "delivery",
                "signed": "delivery",
                "partial_signed": "delivery",
                "refused": "delivery",
                "forwarded": "merge",
                "cancelled": "revision",
            }[s.scan_type]
            entries.append(TimelineEntry(
                event_time=s.event_time, stage=stage, kind=f"scan_{s.scan_type}",
                province=s.province,
                summary=f"{s.node_code} 扫描 {s.scan_type}"
                        + (f"（车次 {s.vehicle_batch_no}）" if s.vehicle_batch_no else ""),
                evidence=(f"scan:{s.scan_id}", f"waybill:{s.waybill_no}"),
            ))

        for b in sorted((self.d.vehicle_batches[k] for k in batches), key=lambda b: b.departed_at):
            cross = "跨省运输" if b.origin_province != b.dest_province else "省内运输"
            entries.append(TimelineEntry(
                event_time=b.departed_at,
                stage="cross_province",
                kind="vehicle_batch",
                province=b.origin_province,
                summary=(
                    f"车次 {b.batch_no}（{b.vehicle_code}）{cross}："
                    f"{b.origin_province}→{b.dest_province}，载 {len(b.waybill_nos)} 份运单"
                ),
                evidence=(f"vehicle:{b.batch_no}",),
            ))

        entries.sort(key=lambda e: e.event_time)
        return entries

    # ------------------------------------------------------- 缺口分析

    def _scan_gaps(self, waybills, parcels):
        missing: list[MissingNode] = []
        scans_by_parcel: dict[str, list] = defaultdict(list)
        scans_by_waybill: dict[str, list] = defaultdict(list)
        for s in self.d.scans:
            if s.waybill_no not in waybills:
                continue
            scans_by_waybill[s.waybill_no].append(s)
            if s.parcel_id:
                scans_by_parcel[s.parcel_id].append(s)

        # 运单级：揽收与终态
        provinces_seen: dict[str, set[str]] = {}
        for no in sorted(waybills):
            wv = self.d.latest_version(no)
            if wv is None:
                continue
            scans = sorted(scans_by_waybill.get(no, []), key=lambda s: s.event_time)
            types = {s.scan_type for s in scans}
            if not scans or "accepted" not in types:
                missing.append(self._gap(
                    f"waybill:{no}", "accepted", "未见揽收扫描，归集网点无法确认",
                    wv.carrier, no, "scan",
                ))
            terminal = types & TERMINAL_SCANS
            # 注销/拒收以版本为准；转寄段本身不签收，终态由承接包裹的后续段给出
            handed_off = wv.revision_reason == "forward" and self._continued_elsewhere(
                no, set(wv.parcel_ids), waybills
            )
            if not terminal and wv.revision_reason not in ("cancellation", "refusal") \
                    and not handed_off:
                if wv.revision_reason == "forward":
                    detail = "运单转寄后未发现承接的后续运单，链条断点"
                    node = "forward_continuation"
                else:
                    detail = "全单未见签收/拒收/注销等终态"
                    node = "terminal_scan"
                missing.append(self._gap(
                    f"waybill:{no}", node, detail,
                    wv.carrier, no, "scan",
                ))
            provinces_seen[no] = {s.province for s in scans}

            # 跨省必须有车次凭证（扫描出现在多省却无车次关联）
            if len(provinces_seen[no]) >= 2:
                covered = {
                    bno for bno, b in self.d.vehicle_batches.items() if no in b.waybill_nos
                }
                scanned_batches = {s.vehicle_batch_no for s in scans if s.vehicle_batch_no}
                if not (covered | scanned_batches):
                    missing.append(self._gap(
                        f"waybill:{no}", "cross_province_vehicle",
                        f"扫描跨越 {sorted(provinces_seen[no])}，但无车辆批次凭证",
                        wv.carrier, no, "vehicle",
                    ))

        # 包裹级终态；部分签收版本下没有终态扫描的包裹即残件
        unsigned: list[str] = []
        partial_waybills = {
            no for no in waybills
            if (wv := self.d.latest_version(no)) is not None
            and wv.revision_reason == "partial_sign"
        }
        for pid in sorted(parcels):
            scans = scans_by_parcel.get(pid, [])
            types = {s.scan_type for s in scans}
            if not (types & TERMINAL_SCANS):
                owner = self._owner_waybill(pid, waybills)
                wv = self.d.latest_version(owner) if owner else None
                if wv is not None and not (
                    {x.scan_type for x in scans_by_waybill.get(owner, [])}
                    & {"refused", "cancelled"}
                ):
                    node = "partial_sign_residual" if owner in partial_waybills else "terminal_scan"
                    detail = ("部分签收后残件，下落待查" if owner in partial_waybills
                              else "包裹未见任何终态扫描")
                    missing.append(self._gap(
                        f"parcel:{pid}", node, detail, wv.carrier, owner, "scan",
                    ))
                    unsigned.append(pid)
        return missing, unsigned

    def _weight_checks(self, parcels, waybills) -> list[WeightAnomaly]:
        anomalies: list[WeightAnomaly] = []
        box_windows = self._split_merge_windows(parcels)

        by_parcel: dict[str, list] = defaultdict(list)
        for w in self.d.weights:
            if w.parcel_id in parcels:
                by_parcel[w.parcel_id].append(w)

        for pid in sorted(parcels):
            obs = sorted(by_parcel.get(pid, []), key=lambda w: w.event_time)
            owner = self._owner_waybill(pid, waybills)
            if not obs:
                wv = self.d.latest_version(owner) if owner else None
                if wv is not None:
                    status = assess_gap(
                        self.d.authorizations, self.d.receipts,
                        carrier=wv.carrier, category="weight", waybill_no=owner,
                    )
                    anomalies.append(WeightAnomaly(
                        parcel_id=pid,
                        detail=f"全程无称量记录（{status}）",
                        evidence=(f"waybill:{owner}",),
                    ))
                continue

            for prev, curr in zip(obs, obs[1:]):
                delta = abs(curr.weight_kg - prev.weight_kg)
                tol = max(self.weight_abs_tol, self.weight_rel_tol * max(prev.weight_kg, 1e-9))
                between = any(
                    prev.event_time <= t <= curr.event_time for t in box_windows.get(pid, [])
                )
                if delta > tol and not between:
                    anomalies.append(WeightAnomaly(
                        parcel_id=pid,
                        detail=(
                            f"{prev.node_code}({prev.weight_kg}kg)→{curr.node_code}"
                            f"({curr.weight_kg}kg) 差 {delta:.2f}kg，期间无拆/并箱记录"
                        ),
                        evidence=(f"weight@{prev.node_code}", f"weight@{curr.node_code}"),
                    ))
        return anomalies

    def _legal_gaps(self, waybills, existing_gaps) -> list[MissingNode]:
        """对链条需要但当前缺失的数据类别，逐项标注缺授权还是缺回执。"""
        gaps: list[MissingNode] = []
        seen = {(g.subject, g.node) for g in existing_gaps}
        needed = ("scan", "weight", "box", "vehicle")
        for no in sorted(waybills):
            wv = self.d.latest_version(no)
            if wv is None:
                continue
            have = {
                "scan": any(s.waybill_no == no for s in self.d.scans),
                "weight": any(w.waybill_no == no for w in self.d.weights),
                "box": any(no in b.waybill_nos for b in self.d.box_events),
                "vehicle": any(no in b.waybill_nos for b in self.d.vehicle_batches.values()),
            }
            for cat in needed:
                if have[cat]:
                    continue
                status = assess_gap(
                    self.d.authorizations, self.d.receipts,
                    carrier=wv.carrier, category=cat, waybill_no=no,
                )
                if status == "awaiting_carrier_data":
                    continue  # 手续齐备，只是数据未回传，不算授权缺口
                key = (f"waybill:{no}", f"missing_{cat}")
                if key in seen:
                    continue
                gaps.append(MissingNode(
                    subject=f"waybill:{no}",
                    node=f"missing_{cat}",
                    detail=f"缺少 {cat} 类材料",
                    carrier=wv.carrier,
                    waybill_no=no,
                    category=cat,
                    legal_status=status,
                ))
        return gaps

    # ------------------------------------------------------- 杂项

    def _version_notes(self, waybills) -> list[str]:
        notes: list[str] = []
        for no in sorted(waybills):
            hist = self.d.version_history(no)
            for wv in hist[1:]:
                notes.append(
                    f"{no} v{wv.version_no}（{wv.revision_reason}，{wv.event_time}）："
                    "更正/注销/转寄/拒收/部分签收不覆盖旧版，原轨迹保留"
                )
        return notes

    def _split_merge_windows(self, parcels) -> dict[str, list[str]]:
        windows: dict[str, list[str]] = defaultdict(list)
        for ev in self.d.box_events:
            if ev.action in ("split", "merged", "packed", "opened"):
                for pid in ev.parcel_ids:
                    if pid in parcels:
                        windows[pid].append(ev.event_time)
        return windows

    def _owner_waybill(self, parcel_id, waybills) -> str | None:
        for no in sorted(waybills):
            wv = self.d.latest_version(no)
            if wv is not None and parcel_id in wv.parcel_ids:
                return no
        # 历史版本挂过也算
        for no in sorted(waybills):
            if any(parcel_id in v.parcel_ids for v in self.d.waybill_versions.get(no, [])):
                return no
        return None

    def _continued_elsewhere(self, waybill_no: str, parcel_ids: set[str], all_waybills) -> bool:
        """转寄段的包裹是否由另一份运单（在转寄时点之后）承接。"""
        forward_time = next(
            (v.event_time for v in self.d.version_history(waybill_no)
             if v.revision_reason == "forward"),
            None,
        )
        for other in all_waybills:
            if other == waybill_no:
                continue
            for v in self.d.waybill_versions.get(other, []):
                if set(v.parcel_ids) & parcel_ids and (
                    forward_time is None or v.event_time >= forward_time
                ):
                    return True
        return False

    def _gap(self, subject, node, detail, carrier, waybill_no, category) -> MissingNode:
        status = assess_gap(
            self.d.authorizations, self.d.receipts,
            carrier=carrier, category=category, waybill_no=waybill_no,
        )
        return MissingNode(
            subject=subject, node=node, detail=detail,
            carrier=carrier, waybill_no=waybill_no,
            category=category, legal_status=status,
        )

    def _is_late(self, event_time: str, recorded_at: str) -> bool:
        from .models import parse_ts
        delta = parse_ts(recorded_at) - parse_ts(event_time)
        return delta.total_seconds() > self.late_record_days * 86400
