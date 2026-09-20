"""共享库存、渠道预留、防超卖、取消/部分发货/替代测试。

覆盖历史事故：直播秒空后线下库存被重复占用。
"""

import unittest

from museum_merch import sales
from museum_merch.catalog import DomainError

from domain_case import DomainTestCase


class InventoryOversellTest(DomainTestCase):
    def _setup(self):
        self.seed_basic()
        self.make_version()
        self.make_po_and_stock(qty=100)
        self.setUp_reservations_and_listings()

    def test_global_and_channel_reservation(self):
        self._setup()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 60, "unit_price": "100"}])
        # 直播预留 60 已满，第 61 件被拒（即使全局还有 40 件可售）
        with self.assertRaises(DomainError) as ctx:
            sales.create_order(
                self.db, "o2", "livestream", "CN",
                [{"version": "v1", "qty": 1, "unit_price": "100"}])
        self.assertIn("预留", ctx.exception.reasons[0])
        # 馆内商店的 40 件仍可独立售出
        sales.create_order(
            self.db, "o3", "museum-store", "CN",
            [{"version": "v1", "qty": 40, "unit_price": "100"}])
        # 全局 100 件占满，任何渠道再来都被拒
        with self.assertRaises(DomainError):
            sales.create_order(
                self.db, "o4", "museum-store", "CN",
                [{"version": "v1", "qty": 1, "unit_price": "100"}])

    def test_cancel_releases_for_resale(self):
        self._setup()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 60, "unit_price": "100"}])
        sales.cancel_order(self.db, "o1")
        view = sales.inventory_view(self.db, "v1")
        self.assertEqual(view["allocated"], 0)
        self.assertEqual(view["sellable"], 100)
        # 释放后直播渠道可重新成交，不产生重复占用
        sales.create_order(
            self.db, "o2", "livestream", "CN",
            [{"version": "v1", "qty": 60, "unit_price": "100"}])
        view = sales.inventory_view(self.db, "v1")
        self.assertEqual(view["allocated"], 60)

    def test_partial_shipment_keeps_remaining_reserved(self):
        self._setup()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 60, "unit_price": "100"}])
        sales.ship_order(self.db, "o1", qty=20)
        order = self.db.execute(
            "SELECT * FROM orders WHERE code='o1'").fetchone()
        self.assertEqual(order["status"], "partially_shipped")
        view = sales.inventory_view(self.db, "v1")
        # 已发 20：on_hand 减 20、allocated 减 20；余 40 仍占用
        self.assertEqual((view["on_hand"], view["allocated"], view["shipped"]),
                         (80, 40, 20))
        # 未发部分取消后，对应库存释放，已发部分不动
        sales.cancel_order(self.db, "o1")
        view = sales.inventory_view(self.db, "v1")
        self.assertEqual((view["on_hand"], view["allocated"], view["shipped"]),
                         (80, 0, 20))

    def test_cannot_sell_without_active_listing(self):
        self.seed_basic()
        self.make_version()
        self.make_po_and_stock()
        with self.assertRaises(DomainError):
            sales.create_order(
                self.db, "o1", "popup", "CN",
                [{"version": "v1", "qty": 1, "unit_price": "100"}])

    def test_reservation_cannot_exceed_sellable_stock(self):
        self.seed_basic()
        self.make_version()
        self.make_po_and_stock(qty=100)
        # 先占用 70 件（无预留限制渠道）
        from museum_merch import production
        sales.list_product(self.db, "v1", "popup", "CN")
        sales.create_order(
            self.db, "o0", "popup", "CN",
            [{"version": "v1", "qty": 70, "unit_price": "100"}])
        with self.assertRaises(DomainError):
            production.set_channel_reservation(self.db, "v1", "livestream", 40)
        production.set_channel_reservation(self.db, "v1", "livestream", 30)

    def test_substitute_uses_clean_version(self):
        self._setup()
        # 第二件作品与完整授权，作为替代版本
        self.seed_basic(holder="h2", artwork="w2", exhibition="ex1",
                        license_code="l2")
        self.make_version(code="v2", name="替代明信片")
        # v2 也备 20 件库存
        from museum_merch import production, incidents
        production.create_supplier(self.db, "s2", "二厂", 10)
        production.create_po(self.db, "po2", "s2", "v2", 20, "5", "2026-10-01")
        production.lock_po(self.db, "po2")
        production.start_batch(self.db, "b2", "po2", 20)
        incidents.qc_failure(self.db, "b2", 20, 0, "李检")
        production.receive_batch(self.db, "b2")
        sales.list_product(self.db, "v2", "livestream", "CN")
        production.set_channel_reservation(self.db, "v2", "livestream", 20)

        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 10, "unit_price": "100"}])
        item_id = self.db.execute(
            "SELECT id FROM order_items WHERE order_id="
            "(SELECT id FROM orders WHERE code='o1')").fetchone()["id"]
        out = sales.substitute_item(self.db, item_id, "v2", 10)
        self.assertEqual(out["qty"], 10)
        v1 = sales.inventory_view(self.db, "v1")
        v2 = sales.inventory_view(self.db, "v2")
        self.assertEqual(v1["allocated"], 0)
        self.assertEqual(v2["allocated"], 10)

    def test_order_snapshots_effective_license(self):
        self._setup()
        sales.create_order(
            self.db, "o1", "livestream", "CN",
            [{"version": "v1", "qty": 2, "unit_price": "100"}])
        item = self.db.execute(
            "SELECT oi.* FROM order_items oi JOIN orders o ON o.id=oi.order_id"
            " WHERE o.code='o1'").fetchone()
        self.assertIsNotNone(item["license_id"])


if __name__ == "__main__":
    unittest.main()
