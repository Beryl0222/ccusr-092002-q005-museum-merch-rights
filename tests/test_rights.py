"""权利审查引擎测试：用途分离、渠道/地域/期限、证据、宣传用途独立判定。"""

import unittest

from museum_merch import catalog
from museum_merch.rights import USE_PROMOTION, USE_REPRODUCTION

from domain_case import DomainTestCase


class RightsEvaluationTest(DomainTestCase):
    def test_promotion_granted_but_reproduction_missing(self):
        # 历史事故一：宣传图获准，但商品复制未获准
        self.seed_basic(uses=[USE_PROMOTION])
        self.make_version()
        result = catalog.gate(self.db, "v1")
        self.assertFalse(result["ok"])
        self.assertFalse(result["rights_ok"])
        self.assertTrue(any("product-reproduction" in r for r in result["reasons"]))
        # 宣传用途本身是通过的
        promo = catalog.check_promotion(self.db, "w1", "livestream", "CN")
        self.assertTrue(promo["ok"])

    def test_reproduction_does_not_cover_promotion(self):
        self.seed_basic(uses=[USE_REPRODUCTION])
        promo = catalog.check_promotion(self.db, "w1", channel="museum-store",
                                        region="CN")
        self.assertFalse(promo["ok"])

    def test_derivative_requires_adaptation_use(self):
        self.seed_basic(uses=[USE_REPRODUCTION])
        self.make_version(code="v-adapt", name="二创帆布袋", is_derivative=True)
        result = catalog.gate(self.db, "v-adapt")
        self.assertFalse(result["ok"])
        uses = result["uses"]
        self.assertTrue(uses["product-reproduction"]["ok"])
        self.assertFalse(uses["product-adaptation"]["ok"])

    def test_channel_region_gate_for_listing(self):
        self.seed_basic(channels=["museum-store"], regions=["CN"])
        self.make_version()
        # 采购门禁（不指定渠道）：仅用途层面通过
        self.assertTrue(catalog.gate(self.db, "v1")["ok"])
        # 馆内商店/CN 可以上架
        self.assertTrue(catalog.gate(self.db, "v1", "museum-store", "CN")["ok"])
        # 直播渠道未授权
        result = catalog.gate(self.db, "v1", "livestream", "CN")
        self.assertFalse(result["ok"])
        self.assertTrue(any("livestream" in r for r in result["reasons"]))
        # 海外地域未授权
        result = catalog.gate(self.db, "v1", "museum-store", "US")
        self.assertFalse(result["ok"])

    def test_validity_period(self):
        self.seed_basic()
        self.make_version()
        self.assertFalse(catalog.gate(self.db, "v1", on_date="2026-07-31")["ok"])
        self.assertTrue(catalog.gate(self.db, "v1", on_date="2026-08-01")["ok"])
        self.assertFalse(catalog.gate(self.db, "v1", on_date="2027-01-01")["ok"])

    def test_license_without_document_has_no_effect(self):
        catalog.create_holder(self.db, "h1", "某艺术家", "artist")
        catalog.create_exhibition(self.db, "ex1", "展", "2026-09-01", "2026-11-30")
        catalog.create_artwork(self.db, "w1", "星夜", "某艺术家", "h1", "ex1")
        catalog.record_license(self.db, "l1", "w1", ["product-reproduction"],
                               "2026-01-01", "2027-12-31")
        # 故意不添加证据文件
        self.make_version()
        result = catalog.gate(self.db, "v1")
        self.assertFalse(result["ok"])
        self.assertTrue(any("证据" in r for r in result["reasons"]))

    def test_review_required(self):
        self.seed_basic()
        catalog.create_version(self.db, "v1", "丝巾", "w1")
        # 未审核
        result = catalog.gate(self.db, "v1")
        self.assertFalse(result["ok"])
        self.assertFalse(result["review_ok"])
        catalog.submit_for_review(self.db, "v1")
        catalog.decide_review(self.db, "v1", "rejected", "王审", notes="图案裁切不当")
        result = catalog.gate(self.db, "v1")
        self.assertFalse(result["ok"])
        self.assertTrue(any("审核" in r for r in result["reasons"]))

    def test_second_license_covers_after_first_revoked(self):
        self.seed_basic(channels=["museum-store"])
        catalog.record_license(
            self.db, "l2", "w1", ["product-reproduction"],
            "2026-08-01", "2027-12-31", channels=["livestream"], regions=["CN"])
        catalog.add_license_document(self.db, "l2", "email", "DOC-2")
        self.make_version()
        # 直播由 l2 覆盖
        self.assertTrue(catalog.gate(self.db, "v1", "livestream", "CN")["ok"])
        catalog.revoke_license(self.db, "l2")
        # l2 撤回后直播断权，馆内仍可由 l1 覆盖
        self.assertFalse(catalog.gate(self.db, "v1", "livestream", "CN")["ok"])
        self.assertTrue(catalog.gate(self.db, "v1", "museum-store", "CN")["ok"])


if __name__ == "__main__":
    unittest.main()
