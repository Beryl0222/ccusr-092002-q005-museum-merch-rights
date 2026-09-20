"""版税权责发生制与结算。

发货时按「当时有效」的授权约定计提（费率%或按件），多权利人按作品份额拆分；
退款按原费率冲回；结算按权利人汇总未结算分录。版税只认实际净销售。
"""

from __future__ import annotations

from typing import Any

from .rights import effective_licenses
from .store import DomainError, Store, new_id, now


def _holder_shares(conn, artwork_id: str, license_holder_id: str) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT holder_id, share_bps FROM artwork_holder_split WHERE artwork_id=?",
        (artwork_id,)).fetchall()
    if rows:
        return [(r["holder_id"], r["share_bps"]) for r in rows]
    return [(license_holder_id, 10000)]


def _pick_license(conn, artwork_id: str, use_type: str, channel: str, region: str,
                  on_date: str):
    matched = effective_licenses(conn, artwork_id, use_type, channel, region, on_date)
    if not matched:
        return None
    # effective_licenses 已按到期日远者优先排序；同作品多授权时取覆盖最稳的一份
    return matched[0]["license"]


def accrue_shipment(conn, *, shipment_id: str, version_id: str, qty: int,
                    unit_price_cents: int, on_date: str) -> None:
    version = conn.execute("SELECT * FROM product_version WHERE id=?",
                           (version_id,)).fetchone()
    shipment = conn.execute("SELECT o.channel, o.region FROM shipment s"
                            " JOIN sales_order o ON o.id=s.order_id"
                            " WHERE s.id=?", (shipment_id,)).fetchone()
    lic = _pick_license(conn, version["artwork_id"], version["use_type"],
                        shipment["channel"], shipment["region"], on_date)
    if lic is None:
        # 销售闸门已先行校验，此处为兜底：无有效授权不得产生版税，也不应发货
        raise DomainError("rights-lapsed",
                          "发货时点无有效授权，无法计提版税", {"version_id": version_id})
    shares = _holder_shares(conn, version["artwork_id"], lic["holder_id"])
    period = on_date[:7]
    net_total = qty * unit_price_cents
    for holder_id, share_bps in shares:
        if lic["royalty_rate_bps"]:
            amount = net_total * lic["royalty_rate_bps"] // 10000
        elif lic["royalty_per_unit_cents"]:
            amount = qty * lic["royalty_per_unit_cents"]
        else:
            amount = 0
        amount = amount * share_bps // 10000
        conn.execute(
            "INSERT INTO royalty_accrual(id,license_id,holder_id,artwork_id,version_id,"
            "basis,ref_id,qty,net_cents,amount_cents,rate_bps,share_bps,period_key,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("roy"), lic["id"], holder_id, version["artwork_id"], version_id,
             "shipment", shipment_id, qty, net_total * share_bps // 10000, amount,
             lic["royalty_rate_bps"], share_bps, period, now()),
        )


def accrue_refund(conn, *, refund_id: str, version_id: str, qty: int,
                  unit_price_cents: int) -> None:
    """退款冲回：按该版本各权利人累计发货分录的件均金额比例冲回，
    费率制与按件制两种约定都适用。"""
    grouped = conn.execute(
        "SELECT license_id, holder_id, artwork_id, share_bps, rate_bps,"
        " SUM(qty) AS qty, SUM(net_cents) AS net, SUM(amount_cents) AS amount"
        " FROM royalty_accrual WHERE version_id=? AND basis='shipment'"
        " GROUP BY holder_id", (version_id,)).fetchall()
    if not grouped:
        raise DomainError("no-accrual", "找不到原发货版税分录，无法冲回")
    refund = conn.execute("SELECT created_at FROM refund WHERE id=?",
                          (refund_id,)).fetchone()
    period = refund["created_at"][:7]
    net_total = qty * unit_price_cents
    for g in grouped:
        if g["qty"] <= 0:
            continue
        share = g["share_bps"]
        amount = g["amount"] * qty // g["qty"]
        net = net_total * share // 10000
        conn.execute(
            "INSERT INTO royalty_accrual(id,license_id,holder_id,artwork_id,version_id,"
            "basis,ref_id,qty,net_cents,amount_cents,rate_bps,share_bps,period_key,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("roy"), g["license_id"], g["holder_id"], g["artwork_id"], version_id,
             "refund", refund_id, -qty, -net, -amount, g["rate_bps"], share, period,
             now()),
        )


def settle(store: Store, *, holder_id: str, period_start: str, period_end: str,
           mark_paid: bool = False) -> dict[str, Any]:
    """把权利人在区间内未结算的分录汇总为一张结算单。"""
    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM royalty_accrual WHERE holder_id=? AND settlement_id IS NULL"
            " AND period_key>=? AND period_key<=? ORDER BY created_at",
            (holder_id, period_start[:7], period_end[:7])).fetchall()
        if not rows:
            raise DomainError("nothing-to-settle", "该区间没有待结算版税分录")
        total = sum(r["amount_cents"] for r in rows)
        gross = sum(r["net_cents"] for r in rows)
        settlement_id = new_id("stl")
        conn.execute(
            "INSERT INTO royalty_settlement(id,holder_id,period_start,period_end,"
            "amount_cents,status,created_at,paid_at) VALUES(?,?,?,?,?,?,?,?)",
            (settlement_id, holder_id, period_start, period_end, total,
             "paid" if mark_paid else "confirmed", now(), now() if mark_paid else None),
        )
        conn.executemany(
            "UPDATE royalty_accrual SET settlement_id=? WHERE id=?",
            [(settlement_id, r["id"]) for r in rows])
    return {"settlement_id": settlement_id, "holder_id": holder_id,
            "amount_cents": total, "net_sales_cents": gross, "entries": len(rows)}
