"""策展、授权与产品版本服务。"""

import json
import sqlite3
from datetime import date

from . import rights


class DomainError(Exception):
    """业务规则冲突，HTTP 层映射为 422 并返回 reasons。"""

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = reasons or [message]


def now() -> str:
    return date.today().isoformat()


def _audit(db, entity_type: str, entity_ref: str, action: str, detail: dict) -> None:
    db.execute(
        "INSERT INTO audit_log(entity_type, entity_ref, action, detail_json, created_at)"
        " VALUES (?,?,?,?,?)",
        (entity_type, entity_ref, action, json.dumps(detail, ensure_ascii=False), now()),
    )


def _one(db, sql: str, params: tuple = ()) -> sqlite3.Row:
    row = db.execute(sql, params).fetchone()
    if row is None:
        raise DomainError("记录不存在")
    return row


# ---------- 基础档案 ----------

def create_holder(db, code: str, name: str, kind: str, contact: str | None = None) -> int:
    cur = db.execute(
        "INSERT INTO right_holders(code,name,kind,contact) VALUES (?,?,?,?)",
        (code, name, kind, contact),
    )
    db.commit()
    return cur.lastrowid


def create_exhibition(db, code: str, title: str, start_date: str, end_date: str) -> int:
    cur = db.execute(
        "INSERT INTO exhibitions(code,title,start_date,end_date) VALUES (?,?,?,?)",
        (code, title, start_date, end_date),
    )
    db.commit()
    return cur.lastrowid


def reschedule_exhibition(db, code: str, start_date: str, end_date: str) -> None:
    _one(db, "SELECT * FROM exhibitions WHERE code=?", (code,))
    db.execute(
        "UPDATE exhibitions SET start_date=?, end_date=?, status='rescheduled' WHERE code=?",
        (start_date, end_date, code),
    )
    _audit(db, "exhibition", code, "reschedule", {"start_date": start_date, "end_date": end_date})
    db.commit()


def create_artwork(db, code: str, title: str, artist: str, holder_code: str,
                   exhibition_code: str | None = None) -> int:
    holder = _one(db, "SELECT * FROM right_holders WHERE code=?", (holder_code,))
    exhibition_id = None
    if exhibition_code:
        exhibition_id = _one(
            db, "SELECT * FROM exhibitions WHERE code=?", (exhibition_code,)
        )["id"]
    cur = db.execute(
        "INSERT INTO artworks(code,title,artist,holder_id,exhibition_id) VALUES (?,?,?,?,?)",
        (code, title, artist, holder["id"], exhibition_id),
    )
    db.commit()
    return cur.lastrowid


# ---------- 授权与证据 ----------

def record_license(
    db,
    code: str,
    artwork_code: str,
    uses: list[str],
    valid_from: str,
    valid_until: str,
    channels: list[str] | None = None,
    regions: list[str] | None = None,
    sublicensable: bool = False,
    royalty_rate: str | None = None,
    royalty_unit_fee: str | None = None,
    signed_at: str | None = None,
    notes: str | None = None,
) -> int:
    artwork = _one(db, "SELECT * FROM artworks WHERE code=?", (artwork_code,))
    cur = db.execute(
        "INSERT INTO licenses(code,artwork_id,holder_id,uses_json,channels_json,regions_json,"
        "valid_from,valid_until,sublicensable,royalty_rate,royalty_unit_fee,status,signed_at,notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
        (
            code, artwork["id"], artwork["holder_id"], json.dumps(uses),
            json.dumps(channels or ["*"]), json.dumps(regions or ["*"]),
            valid_from, valid_until, 1 if sublicensable else 0,
            royalty_rate, royalty_unit_fee, signed_at or now(), notes,
        ),
    )
    _audit(db, "license", code, "record", {"uses": uses, "until": valid_until})
    db.commit()
    return cur.lastrowid


def add_license_document(db, license_code: str, doc_type: str, doc_ref: str,
                         checksum: str | None = None, issued_by: str | None = None,
                         received_at: str | None = None) -> int:
    lic = _one(db, "SELECT * FROM licenses WHERE code=?", (license_code,))
    cur = db.execute(
        "INSERT INTO license_documents(license_id,doc_type,doc_ref,checksum,issued_by,received_at)"
        " VALUES (?,?,?,?,?,?)",
        (lic["id"], doc_type, doc_ref, checksum, issued_by, received_at or now()),
    )
    db.commit()
    return cur.lastrowid


