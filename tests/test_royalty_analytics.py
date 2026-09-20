"""版税权责发生制、退款冲回与结算测试。"""

import unittest

from museum_merch import catalog, fulfillment, royalty
from support import ON, build_world, restock


class RoyaltyTest(unittest.TestCase):
    def setUp(self):
        self.s = build_world()
        restock(self.s, qty=100)
        catalog.list_version(self.s, "v1", "museum-store", "CN", ON)

    def _ship(self, qty):
        oid = fulfillment.place_order(
            self.s, order_no=f"O{qty}", channel="museum-store", region="CN",
            items=[{"version_id": "v1", "qty": qty}], on_date=ON)
        item = self.s.one("SELECT id FROM order_item WHERE order_id=?", (oid,))["id"]
        fulfillment.ship_item(self.s, item_id=item, qty=qty, on_date=ON)
        return item

    def test_royalty_accrues_at_shipment_by_effective_license_and_shares(self):
        self._ship(10)
        # 净额 10*10000=100000，费率 8% = 8000 分
        rows = self.s.all(
            "SELECT holder_id, amount_cents, net_cents, rate_bps, share_bps, basis"
            " FROM royalty_accrual ORDER BY holder_id")
        amounts = {r["holder_id"]: r["amount_cents"] for r in rows}
        # 家属 70% = 5600；机构 30% = 2400
        self.assertEqual(amounts, {"h1": 5600, "h2": 2400})
        self.assertTrue(all(r["rate_bps"] == 800 for r in rows))
        self.assertTrue(all(r["basis"] == "shipment" for r in rows))

    def test_refund_reverses_accrual_and_settlement_excludes_reversed(self):
        item = self._ship(10)
        before = self.s.one(
            "SELECT COALESCE(SUM(amount_cents),0) n FROM royalty_accrual"
            " WHERE basis='shipment'")["n"]
        self.assertEqual(before, 8000)
        # 退 4 件
        fulfillment.return_shipped(
            self.s, item_id=item, qty=4, reason="瑕疵退货", restock=False)
        net = self.s.one(
            "SELECT COALESCE(SUM(amount_cents),0) n FROM royalty_accrual")["n"]
        # 10 件 8000，退 4 件按 4/10 冲回 3200，余 4800
        self.assertEqual(net, 4800)
        h1 = royalty.settle(
            self.s, holder_id="h1", period_start="2026-09-01",
            period_end="2026-09-30")
        # h1：发货 5600 − 退款冲回 5600*4/10=2240 = 3360
        self.assertEqual(h1["amount_cents"], 3360)
        # 已结算分录不可重复结算
        with self.assertRaises(Exception):
            royalty.settle(
                self.s, holder_id="h1", period_start="2026-09-01",
                period_end="2026-09-30")

    def test_per_unit_royalty(self):
        # 按件计费授权覆盖 museum-store 且到期更远，发货时优先匹配它
        from museum_merch import rights
        rights.create_license(
            self.s, id="lic-unit", code="LU", artwork_id="w1", holder_id="h1",
            uses=["reproduction"], channels=["museum-store"], regions=["CN"],
            valid_from="2026-08-01", valid_until="2028-12-31",
            royalty_per_unit_cents=500,
            evidences=[{"kind": "contract", "document_ref": "u.pdf"}])
        item = self._ship(2)
        rows = self.s.all(
            "SELECT holder_id, amount_cents, rate_bps FROM royalty_accrual"
            " WHERE basis='shipment' ORDER BY created_at DESC")
        latest = {r["holder_id"]: r for r in rows[:2]}
        # 每件 500 分：h1 70% = 700，h2 30% = 300
        self.assertEqual(latest["h1"]["amount_cents"], 700)
        self.assertEqual(latest["h2"]["amount_cents"], 300)
        self.assertIsNone(latest["h1"]["rate_bps"])
        # 退 1 件按件均冲回
        fulfillment.return_shipped(
            self.s, item_id=item, qty=1, reason="退货")
        net_h1 = self.s.one(
            "SELECT COALESCE(SUM(amount_cents),0) n FROM royalty_accrual"
            " WHERE holder_id='h1'")["n"]
        self.assertEqual(net_h1, 700 - 350)


class DashboardTest(unittest.TestCase):
    def test_renewals_and_trace(self):
        from museum_merch import analytics, cases, rights
        s = build_world()
        restock(s, qty=100)
        catalog.list_version(s, "v1", "pop-up", "CN", ON)
        due = analytics.renewals_due(s, ON, within_days=45)
        codes = {d["license_code"]: d for d in due}
        self.assertIn("L1-POP", codes)
        self.assertEqual(codes["L1-POP"]["days_remaining"], 41)
        self.assertEqual(codes["L1-POP"]["active_listings"], 1)
        trace = analytics.trace_version(s, "v1")
        self.assertEqual(trace["product_version"]["artwork_title"], "作品甲")
        self.assertEqual(len(trace["licenses"]), 2)
        self.assertTrue(trace["licenses"][0]["evidence"])
        self.assertTrue(trace["gate_reviews"])
        self.assertTrue(trace["production_batches"])

    def test_exposure_flags_stranded_stock(self):
        from museum_merch import analytics
        s = build_world()
        restock(s, qty=100)
        expo = {e["version_id"]: e for e in analytics.stock_exposure(s, ON)}
        e = expo["v1"]
        self.assertEqual(e["on_hand"], 100)
        self.assertTrue(e["sellable_now"])
        self.assertEqual(e["exposure_cents"], 100 * 10000)


if __name__ == "__main__":
    unittest.main()
