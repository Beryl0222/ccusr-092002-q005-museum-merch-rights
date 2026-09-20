"""采购、打样、生产、质检与入库。

采购单只有在版本门禁通过（所需用途的许可全部有效、有证据，且内容审核通过）时
才能锁定供应商产能；锁定即构成合同承诺（committed=1），此后即使授权撤回，
承诺仍然保留，停产/退款通过事件流程处置而不是删单。
"""

from . import rights
from .catalog import DomainError, _audit, _one, get_version, now


def create_supplier(db, code: str, name: str, lead_time_days: int = 30) -> int:
    cur = db.execute(
        "INSERT INTO suppliers(code,name,lead_time_days) VALUES (?,?,?)",
        (code, name, lead_time_days),
    )
    db.commit()
    return cur.lastrowid


def _check_procurement_gate(db, version: dict, targets: list[tuple[str, str]] | None,
                            on_date: str | None) -> None:
    if not targets:
        result = rights.evaluate_version(db, version, on_date=on_date)
        if not result["ok"]:
            raise DomainError("采购门禁未通过", result["reasons"])
        return
    all_reasons: list[str] = []
    for channel, region in targets:
        result = rights.evaluate_version(db, version, channel, region, on_date)
        if not result["ok"]:
            all_reasons.extend(
                f"[{channel}/{region}] {r}" for r in result["reasons"]
            )
    if all_reasons:
        raise DomainError("目标渠道授权门禁未通过", sorted(set(all_reasons)))


def create_po(db, code: str, supplier_code: str, version_code: str, qty: int,
              unit_cost: str, expected_at: str,
              targets: list[tuple[str, str]] | None = None,
              on_date: str | None = None) -> int:
    supplier = _one(db, "SELECT * FROM suppliers WHERE code=?", (supplier_code,))
    version = get_version(db, version_code)
    if qty <= 0:
        raise DomainError("采购数量必须大于 0")
    # 创建即预检，但真正锁定产能在 lock_po；预检不通过直接阻断，避免无效承诺进入流程
    _check_procurement_gate(db, version, targets, on_date)
    cur = db.execute(
        "INSERT INTO purchase_orders(code,supplier_id,version_id,qty,unit_cost,status,"
        "committed,expected_at) VALUES (?,?,?,?,?,'draft',0,?)",
        (code, supplier["id"], version["id"], qty, unit_cost, expected_at),
    )
    db.commit()
    return cur.lastrowid


def lock_po(db, code: str, targets: list[tuple[str, str]] | None = None,
            on_date: str | None = None) -> None:
    """锁定供应商产能：再次执行门禁，通过后构成不可静默撤销的合同承诺。"""
    po = _one(db, "SELECT * FROM purchase_orders WHERE code=?", (code,))
    if po["status"] != "draft":
        raise DomainError(f"采购单 {code} 状态为 {po['status']}，无法锁定")
    version = _one(db, "SELECT * FROM product_versions WHERE id=?", (po["version_id"],))
    _check_procurement_gate(db, dict(version), targets, on_date)
    db.execute(
        "UPDATE purchase_orders SET status='locked', committed=1, locked_at=? WHERE id=?",
        (now(), po["id"]),
    )
    db.execute(
        "UPDATE product_versions SET status='production', updated_at=? WHERE id=? AND status='approved'",
        (now(), version["id"]),
    )
    _audit(db, "purchase_order", code, "lock_capacity", {"qty": po["qty"]})
    db.commit()


def start_batch(db, code: str, po_code: str, qty_planned: int, note: str | None = None) -> int:
    po = _one(db, "SELECT * FROM purchase_orders WHERE code=?", (po_code,))
    if po["status"] not in ("locked", "in_production", "delayed"):
        raise DomainError(f"采购单 {po_code} 尚未锁定产能，不能开批生产")
    cur = db.execute(
        "INSERT INTO production_batches(code,po_id,version_id,qty_planned,status,note)"
        " VALUES (?,?,?,?,'sampling',?)",
        (code, po["id"], po["version_id"], qty_planned, note),
    )
    db.execute("UPDATE purchase_orders SET status='in_production' WHERE id=? AND status!='production_halted'",
               (po["id"],))
    db.commit()
    return cur.lastrowid


