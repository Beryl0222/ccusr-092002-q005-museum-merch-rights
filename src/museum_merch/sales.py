"""上架、订单、共享库存占用与版税结算。

防超卖的两道闸（在同一 BEGIN IMMEDIATE 事务内判定）：
1. 全局：版本可售库存 on_hand - allocated 必须足够；直播、馆内商店、快闪共享同一池。
2. 渠道：该渠道当前已占用（已下单未出库）数量加上本次占用，不得超过预留额度。
订单只减占用、不删记录；部分发货按批次先进先出；取消/退款立即释放占用。
版税按下单时快照的有效许可与实际净销售（发货计销售、退款冲减）结算。
"""

import json
from decimal import Decimal

from . import rights
from .catalog import DomainError, _audit, _one, get_version, now


def _begin(db) -> bool:
    """开启写事务；已在外层事务中则复用（内层不提交、不回滚）。"""
    if db.in_transaction:
        return False
    db.execute("BEGIN IMMEDIATE")
    return True


# ---------- 上架 ----------

def list_product(db, version_code: str, channel: str, region: str,
                 on_date: str | None = None) -> int:
    version = get_version(db, version_code)
    result = rights.evaluate_version(db, version, channel, region, on_date)
    if not result["ok"]:
        raise DomainError("上架门禁未通过", result["reasons"])
    cur = db.execute(
        "INSERT INTO listings(version_id,channel,region,status,listed_at)"
        " VALUES (?,?,?,'active',?)"
        " ON CONFLICT(version_id,channel,region) DO UPDATE SET"
        " status='active', suspended_at=NULL, reason=NULL",
        (version["id"], channel, region, now()),
    )
    if version["status"] in ("approved", "production"):
        db.execute(
            "UPDATE product_versions SET status='on_sale', updated_at=? WHERE id=?",
            (now(), version["id"]),
        )
    _audit(db, "listing", f"{version_code}:{channel}:{region}", "list", {})
    db.commit()
    return cur.lastrowid


def _channel_held(db, version_id: int, channel: str, exclude_item: int | None = None) -> int:
    sql = (
        "SELECT COALESCE(SUM(oi.allocated_qty - oi.shipped_qty),0) AS held"
        " FROM order_items oi JOIN orders o ON o.id=oi.order_id"
        " WHERE oi.version_id=? AND o.channel=?"
        " AND o.status IN ('open','partially_shipped')"
        " AND oi.status IN ('open','partial')"
    )
    params: list = [version_id, channel]
    if exclude_item is not None:
        sql += " AND oi.id<>?"
        params.append(exclude_item)
    return db.execute(sql, params).fetchone()["held"]


def _version_atp(db, version_id: int) -> dict:
    """可承诺库存（Available To Promise）。

    直播/快闪常按在产订单预售：incoming = 未停产采购单中既未入库也未质检报废的数量；
    atp = on_hand + incoming - allocated。批次质检不合格会立刻压缩 incoming，
    从而暴露已售订单的缺口。
    """
    inv = _one(db, "SELECT * FROM inventory WHERE version_id=?", (version_id,))
    row = db.execute(
        """
        SELECT COALESCE(SUM(po.qty
                 - COALESCE((SELECT SUM(received_qty) FROM production_batches
                              WHERE po_id=po.id), 0)
                 - COALESCE((SELECT SUM(qc_failed_qty) FROM production_batches
                              WHERE po_id=po.id), 0)), 0) AS incoming
        FROM purchase_orders po
        WHERE po.version_id=? AND po.status IN ('locked','in_production','delayed')
        """,
        (version_id,),
    ).fetchone()
    incoming = row["incoming"]
    return {"on_hand": inv["on_hand"], "allocated": inv["allocated"],
            "shipped": inv["shipped"], "incoming": incoming,
            "atp": inv["on_hand"] + incoming - inv["allocated"]}


# ---------- 下单 ----------

