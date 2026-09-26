import json
from pathlib import Path

import pytest

from src.logistics import (
    CandidateBook,
    FreezeConflict,
    FreezeRegistry,
    LinkDecision,
    load_casefile,
    replay_sale,
)
from src.logistics.events import (
    EventError,
    field_versions,
    latest_fields,
    waybill_status,
)
from src.logistics.freezes import AuthorizationError, MaterialKind
from src.logistics.replay import GapKind, material_fingerprints_for_replay

FIXTURE = Path("fixtures/case-007.json")


@pytest.fixture()
def case():
    return load_casefile(FIXTURE)


# ---------- 加载与引用完整性 ----------

def test_fixture_loads(case):
    assert case.case_id == "case-007"
    assert set(case.waybill_events) == {"w1", "w2", "w3", "w4", "w5", "w6"}


def test_rejects_bad_domain(tmp_path):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["domain"] = "other"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_casefile(p)


def test_event_time_must_not_go_backwards():
    from src.logistics.casefile import _load_waybill_events

    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wb = next(w for w in data["waybills"] if w["id"] == "w1")
    wb["events"][2]["at"] = "2030-01-01T00:00:00"
    with pytest.raises(EventError):
        _load_waybill_events([wb])


def test_unknown_registered_field_rejected(tmp_path):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["waybills"][0]["events"][0]["payload"]["id_card_no"] = "..."
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(Exception):
        load_casefile(p)


# ---------- 版本留痕：更正不覆盖原值 ----------

def test_correction_keeps_all_versions(case):
    versions = field_versions(case.waybill_events["w2"])
    weights = [v["value"] for v in versions["declared_weight_g"]]
    assert weights == [600, 500]  # 原始申报值仍在
    assert latest_fields(case.waybill_events["w2"])["declared_weight_g"] == 500


def test_correction_requires_fields():
    from src.logistics.events import event_from_dict

    with pytest.raises(EventError):
        event_from_dict({
            "seq": 2, "waybill_id": "w", "type": "correction",
            "at": "2026-03-03T09:00:00", "source": "x",
        })


def test_terminal_states_preserve_original_track(case):
    # w3 转寄后仍可折叠，且原轨迹完整保留
    status = waybill_status(case.waybill_events["w3"])
    assert status["state"] == "forwarded"
    assert status["forward_to"] == "w5"
    assert len(case.waybill_events["w3"]) == 7  # 原轨迹一条不少

    status5 = waybill_status(case.waybill_events["w5"])
    assert status5["state"] == "rejected"

    status6 = waybill_status(case.waybill_events["w6"])
    assert status6["state"] == "partially_signed"
    assert status6["signed_piece_ids"] == ["p1"]
    assert status6["unsigned_piece_ids"] == ["p3"]


# ---------- 候选关联：只提候选，不认定同一 ----------

def test_shared_contact_is_only_candidate(case):
    from src.logistics.associations import build_candidate_book, LinkReason

    book = build_candidate_book(case)
    shared_c1 = [
        l for l in book.links()
        if l.reason == LinkReason.SHARED_CONTACT and l.shared_ref == "c1"
    ]
    assert len(shared_c1) == 1
    link = shared_c1[0]
    assert {link.actor_a, link.actor_b} == {"subject:s1", "subject:s2"}
    assert link.decision == LinkDecision.PENDING
    # 再多共现也不会自动确认，只累积证据
    book.add(link.actor_a, link.actor_b, LinkReason.SHARED_CONTACT,
             shared_ref="c1", evidence="wX")
    assert link.decision == LinkDecision.PENDING
    # 决定只能是“列为线索/排除”，不存在“认定同一人”
    book.decide(link.actor_a, link.actor_b, LinkReason.SHARED_CONTACT,
                LinkDecision.ACCEPTED_LEAD, decided_by="侦查员甲",
                decided_at="2026-03-06T10:00:00", shared_ref="c1",
                note="并线核查，不等于同一人")
    assert link.decision == LinkDecision.ACCEPTED_LEAD


def test_candidate_book_has_no_identity_merge_api():
    book = CandidateBook()
    # 领域层不提供 merge/unify 之类方法
    assert not hasattr(book, "merge_subjects")
    assert not hasattr(book, "unify")
    with pytest.raises(ValueError):
        book.add("subject:x", "subject:x", __import__("src.logistics.associations", fromlist=["LinkReason"]).LinkReason.SHARED_CONTACT)


