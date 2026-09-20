"""权利登记、许可匹配与产品版本准入闸门。"""

from __future__ import annotations

import json
from typing import Any

from .store import DomainError, Store, new_id, now

USE_REPRODUCTION = "reproduction"
USE_ADAPTATION = "adaptation"
USE_PROMOTION = "promotion"
USE_TYPES = (USE_REPRODUCTION, USE_ADAPTATION, USE_PROMOTION)

CHANNELS = ("museum-store", "official-online-store", "livestream", "pop-up")


# ---------------------------------------------------------------- 登记

def create_exhibition(store: Store, *, id: str, title: str, start_date: str,
                      end_date: str, status: str = "scheduled") -> str:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO exhibition(id,title,start_date,end_date,status,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (id, title, start_date, end_date, status, now()),
        )
    return id


def reschedule_exhibition(store: Store, exhibition_id: str, *,
                          start_date: str, end_date: str) -> str:
    """特展改期：记录事件并交处置引擎判断对在产/在售商品的影响。"""
    with store.transaction() as conn:
        old = conn.execute("SELECT * FROM exhibition WHERE id=?", (exhibition_id,)).fetchone()
        if old is None:
            raise DomainError("not-found", "展览不存在")
        conn.execute("UPDATE exhibition SET start_date=?, end_date=? WHERE id=?",
                     (start_date, end_date, exhibition_id))
        event_id = new_id("evt")
        conn.execute(
            "INSERT INTO domain_event(id,type,aggregate_ref,payload,processed,created_at)"
            " VALUES(?,?,?,?,0,?)",
            (event_id, "exhibition.rescheduled", exhibition_id,
             json.dumps({"old_start": old["start_date"], "old_end": old["end_date"],
                         "new_start": start_date, "new_end": end_date}, ensure_ascii=False),
             now()),
        )
    return event_id


def create_holder(store: Store, *, id: str, name: str, kind: str,
                  contact: str | None = None) -> str:
    with store.transaction() as conn:
        conn.execute("INSERT INTO right_holder(id,name,kind,contact) VALUES(?,?,?,?)",
                     (id, name, kind, contact))
    return id


def create_artwork(store: Store, *, id: str, exhibition_id: str | None, title: str,
                   catalog_no: str | None = None,
                   holder_splits: dict[str, int] | None = None) -> str:
    """holder_splits: {holder_id: 基点份额}，合计须为 10000；缺省由授权方自身享有 100%。"""
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO artwork(id,exhibition_id,title,catalog_no,created_at)"
            " VALUES(?,?,?,?,?)",
            (id, exhibition_id, title, catalog_no, now()),
        )
        if holder_splits:
            total = sum(holder_splits.values())
            if total != 10000:
                raise DomainError("invalid-split",
                                  f"作品权利人份额合计须为 10000，当前 {total}")
            for holder_id, share in holder_splits.items():
                conn.execute(
                    "INSERT INTO artwork_holder_split(artwork_id,holder_id,share_bps)"
                    " VALUES(?,?,?)", (id, holder_id, share))
    return id