def create_order(db, code: str, channel: str, region: str,
                 items: list[dict], customer_ref: str | None = None,
                 on_date: str | None = None) -> int:
    """items: [{'version': 版本码, 'qty': 数量, 'unit_price': '单件净价'}]"""
    if not items:
        raise DomainError("订单至少包含一个商品行")
    on_date = on_date or now()
    _owner = _begin(db)
    try:
        cur = db.execute(
            "INSERT INTO orders(code,channel,region,status,customer_ref,created_at)"
            " VALUES (?,?,?,'open',?,?)",
            (code, channel, region, customer_ref, now()),
        )
        order_id = cur.lastrowid
        for line in items:
            version = get_version(db, line["version"])
            qty = int(line["qty"])
            if qty <= 0:
                raise DomainError("订购数量必须大于 0")
            listing = db.execute(
                "SELECT * FROM listings WHERE version_id=? AND channel=? AND region=?"
                " AND status='active'",
                (version["id"], channel, region),
            ).fetchone()
            if listing is None:
                raise DomainError(
                    f"版本 {line['version']} 未在 {channel}/{region} 有效上架"
                )
            # 授权到期/撤回后，即使 listing 未来得及下架也不得继续成交
            result = rights.evaluate_version(db, version, channel, region, on_date)
            if not result["ok"]:
                raise DomainError(
                    f"版本 {line['version']} 权利或审核门禁未通过，不能销售", result["reasons"]
                )
            atp = _version_atp(db, version["id"])
            if atp["atp"] < qty:
                raise DomainError(
                    f"版本 {line['version']} 可承诺库存不足："
                    f"在手 {atp['on_hand']} + 在途 {atp['incoming']}"
                    f" - 已占用 {atp['allocated']} = {atp['atp']}，需求 {qty}"
                )
            reservation = db.execute(
                "SELECT * FROM channel_reservations WHERE version_id=? AND channel=?",
                (version["id"], channel),
            ).fetchone()
            if reservation is not None:
                held = _channel_held(db, version["id"], channel)
                if held + qty > reservation["reserved_qty"]:
                    raise DomainError(
                        f"渠道 {channel} 预留不足：预留 {reservation['reserved_qty']}，"
                        f"已占用 {held}，本次需求 {qty}"
                    )
            lic = rights.pick_order_license(db, version, channel, region, on_date)
            db.execute(
                "INSERT INTO order_items(order_id,version_id,qty,allocated_qty,"
                "unit_price,license_id,status) VALUES (?,?,?,?,?,?,'open')",
                (order_id, version["id"], qty, qty, str(line["unit_price"]),
                 lic["id"] if lic else None),
            )
            db.execute(
                "UPDATE inventory SET allocated=allocated+? WHERE version_id=?",
                (qty, version["id"]),
            )
            db.execute(
                "INSERT INTO stock_ledger(version_id,change_qty,reason,channel,ref,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (version["id"], qty, "order_allocate", channel, code, now()),
            )
        _audit(db, "order", code, "create", {"channel": channel, "items": len(items)})
        if _owner:
            db.commit()
    except Exception:
        if _owner:
            db.rollback()
        raise
    return order_id


def _get_order(db, code: str) -> dict:
    return dict(_one(db, "SELECT * FROM orders WHERE code=?", (code,)))


def _recalc_order_status(db, order_id: int) -> None:
    rows = db.execute(
        "SELECT status, qty, shipped_qty FROM order_items WHERE order_id=?",
        (order_id,),
    ).fetchall()
    if all(r["status"] in ("cancelled", "refunded", "substituted") for r in rows):
        status = "cancelled"
    elif all(r["shipped_qty"] >= r["qty"] for r in rows if r["status"] not in
             ("cancelled", "refunded", "substituted")) and any(
                 r["shipped_qty"] > 0 for r in rows):
        status = "shipped"
    elif any(r["shipped_qty"] > 0 for r in rows):
        status = "partially_shipped"
    else:
        status = "open"
    db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))


# ---------- 发货（按批次先进先出，支持部分发货） ----------

