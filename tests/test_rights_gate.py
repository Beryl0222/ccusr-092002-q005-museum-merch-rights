"""权利边界与准入闸门测试。"""

import unittest

from museum_merch import catalog, rights
from museum_merch.store import DomainError
from support import ON, build_world


class RightsGateTest(unittest.TestCase):
    def setUp(self):
        self.s = build_world()

    def test_promotion_license_does_not_cover_reproduction(self):
        """宣传图获准 ≠ 商品复制获准：帆布袋只有宣传许可，闸门必须失败。"""
        result = rights.evaluate_version(self.s, "vbad", "procurement", ON)
        self.assertEqual(result["decision"], "fail")
        self.assertTrue(result["rights_ok"] is False)
        gap = result["markets"][0]["gaps_by_license"]["L2"]
        self.assertIn("use", gap)
        with self.assertRaises(DomainError) as ctx:
            rights.gate_for_procurement(self.s, "vbad", ON)
        self.assertEqual(ctx.exception.code, "gate-failed")

    def test_all_markets_must_be_covered_for_procurement(self):
        """v1 计划四渠道：主授权不含 pop-up，但有 L1-POP 覆盖，整体应通过。"""
        result = rights.evaluate_version(self.s, "v1", "procurement", ON)
        self.assertEqual(result["decision"], "pass")
        by_market = {m["channel"]: m for m in result["markets"]}
        self.assertEqual(by_market["pop-up"]["matched_license_code"], "L1-POP")
        self.assertEqual(by_market["livestream"]["matched_license_code"], "L1")

    def test_period_boundary(self):
        # 授权起始日前与到期后均不可用
        self.assertEqual(
            rights.evaluate_version(self.s, "v1", "listing", "2026-07-31",
                                    "museum-store", "CN")["decision"], "fail")
        self.assertEqual(
            rights.evaluate_version(self.s, "v1", "listing", "2027-01-01",
                                    "museum-store", "CN")["decision"], "fail")
        # 起讫当日有效
        self.assertEqual(
            rights.evaluate_version(self.s, "v1", "listing", "2026-08-01",
                                    "museum-store", "CN")["decision"], "pass")
        self.assertEqual(
            rights.evaluate_version(self.s, "v1", "listing", "2026-12-31",
                                    "museum-store", "CN")["decision"], "pass")

    def test_region_and_channel_boundary(self):
        # US 地域未授权
        result = rights.evaluate_version(self.s, "v1", "listing", ON,
                                         "museum-store", "US")
        self.assertEqual(result["decision"], "fail")
        # pop-up 在主授权渠道之外，但被 L1-POP 覆盖
        result = rights.evaluate_version(self.s, "v1", "listing", ON,
                                         "pop-up", "CN")
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(result["markets"][0]["matched_license_code"], "L1-POP")

    def test_license_without_verified_evidence_fails(self):
        rights.create_license(
            self.s, id="lic3", code="L3", artwork_id="w1", holder_id="h1",
            uses=["reproduction"], channels=["museum-store"], regions=["CN"],
            valid_from="2026-08-01", valid_until="2026-12-31",
            evidences=[{"kind": "email", "document_ref": "x.eml",
                        "status": "received"}])
        result = rights.evaluate_version(self.s, "v1", "listing", ON,
                                         "museum-store", "CN")
        # 已存在 L1 覆盖所以仍 pass；单独核对 L3 被判凭证未核验
        gap = result["markets"][0]["gaps_by_license"]["L3"]
        self.assertIn("evidence-unverified", gap)

    def test_content_review_required(self):
        vid = catalog.create_version(
            self.s, product_id="p1", artwork_id="w1", use_type="reproduction",
            title="未审稿", price_cents=10000)
        catalog.add_version_market(self.s, vid, "museum-store", "CN")
        result = rights.evaluate_version(self.s, vid, "procurement", ON)
        self.assertFalse(result["content_ok"])
        self.assertEqual(result["decision"], "fail")
        rights.review_content(self.s, vid, "approved", "r")
        result = rights.evaluate_version(self.s, vid, "procurement", ON)
        self.assertEqual(result["decision"], "pass")

    def test_listing_requires_gate_and_is_idempotent_blocked(self):
        restock_like = None  # 上架不依赖库存
        lid = catalog.list_version(self.s, "v1", "museum-store", "CN", ON)
        self.assertTrue(lid)
        with self.assertRaises(DomainError) as ctx:
            catalog.list_version(self.s, "v1", "museum-store", "CN", ON)
        self.assertEqual(ctx.exception.code, "already-listed")

    def test_renewal_forms_version_chain(self):
        new_id = rights.renew_license(
            self.s, code="L1-2027", old_license_id="lic1",
            valid_from="2027-01-01", valid_until="2027-06-30")
        row = self.s.one("SELECT * FROM license WHERE id=?", (new_id,))
        self.assertEqual(row["supersedes_license_id"], "lic1")
        uses = [r["use_type"] for r in self.s.all(
            "SELECT use_type FROM license_use WHERE license_id=?", (new_id,))]
        self.assertEqual(set(uses), {"reproduction", "promotion"})


if __name__ == "__main__":
    unittest.main()