def test_pickup_mismatch_emitted(case):
    from src.logistics.associations import build_candidate_book, LinkReason

    book = build_candidate_book(case)
    mismatches = [l for l in book.links() if l.reason == LinkReason.PICKUP_MISMATCH]
    # w4：面单寄件地在长沙，实际揽收在武汉
    assert any(
        set(l.key) == {"subject:s2", "node:n-hb-wh-sf"} for l in mismatches
    )


def test_shared_box_candidate(case):
    from src.logistics.associations import build_candidate_book, LinkReason

    book = build_candidate_book(case)
    box_links = [l for l in book.links() if l.reason == LinkReason.SHARED_BOX]
    # p1 在武汉由 s2 交出，在东莞代收点由 s3 合并 -> 候选，不是同案结论
    assert any(
        {l.actor_a, l.actor_b} == {"subject:s2", "subject:s3"} for l in box_links
    )


# ---------- 冻结去重 ----------

def _w1_materials():
    return {(MaterialKind.WAYBILL_RECORD, "w1"),
            (MaterialKind.NODE_SCAN_LOG, "n-hn-cs-sf")}


def test_freeze_dedup_across_teams(case):
    reg = FreezeRegistry(case)
    reg.freeze("fz-1", team="湖南组", operator="甲", at="2026-03-06T09:00:00",
               materials=_w1_materials(), authorization_ids={"auth-sf-01"})
    # 湖北组并行扩线冻结同一批材料 -> 拒绝并指明先持有组
    with pytest.raises(FreezeConflict) as exc:
        reg.freeze("fz-2", team="湖北组", operator="乙", at="2026-03-06T09:05:00",
                   materials=_w1_materials(), authorization_ids={"auth-sf-01"})
    assert exc.value.holder_team == "湖南组"
    assert reg.holder_of(MaterialKind.WAYBILL_RECORD, "w1") == "fz-1"


def test_freeze_after_release_allowed(case):
    reg = FreezeRegistry(case)
    reg.freeze("fz-1", team="湖南组", operator="甲", at="2026-03-06T09:00:00",
               materials=_w1_materials(), authorization_ids={"auth-sf-01"})
    reg.release("fz-1", "2026-03-10T09:00:00")
    reg.freeze("fz-3", team="湖北组", operator="乙", at="2026-03-10T10:00:00",
               materials=_w1_materials(), authorization_ids={"auth-sf-01"})
    assert reg.holder_of(MaterialKind.WAYBILL_RECORD, "w1") == "fz-3"


def test_freeze_requires_valid_authorization(case):
    reg = FreezeRegistry(case)
    # DB 同城配（w6）没有任何授权 -> 拒绝冻结
    with pytest.raises(AuthorizationError):
        reg.freeze("fz-x", team="广东组", operator="丙", at="2026-03-06T09:00:00",
                   materials={(MaterialKind.WAYBILL_RECORD, "w6")},
                   authorization_ids={"auth-sf-01"})  # 承运方也不匹配
    with pytest.raises(AuthorizationError):
        reg.freeze("fz-y", team="广东组", operator="丙", at="2026-03-06T09:00:00",
                   materials={(MaterialKind.WAYBILL_RECORD, "w6")},
                   authorization_ids=set())


def test_freeze_wrong_carrier_auth_rejected(case):
    reg = FreezeRegistry(case)
    with pytest.raises(AuthorizationError):
        reg.freeze("fz-z", team="湖北组", operator="丁", at="2026-03-06T09:00:00",
                   materials={(MaterialKind.WAYBILL_RECORD, "w2")},
                   authorization_ids={"auth-sf-01"})


# ---------- 重放与缺口 ----------

def test_replay_full_chain(case):
    r = replay_sale(case, "sale-001")
    # 归集箱 -> 三件 -> 拆成 w2/w3/w4 -> w3 转寄 w5 -> p1/p3 在 w6 外省合并
    assert r.waybill_ids[0] == "w1"
    assert {"w1", "w2", "w3", "w4", "w5", "w6"} == set(r.waybill_ids)
    # 终态
    assert r.piece_outcomes["p1"] == "partially_signed"
    assert r.piece_outcomes["p2"] == "rejected"
    assert r.piece_outcomes["p3"] == "unknown"  # 部分签收中未签的一件
    phases_p1 = [s.phase for s in r.piece_journey["p1"]]
    assert "collected" in phases_p1
    assert "cross_province" in phases_p1
    assert "split" in phases_p1
    assert "merged" in phases_p1
    assert "partially_signed" in phases_p1
    # 至少经过湖南、湖北、广东三省
    provinces = {
        s.detail["province"]
        for steps in r.piece_journey.values() for s in steps
        if "province" in s.detail
    }
    assert {"湖南", "湖北", "广东"} <= provinces


