"""风险事件处置测试：授权撤回、到期巡查、改期、供应商延期、质检缺口。"""

import unittest

from museum_merch import catalog, incidents, production, sales
from museum_merch.catalog import DomainError

from domain_case import DomainTestCase


class LicenseRevocationTest(DomainTestCase):
    def _setup_on_sale(self):
        self.seed_basic(until="2026-12-31")
        self.make_version()
        self.make_po_and_stock()
        self.setUp_reservations_and_listings()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 60, "unit_price": "100"}])

    def test_revocation_suspends_listing_and_halts_po(self):
        self._setup_on_sale()
        catalog.revoke_license(self.db, "l1", doc_ref="REV-1")
        out = incidents.license_revoked(self.db, "l1")
        kinds = {a["action"] for a in out["actions"]}
        self.assertIn("suspend_sale", kinds)
        self.assertIn("halt_production", kinds)
        self.assertIn("refund_or_substitute", kinds)
        # listing 已挂起
        active = self.db.execute(
            "SELECT COUNT(*) AS n FROM listings WHERE status='active'").fetchone()["n"]
        self.assertEqual(active, 0)
        # 采购承诺保留
        po = self.db.execute(
            "SELECT * FROM purchase_orders WHERE code='po1'").fetchone()
        self.assertEqual(po["status"], "production_halted")
        self.assertEqual(po["committed"], 1)
        # 新订单被拦截
        with self.assertRaises(DomainError):
            sales.create_order(
                self.db, "o2", "museum-store", "CN",
                [{"version": "v1", "qty": 1, "unit_price": "100"}])

    def test_pending_refund_or_substitute_resolvable(self):
        self._setup_on_sale()
        catalog.revoke_license(self.db, "l1")
        incidents.license_revoked(self.db, "l1")
        pending = [a for a in incidents.list_pending_actions(self.db)
                   if a["action_type"] == "refund_or_substitute"]
        self.assertEqual(len(pending), 1)
        out = incidents.resolve_action(self.db, pending[0]["id"], "refund")
        self.assertEqual(out["result"]["refunded_qty"], 60)
        self.assertEqual(out["result"]["amount"], "6000.00")
        view = sales.inventory_view(self.db, "v1")
        self.assertEqual(view["allocated"], 0)
        self.assertEqual(view["sellable"], 100)
        # 动作已闭环
        again = incidents.list_pending_actions(self.db)
        self.assertNotIn(pending[0]["id"], {a["id"] for a in again})

    def test_auto_refund(self):
        self._setup_on_sale()
        catalog.revoke_license(self.db, "l1")
        out = incidents.license_revoked(self.db, "l1", auto_refund=True)
        refund_actions = [a for a in out["actions"]
                          if a["action"] == "refund_or_substitute"]
        self.assertTrue(refund_actions[0]["auto"])
        self.assertEqual(sales.inventory_view(self.db, "v1")["allocated"], 0)

    def test_shipped_goods_remain_fulfilled_after_revocation(self):
        self._setup_on_sale()
        # 撤回前已发货 30 件
        sales.ship_order(self.db, "o1", qty=30)
        catalog.revoke_license(self.db, "l1")
        incidents.license_revoked(self.db, "l1")
        pending = [a for a in incidents.list_pending_actions(self.db)
                   if a["action_type"] == "refund_or_substitute"]
        # 只有未发货的 30 件进入队列；已发部分不追回
        self.assertEqual(len(pending), 1)
        item_id = int(pending[0]["entity_ref"])
        item = self.db.execute(
            "SELECT * FROM order_items WHERE id=?", (item_id,)).fetchone()
        self.assertEqual(item["allocated_qty"] - item["shipped_qty"], 30)
        self.assertEqual(item["shipped_qty"], 30)


class LicenseExpiryTest(DomainTestCase):
    def test_expired_license_takes_listings_down(self):
        # 历史事故：授权到期后线上仍在售
        self.seed_basic(until="2026-10-31")
        self.make_version()
        self.make_po_and_stock()
        self.setUp_reservations_and_listings()
        out = incidents.license_expiry_sweep(self.db, on_date="2026-11-01")
        self.assertIn("l1", out["expired_licenses"])
        kinds = {a["action"] for a in out["actions"]}
        self.assertIn("suspend_sale", kinds)
        with self.assertRaises(DomainError):
            sales.create_order(
                self.db, "o1", "livestream", "CN",
                [{"version": "v1", "qty": 1, "unit_price": "100"}])

    def test_orders_within_valid_period_continue_after_expiry(self):
        self.seed_basic(until="2026-10-31")
        self.make_version()
        self.make_po_and_stock()
        self.setUp_reservations_and_listings()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 10, "unit_price": "100"}],
            on_date="2026-10-30")
        incidents.license_expiry_sweep(self.db, on_date="2026-11-01")
        # 库存 100 足以覆盖占用 10，无退款/替代缺口动作
        pending = incidents.list_pending_actions(self.db)
        self.assertEqual(
            [a for a in pending if a["action_type"] == "refund_or_substitute"], [])
        # 已成交订单仍可发货
        rows = sales.ship_order(self.db, "o1")
        self.assertEqual(sum(r["qty"] for r in rows), 10)


