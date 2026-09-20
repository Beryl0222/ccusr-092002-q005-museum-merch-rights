"""采购、生产批次、质检、共享库存与多渠道订单履约。"""

from __future__ import annotations

import json
from typing import Any

from . import royalty
from .rights import evaluate_version, gate_for_procurement
from .store import DomainError, Store, new_id, now

OPEN_PO_STATUSES = ("confirmed", "in-production", "delayed")


# ---------------------------------------------------------------- 库存口径

def stock_totals(conn, version_id: str) -> dict[str, int]:
    """三个口径全部由 stock_ledger 累计：合格在库 / 各渠道预留 / 质检隔离。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(delta_on_hand),0) AS on_hand,"
        " COALESCE(SUM(delta_reserved),0) AS reserved,"
        " COALESCE(SUM(delta_quarantine),0) AS quarantine"
        " FROM stock_ledger WHERE version_id=?", (version_id,)).fetchone()
    return {"on_hand": row["on_hand"], "reserved": row["reserved"],
            "quarantine": row["quarantine"],
            "sellable": row["on_hand"] - row["reserved"]}


def channel_held(conn, version_id: str, channel: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(delta_reserved),0) AS held FROM stock_ledger"
        " WHERE version_id=? AND channel=?", (version_id, channel)).fetchone()
    return row["held"]


def channel_cap(conn, version_id: str, channel: str) -> int:
    """渠道 C 还可再预留多少件共享库存。

    不变量：C 增加预留后，其余每个有额度渠道仍须能从当前用量增长到其额度。
        总剩余 = on_hand − Σ各渠道已预留
        他渠缺口 = Σ_{k≠C} max(0, quota_k − used_k)
        cap_C  = min(quota_C − used_C（若 C 有额度）, 总剩余 − 他渠缺口)
    无额度渠道（直播）只能争抢未被任何额度覆盖的共享部分。
    """
    totals = stock_totals(conn, version_id)
    used_self = channel_held(conn, version_id, channel)
    free = totals["on_hand"] - totals["reserved"]
    other_slack = 0
    own_quota: int | None = None
    for r in conn.execute(
            "SELECT channel, quota_qty FROM channel_quota WHERE version_id=?",
            (version_id,)):
        if r["channel"] == channel:
            own_quota = r["quota_qty"]
        else:
            used_other = channel_held(conn, version_id, r["channel"])
            other_slack += max(0, r["quota_qty"] - used_other)
    cap = max(0, free - other_slack)
    if own_quota is not None:
        cap = min(cap, max(0, own_quota - used_self))
    return cap


def _ledger(conn, *, version_id: str, reason: str, ref: str | None = None,
            channel: str | None = None, batch_id: str | None = None,
            on_hand: int = 0, reserved: int = 0, quarantine: int = 0) -> None:
    conn.execute(
        "INSERT INTO stock_ledger(id,version_id,batch_id,channel,delta_on_hand,"
        "delta_reserved,delta_quarantine,reason,ref,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (new_id("led"), version_id, batch_id, channel, on_hand, reserved, quarantine,
         reason, ref, now()))


# ---------------------------------------------------------------- 采购/生产/质检

def create_po(store: Store, *, po_no: str, version_id: str, supplier_id: str,
              qty_ordered: int, unit_cost_cents: int, expected_at: str | None,
              on_date: str) -> str:
    """采购前强制闸门：权利未齐或内容未过，不得向供应商下单锁产能。"""
    gate_for_procurement(store, version_id, on_date)
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM purchase_order WHERE po_no=?",
                        (po_no,)).fetchone():
            raise DomainError("duplicate", f"采购单号 {po_no} 已存在")
        po_id = new_id("po")
        conn.execute(
            "INSERT INTO purchase_order(id,po_no,version_id,supplier_id,qty_ordered,"
            "unit_cost_cents,status,committed,expected_at,created_at)"
            " VALUES(?,?,?,?,?,?,'draft',0,?,?)",
            (po_id, po_no, version_id, supplier_id, qty_ordered, unit_cost_cents,
             expected_at, now()),
        )
    return po_id


def confirm_po(store: Store, po_id: str) -> None:
    """确认即构成对供应商的合同承诺：此后任何事件都不能删除该单，只能停产/保留。"""
    with store.transaction() as conn:
        po = conn.execute("SELECT * FROM purchase_order WHERE id=?", (po_id,)).fetchone()
        if po is None:
            raise DomainError("not-found", "采购单不存在")
        if po["status"] != "draft":
            raise DomainError("invalid-state", f"采购单状态为 {po['status']}")
        conn.execute(
            "UPDATE purchase_order SET status='confirmed', committed=1, confirmed_at=?"
            " WHERE id=?", (now(), po_id))


def report_po_delay(store: Store, po_id: str, new_expected_at: str,
                    reason: str) -> str:
    """供应商延期：承诺保留，单据转 delayed 并发事件，处置引擎提出可解释方案。"""
    with store.transaction() as conn:
        po = conn.execute("SELECT * FROM purchase_order WHERE id=?", (po_id,)).fetchone()
        if po is None:
            raise DomainError("not-found", "采购单不存在")
        if po["status"] not in OPEN_PO_STATUSES:
            raise DomainError("invalid-state",
                              f"采购单状态 {po['status']} 不可登记延期")
        conn.execute("UPDATE purchase_order SET status='delayed', expected_at=? WHERE id=?",
                     (new_expected_at, po_id))
        event_id = new_id("evt")
        conn.execute(
            "INSERT INTO domain_event(id,type,aggregate_ref,payload,processed,created_at)"
            " VALUES(?,?,?,?,0,?)",
            (event_id, "supplier.delay", po_id,
             json.dumps({"po_no": po["po_no"], "version_id": po["version_id"],
                         "new_expected_at": new_expected_at, "reason": reason},
                        ensure_ascii=False), now()),
        )
    return event_id


def produce_batch(store: Store, *, po_id: str, qty_produced: int) -> str:
    with store.transaction() as conn:
        po = conn.execute("SELECT * FROM purchase_order WHERE id=?", (po_id,)).fetchone()
        if po is None:
            raise DomainError("not-found", "采购单不存在")
        if po["status"] not in OPEN_PO_STATUSES:
            raise DomainError("invalid-state",
                              f"采购单状态 {po['status']}，不能收货投产")
        batch_id = new_id("bat")
        batch_no = f"B{po['po_no']}-{qty_produced}-{new_id('x')[:4]}"
        conn.execute(
            "INSERT INTO production_batch(id,batch_no,po_id,version_id,qty_produced,"
            "status,created_at) VALUES(?,?,?,?,?,'qc-pending',?)",
            (batch_id, batch_no, po_id, po["version_id"], qty_produced, now()),
        )
        produced_total = conn.execute(
            "SELECT COALESCE(SUM(qty_produced),0) AS n FROM production_batch"
            " WHERE po_id=?", (po_id,)).fetchone()["n"]
        if produced_total >= po["qty_ordered"] and po["status"] != "in-production":
            conn.execute("UPDATE purchase_order SET status='completed' WHERE id=?",
                         (po_id,))
        elif po["status"] not in ("in-production", "completed"):
            conn.execute("UPDATE purchase_order SET status='in-production' WHERE id=?",
                         (po_id,))
    return batch_id


def record_qc(store: Store, *, batch_id: str, qty_inspected: int, qty_passed: int,
              notes: str = "") -> str:
    """质检结果：合格品入共享库存，不合格品隔离；整批判退/部分不合格发事件。"""
    qty_failed = qty_inspected - qty_passed
    if qty_passed < 0 or qty_failed < 0:
        raise DomainError("invalid-qc", "质检数量不合法")
    with store.transaction() as conn:
        batch = conn.execute("SELECT * FROM production_batch WHERE id=?",
                             (batch_id,)).fetchone()
        if batch is None:
            raise DomainError("not-found", "批次不存在")
        if batch["status"] != "qc-pending":
            raise DomainError("invalid-state", "该批次已质检")
        if qty_inspected != batch["qty_produced"]:
            raise DomainError("invalid-qc",
                              f"送检数 {qty_inspected} 与生产数 {batch['qty_produced']} 不符")
        decision = "pass" if qty_failed == 0 else ("fail" if qty_passed == 0 else "partial")
        conn.execute(
            "INSERT INTO quality_check(id,batch_id,qty_inspected,qty_passed,qty_failed,"
            "decision,notes,checked_at) VALUES(?,?,?,?,?,?,?,?)",
            (new_id("qc"), batch_id, qty_inspected, qty_passed, qty_failed, decision,
             notes, now()),
        )
        new_status = {"pass": "qualified", "partial": "partial",
                      "fail": "rejected"}[decision]
        conn.execute("UPDATE production_batch SET status=?, qualified_at=? WHERE id=?",
                     (new_status, now() if qty_passed else None, batch_id))
        if qty_passed:
            _ledger(conn, version_id=batch["version_id"], batch_id=batch_id,
                    on_hand=qty_passed, reason="qc-qualified", ref=batch_id)
        if qty_failed:
            _ledger(conn, version_id=batch["version_id"], batch_id=batch_id,
                    quarantine=qty_failed, reason="qc-rejected", ref=batch_id)
        event_id = None
        if decision in ("fail", "partial"):
            event_id = new_id("evt")
            conn.execute(
                "INSERT INTO domain_event(id,type,aggregate_ref,payload,processed,created_at)"
                " VALUES(?,?,?,?,0,?)",
                (event_id, "qc.failed", batch_id,
                 json.dumps({"batch_no": batch["batch_no"],
                             "version_id": batch["version_id"],
                             "qty_passed": qty_passed, "qty_failed": qty_failed,
                             "decision": decision, "notes": notes}, ensure_ascii=False),
                 now()),
            )
    return event_id or ""


# ---------------------------------------------------------------- 订单

def place_order(store: Store, *, order_no: str, channel: str, region: str,
                items: list[dict[str, int]], on_date: str,
                customer_ref: str | None = None) -> str:
    """多渠道共享库存下单：逐版本校验在售状态、销售期权利与渠道预留额度。

    全部行在同一 IMMEDIATE 事务内读余量并写预留，任一行额度不足整单回滚，
    因而直播、馆内、快闪并发下单也不会重复占用同一实物。
    """
    if not items:
        raise DomainError("empty-order", "订单没有明细")
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM sales_order WHERE order_no=?",
                        (order_no,)).fetchone():
            raise DomainError("duplicate", f"订单号 {order_no} 已存在")
        order_id = new_id("ord")
        total = 0
        conn.execute(
            "INSERT INTO sales_order(id,order_no,channel,region,status,customer_ref,"
            "total_cents,created_at) VALUES(?,?,?,?,'active',?,0,?)",
            (order_id, order_no, channel, region, customer_ref, now()),
        )
        item_ids: list[str] = []
        for it in items:
            version_id, qty = it["version_id"], int(it["qty"])
            if qty <= 0:
                raise DomainError("invalid-qty", "订购数量须为正")
            version = conn.execute("SELECT * FROM product_version WHERE id=?",
                                   (version_id,)).fetchone()
            if version is None:
                raise DomainError("not-found", f"产品版本 {version_id} 不存在")
            listing = conn.execute(
                "SELECT id FROM listing WHERE version_id=? AND channel=? AND region=?"
                " AND status='active'", (version_id, channel, region)).fetchone()
            if listing is None:
                raise DomainError("not-listed",
                                  f"版本 {version_id} 未在 {channel}/{region} 在售",
                                  {"version_id": version_id})
            review = evaluate_version(store, version_id, "sale", on_date,
                                      channel, region)
            if review["decision"] != "pass":
                raise DomainError("rights-lapsed",
                                  "下单时权利/审核状态不满足销售条件", review)
            cap = channel_cap(conn, version_id, channel)
            if qty > cap:
                totals = stock_totals(conn, version_id)
                raise DomainError(
                    "oversell",
                    f"渠道 {channel} 对版本 {version_id} 仅可再预留 {cap} 件"
                    f"（申请 {qty}，合格库存 {totals['on_hand']}）",
                    {"available": cap, "requested": qty})
            item_id = new_id("itm")
            conn.execute(
                "INSERT INTO order_item(id,order_id,version_id,qty,qty_shipped,"
                "qty_cancelled,unit_price_cents,status) VALUES(?,?,?,?,0,0,?,'open')",
                (item_id, order_id, version_id, qty, version["price_cents"]),
            )
            conn.execute(
                "INSERT INTO stock_reservation(id,item_id,version_id,channel,qty_held,"
                "qty_consumed,status,created_at) VALUES(?,?,?,?,?,0,'held',?)",
                (new_id("rsv"), item_id, version_id, channel, qty, now()),
            )
            _ledger(conn, version_id=version_id, channel=channel, reserved=qty,
                    reason="order-hold", ref=order_no)
            total += qty * version["price_cents"]
            item_ids.append(item_id)
        conn.execute("UPDATE sales_order SET total_cents=? WHERE id=?", (total, order_id))
    return order_id


def _recalc_order_status(conn, order_id: str) -> None:
    rows = conn.execute(
        "SELECT qty, qty_shipped, qty_cancelled FROM order_item WHERE order_id=?",
        (order_id,)).fetchall()
    shipped_all = all(r["qty_shipped"] + r["qty_cancelled"] >= r["qty"] for r in rows)
    any_shipped = any(r["qty_shipped"] > 0 for r in rows)
    any_cancelled = any(r["qty_cancelled"] > 0 for r in rows)
    if shipped_all:
        status = "shipped" if any_shipped and not any_cancelled else (
            "cancelled" if not any_shipped else "partially-cancelled")
    else:
        status = "partially-shipped" if any_shipped else "active"
    conn.execute("UPDATE sales_order SET status=? WHERE id=?", (status, order_id))


def _open_qty(row) -> int:
    return row["qty"] - row["qty_shipped"] - row["qty_cancelled"]


def cancel_unshipped(store: Store, *, item_id: str, qty: int, reason: str,
                     case_id: str | None = None) -> dict[str, Any]:
    """取消未发货数量：释放该渠道预留并退款。已发生的发货与版税不受影响。"""
    with store.transaction() as conn:
        item = conn.execute("SELECT * FROM order_item WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise DomainError("not-found", "订单明细不存在")
        open_qty = _open_qty(item)
        if qty <= 0 or qty > open_qty:
            raise DomainError("invalid-qty",
                              f"可取消数量为 1..{open_qty}，收到 {qty}")
        order = conn.execute("SELECT * FROM sales_order WHERE id=?",
                             (item["order_id"],)).fetchone()
        conn.execute(
            "UPDATE order_item SET qty_cancelled=qty_cancelled+?,"
            " status=CASE WHEN qty_shipped=0 THEN 'cancelled' ELSE status END WHERE id=?",
            (qty, item_id))
        _ledger(conn, version_id=item["version_id"], channel=order["channel"],
                reserved=-qty, reason="order-cancel", ref=order["order_no"])
        amount = qty * item["unit_price_cents"]
        refund_id = new_id("ref")
        conn.execute(
            "INSERT INTO refund(id,order_id,item_id,version_id,qty,amount_cents,reason,"
            "case_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (refund_id, order["id"], item_id, item["version_id"], qty, amount, reason,
             case_id, now()),
        )
        _recalc_order_status(conn, order["id"])
        return {"refund_id": refund_id, "amount_cents": amount,
                "order_id": order["id"], "order_no": order["order_no"]}


def ship_item(store: Store, *, item_id: str, qty: int, on_date: str) -> list[str]:
    """（部分）发货：按 FIFO 从合格批次扣减实物与预留，并按发货时有效授权计提版税。"""
    shipment_ids: list[str] = []
    with store.transaction() as conn:
        item = conn.execute("SELECT * FROM order_item WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise DomainError("not-found", "订单明细不存在")
        open_qty = _open_qty(item)
        if qty <= 0 or qty > open_qty:
            raise DomainError("invalid-qty", f"可发货数量为 1..{open_qty}，收到 {qty}")
        order = conn.execute("SELECT * FROM sales_order WHERE id=?",
                             (item["order_id"],)).fetchone()
        review = evaluate_version(store, item["version_id"], "sale", on_date,
                                  order["channel"], order["region"])
        if review["decision"] != "pass":
            raise DomainError("rights-lapsed",
                              "发货时授权已失效，不得出库；应转入退款/替代流程", review)
        remaining = qty
        batches = conn.execute(
            "SELECT b.* FROM production_batch b JOIN stock_ledger l ON l.batch_id=b.id"
            " WHERE b.version_id=? AND b.status IN ('qualified','partial')"
            " GROUP BY b.id HAVING SUM(l.delta_on_hand)>0"
            " ORDER BY b.qualified_at, b.batch_no", (item["version_id"],)).fetchall()
        plan: list[tuple[Any, int]] = []
        for b in batches:
            avail = conn.execute(
                "SELECT COALESCE(SUM(delta_on_hand),0) AS n FROM stock_ledger"
                " WHERE batch_id=?", (b["id"],)).fetchone()["n"]
            if avail <= 0:
                continue
            take = min(avail, remaining)
            plan.append((b, take))
            remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise DomainError("insufficient-stock",
                              f"合格批次库存不足，还差 {remaining} 件")
        for b, take in plan:
            sid = new_id("shp")
            conn.execute(
                "INSERT INTO shipment(id,order_id,item_id,version_id,batch_id,qty,shipped_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (sid, order["id"], item_id, item["version_id"], b["id"], take, now()),
            )
            _ledger(conn, version_id=item["version_id"], batch_id=b["id"],
                    channel=order["channel"], on_hand=-take, reserved=-take,
                    reason="shipment", ref=sid)
            royalty.accrue_shipment(conn, shipment_id=sid, version_id=item["version_id"],
                                    qty=take, unit_price_cents=item["unit_price_cents"],
                                    on_date=on_date)
            shipment_ids.append(sid)
        conn.execute(
            "UPDATE order_item SET qty_shipped=qty_shipped+?,"
            " status=CASE WHEN qty_shipped+qty_cancelled+? >= qty THEN 'shipped'"
            " ELSE status END WHERE id=?", (qty, qty, item_id))
        conn.execute(
            "UPDATE stock_reservation SET qty_consumed=qty_consumed+? WHERE item_id=?"
            " AND status='held'", (qty, item_id))
        _recalc_order_status(conn, order["id"])
    return shipment_ids


def return_shipped(store: Store, *, item_id: str, qty: int, reason: str,
                   restock: bool = False, case_id: str | None = None) -> dict[str, Any]:
    """已发货退货退款：冲回版税；restock=True 时实物重回合格库存（不占渠道预留）。"""
    with store.transaction() as conn:
        item = conn.execute("SELECT * FROM order_item WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise DomainError("not-found", "订单明细不存在")
        if qty <= 0 or qty > item["qty_shipped"]:
            raise DomainError("invalid-qty",
                              f"可退数量为 1..{item['qty_shipped']}，收到 {qty}")
        order = conn.execute("SELECT * FROM sales_order WHERE id=?",
                             (item["order_id"],)).fetchone()
        amount = qty * item["unit_price_cents"]
        refund_id = new_id("ref")
        conn.execute(
            "INSERT INTO refund(id,order_id,item_id,version_id,qty,amount_cents,reason,"
            "case_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (refund_id, order["id"], item_id, item["version_id"], qty, amount, reason,
             case_id, now()),
        )
        royalty.accrue_refund(conn, refund_id=refund_id, version_id=item["version_id"],
                              qty=qty, unit_price_cents=item["unit_price_cents"])
        if restock:
            _ledger(conn, version_id=item["version_id"], on_hand=qty,
                    reason="return-restock", ref=refund_id)
        return {"refund_id": refund_id, "amount_cents": amount}
