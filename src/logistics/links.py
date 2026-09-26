"""候选关联引擎。

铁律：系统只**提出候选关联**，不做主体同一认定。

- 同手机号、同地址代号、相同昵称文本只生成 ``weak`` 候选；
- 每条候选保留各自独立的主体引用（``PartyRef``），角色不同时尤其不得合并；
- 同箱、同车次、同包裹拆并产生 ``physical`` 候选（实物轨迹相邻，仍非身份结论）；
- 候选上恒带 ``attribute_match_is_not_identity`` 警示，任何下游都不能把它
  当成"同案参与者"的结论。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .store import Dossier


@dataclass(frozen=True)
class CandidateLink:
    kind: str                          # shared_phone / shared_address / shared_nick /
    #                                    same_box / same_vehicle_batch / co_parcel
    strength: str                      # weak | physical
    party_refs: tuple[str, ...]        # 始终是两个或以上不同的 PartyRef.ref
    shared_value: str                  # 触发关联的属性值（号码/代号/箱号/车次号）
    evidence: tuple[str, ...]          # 运单号/箱事件/车次等证据定位
    rationale: str
    attribute_match_is_not_identity: bool = True

    def as_lead(self) -> str:
        """面向侦查员的表述：始终是'候选'口吻。"""
        return (
            f"[候选·{self.strength}] {self.kind}：主体 {'、'.join(self.party_refs)} "
            f"因 {self.rationale} 被一并提出，需人工核实；"
            "属性相同不等于同一主体或同案参与"
        )


@dataclass
class LinkEngine:
    dossier: Dossier

    # 所有候选的汇总（按强度排序，弱关联排后面）
    def all_candidates(self) -> list[CandidateLink]:
        links: list[CandidateLink] = []
        links.extend(self.attribute_candidates())
        links.extend(self.physical_candidates())
        rank = {"physical": 0, "weak": 1}
        return sorted(links, key=lambda c: (rank[c.strength], c.kind))

    # ------------------------------------------------------- 属性型（弱）

    def attribute_candidates(self) -> list[CandidateLink]:
        parties = self._all_parties()
        links: list[CandidateLink] = []

        def emit(buckets: dict[str, list], kind: str, attr: str):
            for value, refs in buckets.items():
                unique = sorted(set(refs))
                if len(unique) < 2:
                    continue
                links.append(
                    CandidateLink(
                        kind=kind,
                        strength="weak",
                        party_refs=tuple(unique),
                        shared_value=value,
                        evidence=self._evidence_for(unique),
                        rationale=f"{attr}相同（{value}）",
                    )
                )

        by_phone: dict[str, list[str]] = defaultdict(list)
        by_address: dict[str, list[str]] = defaultdict(list)
        by_nick: dict[str, list[str]] = defaultdict(list)
        for ref, party in parties.items():
            if party.phone:
                by_phone[party.phone].append(ref)
            if party.address_code:
                by_address[party.address_code].append(ref)
            if party.display:
                by_nick[party.display.strip()].append(ref)

        emit(by_phone, "shared_phone", "手机号")
        emit(by_address, "shared_address", "地址代号")
        emit(by_nick, "shared_nick", "昵称/署名文本")
        return links

    # ----------------------------------------------------- 实物轨迹（强一档，仍只是候选）

    def physical_candidates(self) -> list[CandidateLink]:
        links: list[CandidateLink] = []

        # 同一箱盒事件把多件包裹（可能挂在不同运单上）绑在一起
        for ev in self.dossier.box_events:
            waybills = tuple(dict.fromkeys(ev.waybill_nos))
            parties = self._parties_on_waybills(waybills)
            if len(parties) >= 2:
                links.append(
                    CandidateLink(
                        kind="same_box",
                        strength="physical",
                        party_refs=tuple(sorted(parties)),
                        shared_value=ev.box_code,
                        evidence=(f"box:{ev.box_code}@{ev.event_time}", *waybills),
                        rationale=f"包裹在 {ev.event_time} 于{ev.province}发生 {ev.action}，共处箱盒",
                    )
                )

        # 同一车辆批次跨省
        for batch in self.dossier.vehicle_batches.values():
            parties = self._parties_on_waybills(batch.waybill_nos)
            if len(parties) >= 2:
                links.append(
                    CandidateLink(
                        kind="same_vehicle_batch",
                        strength="physical",
                        party_refs=tuple(sorted(parties)),
                        shared_value=batch.batch_no,
                        evidence=(f"vehicle:{batch.batch_no}", *batch.waybill_nos),
                        rationale=(
                            f"同乘车次 {batch.batch_no}：{batch.origin_province}"
                            f"→{batch.dest_province}"
                        ),
                    )
                )

        # 同一包裹号被多个运单版本引用（转寄/再合并的典型痕迹）
        parcel_waybills: dict[str, set[str]] = defaultdict(set)
        for versions in self.dossier.waybill_versions.values():
            latest = max(versions, key=lambda v: v.version_no)
            for pid in latest.parcel_ids:
                parcel_waybills[pid].add(latest.waybill_no)
        for pid, waybills in parcel_waybills.items():
            if len(waybills) < 2:
                continue
            wb = tuple(sorted(waybills))
            parties = self._parties_on_waybills(wb)
            if len(parties) >= 2:
                links.append(
                    CandidateLink(
                        kind="co_parcel",
                        strength="physical",
                        party_refs=tuple(sorted(parties)),
                        shared_value=pid,
                        evidence=(f"parcel:{pid}", *wb),
                        rationale=f"同一包裹 {pid} 先后挂在多份运单上（可能转寄/再合并）",
                    )
                )
        return links

    # ------------------------------------------------------------- 工具

    def _all_parties(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for versions in self.dossier.waybill_versions.values():
            latest = max(versions, key=lambda v: v.version_no)
            for p in latest.party_refs():
                out.setdefault(p.ref, p)
        return out

    def _parties_on_waybills(self, waybills) -> set[str]:
        refs: set[str] = set()
        for no in waybills:
            wv = self.dossier.latest_version(no)
            if wv is not None:
                refs.update(p.ref for p in wv.party_refs())
        return refs

    def _evidence_for(self, refs: list[str]) -> tuple[str, ...]:
        ev: list[str] = []
        ref_set = set(refs)
        for no, versions in self.dossier.waybill_versions.items():
            wv = max(versions, key=lambda v: v.version_no)
            if ref_set & {p.ref for p in wv.party_refs()}:
                ev.append(f"waybill:{no}")
        return tuple(ev[:10])

    def candidates_about(self, ref: str) -> list[CandidateLink]:
        """供人工核查某主体时使用：只返回包含该引用的候选。"""
        return [c for c in self.all_candidates() if ref in c.party_refs]