def ship_order(db, code: str, item_id: int | None = None,
               qty: int | None = None) -> list[dict]:
    _owner = _begin(db)
    shipped_rows: list[dict] = []
    try:
        order = _get_order(db, code)
        if order["status"] == "cancelled":
            raise DomainError("订单已取消，不能发货")
        item_sql = "SELECT * FROM order_items WHERE order_id=?"
        params: list = [order["id"]]
        if item_id is not None:
            item_sql += " AND id=?"
            params.append(item_id)
        items = [dict(r) for r in db.execute(item_sql, params).fetchall()
                 if r["status"] in ("open", "partial")]
        if not items:
            raise DomainError("没有可发货的订单行")
        for item in items:
            want = item["qty"] - item["shipped_qty"]
            if qty is not None:
                want = min(want, qty)  # 指定数量时按数量部分发货（单订单行即精确控制）
            if want <= 0:
                continue
            batches = db.execute(
                "SELECT * FROM production_batches WHERE version_id=?"
                " AND status IN ('qc_passed','received')"
                " AND received_qty - shipped_qty > 0 ORDER BY received_at, id",
                (item["version_id"],),
            ).fetchall()
            remaining = want
            for batch in batches:
                available = batch["received_qty"] - batch["shipped_qty"]
                take = min(available, remaining)
                if take <= 0:
                    continue
                db.execute(
                    "UPDATE production_batches SET shipped_qty=shipped_qty+? WHERE id=?",
                    (take, batch["id"]),
                )
                cur = db.execute(
                    "INSERT INTO shipments(order_id,item_id,batch_id,qty,shipped_at)"
                    " VALUES (?,?,?,?,?)",
                    (order["id"], item["id"], batch["id"], take, now()),
                )
                shipped_rows.append({"shipment_id": cur.lastrowid, "batch": batch["code"],
                                     "qty": take})
                db.execute(
                    "UPDATE inventory SET on_hand=on_hand-?, allocated=allocated-?,"
                    " shipped=shipped+? WHERE version_id=?",
                    (take, take, take, item["version_id"]),
                )
                db.execute(
                    "INSERT INTO stock_ledger(version_id,batch_id,change_qty,reason,"
                    "channel,ref,created_at) VALUES (?,?,?,?,?,?,?)",
                    (item["version_id"], batch["id"], -take, "ship", order["channel"],
                     code, now()),
                )
                remaining -= take
                if remaining == 0:
                    break
            shipped_now = want - remaining
            if shipped_now > 0:
                db.execute(
                    "UPDATE order_items SET shipped_qty=shipped_qty+?,"
                    " status=CASE WHEN shipped_qty+?>=qty THEN 'shipped' ELSE 'partial' END"
                    " WHERE id=?",
                    (shipped_now, shipped_now, item["id"]),
                )
            if remaining > 0:
                # 库存不足：保留未发部分的占用，不超卖；订单可部分发货
                db.execute(
                    "INSERT INTO audit_log(entity_type,entity_ref,action,detail_json,created_at)"
                    " VALUES ('order',?,'short_ship',?,?)",
                    (code, json.dumps({"short": remaining, "item_id": item["id"]},
                                      ensure_ascii=False), now()),
                )
        _recalc_order_status(db, order["id"])
        if _owner:
            db.commit()
    except Exception:
        if _owner:
            db.rollback()
        raise
    return shipped_rows


# ---------- 取消 / 退款 / 替代 ----------

def _release(db, item: dict, qty: int, order: dict, reason: str) -> None:
    db.execute(
        "UPDATE inventory SET allocated=allocated-? WHERE version_id=?",
        (qty, item["version_id"]),
    )
    db.execute(
        "INSERT INTO stock_ledger(version_id,change_qty,reason,channel,ref,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (item["version_id"], -qty, reason, order["channel"], order["code"], now()),
    )


def cancel_order(db, code: str, lines: list[dict] | None = None,
                 reason: str = "customer_cancel") -> dict:
    """取消整单或指定行的指定未发货数量，立即释放渠道预留与全局占用。"""
    _owner = _begin(db)
    released = 0
    try:
        order = _get_order(db, code)
        wanted = {int(l["item_id"]): int(l.get("qty", 10**12)) for l in (lines or [])}
        items = db.execute(
            "SELECT * FROM order_items WHERE order_id=? AND status IN ('open','partial')",
            (order["id"],),
        ).fetchall()
        for row in items:
            item = dict(row)
            unshipped = item["allocated_qty"] - item["shipped_qty"]
            qty = min(unshipped, wanted.get(item["id"], unshipped)) if wanted else unshipped
            if qty <= 0:
                continue
            _release(db, item, qty, order, reason)
            new_shipped = item["shipped_qty"]
            fully = new_shipped == 0 and (qty >= unshipped)
            db.execute(
                "UPDATE order_items SET allocated_qty=?, status=? WHERE id=?",
                (item["allocated_qty"] - qty,
                 "cancelled" if fully else "partial", item["id"]),
            )
            released += qty
        _recalc_order_status(db, order["id"])
        _audit(db, "order", code, "cancel", {"released": released, "reason": reason})
        if _owner:
            db.commit()
    except Exception:
        if _owner:
            db.rollback()
        raise
    return {"order": code, "released": released}