def create_license(store: Store, *, id: str, code: str, artwork_id: str, holder_id: str,
                   uses: list[str], channels: list[str], regions: list[str],
                   valid_from: str, valid_until: str,
                   royalty_rate_bps: int | None = None,
                   royalty_per_unit_cents: int | None = None,
                   sublicensable: bool = False, signed_at: str | None = None,
                   evidences: list[dict[str, Any]] | None = None,
                   supersedes_license_id: str | None = None) -> str:
    """登记一份授权及其用途/渠道/地域边界与凭证。宣传与复制是不同 use，互不替代。"""
    for use in uses:
        if use not in USE_TYPES:
            raise DomainError("invalid-use", f"未知授权用途: {use}")
    if valid_until < valid_from:
        raise DomainError("invalid-period", "授权截止日早于起始日")
    with store.transaction() as conn:
        if conn.execute("SELECT 1 FROM license WHERE code=?", (code,)).fetchone():
            raise DomainError("duplicate", f"授权编号 {code} 已存在")
        conn.execute(
            "INSERT INTO license(id,code,artwork_id,holder_id,sublicensable,status,"
            "valid_from,valid_until,royalty_rate_bps,royalty_per_unit_cents,"
            "supersedes_license_id,signed_at,created_at)"
            " VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?)",
            (id, code, artwork_id, holder_id, int(sublicensable), valid_from, valid_until,
             royalty_rate_bps, royalty_per_unit_cents, supersedes_license_id,
             signed_at or valid_from, now()),
        )
        conn.executemany("INSERT INTO license_use(license_id,use_type) VALUES(?,?)",
                         [(id, u) for u in uses])
        conn.executemany("INSERT INTO license_channel(license_id,channel) VALUES(?,?)",
                         [(id, c) for c in channels])
        conn.executemany("INSERT INTO license_region(license_id,region) VALUES(?,?)",
                         [(id, r) for r in regions])
        for ev in evidences or []:
            conn.execute(
                "INSERT INTO license_evidence(id,license_id,kind,document_ref,checksum,"
                "status,summary,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                (new_id("evd"), id, ev["kind"], ev["document_ref"], ev.get("checksum"),
                 ev.get("status", "verified"), ev.get("summary"), now()),
            )
    return id


def renew_license(store: Store, *, code: str, old_license_id: str,
                  valid_from: str, valid_until: str, **kwargs: Any) -> str:
    """续展：复制旧授权的边界与凭证，形成版本链（待续权利看板用）。"""
    old = store.must_one("SELECT * FROM license WHERE id=?", (old_license_id,), "授权")
    uses = [r["use_type"] for r in store.all(
        "SELECT use_type FROM license_use WHERE license_id=?", (old_license_id,))]
    channels = [r["channel"] for r in store.all(
        "SELECT channel FROM license_channel WHERE license_id=?", (old_license_id,))]
    regions = [r["region"] for r in store.all(
        "SELECT region FROM license_region WHERE license_id=?", (old_license_id,))]
    evidences = [{"kind": r["kind"], "document_ref": r["document_ref"],
                  "checksum": r["checksum"], "status": r["status"],
                  "summary": r["summary"]}
                 for r in store.all(
                     "SELECT * FROM license_evidence WHERE license_id=?", (old_license_id,))]
    return create_license(
        store, id=new_id("lic"), code=code, artwork_id=old["artwork_id"],
        holder_id=old["holder_id"], uses=uses, channels=channels, regions=regions,
        valid_from=valid_from, valid_until=valid_until,
        royalty_rate_bps=kwargs.get("royalty_rate_bps", old["royalty_rate_bps"]),
        royalty_per_unit_cents=kwargs.get(
            "royalty_per_unit_cents", old["royalty_per_unit_cents"]),
        sublicensable=bool(old["sublicensable"]),
        evidences=evidences, supersedes_license_id=old_license_id)


def withdraw_license(store: Store, license_id: str, reason: str) -> str:
    """授权撤回：状态改 withdrawn 并发事件（处置引擎负责停产/停售/退款），历史记录保留。"""
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM license WHERE id=?", (license_id,)).fetchone()
        if row is None:
            raise DomainError("not-found", "授权不存在")
        if row["status"] != "active":
            raise DomainError("invalid-state", f"授权当前状态为 {row['status']}，无法撤回")
        conn.execute("UPDATE license SET status='withdrawn', withdrawn_at=?, withdraw_reason=?"
                     " WHERE id=?", (now(), reason, license_id))
        event_id = new_id("evt")
        conn.execute(
            "INSERT INTO domain_event(id,type,aggregate_ref,payload,processed,created_at)"
            " VALUES(?,?,?,?,0,?)",
            (event_id, "license.withdrawn", license_id,
             json.dumps({"code": row["code"], "reason": reason}, ensure_ascii=False), now()),
        )
        return event_id


