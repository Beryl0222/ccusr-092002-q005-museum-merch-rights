"""演示数据：一次贯穿策展→授权→审核→采购→生产→质检→多渠道销售→版税的完整链路。"""

from __future__ import annotations

from typing import Any

from . import catalog, cases, fulfillment, rights
from .store import Store

TODAY = "2026-09-20"


def load_demo(store: Store, on: str = TODAY) -> dict[str, Any]:
    if store.one("SELECT 1 FROM exhibition WHERE id='ex-ink-2026'"):
        return {"note": "演示数据已存在，跳过"}

    rights.create_exhibition(
        store, id="ex-ink-2026", title="墨色之间：近现代水墨特展",
        start_date="2026-09-01", end_date="2026-11-30", status="ongoing")

    rights.create_holder(store, id="holder-artist", name="林一白", kind="artist")
    rights.create_holder(store, id="holder-estate", name="林氏家属（遗产管理）",
                         kind="family", contact="estate@example.org")
    rights.create_holder(store, id="holder-museum-partner",
                         name="市近现代艺术馆（合作机构）", kind="institution")

    # 主作品由家属 70% / 合作机构 30% 共有
    rights.create_artwork(
        store, id="work-2026-41", exhibition_id="ex-ink-2026",
        title="秋山行旅图（1962）", catalog_no="INK-41",
        holder_splits={"holder-estate": 7000, "holder-museum-partner": 3000})
    # 对照作品：艺术家本人单独授权
    rights.create_artwork(
        store, id="work-2026-42", exhibition_id="ex-ink-2026",
        title="墨竹册页", catalog_no="INK-42")

    # 主授权：复制+宣传，三个线上/馆内渠道，CN，8% 版税，凭证齐全
    rights.create_license(
        store, id="lic-ink41-main", code="LIC-2026-0041",
        artwork_id="work-2026-41", holder_id="holder-estate",
        uses=["reproduction", "promotion"],
        channels=["museum-store", "official-online-store", "livestream"],
        regions=["CN"], valid_from="2026-08-01", valid_until="2026-12-31",
        royalty_rate_bps=800, signed_at="2026-07-25",
        evidences=[{"kind": "contract", "document_ref": "contracts/LIC-2026-0041.pdf",
                    "checksum": "sha256:9f0c", "summary": "家属签署复制与宣传授权"}])
    # 快闪渠道另签一份短授权（10 月底到期，演示待续权利）
    rights.create_license(
        store, id="lic-ink41-popup", code="LIC-2026-0041-POP",
        artwork_id="work-2026-41", holder_id="holder-estate",
        uses=["reproduction"], channels=["pop-up"], regions=["CN"],
        valid_from="2026-09-01", valid_until="2026-10-31",
        royalty_rate_bps=800, signed_at="2026-08-20",
        evidences=[{"kind": "consent-form",
                    "document_ref": "consent/LIC-2026-0041-POP.pdf",
                    "summary": "快闪限定销售书面同意"}])
    # 对照：只有宣传许可、没有商品复制许可 —— 商品闸门必须失败
    rights.create_license(
        store, id="lic-ink42-promo", code="LIC-2026-0042-PROMO",
        artwork_id="work-2026-42", holder_id="holder-artist",
        uses=["promotion"], channels=["official-online-store", "museum-store",
                                      "livestream", "pop-up"],
        regions=["CN"], valid_from="2026-08-01", valid_until="2026-12-31",
        evidences=[{"kind": "email", "document_ref": "mail/artist-promo-ok.eml",
                    "summary": "艺术家邮件仅同意宣传图使用"}])

    catalog.create_supplier(store, id="sup-hangzhou", name="杭州印造工坊",
                            lead_time_days=35)

    catalog.create_product(
        store, id="prod-scarf", sku="SCARF-INK41", name="秋山行旅真丝方巾",
        exhibition_id="ex-ink-2026")
    vid = catalog.create_version(
        store, product_id="prod-scarf", artwork_id="work-2026-41",
        use_type="reproduction", title="秋山行旅真丝方巾·标准版",
        price_cents=29800, spec="90cm 真丝斜纹", version_id="ver-scarf-v1")
    for ch, rg in [("museum-store", "CN"), ("official-online-store", "CN"),
                   ("livestream", "CN"), ("pop-up", "CN")]:
        catalog.add_version_market(store, vid, ch, rg)
    # 共享库存 500 件：馆内预留 120、快闪 80，其余三渠道共享争抢
    catalog.set_channel_quota(store, vid, "museum-store", 120)
    catalog.set_channel_quota(store, vid, "pop-up", 80)
    rights.review_content(store, vid, "approved", reviewer="curator-zhou",
                          notes="图稿与藏品一致，文字无误")

    catalog.create_product(
        store, id="prod-bag", sku="BAG-INK42", name="墨竹帆布袋",
        exhibition_id="ex-ink-2026")
    bag_vid = catalog.create_version(
        store, product_id="prod-bag", artwork_id="work-2026-42",
        use_type="reproduction", title="墨竹帆布袋·标准版",
        price_cents=7900, version_id="ver-bag-v1")
    catalog.add_version_market(store, bag_vid, "museum-store", "CN")
    rights.review_content(store, bag_vid, "approved", reviewer="curator-zhou")

    po = fulfillment.create_po(
        store, po_no="PO-2026-0001", version_id=vid, supplier_id="sup-hangzhou",
        qty_ordered=500, unit_cost_cents=9200, expected_at="2026-09-25", on_date=on)
    fulfillment.confirm_po(store, po)
    batch = fulfillment.produce_batch(store, po_id=po, qty_produced=500)
    fulfillment.record_qc(store, batch_id=batch, qty_inspected=500,
                          qty_passed=480, notes="20 件印花偏移")

    # 三渠道上架（各自跑闸门并留快照）
    for ch in ("museum-store", "official-online-store", "livestream", "pop-up"):
        catalog.list_version(store, vid, ch, "CN", on)

    # 订单：馆内 100、直播 280（秒空共享池；尝试超过 280 会被拒——馆内 120、
    # 快闪 80 的额度受保护，不会被直播重复占用）、快闪 60
    o1 = fulfillment.place_order(
        store, order_no="ORD-M-001", channel="museum-store", region="CN",
        items=[{"version_id": vid, "qty": 100}], on_date=on)
    o2 = fulfillment.place_order(
        store, order_no="ORD-L-001", channel="livestream", region="CN",
        items=[{"version_id": vid, "qty": 280}], on_date=on)
    o3 = fulfillment.place_order(
        store, order_no="ORD-P-001", channel="pop-up", region="CN",
        items=[{"version_id": vid, "qty": 60}], on_date=on)
    item_l = store.one("SELECT id FROM order_item WHERE order_id=?", (o2,))["id"]
    fulfillment.ship_item(store, item_id=item_l, qty=200, on_date=on)  # 部分发货

    # 处理质检事件：合格品已入库，「重新下单补足缺口」作为待人工裁决动作保留
    cases.process_events(store, on)

    return {
        "exhibition_id": "ex-ink-2026",
        "artworks": ["work-2026-41", "work-2026-42"],
        "licenses": ["lic-ink41-main", "lic-ink41-popup", "lic-ink42-promo"],
        "scarf_version": vid,
        "blocked_demo_version": bag_vid,
        "batch_id": batch,
        "orders": {"museum": o1, "livestream": o2, "popup": o3},
    }
