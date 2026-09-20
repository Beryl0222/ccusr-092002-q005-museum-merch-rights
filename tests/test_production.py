"""采购/生产门禁、目标渠道预检、质检隔离测试。"""

import unittest

from museum_merch import catalog, incidents, production
from museum_merch.catalog import DomainError

from domain_case import DomainTestCase


class ProcurementGateTest(DomainTestCase):
    def test_po_cannot_lock_without_rights_and_review(self):
        self.seed_basic(uses=["exhibition-promotion"])  # 无商品复制权
        self.make_version(review="approved")
        production.create_supplier(self.db, "s1", "丝巾厂", 30)
        with self.assertRaises(DomainError) as ctx:
            production.create_po(self.db, "po1", "s1", "v1", 100, "30",
                                 "2026-09-20")
        self.assertTrue(any("product-reproduction" in r for r in ctx.exception.reasons))

    def test_po_target_channel_precheck(self):
        self.seed_basic(channels=["museum-store"])
        self.make_version()
        production.create_supplier(self.db, "s1", "厂", 30)
        # 为未授权渠道直播备货：创建即被拦截
        with self.assertRaises(DomainError) as ctx:
            production.create_po(
                self.db, "po1", "s1", "v1", 100, "30", "2026-09-20",
                targets=[("livestream", "CN")])
        self.assertTrue(any("livestream" in r for r in ctx.exception.reasons))
        # 授权渠道可以
        production.create_po(
            self.db, "po1", "s1", "v1", 100, "30", "2026-09-20",
            targets=[("museum-store", "CN")])
        production.lock_po(self.db, "po1", targets=[("museum-store", "CN")])
        po = self.db.execute(
            "SELECT * FROM purchase_orders WHERE code='po1'").fetchone()
        self.assertEqual(po["status"], "locked")
        self.assertEqual(po["committed"], 1)

    def test_lock_rechecks_gate(self):
        self.seed_basic()
        self.make_version()
        production.create_supplier(self.db, "s1", "厂", 30)
        production.create_po(self.db, "po1", "s1", "v1", 100, "30", "2026-10-01")
        catalog.revoke_license(self.db, "l1")
        with self.assertRaises(DomainError):
            production.lock_po(self.db, "po1")

    def test_batch_cannot_start_before_lock(self):
        self.seed_basic()
        self.make_version()
        production.create_supplier(self.db, "s1", "厂", 30)
        production.create_po(self.db, "po1", "s1", "v1", 100, "30", "2026-10-01")
        with self.assertRaises(DomainError):
            production.start_batch(self.db, "b1", "po1", 100)

    def test_qc_failed_quarantined_and_not_receivable(self):
        self.seed_basic()
        self.make_version()
        production.create_supplier(self.db, "s2", "厂", 30)
        production.create_po(self.db, "po2", "s2", "v1", 50, "30", "2026-10-01")
        production.lock_po(self.db, "po2")
        production.start_batch(self.db, "b2", "po2", 50)
        out = incidents.qc_failure(self.db, "b2", 0, 50, "李检", note="色差")
        self.assertIsNotNone(out["incident_id"])
        batch = self.db.execute(
            "SELECT * FROM production_batches WHERE code='b2'").fetchone()
        self.assertEqual(batch["status"], "qc_failed")
        with self.assertRaises(DomainError):
            production.receive_batch(self.db, "b2")

    def test_partial_qc_only_passed_stock_receivable(self):
        self.seed_basic()
        self.make_version()
        production.create_supplier(self.db, "s2", "厂", 30)
        production.create_po(self.db, "po2", "s2", "v1", 50, "30", "2026-10-01")
        production.lock_po(self.db, "po2")
        production.start_batch(self.db, "b2", "po2", 50)
        incidents.qc_failure(self.db, "b2", 40, 10, "李检")
        production.receive_batch(self.db, "b2")
        inv = self.db.execute("SELECT * FROM inventory WHERE version_id="
                              "(SELECT id FROM product_versions WHERE code='v1')").fetchone()
        self.assertEqual(inv["on_hand"], 40)


if __name__ == "__main__":
    unittest.main()
