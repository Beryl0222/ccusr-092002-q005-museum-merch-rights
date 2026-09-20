"""风险看板与商品全链路追溯。"""

from .catalog import _one


def _dates(db, sql: str, params=()):
    return [r[0] for r in db.execute(sql, params).fetchall()]


def risk_dashboard(db, exhibition_code: str | None = None,
                   horizon_days: int = 60) -> dict:
    """临展结束前风险：到期/待续权利、库存敞口、未决事件动作、延期采购单。"""
    if exhibition_code:
        exhibition = dict(_one(db, "SELECT * FROM exhibitions WHERE code=?",
                               (exhibition_code,)))
        end_date = exhibition["end_date"]
        scope_filter, params = "AND a.exhibition_id=?", (exhibition["id"],)
    else:
        exhibition = None
        end_date = None
        scope_filter, params = "", ()

    # 待续权利：覆盖在展作品、仍有在售/在产版本的许可；active 临期与 expired 已断权都列出
    expiring_rows = db.execute(
        f"""
        SELECT DISTINCT l.code, l.status AS license_status, l.valid_until,
               a.code AS artwork_code,
               a.title AS artwork_title,
               (SELECT COUNT(*) FROM product_versions v WHERE v.artwork_id=a.id
                 AND v.status IN ('approved','production','on_sale','sale_suspended')) AS live_versions
        FROM licenses l JOIN artworks a ON a.id=l.artwork_id
        WHERE l.status IN ('active','expired') {scope_filter}
        ORDER BY l.valid_until
        """,
        params,
    ).fetchall()

    expiring = []
    for r in expiring_rows:
        expiring.append({
            "license": r["code"],
            "status": r["license_status"],
            "artwork": r["artwork_code"],
            "artwork_title": r["artwork_title"],
            "valid_until": r["valid_until"],
            "live_versions": r["live_versions"],
            "expires_before_exhibition_end": bool(end_date and r["valid_until"] < end_date),
        })
    expiring.sort(key=lambda x: (x["status"] != "expired", x["valid_until"]))

    # 库存敞口：被占用但没有足够在手库存覆盖；以及在途批次延期/停产无法补货
    # 库存敞口：以 ATP（在手 + 在途未废数量 − 已占用）衡量
    exposure_rows = db.execute(
        """
        SELECT v.code AS version, v.name, i.on_hand, i.allocated, i.shipped,
               COALESCE((SELECT SUM(po.qty
                 - COALESCE((SELECT SUM(received_qty) FROM production_batches WHERE po_id=po.id),0)
                 - COALESCE((SELECT SUM(qc_failed_qty) FROM production_batches WHERE po_id=po.id),0))
                 FROM purchase_orders po WHERE po.version_id=v.id
                 AND po.status IN ('locked','in_production','delayed')),0) AS incoming,
               (SELECT COUNT(*) FROM purchase_orders po WHERE po.version_id=v.id
                 AND po.status IN ('delayed','production_halted')) AS at_risk_pos
        FROM inventory i JOIN product_versions v ON v.id=i.version_id
        WHERE i.allocated > 0 OR i.on_hand > 0
        """
    ).fetchall()
    exposure = []
    for r in exposure_rows:
        exposure.append({
            "version": r["version"],
            "name": r["name"],
            "on_hand": r["on_hand"],
            "incoming": r["incoming"],
            "allocated": r["allocated"],
            "shipped": r["shipped"],
            "atp": r["on_hand"] + r["incoming"] - r["allocated"],
            "at_risk_purchase_orders": r["at_risk_pos"],
        })

    pending = [dict(r) for r in db.execute(
        "SELECT a.id, a.action_type, a.entity_type, a.entity_ref, a.detail,"
        " i.code AS incident_code, i.type AS incident_type"
        " FROM incident_actions a JOIN incidents i ON i.id=a.incident_id"
        " WHERE a.status='pending' ORDER BY a.id"
    ).fetchall()]

    delayed_pos = [dict(r) for r in db.execute(
        "SELECT code, expected_at, status, note FROM purchase_orders"
        " WHERE status IN ('delayed','production_halted') ORDER BY expected_at"
    ).fetchall()]

    suspended = [dict(r) for r in db.execute(
        "SELECT v.code AS version, li.channel, li.region, li.reason, li.suspended_at"
        " FROM listings li JOIN product_versions v ON v.id=li.version_id"
        " WHERE li.status='suspended' ORDER BY li.suspended_at DESC"
    ).fetchall()]

    return {
        "exhibition": exhibition_code,
        "exhibition_end": end_date,
        "renewals_needed": expiring,
        "inventory_exposure": exposure,
        "pending_actions": pending,
        "delayed_or_halted_purchase_orders": delayed_pos,
        "suspended_listings": suspended,
    }


