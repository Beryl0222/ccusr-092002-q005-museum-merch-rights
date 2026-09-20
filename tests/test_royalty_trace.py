"""版税结算与追溯/看板测试。"""

import unittest

from museum_merch import catalog, incidents, production, reporting, sales

from domain_case import DomainTestCase


class RoyaltyTest(DomainTestCase):
    def _setup_sales(self):
        self.seed_basic(royalty_rate="0.10")
        self.make_version()
        self.make_po_and_stock(qty=100)
        self.setUp_reservations_and_listings()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 60, "unit_price": "100"}])
        sales.create_order(
            self.db, "o2", "museum-store", "CN",
            [{"version": "v1", "qty": 20, "unit_price": "90"}])
        sales.ship_order(self.db, "o1")
        sales.ship_order(self.db, "o2")

    def test_royalty_on_net_confirmed_sales(self):
        self._setup_sales()
        out = sales.settle_royalty(self.db, "l1", "2026-01-01", "2027-01-01")
        # 60*100 + 20*90 = 7800；10% = 780
        self.assertEqual(out["net_sales"], "7800")
        self.assertEqual(out["royalty_amount"], "780.00")
        self.assertEqual(out["net_qty"], 80)

    def test_unshipped_refund_excluded_from_royalty(self):
        self._setup_sales()
        # 撤回导致一笔新订单退款（未发货），不影响已确认净销售
        sales.create_order(
            self.db, "o3", "livestream", "CN",
            [{"version": "v1", "qty": 5, "unit_price": "100"}])
        item = self.db.execute(
            "SELECT oi.id FROM order_items oi JOIN orders o ON o.id=oi.order_id"
            " WHERE o.code='o3'").fetchone()
        sales.refund_item(self.db, item["id"], 5, "撤回退款")
        out = sales.settle_royalty(self.db, "l1", "2026-01-01", "2027-01-01")
        self.assertEqual(out["net_sales"], "7800")
        self.assertEqual(out["royalty_amount"], "780.00")

    def test_unit_fee_royalty(self):
        self.seed_basic(royalty_rate=None)
        lic = self.db.execute("SELECT * FROM licenses WHERE code='l1'").fetchone()
        self.db.execute("UPDATE licenses SET royalty_rate=NULL, royalty_unit_fee='8'"
                        " WHERE id=?", (lic["id"],))
        self.db.commit()
        self.make_version()
        self.make_po_and_stock(qty=100)
        self.setUp_reservations_and_listings()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 10, "unit_price": "100"}])
        sales.ship_order(self.db, "o1")
        out = sales.settle_royalty(self.db, "l1", "2026-01-01", "2027-01-01")
        self.assertEqual(out["royalty_amount"], "80.00")


class TraceDashboardTest(DomainTestCase):
    def test_trace_covers_full_chain(self):
        self.seed_basic(until="2026-10-31")
        self.make_version()
        self.make_po_and_stock()
        self.setUp_reservations_and_listings()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 10, "unit_price": "100"}])
        sales.ship_order(self.db, "o1")
        sales.settle_royalty(self.db, "l1", "2026-01-01", "2027-01-01")
        incidents.license_expiry_sweep(self.db, on_date="2026-11-01")
        trace = reporting.trace_version(self.db, "v1")
        self.assertEqual(trace["artwork"]["code"], "w1")
        self.assertEqual(trace["right_holder"]["code"], "h1")
        self.assertEqual(trace["exhibition"]["code"], "ex1")
        self.assertTrue(trace["licenses"])
        self.assertTrue(trace["licenses"][0]["documents"])
        self.assertEqual(trace["content_reviews"][0]["decision"], "approved")
        self.assertEqual(trace["purchase_orders"][0]["code"], "po1")
        self.assertTrue(trace["purchase_orders"][0]["committed"])
        self.assertEqual(trace["purchase_orders"][0]["batches"][0]["code"], "b1")
        self.assertEqual(trace["sales"][0]["license_snapshot"], "l1")
        self.assertTrue(trace["sales"][0]["shipments"])
        self.assertEqual(len(trace["royalty_settlements"]), 1)
        incident_types = {i["type"] for i in trace["incidents"]}
        self.assertIn("license_expired", incident_types)

    def test_dashboard_shows_exposure_and_renewals(self):
        self.seed_basic(until="2026-10-31")
        self.make_version()
        self.make_po_and_stock()
        self.setUp_reservations_and_listings()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 60, "unit_price": "100"}])
        incidents.license_expiry_sweep(self.db, on_date="2026-11-01")
        dash = reporting.risk_dashboard(self.db, "ex1")
        self.assertTrue(any(r["license"] == "l1" for r in dash["renewals_needed"]))
        self.assertTrue(any(r["version"] == "v1"
                            for r in dash["inventory_exposure"]))
        self.assertTrue(dash["suspended_listings"])
        self.assertTrue(any(po["status"] == "production_halted"
                            for po in dash["delayed_or_halted_purchase_orders"]))


if __name__ == "__main__":
    unittest.main()
