"""异常事件处置引擎测试：撤回、到期、改期、延期、替代。"""

import unittest

from museum_merch import cases, catalog, fulfillment, rights
from museum_merch.store import DomainError
from support import ON, build_world, restock


def _action_map(s, case_id):
    return {r["action_type"]: r for r in s.all(
        "SELECT * FROM case_action WHERE case_id=?", (case_id,))}


class WithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.s = build_world()
        restock(self.s, qty=100)
        for ch in ("museum-store", "official-online-store", "livestream", "pop-up"):
            catalog.list_version(self.s, "v1", ch, "CN", ON)
        self.o_museum = fulfillment.place_order(
            self.s, order_no="OM", channel="museum-store", region="CN",
            items=[{"version_id": "v1", "qty": 6}], on_date=ON)
        self.o_popup = fulfillment.place_order(
            self.s, order_no="OP", channel="pop-up", region="CN",
            items=[{"version_id": "v1", "qty": 4}], on_date=ON)
        item = self.s.one(
            "SELECT id FROM order_item WHERE order_id=?", (self.o_museum,))["id"]
        fulfillment.ship_item(self.s, item_id=item, qty=2, on_date=ON)
        rights.withdraw_license(self.s, "lic1", "家属书面撤回")
        self.case_ids = cases.process_events(self.s, ON)
        self.case_id = self.s.one(
            "SELECT id FROM case_record WHERE kind='rights-withdrawal' LIMIT 1")["id"]

    def test_listings_outside_withdrawn_scope_stop_only_there(self):
        # lic1 覆盖的三个渠道停售；快闪另有 L1-POP，继续在售
        statuses = {r["channel"]: r["status"] for r in self.s.all(
            "SELECT channel,status FROM listing")}
        self.assertEqual(statuses["museum-store"], "stopped")
        self.assertEqual(statuses["livestream"], "stopped")
        self.assertEqual(statuses["official-online-store"], "stopped")
        self.assertEqual(statuses["pop-up"], "active")

    def test_unshipped_refunded_but_shipment_and_commitment_kept(self):
        refunds = self.s.all(
            "SELECT qty,amount_cents FROM refund WHERE version_id='v1'")
        # 馆内 6 件已发 2 件，退剩余 4 件；快闪不在工单范围内不退
        self.assertEqual([(r["qty"], r["amount_cents"]) for r in refunds],
                         [(4, 4 * 10000)])
        # 已发货的 2 件保留（库存只少了这 2 件 + 快闪 4 件预留）
        totals = fulfillment.stock_totals(self.s.conn, "v1")
        self.assertEqual(totals["on_hand"], 98)
        # 采购单已完成，承诺记录仍在
        po = self.s.one("SELECT po_no,status,committed FROM purchase_order LIMIT 1")
        self.assertEqual(po["committed"], 1)
        self.assertIn(po["status"], ("completed", "halted"))

    def test_version_still_sells_in_unaffected_market(self):
        # 主授权撤回只影响其覆盖的三渠道；快闪有独立授权，版本不被整体封锁，
        # 快闪渠道仍可正常下单
        v = self.s.one("SELECT status FROM product_version WHERE id='v1'")
        self.assertNotEqual(v["status"], "blocked")
        oid = fulfillment.place_order(
            self.s, order_no="P2", channel="pop-up", region="CN",
            items=[{"version_id": "v1", "qty": 1}], on_date=ON)
        self.assertTrue(oid)

    def test_case_is_explainable_and_auditable(self):
        case = self.s.one("SELECT title,explanation,scope FROM case_record"
                          " WHERE id=?", (self.case_id,))
        self.assertIn("L1", case["title"])
        actions = _action_map(self.s, self.case_id)
        self.assertIn("delist", actions)
        self.assertIn("refund-unshipped", actions)
        self.assertIn("renew-license", actions)
        # 续展是人工动作，保持 proposed，工单因此仍是 open
        self.assertEqual(actions["renew-license"]["status"], "proposed")
        self.assertEqual(
            self.s.one("SELECT status FROM case_record WHERE id=?",
                       (self.case_id,))["status"], "open")


class ExpiryTest(unittest.TestCase):
    def test_sweep_stops_popup_after_expiry(self):
        s = build_world()
        restock(s, qty=100)
        catalog.list_version(s, "v1", "pop-up", "CN", ON)
        fulfillment.place_order(
            s, order_no="P1", channel="pop-up", region="CN",
            items=[{"version_id": "v1", "qty": 3}], on_date=ON)
        cs = cases.sweep_expiries(s, "2026-11-01")
        self.assertTrue(cs)
        kinds = {s.one("SELECT kind FROM case_record WHERE id=?", (c,))["kind"]
                 for c in cs}
        self.assertIn("rights-expired", kinds)
        self.assertEqual(
            s.one("SELECT status FROM listing WHERE channel='pop-up'")["status"],
            "stopped")
        # 未发货 3 件自动退款
        self.assertEqual(s.one("SELECT COALESCE(SUM(qty),0) n FROM refund")["n"], 3)
        self.assertEqual(
            s.one("SELECT status FROM license WHERE id='lic1-pop'")["status"],
            "expired")