def test_replay_reports_missing_receipt_for_late_correction(case):
    r = replay_sale(case, "sale-001")
    refs = [(g.kind, g.ref) for g in r.gaps]
    # w3 的口头更正没有查询回执
    assert (GapKind.MISSING_RECEIPT, "w3") in refs
    # w2 的更正有回执 -> 不报
    assert (GapKind.MISSING_RECEIPT, "w2") not in refs


def test_replay_reports_missing_authorization(case):
    r = replay_sale(case, "sale-001")
    refs = [(g.kind, g.ref) for g in r.gaps]
    # 转寄运单 w5 与同城配 w6 均无授权
    assert (GapKind.MISSING_AUTHORIZATION, "w5") in refs
    assert (GapKind.MISSING_AUTHORIZATION, "w6") in refs


def test_replay_reports_weight_gap(case):
    r = replay_sale(case, "sale-001")
    # w3 未登记申报重量
    assert any(g.kind == GapKind.MISSING_WEIGHT and g.ref == "w3" for g in r.gaps)
    # w1（1500g）与 w2（更正后500g）勾稽通过
    assert not any(g.ref == "w1" for g in r.gaps if "weight" in g.kind)
    assert not any(g.ref == "w2" for g in r.gaps if g.kind == GapKind.UNEVENT_WEIGHT_MISMATCH)


def test_replay_reports_unresolved_piece(case):
    r = replay_sale(case, "sale-001")
    gaps = [(g.kind, g.ref) for g in r.gaps]
    assert (GapKind.UNRESOLVED_PIECE, "p3") in gaps


def test_replay_material_fingerprints(case):
    r = replay_sale(case, "sale-001")
    fps = material_fingerprints_for_replay(r, case)
    assert (MaterialKind.QUERY_RECEIPT, "w2/q-yt-1") in fps
    assert (MaterialKind.VEHICLE_BATCH_MANIFEST, "b1") in fps
    # 重放得出的材料指纹可直接用于冻结，且仍受去重约束
    reg = FreezeRegistry(case)
    # 未授权材料（w6）混入时整体拒绝
    with pytest.raises(AuthorizationError):
        reg.freeze("fz-all", team="联合组", operator="甲", at="2026-03-06T09:00:00",
                   materials=fps,
                   authorization_ids={"auth-sf-01", "auth-yt-01", "auth-zt-01"})


def test_unknown_sale_raises(case):
    with pytest.raises(KeyError):
        replay_sale(case, "nope")


def test_dangling_forward_reported_as_gap():
    from src.logistics.casefile import CaseFile
    from src.logistics.entities import Piece, Sale
    from src.logistics.events import event_from_dict

    def ev(seq, etype, at, payload):
        return event_from_dict(
            {"seq": seq, "type": etype, "at": at, "source": "x", "payload": payload},
            expected_waybill="w",
        )

    events = [
        ev(1, "registered", "2026-03-01T08:00:00",
           {"carrier": "C", "sender_name": "甲", "receiver_name": "乙"}),
        ev(2, "packed", "2026-03-01T08:10:00", {"piece_ids": ["p"]}),
        ev(3, "forwarded", "2026-03-02T08:00:00",
           {"to_waybill_id": "w-missing"}),
    ]
    case = CaseFile(
        domain="logistics-backtracking", version=1, case_id="t",
        subjects={}, contacts={}, addresses={}, nodes={}, vehicle_batches={},
        pieces={"p": Piece(id="p", description="虚构件", weight_g=100)},
        boxes={},
        sales={"s": Sale(id="s", at="2026-03-01T07:00:00",
                         buyer_nick="乙", piece_ids=("p",))},
        authorizations={},
        waybill_events={"w": events},
        forward_links={"w": "w-missing"},
        dangling_forwards={"w": "w-missing"},
    )
    r = replay_sale(case, "s")
    assert (GapKind.DANGLING_FORWARD, "w") in [(g.kind, g.ref) for g in r.gaps]
    assert r.piece_outcomes["p"] == "forwarded"
