"""管理看板：临展结束前风险、库存敞口、待续权利；商品全链路追溯。"""

from __future__ import annotations

import json
from typing import Any

from . import fulfillment
from .store import Store


# ---------------------------------------------------------------- 待续权利与风险

def renewals_due(store: Store, on_date: str, within_days_from: str | None = None,
                 within_days: int = 60) -> list[dict[str, Any]]:
    """到期巡检视角：未来 within_days 内到期、且仍有在售/在产版本依赖的授权。"""
    conn = store.conn
    horizon = _add_days(within_days_from or on_date, within_days)
    rows = conn.execute(
        "SELECT l.*, a.title AS artwork_title FROM license l JOIN artwork a"
        " ON a.id=l.artwork_id WHERE l.status='active' AND l.valid_until>=?"
        " AND l.valid_until<=? ORDER BY l.valid_until",
        (on_date, horizon)).fetchall()
    out = []
    for lic in rows:
        dependents = conn.execute(
            "SELECT v.id, v.title, v.status FROM product_version v WHERE v.artwork_id=?"
            " AND v.status!='blocked'", (lic["artwork_id"],)).fetchall()
        if not dependents:
            continue
        active_listings = conn.execute(
            "SELECT COUNT(*) AS n FROM listing g JOIN product_version v ON v.id=g.version_id"
            " WHERE g.status='active' AND v.artwork_id=?", (lic["artwork_id"],)).fetchone()["n"]
        open_pos = conn.execute(
            "SELECT COUNT(*) AS n FROM purchase_order p JOIN product_version v"
            " ON v.id=p.version_id WHERE v.artwork_id=? AND p.status IN"
            " ('confirmed','in-production','delayed')", (lic["artwork_id"],)).fetchone()["n"]
        open_orders = conn.execute(
            "SELECT COALESCE(SUM(i.qty-i.qty_shipped-i.qty_cancelled),0) AS n"
            " FROM order_item i JOIN product_version v ON v.id=i.version_id"
            " WHERE v.artwork_id=? AND i.status='open'", (lic["artwork_id"],)).fetchone()["n"]
        out.append({
            "license_id": lic["id"], "license_code": lic["code"],
            "artwork_id": lic["artwork_id"], "artwork_title": lic["artwork_title"],
            "holder_id": lic["holder_id"], "valid_until": lic["valid_until"],
            "days_remaining": _days_between(on_date, lic["valid_until"]),
            "dependent_versions": [dict(d) for d in dependents],
            "active_listings": active_listings, "open_pos": open_pos,
            "unshipped_qty": open_orders,
        })
    return out


def exhibition_risk(store: Store, exhibition_id: str, on_date: str) -> dict[str, Any]:
    """临展结束前风险总览：展期、授权缺口、库存敞口、未结工单、未发货订单。"""
    conn = store.conn
    ex = conn.execute("SELECT * FROM exhibition WHERE id=?",
                      (exhibition_id,)).fetchone()
    if ex is None:
        return {"error": "展览不存在"}
    versions = conn.execute(
        "SELECT v.* FROM product_version v JOIN product p ON p.id=v.product_id"
        " WHERE p.exhibition_id=?", (exhibition_id,)).fetchall()
    version_risks = []
    for v in versions:
        review = _quick_review(store, v["id"], on_date)
        totals = fulfillment.stock_totals(conn, v["id"])
        unshipped = conn.execute(
            "SELECT COALESCE(SUM(qty-qty_shipped-qty_cancelled),0) AS n FROM order_item"
            " WHERE version_id=? AND status='open'", (v["id"],)).fetchone()["n"]
        quarantine = totals["quarantine"]
        open_po_qty = conn.execute(
            "SELECT COALESCE(SUM(qty_ordered),0) AS n FROM purchase_order"
            " WHERE version_id=? AND status IN ('confirmed','in-production','delayed')",
            (v["id"],)).fetchone()["n"]
        version_risks.append({
            "version_id": v["id"], "title": v["title"], "status": v["status"],
            "content_status": v["content_status"],
            "rights_markets": review,
            "stock": totals, "unshipped_qty": unshipped,
            "quarantine_qty": quarantine, "open_po_qty": open_po_qty,
            "exposure_cents": _exposure_cents(conn, v, totals, unshipped),
        })
    open_cases = conn.execute(
        "SELECT c.case_no,c.kind,c.title,c.status FROM case_record c"
        " WHERE c.status!='closed' AND ("
        " c.version_id IN (SELECT v.id FROM product_version v JOIN product p"
        " ON p.id=v.product_id WHERE p.exhibition_id=?)"
        " OR json_extract(c.scope,'$.exhibition_id')=?)",
        (exhibition_id, exhibition_id)).fetchall()
    ex_artwork_ids = [r["artwork_id"] for r in conn.execute(
        "SELECT v2.artwork_id FROM product_version v2 JOIN product p3"
        " ON p3.id=v2.product_id WHERE p3.exhibition_id=?", (exhibition_id,))]
    due = [r for r in renewals_due(store, on_date)
           if r["artwork_id"] in ex_artwork_ids]
    return {
        "exhibition": dict(ex),
        "days_to_close": _days_between(on_date, ex["end_date"]),
        "versions": version_risks,
        "open_cases": [dict(c) for c in open_cases],
        "renewals_due": due,
    }


