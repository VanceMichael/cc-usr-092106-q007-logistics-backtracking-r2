"""候选关联。

红线：系统只**提出**候选关联。同手机号、同地址、同昵称只说明“值得并线核查”，
不代表同一主体，更不代表同案参与。本模块刻意不提供任何“合并主体”的接口；

- 候选默认 ``pending``；
- 侦查员只能把它 ``accept``（列为扩线线索）或 ``rule_out``（排除），
  两种决定都记录人和时间，且 accept 仍然不是身份认定；
- 同一对主体因多个信号反复共现，只会累积证据与权重，状态不会自动变成确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from .casefile import CaseFile
from .events import latest_fields


class LinkReason(str, Enum):
    SHARED_CONTACT = "shared_contact"       # 不同主体留过同一联系方式
    SHARED_ADDRESS = "shared_address"       # 不同主体用过同一地址代号
    SHARED_BOX = "shared_box"               # 货物在同一箱/批次中归集
    SHARED_BATCH = "shared_vehicle_batch"   # 搭乘同一车次
    NICKNAME_MATCH = "nickname_match"       # 网络昵称相同（弱信号）
    PICKUP_MISMATCH = "pickup_mismatch"     # 面单寄件地与实际揽收网点不一致（观察项）


class LinkDecision(str, Enum):
    PENDING = "pending"
    ACCEPTED_LEAD = "accepted_lead"  # 列为扩线线索，仍非身份认定
    RULED_OUT = "ruled_out"


# 权重只用于排序线索强弱，不产生任何自动结论。
_REASON_WEIGHT = {
    LinkReason.SHARED_CONTACT: 3,
    LinkReason.SHARED_ADDRESS: 2,
    LinkReason.SHARED_BOX: 3,
    LinkReason.SHARED_BATCH: 1,
    LinkReason.NICKNAME_MATCH: 1,
    LinkReason.PICKUP_MISMATCH: 1,
}


@dataclass
class CandidateLink:
    """两个“行为方”之间的一条候选关联。"""

    key: tuple[str, str]  # 两个行为方键，已排序去重
    reason: LinkReason
    weight: int
    shared_ref: str  # 共现的联系方式/地址/箱/车次 id；观察项可为空
    evidence: list[str] = field(default_factory=list)  # 运单号等出处
    decision: LinkDecision = LinkDecision.PENDING
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None

    @property
    def actor_a(self) -> str:
        return self.key[0]

    @property
    def actor_b(self) -> str:
        return self.key[1]


class CandidateBook:
    def __init__(self) -> None:
        # (actor_a, actor_b, reason, shared_ref) -> CandidateLink
        self._links: dict[tuple[str, str, LinkReason, str], CandidateLink] = {}

    def add(
        self,
        actor_a: str,
        actor_b: str,
        reason: LinkReason,
        *,
        shared_ref: str = "",
        evidence: str = "",
    ) -> CandidateLink:
        """登记一条候选；同一组合再次共现时累积证据，不改变决定状态。"""

        if actor_a == actor_b:
            raise ValueError("候选关联必须发生在两个不同行为方之间")
        key = tuple(sorted((actor_a, actor_b)))
        slot = (key[0], key[1], reason, shared_ref)
        link = self._links.get(slot)
        if link is None:
            link = CandidateLink(
                key=key,
                reason=reason,
                weight=_REASON_WEIGHT[reason],
                shared_ref=shared_ref,
            )
            self._links[slot] = link
        if evidence and evidence not in link.evidence:
            link.evidence.append(evidence)
        return link

    def decide(
        self,
        actor_a: str,
        actor_b: str,
        reason: LinkReason,
        decision: LinkDecision,
        *,
        decided_by: str,
        decided_at: str,
        shared_ref: str = "",
        note: str = "",
    ) -> CandidateLink:
        """记录侦查员对候选的判断；不允许“认定同一人”这类决定。"""

        key = tuple(sorted((actor_a, actor_b)))
        slot = (key[0], key[1], reason, shared_ref)
        if slot not in self._links:
            raise KeyError("候选不存在，不能对未提出的关联作决定")
        link = self._links[slot]
        link.decision = decision
        link.decided_by = decided_by
        link.decided_at = decided_at
        link.decision_note = note
        return link

    def links(self, *, include_decided: bool = True) -> list[CandidateLink]:
        """按权重降序返回候选；默认含已作决定的，便于审计。"""

        out = list(self._links.values())
        if not include_decided:
            out = [l for l in out if l.decision == LinkDecision.PENDING]
        return sorted(out, key=lambda l: (-l.weight, l.actor_a, l.actor_b))

    def pairs(self) -> list[tuple[str, str, int]]:
        """汇总两个行为方之间的全部待核信号强度（仍为候选，不是结论）。"""

        score: dict[tuple[str, str], int] = {}
        for link in self._links.values():
            if link.decision == LinkDecision.RULED_OUT:
                continue
            score[link.key] = score.get(link.key, 0) + link.weight
        return sorted(
            ((a, b, w) for (a, b), w in score.items()),
            key=lambda x: (-x[2], x[0], x[1]),
        )


def _actor_subject(case: CaseFile, subject_id: str | None, role_label: str, wid: str) -> str:
    """行为方键：优先用主体 id；主体未登记者以运单角色占位，绝不互相合并。"""

    if subject_id and subject_id in case.subjects:
        return f"subject:{subject_id}"
    return f"role:{wid}:{role_label}"


def build_candidate_book(case: CaseFile) -> CandidateBook:
    """从全部运单事件流提取候选关联。

    包括：寄件/收件主体与同一联系方式、同一地址代号的共现；同箱归集；
    同车次运输；昵称相同；面单寄件地与实际揽收网点不一致。
    """

    book = CandidateBook()

    # 共现索引：联系方式/地址/昵称/车次角色键 -> (行为方, 运单) 列表
    contact_users: dict[str, list[tuple[str, str]]] = {}
    address_users: dict[str, list[tuple[str, str]]] = {}
    nick_users: dict[str, list[tuple[str, str]]] = {}
    batch_actors: dict[str, list[tuple[str, str]]] = {}
    piece_actors: dict[str, list[tuple[str, str]]] = {}

    for wid, events in case.waybill_events.items():
        fields = latest_fields(events)
        sender = _actor_subject(case, fields.get("sender_subject_id"), "sender", wid)
        receiver = _actor_subject(case, fields.get("receiver_subject_id"), "receiver", wid)
        parties = {"sender": sender, "receiver": receiver}

        for role, actor in parties.items():
            prefix = "sender" if role == "sender" else "receiver"
            cid = fields.get(f"{prefix}_contact_id")
            aid = fields.get(f"{prefix}_address_id")
            name = fields.get(f"{prefix}_name") or ""
            if cid:
                contact_users.setdefault(cid, []).append((actor, wid))
            if aid:
                address_users.setdefault(aid, []).append((actor, wid))
            if name:
                nick_users.setdefault(name, []).append((actor, wid))

        # 寄件人与收件人之间天然存在一条“同单”关系？——不算候选，
        # 一笔交易本身就是合法行为，避免把正常买卖双方互相牵连。

        # 面单寄件地 vs 实际揽收网点：观察项（寄件角色自身的异常信号，
        # 用地址代号与网点所在城市不一致体现，不产生身份结论）。
        sender_addr_id = fields.get("sender_address_id")
        pickup_id = fields.get("pickup_node_id")
        if sender_addr_id and pickup_id:
            addr = case.ref_address(sender_addr_id)
            node = case.ref_node(pickup_id)
            if addr and node and (addr.city != node.city or addr.province != node.province):
                book.add(
                    sender,
                    f"node:{pickup_id}",
                    LinkReason.PICKUP_MISMATCH,
                    shared_ref=f"{sender_addr_id}!={pickup_id}",
                    evidence=wid,
                )

        for ev in events:
            if ev.type == "vehicle_batch":
                bid = ev.payload["batch_id"]
                # 同车次共现只在相同角色之间成立：正常交易的寄收双方
                # 同乘一车不构成彼此的候选关联。
                batch_actors.setdefault(f"{bid}#sender", []).append((sender, wid))
                batch_actors.setdefault(f"{bid}#receiver", []).append((receiver, wid))
            if ev.type in ("packed", "merged"):
                # 谁是该批件的寄件方（归集仓库嫌疑人），按运单寄件人记
                for pid in ev.payload.get("piece_ids", []):
                    piece_actors.setdefault(pid, []).append((sender, wid))
            elif ev.type == "unpacked":
                for pid in ev.payload.get("piece_ids", []):
                    piece_actors.setdefault(pid, []).append((sender, wid))

    def _emit_shared(users: dict, reason: LinkReason):
        for ref_id, usages in users.items():
            actors = sorted({a for a, _ in usages})
            if len(actors) < 2:
                continue
            ev_wids = sorted({w for _, w in usages})
            for i in range(len(actors)):
                for j in range(i + 1, len(actors)):
                    book.add(
                        actors[i], actors[j], reason,
                        shared_ref=ref_id, evidence=ev_wids[0],
                    )

    _emit_shared(contact_users, LinkReason.SHARED_CONTACT)
    _emit_shared(address_users, LinkReason.SHARED_ADDRESS)
    _emit_shared(nick_users, LinkReason.NICKNAME_MATCH)
    _emit_shared(batch_actors, LinkReason.SHARED_BATCH)

    # 同箱归集：出现在同一批 piece 集合里的不同寄件行为方
    box_groups: dict[frozenset[str], list[tuple[str, str]]] = {}
    for pid, usages in piece_actors.items():
        actors = frozenset(a for a, _ in usages)
        if len(actors) > 1:
            box_groups.setdefault(actors, []).extend(usages)
    for actors, usages in box_groups.items():
        actors_l = sorted(actors)
        wid = sorted({w for _, w in usages})[0]
        for i in range(len(actors_l)):
            for j in range(i + 1, len(actors_l)):
                book.add(actors_l[i], actors_l[j], LinkReason.SHARED_BOX, evidence=wid)

    return book
