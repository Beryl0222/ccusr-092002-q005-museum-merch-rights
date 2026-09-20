"""测试公共夹具：每次用例使用独立临时数据库。"""

import os
import tempfile
import unittest

from museum_merch import catalog, production, reporting, incidents, sales
from museum_merch.database import connect, init_db


class DomainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self.tmp.name, "test.db")
        self.db = connect()
        init_db(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    # ---------- 造数助手 ----------

    def seed_basic(self, uses=None, channels=None, regions=None,
                   until="2026-12-31", royalty_rate="0.12",
                   artwork="w1", holder="h1", exhibition="ex1",
                   license_code="l1"):
        if not self.db.execute("SELECT 1 FROM right_holders WHERE code=?",
                               (holder,)).fetchone():
            catalog.create_holder(self.db, holder, "某艺术家", "artist")
        if not self.db.execute("SELECT 1 FROM exhibitions WHERE code=?",
                               (exhibition,)).fetchone():
            catalog.create_exhibition(self.db, exhibition, "光影特展",
                                      "2026-09-01", "2026-11-30")
        if not self.db.execute("SELECT 1 FROM artworks WHERE code=?",
                               (artwork,)).fetchone():
            catalog.create_artwork(self.db, artwork, "星夜", "某艺术家",
                                   holder, exhibition)
        catalog.record_license(
            self.db, license_code, artwork,
            uses or ["product-reproduction", "exhibition-promotion"],
            "2026-08-01", until, channels=channels or ["*"],
            regions=regions or ["*"], royalty_rate=royalty_rate)
        catalog.add_license_document(self.db, license_code, "contract", "DOC-1")

    def make_version(self, code="v1", name="星夜丝巾", is_derivative=False,
                     review="approved"):
        catalog.create_version(self.db, code, name, "w1", is_derivative)
        if review:
            catalog.submit_for_review(self.db, code)
            catalog.decide_review(self.db, code, review, "张审")
        return code

    def make_po_and_stock(self, version="v1", qty=100):
        production.create_supplier(self.db, "s1", "丝巾厂", 30)
        production.create_po(self.db, "po1", "s1", version, qty, "30",
                             "2026-09-20")
        production.lock_po(self.db, "po1")
        production.start_batch(self.db, "b1", "po1", qty)
        incidents.qc_failure(self.db, "b1", qty, 0, "李检")
        production.receive_batch(self.db, "b1")
        return {"po": "po1", "batch": "b1"}

    def setUp_reservations_and_listings(self, version="v1"):
        production.set_channel_reservation(self.db, version, "livestream", 60)
        production.set_channel_reservation(self.db, version, "museum-store", 40)
        sales.list_product(self.db, version, "livestream", "CN")
        sales.list_product(self.db, version, "museum-store", "CN")
