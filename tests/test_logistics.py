"""端到端与单元测试：版本轨迹、合法调取、候选关联、冻结去重、倒查重放。

运行：python -m unittest discover -s tests
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.logistics import (
    Authorization,
    CandidateLink,
    Dossier,
    DuplicateFreeze,
    FreezeLedger,
    LinkEngine,
    LegalAccessError,
    QueryReceipt,
    Replayer,
)
from src.logistics.freeze import batch_fingerprint
from src.logistics.load import load_bundle
from src.logistics.models import (
    BoxEvent,
    PartyRef,
    PartyRole,
    SaleSeed,
    ScanEvent,
    WaybillVersion,
    WeightObservation,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "logistics_case.json"

try:
    import fcntl
except ImportError:
    fcntl = None


def loaded_dossier(tmpdir) -> Dossier:
    d = Dossier(Path(tmpdir) / "case.jsonl")
    load_bundle(FIXTURE, d)
    return d


class FixtureReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = loaded_dossier(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_expansion_discovers_full_chain(self):
        seed = self.d.seeds["sale-2026-001"]
        result = Replayer(self.d).replay(seed)
        # 从一条售药发货记录出发，扩展到两家承运方、四份运单、三个车次
        self.assertEqual(
            set(result.discovered_waybills),
            {"SSD-W001", "SSD-W005", "JYT-W002", "JYT-W003"},
        )
        self.assertEqual(set(result.discovered_parcels), {"P-1", "P-2", "P-3", "P-4"})
        self.assertEqual(set(result.discovered_boxes), {"BOX-7", "BOX-9"})
        self.assertEqual(
            set(result.discovered_batches), {"SSD-V-01", "JYT-V-02", "JYT-V-03"}
        )

    def test_timeline_stages_in_order(self):
        seed = self.d.seeds["sale-2026-001"]
        result = Replayer(self.d).replay(seed)
        stages = [e.stage for e in result.timeline]
        self.assertEqual(stages[0], "seed")
        self.assertIn("consolidation", stages)   # 归集
        self.assertIn("split", stages)           # 拆分
        self.assertIn("cross_province", stages)  # 跨省
        self.assertIn("merge", stages)           # 外省再合并
        self.assertIn("delivery", stages)        # 签收/拒收
        times = [e.event_time for e in result.timeline]
        self.assertEqual(times, sorted(times))

    def test_partial_sign_leaves_residual(self):
        result = Replayer(self.d).replay(self.d.seeds["sale-2026-001"])
        self.assertEqual(result.unsigned_parcels, ["P-2"])
        nodes = {(g.subject, g.node) for g in result.missing_nodes}
        self.assertIn(("parcel:P-2", "partial_sign_residual"), nodes)

    def test_weight_anomaly_without_box_event(self):
        result = Replayer(self.d).replay(self.d.seeds["sale-2026-001"])
        anomaly_parcels = {a.parcel_id for a in result.weight_anomalies}
        # P-4 在 1.00→1.80kg 之间没有拆/并箱事件（装箱拆分发生在称量之前）
        self.assertIn("P-4", anomaly_parcels)
        # P-1/P-2/P-3 重量平稳，不应告警
        self.assertNotIn("P-1", anomaly_parcels)
        self.assertNotIn("P-3", anomaly_parcels)

    def test_gap_distinguishes_missing_receipt(self):
        result = Replayer(self.d).replay(self.d.seeds["sale-2026-001"])
        gap = next(
            g for g in result.legal_gaps
            if g.waybill_no == "JYT-W003" and g.category == "weight"
        )
        # 授权覆盖重量类别，但从未就 JYT-W003 取过重量回执
        self.assertEqual(gap.legal_status, "missing_receipt")

    def test_narrative_and_unresolved(self):
        result = Replayer(self.d).replay(self.d.seeds["sale-2026-001"])
        self.assertFalse(result.fully_resolved)
        text = result.narrative()
        self.assertIn("残件", text)
        self.assertIn("P-2", text)


class VersionHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = loaded_dossier(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_late_correction_keeps_original_track(self):
        hist = self.d.version_history("SSD-W001")
        self.assertEqual([v.version_no for v in hist], [1, 2, 3])
        # 首版面单写的是东风路营业点，晚到更正为城郊工业园营业点——两者都在
        self.assertEqual(hist[0].pickup_outlet.outlet_code, "SSD-OUT-12")
        self.assertEqual(hist[1].revision_reason, "late_correction")
        self.assertEqual(hist[1].pickup_outlet.outlet_code, "SSD-OUT-09")
        # latest 只反映"当前版本"，原轨迹仍可逐版读取
        self.assertEqual(self.d.latest_version("SSD-W001").version_no, 3)
        self.assertEqual(
            self.d.latest_version("SSD-W001").revision_reason, "partial_sign"
        )

    def test_cancellation_and_refusal_preserved(self):
        self.assertEqual(
            self.d.latest_version("SSD-W005").revision_reason, "cancellation"
        )
        self.assertEqual(
            self.d.latest_version("JYT-W003").revision_reason, "refusal"
        )
        self.assertEqual(len(self.d.version_history("SSD-W005")), 2)

    def test_versions_are_append_only(self):
        wv = self.d.version_history("SSD-W001")[0]
        with self.assertRaises(ValueError):
            self.d.add_waybill_version(wv)  # 同版本号不得覆盖

        kwargs = dict(
            waybill_no="SSD-W999", carrier="SSD", version_no=2,
            revision_reason="late_correction",
            sender=wv.sender, receiver=wv.receiver, pickup_outlet=wv.pickup_outlet,
            parcel_ids=("PX",), event_time="2026-03-01T08:00:00+08:00",
            recorded_at="2026-03-12T15:00:00+08:00",
        )
        # 缺首版不能直接上 v2
        with self.assertRaises(ValueError):
            self.d.add_waybill_version(WaybillVersion(receipt_id="RC-SSD-W001", **kwargs))

    def test_journal_survives_reopen(self):
        path = Path(self.tmp.name) / "case.jsonl"
        again = Dossier(path)
        self.assertEqual(len(again.version_history("SSD-W001")), 3)
        self.assertEqual(len(again.scans), 17)
        self.assertIn("sale-2026-001", again.seeds)


class LegalAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = loaded_dossier(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _weight(self, receipt_id="RC-SSD-W001", waybill="SSD-W001", carrier="SSD"):
        return WeightObservation(
            parcel_id="P-X", waybill_no=waybill, node_code="N",
            weight_kg=1.0, event_time="2026-03-01T08:00:00+08:00",
            recorded_at="2026-03-12T15:00:00+08:00", receipt_id=receipt_id,
        )

    def test_missing_receipt_rejected(self):
        with self.assertRaises(LegalAccessError):
            self.d.add_weight(self._weight(receipt_id="RC-NOPE"))

    def test_carrier_mismatch_rejected(self):
        w = WeightObservation(
            parcel_id="P-X", waybill_no="JYT-W002", node_code="N", weight_kg=1.0,
            event_time="2026-03-01T08:00:00+08:00",
            recorded_at="2026-03-13T11:00:00+08:00",
            receipt_id="RC-SSD-W001",  # SSD 回执不能佐证 JYT 运单
        )
        with self.assertRaises(LegalAccessError):
            self.d.add_weight(w)

    def test_scope_rejects_unlisted_waybill(self):
        # AUTH-01 只列明 SSD-W001/W005；登记一份指向 W777 的回执必须被拒
        receipt = QueryReceipt(
            receipt_id="RC-SSD-W777", auth_id="AUTH-01", carrier="SSD",
            waybill_no="SSD-W777", requested_at="2026-03-10T09:00:00+08:00",
            received_at="2026-03-12T15:00:00+08:00",
            categories=("waybill",),
        )
        with self.assertRaises(LegalAccessError):
            self.d.add_receipt(receipt)

    def test_revoked_authorization_rejects(self):
        self.d.revoke_authorization("AUTH-01")
        with self.assertRaises(LegalAccessError):
            self.d.add_receipt(QueryReceipt(
                receipt_id="RC-X", auth_id="AUTH-01", carrier="SSD",
                waybill_no="SSD-W001", requested_at="2026-03-20T09:00:00+08:00",
                received_at="2026-03-20T10:00:00+08:00", categories=("waybill",),
            ))
        # 撤销本身也留痕，旧授权不删除
        self.assertTrue(self.d.authorizations["AUTH-01"].revoked)

    def test_receipt_outside_validity_window_rejected(self):
        with self.assertRaises(LegalAccessError):
            self.d.add_receipt(QueryReceipt(
                receipt_id="RC-OLD", auth_id="AUTH-01", carrier="SSD",
                waybill_no="SSD-W001", requested_at="2027-01-10T09:00:00+08:00",
                received_at="2027-01-10T10:00:00+08:00", categories=("waybill",),
            ))


class CandidateLinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = loaded_dossier(self.tmp.name)
        self.engine = LinkEngine(self.d)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_phone_is_only_weak_candidate_and_parties_stay_distinct(self):
        candidates = self.engine.attribute_candidates()
        phone = next(c for c in candidates if c.kind == "shared_phone")
        self.assertEqual(phone.strength, "weak")
        self.assertEqual(phone.shared_value, "13800000001")
        # 同一手机号出现在三家面单的寄件人位置，引用彼此独立，
        # 系统没有也无法把它们"认定为同一人"
        self.assertEqual(
            set(phone.party_refs),
            {"SSD-W001#sender", "SSD-W005#sender", "JYT-W002#sender"},
        )
        self.assertTrue(phone.attribute_match_is_not_identity)
        self.assertIn("候选", phone.as_lead())

    def test_shared_nickname_with_different_phone_still_weak(self):
        candidates = self.engine.attribute_candidates()
        nick = next(
            c for c in candidates
            if c.kind == "shared_nick" and c.shared_value == "城北老李"
        )
        # "城北老李"在两份运单上手机号不同（…0099 vs …0055），仍只是候选
        self.assertEqual(nick.strength, "weak")
        self.assertEqual(
            set(nick.party_refs), {"SSD-W001#receiver", "JYT-W003#receiver"}
        )

    def test_physical_candidates_from_box_and_vehicle(self):
        physical = self.engine.physical_candidates()
        kinds = {c.kind for c in physical}
        self.assertIn("same_box", kinds)
        self.assertIn("same_vehicle_batch", kinds)
        merge = next(c for c in physical if c.shared_value == "BOX-9")
        # 再合箱连接的是 SSD 与 JYT 两侧的主体
        self.assertTrue(
            {"SSD-W001#receiver", "JYT-W003#receiver"} <= set(merge.party_refs)
        )
        for c in physical:
            self.assertTrue(c.attribute_match_is_not_identity)

    def test_no_api_asserts_identity(self):
        # 引擎的全部输出都是候选；不存在任何"判定同一主体"的字段/方法
        for c in self.engine.all_candidates():
            self.assertIsInstance(c, CandidateLink)
            self.assertIn(c.strength, ("weak", "physical"))


class FreezeLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "freeze.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fingerprint_is_order_insensitive(self):
        a = batch_fingerprint(["m1", "m2", "m3"])
        b = batch_fingerprint(["m3", "m1", "m2", "m1"])  # 乱序+重复
        self.assertEqual(a, b)

    def test_same_batch_cannot_be_frozen_by_two_teams(self):
        with FreezeLedger(self.path) as ledger:
            r1 = ledger.freeze(
                case_id="C", team_id="team-dongjiang",
                material_keys=["SSD-W001", "JYT-W002"], reason="并案扩线",
            )
            self.assertTrue(r1.created)
            with self.assertRaises(DuplicateFreeze) as ctx:
                ledger.freeze(
                    case_id="C", team_id="team-xilin",
                    material_keys=["JYT-W002", "SSD-W001"],
                )
            self.assertIn("team-dongjiang", str(ctx.exception))

    def test_same_team_resubmit_is_idempotent(self):
        with FreezeLedger(self.path) as ledger:
            r1 = ledger.freeze(case_id="C", team_id="t1", material_keys=["a", "b"])
            r2 = ledger.freeze(case_id="C", team_id="t1", material_keys=["b", "a"])
            self.assertFalse(r2.created)
            self.assertEqual(r1.record.freeze_id, r2.record.freeze_id)
            self.assertEqual(len(ledger.all_records()), 1)

    def test_partial_overlap_warns_but_allowed(self):
        with FreezeLedger(self.path) as ledger:
            ledger.freeze(case_id="C", team_id="t1", material_keys=["a", "b", "c"])
            r = ledger.freeze(case_id="C", team_id="t2", material_keys=["c", "d"])
            self.assertTrue(r.created)
            self.assertEqual(len(r.warnings), 1)
            self.assertIn("t1", r.warnings[0])

    @unittest.skipIf(fcntl is None, "平台无 fcntl")
    def test_cross_process_file_lock(self):
        ledger = FreezeLedger(self.path)
        ledger.__enter__()
        try:
            # 另开一个句柄以非阻塞方式抢同一把锁，必须失败 → 并行小组互斥
            fh = open(self.path.with_suffix(".jsonl.lock"), "w")
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.close()
        finally:
            ledger.__exit__(None, None, None)

    def test_ledger_persists(self):
        with FreezeLedger(self.path) as ledger:
            ledger.freeze(case_id="C", team_id="t1", material_keys=["a"])
        with FreezeLedger(self.path) as ledger:
            self.assertEqual(len(ledger.all_records()), 1)
            with self.assertRaises(DuplicateFreeze):
                ledger.freeze(case_id="C", team_id="t2", material_keys=["a"])


if __name__ == "__main__":
    unittest.main()