def _inserted_id(conn, rowid, prefix):
    return conn.execute("SELECT id FROM domain_event WHERE rowid=?", (rowid,)).fetchone()[0]


# ---------------------------------------------------------------- 匹配

def _scopes(conn, license_id: str) -> tuple[set[str], set[str], set[str]]:
    uses = {r["use_type"] for r in conn.execute(
        "SELECT use_type FROM license_use WHERE license_id=?", (license_id,))}
    channels = {r["channel"] for r in conn.execute(
        "SELECT channel FROM license_channel WHERE license_id=?", (license_id,))}
    regions = {r["region"] for r in conn.execute(
        "SELECT region FROM license_region WHERE license_id=?", (license_id,))}
    return uses, channels, regions


def effective_licenses(conn, artwork_id: str, use_type: str, channel: str,
                       region: str, on_date: str) -> list[dict[str, Any]]:
    """返回在 on_date 对该用途/渠道/地域有效的授权（含核验凭证），按到期日远者优先。"""
    rows = conn.execute(
        "SELECT * FROM license WHERE artwork_id=? AND status='active'"
        " AND valid_from<=? AND valid_until>=? ORDER BY valid_until DESC, created_at DESC",
        (artwork_id, on_date, on_date)).fetchall()
    matched: list[dict[str, Any]] = []
    for lic in rows:
        uses, channels, regions = _scopes(conn, lic["id"])
        if use_type not in uses or channel not in channels or region not in regions:
            continue
        evidence = conn.execute(
            "SELECT * FROM license_evidence WHERE license_id=? AND status='verified'",
            (lic["id"],)).fetchall()
        if not evidence:
            continue
        matched.append({"license": lic, "evidence": evidence})
    return matched


def _market_gaps(conn, lic, use_type: str, channel: str, region: str) -> list[str]:
    uses, channels, regions = _scopes(conn, lic["id"])
    gaps = []
    if use_type not in uses:
        gaps.append("use")
    if channel not in channels:
        gaps.append("channel")
    if region not in regions:
        gaps.append("region")
    return gaps


# ---------------------------------------------------------------- 闸门