def record_qc(db, batch_code: str, passed_qty: int, failed_qty: int,
              inspector: str, note: str | None = None) -> dict:
    """登记质检结果。不合格数量隔离，不允许入库；全部不合格则批次隔离。

    返回 {'ok': bool, 'batch_id': int, 'passed': int, 'failed': int}，
    由调用方（或事件引擎）据此触发 rework_or_reorder 处置。
    """
    batch = _one(db, "SELECT * FROM production_batches WHERE code=?", (batch_code,))
    if batch["status"] not in ("sampling", "in_production"):
        raise DomainError(f"批次 {batch_code} 当前状态 {batch['status']}，不能登记质检")
    if passed_qty < 0 or failed_qty < 0 or passed_qty + failed_qty > batch["qty_planned"] + 10**-6:
        raise DomainError("质检数量超出批次计划数量")
    status = "qc_passed" if passed_qty > 0 and failed_qty == 0 else (
        "qc_failed" if passed_qty == 0 else "quarantined"
    )
    # 部分合格：合格部分可入库，不合格部分隔离，批次标记 quarantined
    db.execute(
        "UPDATE production_batches SET status=?, qc_passed_qty=?, qc_failed_qty=?,"
        " inspected_at=?, inspector=?, note=? WHERE id=?",
        (status, passed_qty, failed_qty, now(), inspector, note, batch["id"]),
    )
    _audit(db, "batch", batch_code, "qc", {"passed": passed_qty, "failed": failed_qty})
    db.commit()
    return {"ok": failed_qty == 0, "batch_id": batch["id"],
            "passed": passed_qty, "failed": failed_qty}


def receive_batch(db, batch_code: str, received_qty: int | None = None) -> None:
    """质检合格数量入库（共享库存），逐笔写入库存台账。"""
    batch = _one(db, "SELECT * FROM production_batches WHERE code=?", (batch_code,))
    if batch["status"] not in ("qc_passed", "quarantined"):
        raise DomainError(f"批次 {batch_code} 未通过质检，不能入库")
    qty = received_qty if received_qty is not None else batch["qc_passed_qty"]
    if qty > batch["qc_passed_qty"] - batch["received_qty"]:
        raise DomainError("入库数量不能超过质检合格余量")
    db.execute(
        "UPDATE production_batches SET received_qty=received_qty+?, received_at=?, status='received'"
        " WHERE id=?",
        (qty, now(), batch["id"]),
    )
    db.execute(
        "UPDATE inventory SET on_hand=on_hand+? WHERE version_id=?",
        (qty, batch["version_id"]),
    )
    db.execute(
        "INSERT INTO stock_ledger(version_id,batch_id,change_qty,reason,ref,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (batch["version_id"], batch["id"], qty, "qc_passed_received", batch_code, now()),
    )
    db.commit()


def set_channel_reservation(db, version_code: str, channel: str, reserved_qty: int) -> None:
    version = get_version(db, version_code)
    if reserved_qty < 0:
        raise DomainError("预留数量不能为负")
    # 可承诺量 = 在手 + 在途（未停产且未质检报废）− 已占用
    atp_row = db.execute(
        """
        SELECT i.on_hand - i.allocated
             + COALESCE((SELECT SUM(po.qty
                   - COALESCE((SELECT SUM(received_qty) FROM production_batches WHERE po_id=po.id),0)
                   - COALESCE((SELECT SUM(qc_failed_qty) FROM production_batches WHERE po_id=po.id),0))
                   FROM purchase_orders po WHERE po.version_id=i.version_id
                   AND po.status IN ('locked','in_production','delayed')),0) AS atp
        FROM inventory i WHERE i.version_id=?
        """,
        (version["id"],),
    ).fetchone()
    sellable = atp_row["atp"]
    others = db.execute(
        "SELECT COALESCE(SUM(reserved_qty),0) AS s FROM channel_reservations"
        " WHERE version_id=? AND channel<>?",
        (version["id"], channel),
    ).fetchone()["s"]
    if others + reserved_qty > sellable:
        raise DomainError(
            f"预留总量 {others + reserved_qty} 超过可承诺库存 {sellable}"
        )
    db.execute(
        "INSERT INTO channel_reservations(version_id,channel,reserved_qty) VALUES (?,?,?)"
        " ON CONFLICT(version_id,channel) DO UPDATE SET reserved_qty=excluded.reserved_qty",
        (version["id"], channel, reserved_qty),
    )
    db.commit()
