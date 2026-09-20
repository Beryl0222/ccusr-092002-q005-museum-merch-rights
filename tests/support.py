"""测试公共夹具：搭建一个最小但完整的特展文创场景。"""

from __future__ import annotations

from museum_merch import catalog, fulfillment, rights
from museum_merch.store import Store, open_store

ON = "2026-09-20"


def build_world() -> Store:
    s = open_store()
    s.init_schema()

    rights.create_exhibition(
        s, id="ex1", title="测试特展", start_date="2026-09-01",
        end_date="2026-11-30", status="ongoing")
    rights.create_holder(s, id="h1", name="家属", kind="family")
    rights.create_holder(s, id="h2", name="合作机构", kind="institution")
    rights.create_holder(s, id="artist", name="艺术家本人", kind="artist")

    # w1：家属70%/机构30%共有
    rights.create_artwork(
        s, id="w1", exhibition_id="ex1", title="作品甲",
        holder_splits={"h1": 7000, "h2": 3000})
    rights.create_artwork(s, id="w2", exhibition_id="ex1", title="作品乙")

    rights.create_license(
        s, id="lic1", code="L1", artwork_id="w1", holder_id="h1",
        uses=["reproduction", "promotion"],
        channels=["museum-store", "official-online-store", "livestream"],
        regions=["CN"], valid_from="2026-08-01", valid_until="2026-12-31",
        royalty_rate_bps=800,
        evidences=[{"kind": "contract", "document_ref": "c1.pdf"}])
    rights.create_license(
        s, id="lic1-pop", code="L1-POP", artwork_id="w1", holder_id="h1",
        uses=["reproduction"], channels=["pop-up"], regions=["CN"],
        valid_from="2026-09-01", valid_until="2026-10-31",
        royalty_rate_bps=800,
        evidences=[{"kind": "consent-form", "document_ref": "c2.pdf"}])
    # w2 仅有宣传许可
    rights.create_license(
        s, id="lic2", code="L2", artwork_id="w2", holder_id="artist",
        uses=["promotion"], channels=["museum-store"], regions=["CN"],
        valid_from="2026-08-01", valid_until="2026-12-31",
        evidences=[{"kind": "email", "document_ref": "m.eml"}])

    catalog.create_supplier(s, id="sup1", name="工坊", lead_time_days=30)
    catalog.create_product(s, id="p1", sku="SKU1", name="方巾", exhibition_id="ex1")
    vid = catalog.create_version(
        s, product_id="p1", artwork_id="w1", use_type="reproduction",
        title="方巾v1", price_cents=10000, version_id="v1")
    for ch, rg in [("museum-store", "CN"), ("official-online-store", "CN"),
                   ("livestream", "CN"), ("pop-up", "CN")]:
        catalog.add_version_market(s, vid, ch, rg)
    catalog.set_channel_quota(s, vid, "museum-store", 10)
    catalog.set_channel_quota(s, vid, "pop-up", 5)
    rights.review_content(s, vid, "approved", "reviewer")

    catalog.create_product(s, id="p2", sku="SKU2", name="帆布袋",
                           exhibition_id="ex1")
    vbad = catalog.create_version(
        s, product_id="p2", artwork_id="w2", use_type="reproduction",
        title="帆布袋v1", price_cents=5000, version_id="vbad")
    catalog.add_version_market(s, vbad, "museum-store", "CN")
    rights.review_content(s, vbad, "approved", "reviewer")
    return s


def restock(s: Store, version_id: str = "v1", qty: int = 100,
            passed: int | None = None) -> tuple[str, str]:
    """下采购单→确认→生产→质检合格入库，返回 (po_id, batch_id)。"""
    po = fulfillment.create_po(
        s, po_no=f"PO-{qty}", version_id=version_id, supplier_id="sup1",
        qty_ordered=qty, unit_cost_cents=3000, expected_at="2026-09-25", on_date=ON)
    fulfillment.confirm_po(s, po)
    batch = fulfillment.produce_batch(s, po_id=po, qty_produced=qty)
    fulfillment.record_qc(
        s, batch_id=batch, qty_inspected=qty, qty_passed=passed if passed is not None else qty)
    return po, batch