class RescheduleTest(unittest.TestCase):
    def test_reschedule_flags_renewal_but_keeps_commitments(self):
        s = build_world()
        po, _ = restock(s, qty=10)
        rights.reschedule_exhibition(
            s, "ex1", start_date="2027-02-01", end_date="2027-04-30")
        cs = cases.process_events(s, ON)
        case_id = s.one(
            "SELECT id FROM case_record WHERE kind='reschedule' LIMIT 1")["id"]
        actions = _action_map(s, case_id)
        self.assertIn("renew-license", actions)
        # 采购承诺保留，不撤单
        self.assertEqual(
            s.one("SELECT committed FROM purchase_order WHERE id=?", (po,))["committed"],
            1)


class SupplierDelayTest(unittest.TestCase):
    def test_delay_beyond_license_halts_but_keeps_po(self):
        s = build_world()
        po = fulfillment.create_po(
            s, po_no="PO2", version_id="v1", supplier_id="sup1",
            qty_ordered=20, unit_cost_cents=3000, expected_at="2026-10-01",
            on_date=ON)
        fulfillment.confirm_po(s, po)
        fulfillment.report_po_delay(s, po, "2027-02-15", "工厂排产延后")
        cases.process_events(s, ON)
        case_id = s.one(
            "SELECT id FROM case_record WHERE kind='supplier-delay' LIMIT 1")["id"]
        actions = _action_map(s, case_id)
        self.assertIn("halt-production", actions)
        self.assertIn("renew-license", actions)
        self.assertEqual(
            s.one("SELECT status,committed FROM purchase_order WHERE id=?", (po,))["status"],
            "halted")
        self.assertEqual(
            s.one("SELECT committed FROM purchase_order WHERE id=?", (po,))["committed"],
            1)

    def test_delay_within_license_just_awaits(self):
        s = build_world()
        po = fulfillment.create_po(
            s, po_no="PO3", version_id="v1", supplier_id="sup1",
            qty_ordered=20, unit_cost_cents=3000, expected_at="2026-10-01",
            on_date=ON)
        fulfillment.confirm_po(s, po)
        fulfillment.report_po_delay(s, po, "2026-10-15", "物流延迟")
        cs = cases.process_events(s, ON)
        case_id = cs[-1]
        actions = _action_map(s, case_id)
        self.assertIn("await", actions)
        self.assertNotIn("halt-production", actions)


class SubstitutionTest(unittest.TestCase):
    def _setup_with_v2(self, preoccupy_v2: int = 0):
        s = build_world()
        restock(s, qty=100)
        for ch in ("museum-store", "livestream"):
            catalog.list_version(s, "v1", ch, "CN", ON)
        v2 = catalog.create_version(
            s, product_id="p1", artwork_id="w1", use_type="reproduction",
            title="方巾v2", price_cents=10000, version_id="v2")
        for ch in ("museum-store", "livestream"):
            catalog.add_version_market(s, v2, ch, "CN")
        rights.review_content(s, v2, "approved", "r")
        # v2 入库 50
        po2 = fulfillment.create_po(
            s, po_no="PO-V2", version_id="v2", supplier_id="sup1",
            qty_ordered=50, unit_cost_cents=3000, expected_at=None, on_date=ON)
        fulfillment.confirm_po(s, po2)
        b2 = fulfillment.produce_batch(s, po_id=po2, qty_produced=50)
        fulfillment.record_qc(s, batch_id=b2, qty_inspected=50, qty_passed=50)
        for ch in ("museum-store", "livestream"):
            catalog.list_version(s, v2, ch, "CN", ON)
        if preoccupy_v2:
            fulfillment.place_order(
                s, order_no="V2-HOLD", channel="museum-store", region="CN",
                items=[{"version_id": "v2", "qty": preoccupy_v2}], on_date=ON)
        fulfillment.place_order(
            s, order_no="M", channel="museum-store", region="CN",
            items=[{"version_id": "v1", "qty": 5}], on_date=ON)
        rights.withdraw_license(s, "lic1", "撤回")
        cases.process_events(s, ON)
        case_id = s.one(
            "SELECT id FROM case_record WHERE kind='rights-withdrawal' LIMIT 1")["id"]
        return s, case_id, v2

    def test_refund_held_until_substitution_decided(self):
        s, case_id, v2 = self._setup_with_v2()
        # 退款挂起、尚未发生
        self.assertEqual(s.one("SELECT COUNT(*) n FROM refund")["n"], 0)
        out = cases.execute_substitution(s, case_id, v2)
        self.assertEqual(sum(x["qty"] for x in out["substituted"]), 5)
        # 转入 v2，仍无退款
        self.assertEqual(s.one("SELECT COUNT(*) n FROM refund")["n"], 0)
        item = s.one(
            "SELECT version_id FROM order_item WHERE order_id="
            "(SELECT id FROM sales_order WHERE order_no='M')")
        self.assertEqual(item["version_id"], "v2")
        # v2 预留 5
        self.assertEqual(fulfillment.stock_totals(s.conn, "v2")["reserved"], 5)

    def test_decline_substitution_triggers_refund(self):
        s, case_id, v2 = self._setup_with_v2()
        cases.decline_substitution(s, case_id, "不替代")
        self.assertEqual(s.one("SELECT COALESCE(SUM(qty),0) n FROM refund")["n"], 5)

    def test_substitution_rejected_when_v2_oversells(self):
        # v2 共 50 件，撤回前先用馆内渠道占 46，余 4 < 待替代 5
        s, case_id, v2 = self._setup_with_v2(preoccupy_v2=46)
        with self.assertRaises(DomainError) as ctx:
            cases.execute_substitution(s, case_id, v2)
        self.assertEqual(ctx.exception.code, "oversell")


if __name__ == "__main__":
    unittest.main()
