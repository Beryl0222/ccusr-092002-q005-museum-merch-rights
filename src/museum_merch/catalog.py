"""产品、版本与上架管理。"""

from __future__ import annotations

from . import rights
from .rights import evaluate_version, gate_for_listing
from .store import DomainError, Store, new_id, now


def create_supplier(store: Store, *, id: str, name: str,
                    lead_time_days: int | None = None) -> str:
    with store.transaction() as conn:
        conn.execute("INSERT INTO supplier(id,name,lead_time_days) VALUES(?,?,?)",
                     (id, name, lead_time_days))
    return id


def create_product(store: Store, *, id: str, sku: str, name: str,
                   exhibition_id: str | None = None) -> str:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO product(id,sku,name,exhibition_id,status,created_at)"
            " VALUES(?,?,?,?,'active',?)",
            (id, sku, name, exhibition_id, now()),
        )
    return id


def create_version(store: Store, *, product_id: str, artwork_id: str, use_type: str,
                   title: str, price_cents: int, spec: str | None = None,
                   version_no: int | None = None, version_id: str | None = None) -> str:
    """新建产品版本，默认 draft + 内容待审，必须通过闸门才能采购/上架。"""
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM product WHERE id=?", (product_id,)).fetchone() is None:
            raise DomainError("not-found", "产品不存在")
        if version_no is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 AS n FROM product_version"
                " WHERE product_id=?", (product_id,)).fetchone()
            version_no = row["n"]
        vid = version_id or new_id("ver")
        conn.execute(
            "INSERT INTO product_version(id,product_id,version_no,artwork_id,use_type,"
            "title,spec,price_cents,status,content_status,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,'draft','pending',?)",
            (vid, product_id, version_no, artwork_id, use_type, title, spec,
             price_cents, now()),
        )
    return vid


def add_version_market(store: Store, version_id: str, channel: str, region: str) -> None:
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM product_version WHERE id=?",
                        (version_id,)).fetchone() is None:
            raise DomainError("not-found", "产品版本不存在")
        conn.execute(
            "INSERT OR IGNORE INTO version_market(version_id,channel,region) VALUES(?,?,?)",
            (version_id, channel, region))


def set_channel_quota(store: Store, version_id: str, channel: str, quota_qty: int) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO channel_quota(version_id,channel,quota_qty) VALUES(?,?,?)"
            " ON CONFLICT(version_id,channel) DO UPDATE SET quota_qty=excluded.quota_qty",
            (version_id, channel, quota_qty))


def list_version(store: Store, version_id: str, channel: str, region: str,
                 on_date: str) -> str:
    """上架：实时跑权利+审核闸门，通过才创建在售 listing，并留存审查快照。"""
    result = gate_for_listing(store, version_id, channel, region, on_date)
    with store.transaction() as conn:
        dup = conn.execute(
            "SELECT id FROM listing WHERE version_id=? AND channel=? AND region=?"
            " AND status='active'", (version_id, channel, region)).fetchone()
        if dup:
            raise DomainError("already-listed", "该版本在此渠道地域已在售")
        listing_id = new_id("lst")
        conn.execute(
            "INSERT INTO listing(id,version_id,channel,region,status,gate_review_id,listed_at)"
            " VALUES(?,?,?,?,'active',?,?)",
            (listing_id, version_id, channel, region, result["gate_review_id"], now()),
        )
        if conn.execute("SELECT status FROM product_version WHERE id=?",
                        (version_id,)).fetchone()["status"] == "draft":
            conn.execute("UPDATE product_version SET status='approved' WHERE id=?",
                         (version_id,))
    return listing_id


def stop_listing(store: Store, listing_id: str, case_id: str | None = None) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE listing SET status='stopped', stopped_at=?, case_id=?"
            " WHERE id=? AND status='active'", (now(), case_id, listing_id))
        conn.execute("UPDATE product_version SET status='blocked',"
                     " blocked_reason=COALESCE(blocked_reason,'') "
                     "WHERE id=(SELECT version_id FROM listing WHERE id=?)", (listing_id,))


def resume_version(store: Store, version_id: str, on_date: str,
                   channels_regions: list[tuple[str, str]] | None = None) -> list[str]:
    """续展/重新取得授权后恢复版本：按市场逐个重跑闸门，通过才解除封锁/重新上架。"""
    if channels_regions is None:
        markets = store.all(
            "SELECT channel,region FROM version_market WHERE version_id=?",
            (version_id,))
        channels_regions = [(m["channel"], m["region"]) for m in markets]
    relisted: list[str] = []
    for ch, rg in channels_regions:
        review = rights.evaluate_version(
            store, version_id, "listing", on_date, ch, rg)
        if review["decision"] != "pass":
            raise DomainError("gate-failed",
                              f"{ch}/{rg} 权利仍未恢复，不能重新上架", review)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE product_version SET status='approved', blocked_reason=NULL"
            " WHERE id=?", (version_id,))
    for ch, rg in channels_regions:
        exists = store.one(
            "SELECT 1 FROM listing WHERE version_id=? AND channel=? AND region=?",
            (version_id, ch, rg))
        gate_for_listing(store, version_id, ch, rg, on_date)  # 留存恢复时闸门快照
        if exists is None:
            relisted.append(list_version(store, version_id, ch, rg, on_date))
        else:
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE listing SET status='active', stopped_at=NULL, case_id=NULL"
                    " WHERE version_id=? AND channel=? AND region=?",
                    (version_id, ch, rg))
            relisted.append(f"reactivated:{ch}/{rg}")
    return relisted