def evaluate_version(store: Store, version_id: str, stage: str, on_date: str,
                     channel: str | None = None, region: str | None = None) -> dict[str, Any]:
    """对产品版本做准入审查。stage=procurement 时核对全部计划市场；
    listing/sale 时只核对指定渠道地域。任一必需权利缺失或无凭证即 fail。"""
    conn = store.conn
    version = conn.execute("SELECT * FROM product_version WHERE id=?",
                           (version_id,)).fetchone()
    if version is None:
        raise DomainError("not-found", "产品版本不存在")

    if channel and region:
        markets = [{"channel": channel, "region": region}]
    else:
        markets = [dict(r) for r in conn.execute(
            "SELECT channel,region FROM version_market WHERE version_id=?",
            (version_id,)).fetchall()]
        if not markets:
            raise DomainError("no-market", "产品版本尚未登记任何计划渠道与地域")

    market_results = []
    for m in markets:
        candidates = conn.execute(
            "SELECT * FROM license WHERE artwork_id=? ORDER BY created_at DESC",
            (version["artwork_id"],)).fetchall()
        gaps_by_license = {}
        chosen = None
        for lic in candidates:
            status = lic["status"]
            if status == "withdrawn":
                gaps_by_license[lic["code"]] = ["withdrawn"]
                continue
            if on_date < lic["valid_from"] or on_date > lic["valid_until"]:
                gaps_by_license[lic["code"]] = ["period"]
                continue
            gaps = _market_gaps(conn, lic, version["use_type"], m["channel"], m["region"])
            evidence = conn.execute(
                "SELECT id,kind,document_ref,status FROM license_evidence"
                " WHERE license_id=?", (lic["id"],)).fetchall()
            if not evidence:
                gaps.append("no-evidence")
            elif not any(e["status"] == "verified" for e in evidence):
                gaps.append("evidence-unverified")
            gaps_by_license[lic["code"]] = gaps
            if not gaps and chosen is None:
                chosen = lic
        result = {"channel": m["channel"], "region": m["region"],
                  "gaps_by_license": gaps_by_license}
        if chosen is not None:
            result["matched_license_id"] = chosen["id"]
            result["matched_license_code"] = chosen["code"]
            result["valid_until"] = chosen["valid_until"]
            result["evidence"] = [
                {"id": e["id"], "kind": e["kind"], "document_ref": e["document_ref"]}
                for e in conn.execute(
                    "SELECT id,kind,document_ref FROM license_evidence"
                    " WHERE license_id=? AND status='verified'", (chosen["id"],))]
            result["decision"] = "pass"
        else:
            result["decision"] = "fail"
        market_results.append(result)

    content_ok = version["content_status"] == "approved"
    not_blocked = version["status"] != "blocked"
    # 注：不按「是否存在未结权利工单」做全局阻断——部分市场失效时，独立授权
    # 覆盖的市场（如另签的快闪授权）必须照常经营；失效市场由其已停售 listing
    # 与市场级权利缺口精确拦截。
    blocking_cases: list[str] = []
    rights_ok = all(m["decision"] == "pass" for m in market_results)

    decision = "pass" if (rights_ok and content_ok and not_blocked
                          and not blocking_cases) else "fail"
    return {
        "version_id": version_id,
        "stage": stage,
        "evaluated_on": on_date,
        "decision": decision,
        "rights_ok": rights_ok,
        "content_ok": content_ok,
        "content_status": version["content_status"],
        "version_status": version["status"],
        "blocking_cases": blocking_cases,
        "markets": market_results,
    }


def _save_review(store, version_id: str, stage: str, result: dict[str, Any],
                 channel: str | None, region: str | None) -> str:
    review_id = new_id("gate")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO gate_review(id,version_id,stage,channel,region,decision,detail,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (review_id, version_id, stage, channel, region, result["decision"],
             json.dumps(result, ensure_ascii=False, default=str), now()),
        )
    return review_id


def review_content(store: Store, version_id: str, decision: str, reviewer: str,
                   notes: str = "") -> None:
    if decision not in ("approved", "rejected"):
        raise DomainError("invalid-decision", "内容审核结论须为 approved/rejected")
    with store.transaction() as conn:
        v = conn.execute("SELECT id FROM product_version WHERE id=?",
                         (version_id,)).fetchone()
        if v is None:
            raise DomainError("not-found", "产品版本不存在")
        conn.execute(
            "INSERT INTO content_review(id,version_id,decision,reviewer,notes,decided_at)"
            " VALUES(?,?,?,?,?,?)",
            (new_id("rev"), version_id, decision, reviewer, notes, now()),
        )
        conn.execute("UPDATE product_version SET content_status=? WHERE id=?",
                     (decision, version_id))


def gate_for_procurement(store: Store, version_id: str, on_date: str) -> dict[str, Any]:
    """采购前闸门：全部计划市场权利有效 + 内容审核通过，失败时给出逐项缺口。"""
    result = evaluate_version(store, version_id, "procurement", on_date)
    review_id = _save_review(store, version_id, "procurement", result, None, None)
    result["gate_review_id"] = review_id
    if result["decision"] != "pass":
        raise DomainError("gate-failed", "权利闸门未通过，不可进入采购", result)
    return result


def gate_for_listing(store: Store, version_id: str, channel: str, region: str,
                     on_date: str) -> dict[str, Any]:
    result = evaluate_version(store, version_id, "listing", on_date, channel, region)
    review_id = _save_review(store, version_id, "listing", result, channel, region)
    result["gate_review_id"] = review_id
    if result["decision"] != "pass":
        raise DomainError("gate-failed", "权利闸门未通过，不可上架", result)
    return result