def refund_item(db, item_id: int, qty: int | None, reason: str) -> dict:
    """对未发货占用数量退款并释放库存（用于授权撤回等处置）。已寄出部分不追回。"""
    _owner = _begin(db)
    try:
        item = dict(_one(db, "SELECT * FROM order_items WHERE id=?", (item_id,)))
        order = _one(db, "SELECT * FROM orders WHERE id=?", (item["order_id"],))
        refundable = item["allocated_qty"] - item["shipped_qty"] - item["refunded_qty"]
        qty = min(refundable, qty) if qty is not None else refundable
        if qty <= 0:
            raise DomainError("该订单行没有可退款的未发货数量")
        amount = (Decimal(item["unit_price"]) * qty).quantize(Decimal("0.01"))
        db.execute(
            "INSERT INTO refunds(order_id,item_id,qty,amount,reason,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (order["id"], item_id, qty, str(amount), reason, now()),
        )
        _release(db, item, qty, dict(order), "refund_release")
        new_refunded = item["refunded_qty"] + qty
        new_allocated = item["allocated_qty"] - qty
        status = "refunded" if item["shipped_qty"] == 0 else "partial"
        db.execute(
            "UPDATE order_items SET refunded_qty=?, allocated_qty=?, status=? WHERE id=?",
            (new_refunded, new_allocated, status, item_id),
        )
        _recalc_order_status(db, order["id"])
        db.commit()
        return {"item_id": item_id, "refunded_qty": qty, "amount": str(amount)}
    except Exception:
        db.rollback()
        raise


def substitute_item(db, item_id: int, substitute_version_code: str,
                    qty: int | None = None) -> dict:
    """用另一（权利干净的）版本替代未发货部分：释放原占用，在新版本上重新占用。"""
    _owner = _begin(db)
    try:
        old = dict(_one(db, "SELECT * FROM order_items WHERE id=?", (item_id,)))
        order = dict(_one(db, "SELECT * FROM orders WHERE id=?", (old["order_id"],)))
        replaceable = old["allocated_qty"] - old["shipped_qty"]
        qty = min(replaceable, qty) if qty is not None else replaceable
        if qty <= 0:
            raise DomainError("该订单行没有可替代的未发货数量")
        new_version = get_version(db, substitute_version_code)
        result = rights.evaluate_version(
            db, new_version, order["channel"], order["region"]
        )
        if not result["ok"]:
            raise DomainError("替代版本的权利或审核门禁未通过", result["reasons"])
        atp = _version_atp(db, new_version["id"])
        if atp["atp"] < qty:
            raise DomainError(
                f"替代版本可承诺库存不足：在手 {atp['on_hand']} + 在途 {atp['incoming']}"
                f" - 已占用 {atp['allocated']} = {atp['atp']}")
        reservation = db.execute(
            "SELECT * FROM channel_reservations WHERE version_id=? AND channel=?",
            (new_version["id"], order["channel"]),
        ).fetchone()
        if reservation is not None:
            held = _channel_held(db, new_version["id"], order["channel"])
            if held + qty > reservation["reserved_qty"]:
                raise DomainError("替代版本在该渠道的预留不足")
        lic = rights.pick_order_license(db, new_version, order["channel"], order["region"])
        # 先释放原占用
        _release(db, old, qty, order, "substitute_release")
        cur = db.execute(
            "INSERT INTO order_items(order_id,version_id,qty,allocated_qty,unit_price,"
            "license_id,substitute_for_item_id,status) VALUES (?,?,?,?,?,?,?,'open')",
            (order["id"], new_version["id"], qty, qty, old["unit_price"],
             lic["id"] if lic else None, old["id"]),
        )
        db.execute(
            "UPDATE inventory SET allocated=allocated+? WHERE version_id=?",
            (qty, new_version["id"]),
        )
        db.execute(
            "INSERT INTO stock_ledger(version_id,change_qty,reason,channel,ref,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (new_version["id"], qty, "substitute_allocate", order["channel"],
             order["code"], now()),
        )
        fully = old["shipped_qty"] == 0 and qty >= replaceable
        db.execute(
            "UPDATE order_items SET allocated_qty=?, status=? WHERE id=?",
            (old["allocated_qty"] - qty, "substituted" if fully else "partial", old["id"]),
        )
        _recalc_order_status(db, order["id"])
        db.commit()
        return {"new_item_id": cur.lastrowid, "version": substitute_version_code, "qty": qty}
    except Exception:
        db.rollback()
        raise