def trace_version(db, version_code: str) -> dict:
    """从任一商品版本向前追溯：作品来源 → 许可与证据 → 审核 → 采购批次 →
    库存 → 上架 → 订单/发货/退款 → 版税。"""
    version = dict(_one(db, "SELECT * FROM product_versions WHERE code=?",
                        (version_code,)))
    artwork = dict(_one(db, "SELECT * FROM artworks WHERE id=?",
                        (version["artwork_id"],)))
    holder = dict(_one(db, "SELECT * FROM right_holders WHERE id=?",
                       (artwork["holder_id"],)))
    exhibition = None
    if artwork["exhibition_id"]:
        exhibition = dict(_one(db, "SELECT * FROM exhibitions WHERE id=?",
                               (artwork["exhibition_id"],)))

    licenses = []
    for lrow in db.execute("SELECT * FROM licenses WHERE artwork_id=?",
                           (artwork["id"],)).fetchall():
        lic = dict(lrow)
        docs = [dict(r) for r in db.execute(
            "SELECT doc_type, doc_ref, issued_by, received_at FROM license_documents"
            " WHERE license_id=? ORDER BY received_at", (lic["id"],)).fetchall()]
        licenses.append({
            "code": lic["code"], "uses": lic["uses_json"],
            "channels": lic["channels_json"], "regions": lic["regions_json"],
            "valid_from": lic["valid_from"], "valid_until": lic["valid_until"],
            "status": lic["status"], "revoked_at": lic["revoked_at"],
            "royalty_rate": lic["royalty_rate"], "royalty_unit_fee": lic["royalty_unit_fee"],
            "documents": docs,
        })

    reviews = [dict(r) for r in db.execute(
        "SELECT decision, reviewer, decided_at, notes, doc_ref FROM content_reviews"
        " WHERE version_id=? ORDER BY decided_at", (version["id"],)).fetchall()]

    pos = []
    for prow in db.execute("SELECT * FROM purchase_orders WHERE version_id=?",
                           (version["id"],)).fetchall():
        po = dict(prow)
        supplier = dict(_one(db, "SELECT code, name FROM suppliers WHERE id=?",
                             (po["supplier_id"],)))
        batches = [dict(r) for r in db.execute(
            "SELECT code,status,qty_planned,qc_passed_qty,qc_failed_qty,received_qty,"
            "shipped_qty,inspected_at,received_at,inspector,note"
            " FROM production_batches WHERE po_id=? ORDER BY id", (po["id"],)).fetchall()]
        pos.append({"code": po["code"], "supplier": supplier, "qty": po["qty"],
                    "status": po["status"], "committed": bool(po["committed"]),
                    "locked_at": po["locked_at"], "expected_at": po["expected_at"],
                    "completed_at": po["completed_at"], "batches": batches})

    inv = dict(_one(db, "SELECT * FROM inventory WHERE version_id=?", (version["id"],)))
    ledger = [dict(r) for r in db.execute(
        "SELECT change_qty,reason,channel,batch_id,ref,created_at FROM stock_ledger"
        " WHERE version_id=? ORDER BY id", (version["id"],)).fetchall()]
    reservations = [dict(r) for r in db.execute(
        "SELECT channel,reserved_qty FROM channel_reservations WHERE version_id=?",
        (version["id"],)).fetchall()]

    listings = [dict(r) for r in db.execute(
        "SELECT channel,region,status,listed_at,suspended_at,reason FROM listings"
        " WHERE version_id=? ORDER BY channel,region", (version["id"],)).fetchall()]

    sales = []
    for irow in db.execute(
        "SELECT oi.*, o.code AS order_code, o.channel, o.region, o.status AS order_status"
        " FROM order_items oi JOIN orders o ON o.id=oi.order_id"
        " WHERE oi.version_id=? ORDER BY oi.id", (version["id"],)).fetchall():
        item = dict(irow)
        lic_code = None
        if item["license_id"]:
            lic_code = _one(db, "SELECT code FROM licenses WHERE id=?",
                            (item["license_id"],))["code"]
        shipments = [dict(r) for r in db.execute(
            "SELECT s.id, s.qty, s.shipped_at, b.code AS batch_code FROM shipments s"
            " LEFT JOIN production_batches b ON b.id=s.batch_id WHERE s.item_id=?",
            (item["id"],)).fetchall()]
        refunds = [dict(r) for r in db.execute(
            "SELECT qty,amount,reason,created_at FROM refunds WHERE item_id=?",
            (item["id"],)).fetchall()]
        sales.append({"order": item["order_code"], "channel": item["channel"],
                      "region": item["region"], "order_status": item["order_status"],
                      "item_id": item["id"], "qty": item["qty"],
                      "allocated": item["allocated_qty"],
                      "shipped": item["shipped_qty"], "refunded": item["refunded_qty"],
                      "unit_price": item["unit_price"], "item_status": item["status"],
                      "license_snapshot": lic_code,
                      "shipments": shipments, "refunds": refunds,
                      "substitute_for_item_id": item["substitute_for_item_id"]})

    # 版税：凡快照到本作品许可的结算行
    royalty = [dict(r) for r in db.execute(
        """
        SELECT rs.period_from, rs.period_to, rs.net_sales, rs.rate, rs.amount,
               l.code AS license_code
        FROM royalty_settlements rs JOIN licenses l ON l.id=rs.license_id
        WHERE l.artwork_id=? ORDER BY rs.period_from
        """,
        (artwork["id"],)).fetchall()]

    # 事件：收集本版本相关的全部实体引用（许可/版本/采购单/批次/上架/订单行）
    license_codes = [l["code"] for l in licenses]
    po_codes = [p["code"] for p in pos]
    batch_codes = [b["code"] for p in pos for b in p["batches"]]
    listing_refs = [f"{version_code}:{l['channel']}:{l['region']}" for l in listings]
    item_ids = [s["item_id"] for s in sales]
    refs = set(po_codes + batch_codes + listing_refs + [version_code]
               + [str(i) for i in item_ids])
    incident_rows = db.execute(
        "SELECT DISTINCT i.* FROM incidents i"
        " JOIN incident_actions a ON a.incident_id=i.id ORDER BY i.occurred_at"
    ).fetchall()
    incidents = []
    for irow in incident_rows:
        inc = dict(irow)
        if inc["type"] == "license_revoked" and inc["subject"] in license_codes:
            incidents.append({"code": inc["code"], "type": inc["type"],
                              "subject": inc["subject"], "summary": inc["summary"],
                              "occurred_at": inc["occurred_at"]})
            continue
        actions = db.execute(
            "SELECT entity_type, entity_ref FROM incident_actions WHERE incident_id=?",
            (inc["id"],)).fetchall()
        if any(a["entity_ref"] in refs for a in actions):
            incidents.append({"code": inc["code"], "type": inc["type"],
                              "subject": inc["subject"], "summary": inc["summary"],
                              "occurred_at": inc["occurred_at"]})

    return {
        "version": {"code": version["code"], "name": version["name"],
                    "status": version["status"], "is_derivative": bool(version["is_derivative"])},
        "artwork": {"code": artwork["code"], "title": artwork["title"],
                    "artist": artwork["artist"]},
        "right_holder": {"code": holder["code"], "name": holder["name"], "kind": holder["kind"]},
        "exhibition": exhibition and {"code": exhibition["code"], "title": exhibition["title"],
                                      "start_date": exhibition["start_date"],
                                      "end_date": exhibition["end_date"],
                                      "status": exhibition["status"]},
        "licenses": licenses,
        "content_reviews": reviews,
        "purchase_orders": pos,
        "inventory": {"on_hand": inv["on_hand"], "allocated": inv["allocated"],
                      "shipped": inv["shipped"]},
        "reservations": reservations,
        "stock_ledger": ledger,
        "listings": listings,
        "sales": sales,
        "royalty_settlements": royalty,
        "incidents": incidents,
    }