class ExhibitionRescheduleTest(DomainTestCase):
    def test_reschedule_flags_renewals_and_preventive_suspension(self):
        self.seed_basic(until="2026-11-30")
        self.make_version()
        out = incidents.exhibition_rescheduled(
            self.db, "ex1", "2027-01-10", "2027-03-31")
        kinds = {a["action"] for a in out["actions"]}
        self.assertIn("renew_license", kinds)
        self.assertIn("suspend_sale", kinds)
        ex = self.db.execute(
            "SELECT * FROM exhibitions WHERE code='ex1'").fetchone()
        self.assertEqual(ex["status"], "rescheduled")
        self.assertEqual(ex["end_date"], "2027-03-31")

    def test_renewal_action_resolves_with_document(self):
        self.seed_basic(until="2026-11-30")
        self.make_version()
        incidents.exhibition_rescheduled(self.db, "ex1", "2027-01-10", "2027-03-31")
        action = next(a for a in incidents.list_pending_actions(self.db)
                      if a["action_type"] == "renew_license")
        incidents.resolve_action(
            self.db, action["id"], "renew",
            new_until="2027-04-30", doc_ref="RENEW-1")
        lic = self.db.execute(
            "SELECT * FROM licenses WHERE code='l1'").fetchone()
        self.assertEqual(lic["valid_until"], "2027-04-30")
        self.assertEqual(lic["status"], "active")


class SupplierDelayTest(DomainTestCase):
    def test_delay_preserves_commitment_and_warns_on_license_deadline(self):
        self.seed_basic(until="2026-10-31")
        self.make_version()
        self.make_po_and_stock()
        out = incidents.supplier_delay(
            self.db, "po1", "2026-11-15", reason="面料晚到")
        kinds = {a["action"] for a in out["actions"]}
        self.assertIn("preserve_commitment", kinds)
        self.assertIn("rework_or_reorder", kinds)  # 新交期晚于授权到期
        po = self.db.execute(
            "SELECT * FROM purchase_orders WHERE code='po1'").fetchone()
        self.assertEqual(po["status"], "delayed")
        self.assertEqual(po["committed"], 1)

    def test_delay_within_license_window_only_preserves(self):
        self.seed_basic(until="2026-12-31")
        self.make_version()
        self.make_po_and_stock()
        out = incidents.supplier_delay(self.db, "po1", "2026-10-15")
        kinds = {a["action"] for a in out["actions"]}
        self.assertEqual(kinds, {"preserve_commitment"})


class QcShortageTest(DomainTestCase):
    def test_failed_batch_creates_refund_or_substitute_shortage(self):
        self.seed_basic()
        self.make_version()
        production.create_supplier(self.db, "s1", "厂", 30)
        production.create_po(self.db, "po1", "s1", "v1", 100, "30", "2026-09-20")
        production.lock_po(self.db, "po1")
        # 先建一个 50 件的旧批次并全部合格入库
        production.start_batch(self.db, "b0", "po1", 50)
        incidents.qc_failure(self.db, "b0", 50, 0, "李检")
        production.receive_batch(self.db, "b0")
        self.setUp_reservations_and_listings()
        # 已售 80 件，但只有 50 件在手
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 50, "unit_price": "100"}])
        sales.create_order(
            self.db, "o2", "museum-store", "CN",
            [{"version": "v1", "qty": 30, "unit_price": "100"}])
        # 第二批 50 件全废：应出现 30 件缺口的退款/替代动作
        production.start_batch(self.db, "b1", "po1", 50)
        out = incidents.qc_failure(self.db, "b1", 0, 50, "李检")
        shortage_actions = [a for a in out["actions"]
                            if a["action"] == "refund_or_substitute"]
        self.assertEqual(sum(a["shortage"] for a in shortage_actions), 30)


if __name__ == "__main__":
    unittest.main()