def revoke_license(db, code: str, revoked_at: str | None = None,
                   doc_ref: str | None = None) -> None:
    """仅登记撤回事实；停产/停售/退款处置由 incidents 流程统一生成。"""
    lic = _one(db, "SELECT * FROM licenses WHERE code=?", (code,))
    if lic["status"] != "active":
        raise DomainError(f"许可 {code} 当前状态为 {lic['status']}，无法撤回")
    revoked_at = revoked_at or now()
    db.execute(
        "UPDATE licenses SET status='revoked', revoked_at=? WHERE id=?",
        (revoked_at, lic["id"]),
    )
    if doc_ref:
        db.execute(
            "INSERT INTO license_documents(license_id,doc_type,doc_ref,received_at)"
            " VALUES (?,?,?,?)",
            (lic["id"], "revocation-notice", doc_ref, revoked_at),
        )
    _audit(db, "license", code, "revoke", {"revoked_at": revoked_at, "doc_ref": doc_ref})
    db.commit()


def renew_license(db, code: str, new_until: str, doc_ref: str,
                  received_at: str | None = None) -> None:
    lic = _one(db, "SELECT * FROM licenses WHERE code=?", (code,))
    db.execute(
        "UPDATE licenses SET valid_until=?, status='active', revoked_at=NULL WHERE id=?",
        (new_until, lic["id"]),
    )
    db.execute(
        "INSERT INTO license_documents(license_id,doc_type,doc_ref,received_at)"
        " VALUES (?,?,?,?)",
        (lic["id"], "renewal", doc_ref, received_at or now()),
    )
    _audit(db, "license", code, "renew", {"new_until": new_until})
    db.commit()


# ---------- 产品版本与内容审核 ----------

def create_version(db, code: str, name: str, artwork_code: str,
                   is_derivative: bool = False) -> int:
    artwork = _one(db, "SELECT * FROM artworks WHERE code=?", (artwork_code,))
    ts = now()
    cur = db.execute(
        "INSERT INTO product_versions(code,name,artwork_id,is_derivative,status,created_at,updated_at)"
        " VALUES (?,?,?,?,'draft',?,?)",
        (code, name, artwork["id"], 1 if is_derivative else 0, ts, ts),
    )
    db.execute(
        "INSERT INTO inventory(version_id) VALUES (?)", (cur.lastrowid,)
    )
    db.commit()
    return cur.lastrowid


def get_version(db, code: str) -> dict:
    return dict(_one(db, "SELECT * FROM product_versions WHERE code=?", (code,)))


def submit_for_review(db, code: str) -> None:
    _one(db, "SELECT * FROM product_versions WHERE code=?", (code,))
    db.execute(
        "UPDATE product_versions SET status='in_review', updated_at=? WHERE code=?",
        (now(), code),
    )
    db.commit()


def decide_review(db, code: str, decision: str, reviewer: str,
                  notes: str | None = None, doc_ref: str | None = None) -> None:
    if decision not in ("approved", "rejected", "changes_requested"):
        raise DomainError("审核结论必须是 approved / rejected / changes_requested")
    version = _one(db, "SELECT * FROM product_versions WHERE code=?", (code,))
    db.execute(
        "INSERT INTO content_reviews(version_id,decision,reviewer,decided_at,notes,doc_ref)"
        " VALUES (?,?,?,?,?,?)",
        (version["id"], decision, reviewer, now(), notes, doc_ref),
    )
    new_status = {"approved": "approved", "rejected": "rejected",
                  "changes_requested": "in_review"}[decision]
    db.execute(
        "UPDATE product_versions SET status=?, updated_at=? WHERE id=?",
        (new_status, now(), version["id"]),
    )
    _audit(db, "version", code, f"review_{decision}", {"reviewer": reviewer})
    db.commit()


def gate(db, code: str, channel: str | None = None, region: str | None = None,
         on_date: str | None = None) -> dict:
    """采购门禁（无渠道/地域）或上架门禁（带渠道/地域）的可解释结论。"""
    return rights.evaluate_version(db, get_version(db, code), channel, region, on_date)


def check_promotion(db, artwork_code: str, channel: str | None = None,
                    region: str | None = None, on_date: str | None = None) -> dict:
    artwork = _one(db, "SELECT * FROM artworks WHERE code=?", (artwork_code,))
    return rights.evaluate_promotion(db, artwork["id"], channel, region, on_date)