def stock_exposure(store: Store, on_date: str) -> list[dict[str, Any]]:
    """库存敞口：合格在库 + 在途采购 − 已预留；并标注当前是否仍有权销售。"""
    conn = store.conn
    rows = conn.execute(
        "SELECT v.id AS version_id, v.title, p.sku, p.name AS product_name,"
        " v.price_cents, v.status FROM product_version v JOIN product p ON p.id=v.product_id"
    ).fetchall()
    out = []
    for v in rows:
        totals = fulfillment.stock_totals(conn, v["version_id"])
        inbound = conn.execute(
            "SELECT COALESCE(SUM(qty_ordered),0) AS n FROM purchase_order"
            " WHERE version_id=? AND status IN ('confirmed','in-production','delayed')",
            (v["version_id"],)).fetchone()["n"]
        review = _quick_review(store, v["version_id"], on_date)
        sellable_markets = [m for m in review if m["decision"] == "pass"]
        out.append({
            "version_id": v["version_id"], "sku": v["sku"],
            "product_name": v["product_name"], "title": v["title"],
            "on_hand": totals["on_hand"], "reserved": totals["reserved"],
            "sellable": totals["sellable"], "quarantine": totals["quarantine"],
            "inbound_po_qty": inbound,
            "excess_qty": max(0, totals["sellable"]),
            "sellable_now": bool(sellable_markets) and v["status"] != "blocked",
            "failing_markets": [m for m in review if m["decision"] == "fail"],
            "exposure_cents": totals["sellable"] * v["price_cents"],
        })
    return out


def _quick_review(store: Store, version_id: str, on_date: str) -> list[dict[str, Any]]:
    from .rights import evaluate_version
    result = evaluate_version(store, version_id, "sale", on_date)
    return [{"channel": m["channel"], "region": m["region"], "decision": m["decision"],
             "matched_license_code": m.get("matched_license_code"),
             "valid_until": m.get("valid_until"),
             "gaps": m.get("gaps_by_license", {})}
            for m in result["markets"]]


def _exposure_cents(conn, version, totals, unshipped) -> int:
    # 敞口 = 已预留未发货（可能要退款）+ 无渠道承接的可售库存 的货值
    stranded = max(0, totals["sellable"])
    return (unshipped + stranded) * version["price_cents"]


# ---------------------------------------------------------------- 全链路追溯

