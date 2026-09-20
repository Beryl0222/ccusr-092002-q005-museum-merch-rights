"""采购、质检、共享库存与多渠道预留测试。"""

import threading
import unittest

from museum_merch import cases, catalog, fulfillment
from museum_merch.store import DomainError
from support import ON, build_world, restock


class ProcurementQcTest(unittest.TestCase):
    def setUp(self):
        self.s = build_world()

    def test_po_blocked_without_gate(self):
        with self.assertRaises(DomainError) as ctx:
            fulfillment.create_po(
                self.s, po_no="PO-X", version_id="vbad", supplier_id="sup1",
                qty_ordered=10, unit_cost_cents=100, expected_at=None, on_date=ON)
        self.assertEqual(ctx.exception.code, "gate-failed")

    def test_confirmed_po_is_a_commitment(self):
        po, _ = restock(self.s, qty=50)
        row = self.s.one("SELECT status,committed FROM purchase_order WHERE id=?", (po,))
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["committed"], 1)

    def test_qc_partial_splits_qualified_and_quarantine(self):
        _, batch = restock(self.s, qty=100, passed=90)
        totals = fulfillment.stock_totals(self.s.conn, "v1")
        self.assertEqual(totals["on_hand"], 90)
        self.assertEqual(totals["quarantine"], 10)
        b = self.s.one("SELECT status FROM production_batch WHERE id=?", (batch,))
        self.assertEqual(b["status"], "partial")
        # 质检失败生成处置工单
        cs = cases.process_events(self.s, ON)
        kinds = [self.s.one("SELECT kind FROM case_record WHERE id=?", (c,))["kind"]
                 for c in cs]
        self.assertIn("qc-failed", kinds)


class InventoryQuotaTest(unittest.TestCase):
    def setUp(self):
        self.s = build_world()
        restock(self.s, qty=100)
        catalog.list_version(self.s, "v1", "museum-store", "CN", ON)
        catalog.list_version(self.s, "v1", "livestream", "CN", ON)
        catalog.list_version(self.s, "v1", "pop-up", "CN", ON)

    def test_quotas_protect_offline_channels(self):
        # 馆内额度 10、快闪 5：无额度的直播最多抢 100-10-5=85
        cap = fulfillment.channel_cap(self.s.conn, "v1", "livestream")
        self.assertEqual(cap, 85)
        fulfillment.place_order(
            self.s, order_no="L1", channel="livestream", region="CN",
            items=[{"version_id": "v1", "qty": 85}], on_date=ON)
        self.assertEqual(
            fulfillment.channel_cap(self.s.conn, "v1", "livestream"), 0)
        # 馆内仍可下到自身额度 10
        self.assertEqual(
            fulfillment.channel_cap(self.s.conn, "v1", "museum-store"), 10)
        # 快闪仍可下 5
        self.assertEqual(
            fulfillment.channel_cap(self.s.conn, "v1", "pop-up"), 5)

    def test_oversell_rejected(self):
        with self.assertRaises(DomainError) as ctx:
            fulfillment.place_order(
                self.s, order_no="BIG", channel="livestream", region="CN",
                items=[{"version_id": "v1", "qty": 86}], on_date=ON)
        self.assertEqual(ctx.exception.code, "oversell")
        # 失败整单回滚，不留任何预留
        totals = fulfillment.stock_totals(self.s.conn, "v1")
        self.assertEqual(totals["reserved"], 0)

    def test_concurrent_orders_never_oversell(self):
        results = []

        def buyer(i):
            try:
                fulfillment.place_order(
                    self.s, order_no=f"C{i}", channel="livestream", region="CN",
                    items=[{"version_id": "v1", "qty": 10}], on_date=ON)
                results.append("ok")
            except DomainError as e:
                results.append(e.code)

        threads = [threading.Thread(target=buyer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 可争抢 85 件，每单 10 件，仅 8 单成功
        self.assertEqual(results.count("ok"), 8)
        self.assertTrue(all(r == "oversell" for r in results if r != "ok"))
        totals = fulfillment.stock_totals(self.s.conn, "v1")
        self.assertEqual(totals["reserved"], 80)
        self.assertGreaterEqual(totals["on_hand"] - totals["reserved"], 0)

    def test_partial_shipment_and_cancel(self):
        oid = fulfillment.place_order(
            self.s, order_no="M1", channel="museum-store", region="CN",
            items=[{"version_id": "v1", "qty": 8}], on_date=ON)
        item = self.s.one("SELECT id FROM order_item WHERE order_id=?", (oid,))["id"]
        fulfillment.ship_item(self.s, item_id=item, qty=3, on_date=ON)
        mid = fulfillment.stock_totals(self.s.conn, "v1")
        self.assertEqual(mid["on_hand"], 97)
        self.assertEqual(mid["reserved"], 5)
        # 取消剩余 5 件，释放预留
        out = fulfillment.cancel_unshipped(
            self.s, item_id=item, qty=5, reason="客户取消")
        self.assertEqual(out["amount_cents"], 5 * 10000)
        end = fulfillment.stock_totals(self.s.conn, "v1")
        self.assertEqual(end["reserved"], 0)
        self.assertEqual(end["on_hand"], 97)
        order = self.s.one("SELECT status FROM sales_order WHERE id=?", (oid,))
        self.assertEqual(order["status"], "partially-cancelled")

    def test_shipment_blocked_when_rights_lapsed(self):
        oid = fulfillment.place_order(
            self.s, order_no="M2", channel="museum-store", region="CN",
            items=[{"version_id": "v1", "qty": 5}], on_date=ON)
        item = self.s.one("SELECT id FROM order_item WHERE order_id=?", (oid,))["id"]
        with self.assertRaises(DomainError) as ctx:
            # 主授权 2026-12-31 到期；到期后不得发货
            fulfillment.ship_item(self.s, item_id=item, qty=5,
                                  on_date="2027-01-05")
        self.assertEqual(ctx.exception.code, "rights-lapsed")
        # 库存未被扣减
        totals = fulfillment.stock_totals(self.s.conn, "v1")
        self.assertEqual(totals["on_hand"], 100)


if __name__ == "__main__":
    unittest.main()