# ---------- 库存视图 ----------

def inventory_view(db, version_code: str) -> dict:
    version = get_version(db, version_code)
    atp = _version_atp(db, version["id"])
    channels = db.execute(
        "SELECT channel, reserved_qty FROM channel_reservations WHERE version_id=?",
        (version["id"],),
    ).fetchall()
    channel_view = []
    for row in channels:
        held = _channel_held(db, version["id"], row["channel"])
        channel_view.append({
            "channel": row["channel"],
            "reserved": row["reserved_qty"],
            "held": held,
            "reservation_free": row["reserved_qty"] - held,
        })
    return {
        "version": version_code,
        "on_hand": atp["on_hand"],
        "incoming": atp["incoming"],
        "allocated": atp["allocated"],
        "shipped": atp["shipped"],
        "sellable": atp["atp"],
        "channels": channel_view,
    }


# ---------- 版税结算 ----------

def settle_royalty(db, license_code: str, period_from: str, period_to: str) -> dict:
    lic = dict(_one(db, "SELECT * FROM licenses WHERE code=?", (license_code,)))
    sales = db.execute(
        "SELECT s.id AS shipment_id, s.qty, s.shipped_at, oi.unit_price, oi.id AS item_id"
        " FROM shipments s JOIN order_items oi ON oi.id=s.item_id"
        " WHERE oi.license_id=? AND s.shipped_at BETWEEN ? AND ?",
        (lic["id"], period_from, period_to),
    ).fetchall()
    refunds = db.execute(
        "SELECT r.* FROM refunds r JOIN order_items oi ON oi.id=r.item_id"
        " WHERE oi.license_id=? AND r.created_at BETWEEN ? AND ?",
        (lic["id"], period_from, period_to),
    ).fetchall()
    gross = sum((Decimal(r["unit_price"]) * r["qty"] for r in sales), Decimal("0"))
    # 本系统在发货时确认销售；refund_item 只退未发货占用（已发货不追回），
    # 因此这些退款对应的销售从未被确认，不冲减净销售额，仅作备注留存。
    unshipped_refunds = sum((Decimal(r["amount"]) for r in refunds), Decimal("0"))
    net_sales = gross
    net_qty = sum(r["qty"] for r in sales)
    if lic["royalty_rate"]:
        amount = (net_sales * Decimal(lic["royalty_rate"])).quantize(Decimal("0.01"))
        rate = lic["royalty_rate"]
    elif lic["royalty_unit_fee"]:
        amount = (Decimal(lic["royalty_unit_fee"]) * max(net_qty, 0)).quantize(Decimal("0.01"))
        rate = None
    else:
        raise DomainError(f"许可 {license_code} 未约定版税比例或单件费用")
    cur = db.execute(
        "INSERT INTO royalty_settlements(license_id,period_from,period_to,net_sales,"
        "rate,amount,status,created_at) VALUES (?,?,?,?,?,?,'settled',?)",
        (lic["id"], period_from, period_to, str(net_sales), rate, str(amount), now()),
    )
    settlement_id = cur.lastrowid
    for r in sales:
        db.execute(
            "INSERT INTO royalty_settlement_lines(settlement_id,kind,item_id,shipment_id,"
            "qty,amount,occurred_at) VALUES (?,'sale',?,?,?,?,?)",
            (settlement_id, r["item_id"], r["shipment_id"], r["qty"],
             str(Decimal(r["unit_price"]) * r["qty"]), r["shipped_at"]),
        )
    for r in refunds:
        # 未发货退款不计入净销售；以零金额明细保留可追溯记录
        db.execute(
            "INSERT INTO royalty_settlement_lines(settlement_id,kind,item_id,refund_id,"
            "qty,amount,occurred_at) VALUES (?,'unshipped_refund',?,?,?, '0', ?)",
            (settlement_id, r["item_id"], r["id"], r["qty"], r["created_at"]),
        )
    _audit(db, "license", license_code, "royalty_settle",
           {"period": [period_from, period_to], "amount": str(amount)})
    db.commit()
    return {
        "settlement_id": settlement_id,
        "license": license_code,
        "period": [period_from, period_to],
        "gross_sales": str(gross),
        "unshipped_refunds_excluded": str(unshipped_refunds),
        "net_sales": str(net_sales),
        "net_qty": net_qty,
        "rate": rate,
        "royalty_amount": str(amount),
    }