def trace_version(store: Store, version_id: str) -> dict[str, Any]:
    """从任一商品版本回溯：作品来源→授权与凭证→内容审核→闸门→采购批次质检→
    库存→订单发货→退款→版税。"""
    conn = store.conn
    v = conn.execute(
        "SELECT v.*, p.sku, p.name AS product_name, a.title AS artwork_title,"
        " a.exhibition_id, e.title AS exhibition_title FROM product_version v"
        " JOIN product p ON p.id=v.product_id JOIN artwork a ON a.id=v.artwork_id"
        " LEFT JOIN exhibition e ON e.id=a.exhibition_id WHERE v.id=?",
        (version_id,)).fetchone()
    if v is None:
        return {"error": "产品版本不存在"}
    holders = conn.execute(
        "SELECT h.*, s.share_bps FROM artwork_holder_split s JOIN right_holder h"
        " ON h.id=s.holder_id WHERE s.artwork_id=?", (v["artwork_id"],)).fetchall()
    licenses = []
    for lic in conn.execute("SELECT * FROM license WHERE artwork_id=? ORDER BY created_at",
                            (v["artwork_id"],)):
        licenses.append({
            **{k: lic[k] for k in lic.keys()},
            "uses": [r["use_type"] for r in conn.execute(
                "SELECT use_type FROM license_use WHERE license_id=?", (lic["id"],))],
            "channels": [r["channel"] for r in conn.execute(
                "SELECT channel FROM license_channel WHERE license_id=?", (lic["id"],))],
            "regions": [r["region"] for r in conn.execute(
                "SELECT region FROM license_region WHERE license_id=?", (lic["id"],))],
            "evidence": [dict(e) for e in conn.execute(
                "SELECT id,kind,document_ref,checksum,status,summary,recorded_at"
                " FROM license_evidence WHERE license_id=?", (lic["id"],))],
        })
    markets = [dict(r) for r in conn.execute(
        "SELECT channel,region FROM version_market WHERE version_id=?", (version_id,))]
    reviews = [dict(r) for r in conn.execute(
        "SELECT id,decision,reviewer,notes,decided_at FROM content_review"
        " WHERE version_id=? ORDER BY decided_at", (version_id,))]
    gates = [{"id": r["id"], "stage": r["stage"], "channel": r["channel"],
              "region": r["region"], "decision": r["decision"], "created_at": r["created_at"],
              "detail": json.loads(r["detail"])}
             for r in conn.execute(
                 "SELECT * FROM gate_review WHERE version_id=? ORDER BY created_at",
                 (version_id,))]
    listings = [dict(r) for r in conn.execute(
        "SELECT * FROM listing WHERE version_id=?", (version_id,))]
    batches = []
    for b in conn.execute(
            "SELECT b.*, po.po_no, po.status AS po_status, po.committed FROM"
            " production_batch b JOIN purchase_order po ON po.id=b.po_id"
            " WHERE b.version_id=? ORDER BY b.created_at", (version_id,)):
        batches.append({
            **{k: b[k] for k in b.keys()},
            "qc": [dict(q) for q in conn.execute(
                "SELECT id,qty_inspected,qty_passed,qty_failed,decision,notes,checked_at"
                " FROM quality_check WHERE batch_id=?", (b["id"],))]})
    pos = [dict(r) for r in conn.execute(
        "SELECT po.*, s.name AS supplier_name FROM purchase_order po"
        " JOIN supplier s ON s.id=po.supplier_id WHERE po.version_id=?",
        (version_id,))]
    ledger = [dict(r) for r in conn.execute(
        "SELECT id,batch_id,channel,delta_on_hand,delta_reserved,delta_quarantine,"
        "reason,ref,created_at FROM stock_ledger WHERE version_id=? ORDER BY created_at",
        (version_id,))]
    items = conn.execute(
        "SELECT i.*, o.order_no, o.channel, o.region FROM order_item i"
        " JOIN sales_order o ON o.id=i.order_id WHERE i.version_id=?",
        (version_id,)).fetchall()
    orders = []
    for i in items:
        orders.append({
            **{k: i[k] for k in i.keys()},
            "shipments": [dict(s) for s in conn.execute(
                "SELECT * FROM shipment WHERE item_id=?", (i["id"],))],
            "refunds": [dict(r) for r in conn.execute(
                "SELECT * FROM refund WHERE item_id=?", (i["id"],))]})
    royalty_rows = [dict(r) for r in conn.execute(
        "SELECT id,license_id,holder_id,basis,ref_id,qty,net_cents,amount_cents,"
        "rate_bps,share_bps,period_key,settlement_id,created_at FROM royalty_accrual"
        " WHERE version_id=? ORDER BY created_at", (version_id,))]
    cases = [dict(r) for r in conn.execute(
        "SELECT id,case_no,kind,title,status,created_at FROM case_record"
        " WHERE version_id=?", (version_id,))]
    return {
        "product_version": {k: v[k] for k in v.keys()},
        "holder_splits": [dict(h) for h in holders],
        "planned_markets": markets,
        "licenses": licenses,
        "content_reviews": reviews,
        "gate_reviews": gates,
        "listings": listings,
        "purchase_orders": pos,
        "production_batches": batches,
        "stock_ledger": ledger,
        "orders": orders,
        "royalty_accruals": royalty_rows,
        "cases": cases,
    }


def _add_days(date_str: str, days: int) -> str:
    from datetime import date, timedelta
    y, m, d = map(int, date_str.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def _days_between(a: str, b: str) -> int:
    from datetime import date
    ya, ma, da = map(int, a.split("-"))
    yb, mb, db = map(int, b.split("-"))
    return (date(yb, mb, db) - date(ya, ma, da)).days
