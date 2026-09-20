"""风险事件引擎。

四类事件触发可解释处置，并保留已经发生的合同承诺：
- license_revoked 授权撤回：停售、停产、已承诺采购单挂起（不删单不违约记账）、
  未发货订单进入"退款或替代"待决队列。
- exhibition_rescheduled 特展改期：比对新展期与各许可期限，产生待续/停售动作。
- supplier_delay 供应商延期：采购单延期；若新交期晚于授权到期则给出替代/重排建议。
- qc_failed 质检不合格：隔离批次、重排生产；库存无法覆盖的已占用订单进入退款/替代队列。

动作分两类：applied（系统可立即执行的停售/停产/隔离）与 pending（需要人工选择
退款还是替代、是否续签），所有动作均带 detail 解释，可从事件与商品双向追溯。
"""

import json

from . import rights, sales
from .catalog import DomainError, _audit, _one, now
from .production import record_qc as _record_qc
from .sales import refund_item, substitute_item


def _log_action(db, incident_id: int, action_type: str, entity_type: str,
                entity_ref: str, detail: str, status: str = "applied") -> int:
    cur = db.execute(
        "INSERT INTO incident_actions(incident_id,action_type,entity_type,entity_ref,"
        "status,detail,created_at) VALUES (?,?,?,?,?,?,?)",
        (incident_id, action_type, entity_type, entity_ref, status, detail, now()),
    )
    return cur.lastrowid


def _create_incident(db, itype: str, subject: str, payload: dict,
                     summary: str) -> int:
    code = f"INC-{itype}-{db.execute('SELECT COUNT(*)+1 AS n FROM incidents').fetchone()['n']:04d}"
    cur = db.execute(
        "INSERT INTO incidents(code,type,subject,payload_json,status,summary,occurred_at)"
        " VALUES (?,?,?,?,'open',?,?)",
        (code, itype, subject, json.dumps(payload, ensure_ascii=False), summary, now()),
    )
    return cur.lastrowid


# ---------- 授权撤回 / 到期 ----------

def _suspend_and_halt(db, incident_id: int, lic: dict, cause: str) -> tuple[list[dict], list[int]]:
    """失去权利覆盖后的通用处置：停售 listing、停产采购单、版本挂起。

    cause 用于解释动作来源（撤回 / 到期）。已锁定的采购合同承诺一律保留。
    返回 (动作列表, 实际受影响的版本 id 列表)。
    """
    actions: list[dict] = []
    affected: list[int] = []
    versions = db.execute(
        "SELECT * FROM product_versions WHERE artwork_id=?", (lic["artwork_id"],)
    ).fetchall()
    for vrow in versions:
        version = dict(vrow)
        still_covered = rights.evaluate_version(db, version, on_date=now())["rights_ok"]
        if still_covered:
            continue  # 存在其他有效许可覆盖该版本，无需处置
        affected.append(version["id"])
        listings = db.execute(
            "SELECT * FROM listings WHERE version_id=? AND status='active'",
            (version["id"],),
        ).fetchall()
        for listing in listings:
            chk = rights.evaluate_version(
                db, version, listing["channel"], listing["region"], now()
            )
            if not chk["ok"]:
                db.execute(
                    "UPDATE listings SET status='suspended', suspended_at=?,"
                    " reason=? WHERE id=?",
                    (now(), f"{cause}：{'；'.join(chk['reasons'])}", listing["id"]),
                )
                ref = f"{version['code']}:{listing['channel']}:{listing['region']}"
                _log_action(db, incident_id, "suspend_sale", "listing", ref,
                            f"{cause}，{listing['channel']}/{listing['region']} 立即停售")
                actions.append({"action": "suspend_sale", "entity": ref})
        pos = db.execute(
            "SELECT * FROM purchase_orders WHERE version_id=?"
            " AND status IN ('locked','in_production','delayed','draft')",
            (version["id"],),
        ).fetchall()
        for po in pos:
            db.execute(
                "UPDATE purchase_orders SET status='production_halted' WHERE id=?",
                (po["id"],),
            )
            _log_action(db, incident_id, "halt_production", "purchase_order",
                        po["code"],
                        f"{cause}，采购单 {po['code']}（{po['qty']} 件）停产；"
                        f"已锁定产能构成的合同承诺保留（committed={po['committed']}），"
                        "按合同与供应商结算，不删除订单")
            actions.append({"action": "halt_production", "entity": po["code"]})
        db.execute(
            "UPDATE product_versions SET status='sale_suspended', updated_at=?"
            " WHERE id=? AND status IN ('on_sale','production','approved')",
            (now(), version["id"]),
        )
    return actions, affected


def license_revoked(db, license_code: str, auto_refund: bool = False) -> dict:
    """许可撤回后的统一处置（许可状态已由 catalog.revoke_license 改写）。"""
    db.execute("BEGIN IMMEDIATE")
    lic = dict(_one(db, "SELECT * FROM licenses WHERE code=?", (license_code,)))
    incident_id = _create_incident(
        db, "license_revoked", license_code, {"license_id": lic["id"]},
        f"许可 {license_code} 撤回，停止相关产品的生产与销售",
    )
    actions, affected_version_ids = _suspend_and_halt(
        db, incident_id, lic, cause=f"许可 {license_code} 撤回")
    # 撤回（不同于自然到期）：撤回日前已下单但未发货的订单进入退款/替代队列
    if affected_version_ids:
        placeholders = ",".join("?" * len(affected_version_ids))
        items = db.execute(
            f"SELECT oi.* FROM order_items oi JOIN orders o ON o.id=oi.order_id"
            f" WHERE oi.version_id IN ({placeholders}) AND oi.status IN ('open','partial')"
            " AND oi.allocated_qty - oi.shipped_qty > 0",
            affected_version_ids,
        ).fetchall()
    else:
        items = []
    for item in items:
        unshipped = item["allocated_qty"] - item["shipped_qty"]
        detail = (f"订单行 {item['id']} 尚有 {unshipped} 件未发货，"
                  f"请选择替代版本或按净价 {item['unit_price']} 退款")
        action_id = _log_action(
            db, incident_id, "refund_or_substitute", "order_item",
            str(item["id"]), detail,
            status="applied" if auto_refund else "pending",
        )
        if auto_refund:
            refund_item(db, item["id"], None,
                        f"许可 {license_code} 撤回，自动退款")
            db.execute(
                "UPDATE incident_actions SET resolved_at=?, "
                "detail=detail||'（已自动退款）' WHERE id=?",
                (now(), action_id),
            )
        actions.append({"action": "refund_or_substitute",
                        "item_id": item["id"], "auto": auto_refund})
    db.execute("UPDATE incidents SET status='processed', processed_at=? WHERE id=?",
               (now(), incident_id))
    _audit(db, "incident", license_code, "license_revoked_processed",
           {"actions": len(actions)})
    db.commit()
    return {"incident_id": incident_id, "license": license_code, "actions": actions}


def license_expiry_sweep(db, on_date: str | None = None) -> dict:
    """到期巡查：把已过期许可标记 expired，并对其覆盖的在售/在产版本停售停产。

    授权期内已成交的订单继续履行（按下单时快照许可结算版税），
    但到期之后不得继续上架成交——对应历史上"到期后线上仍在售"的事故。
    """
    on_date = on_date or now()
    db.execute("BEGIN IMMEDIATE")
    expired = db.execute(
        "SELECT * FROM licenses WHERE status='active' AND valid_until < ?",
        (on_date,),
    ).fetchall()
    all_actions: list[dict] = []
    incident_ids: list[int] = []
    for erow in expired:
        lic = dict(erow)
        db.execute("UPDATE licenses SET status='expired' WHERE id=?", (lic["id"],))
        incident_id = _create_incident(
            db, "license_expired", lic["code"], {"license_id": lic["id"]},
            f"许可 {lic['code']} 已于 {lic['valid_until']} 到期",
        )
        actions, affected_ids = _suspend_and_halt(
            db, incident_id, lic, cause=f"许可 {lic['code']} 已到期")
        # 授权期内成交的订单继续履行；但若停产导致可承诺库存无法覆盖已占用订单，
        # 缺口进入退款/替代队列，而不是无限期挂起
        for version_id in affected_ids:
            atp = sales._version_atp(db, version_id)
            shortage = -atp["atp"]
            if shortage <= 0:
                continue
            rows = db.execute(
                "SELECT oi.* FROM order_items oi JOIN orders o ON o.id=oi.order_id"
                " WHERE oi.version_id=? AND oi.status IN ('open','partial')"
                " AND oi.allocated_qty - oi.shipped_qty > 0"
                " ORDER BY o.created_at, oi.id",
                (version_id,),
            ).fetchall()
            for item in rows:
                if shortage <= 0:
                    break
                cover = min(item["allocated_qty"] - item["shipped_qty"], shortage)
                _log_action(db, incident_id, "refund_or_substitute", "order_item",
                            str(item["id"]),
                            f"许可 {lic['code']} 到期停产后库存缺口 {cover} 件"
                            f"（订单行 {item['id']}），授权期内订单优先履约，"
                            "无法覆盖部分请选择退款或替代",
                            status="pending")
                actions.append({"action": "refund_or_substitute",
                                "item_id": item["id"], "shortage": cover})
                shortage -= cover
        db.execute("UPDATE incidents SET status='processed', processed_at=? WHERE id=?",
                   (now(), incident_id))
        incident_ids.append(incident_id)
        all_actions.extend(actions)
    db.commit()
    return {"on_date": on_date, "expired_licenses": [dict(r)["code"] for r in expired],
            "incident_ids": incident_ids, "actions": all_actions}


# ---------- 特展改期 ----------

def exhibition_rescheduled(db, exhibition_code: str, new_start: str,
                           new_end: str) -> dict:
    from .catalog import reschedule_exhibition
    db.execute("BEGIN IMMEDIATE")
    exhibition = dict(_one(db, "SELECT * FROM exhibitions WHERE code=?",
                           (exhibition_code,)))
    db.execute(
        "UPDATE exhibitions SET start_date=?, end_date=?, status='rescheduled'"
        " WHERE id=?",
        (new_start, new_end, exhibition["id"]),
    )
    _audit(db, "exhibition", exhibition_code, "reschedule",
           {"start_date": new_start, "end_date": new_end})
    incident_id = _create_incident(
        db, "exhibition_rescheduled", exhibition_code,
        {"new_start": new_start, "new_end": new_end},
        f"展览 {exhibition_code} 改期至 {new_start}~{new_end}",
    )
    actions: list[dict] = []
    artworks = db.execute(
        "SELECT * FROM artworks WHERE exhibition_id=?", (exhibition["id"],)
    ).fetchall()
    for arow in artworks:
        licenses = db.execute(
            "SELECT * FROM licenses WHERE artwork_id=? AND status='active'",
            (arow["id"],),
        ).fetchall()
        for lrow in licenses:
            lic = dict(lrow)
            gaps = []
            if lic["valid_until"] < new_end:
                gaps.append(f"许可到期 {lic['valid_until']} 早于新展期结束 {new_end}")
            if lic["valid_from"] > new_start:
                gaps.append(f"许可生效 {lic['valid_from']} 晚于新展期开始 {new_start}")
            if gaps:
                _log_action(db, incident_id, "renew_license", "license", lic["code"],
                            "；".join(gaps) + "，需在改期后的空档前完成续签",
                            status="pending")
                actions.append({"action": "renew_license", "entity": lic["code"],
                                "reasons": gaps})
        # 已上架版本：按新展期第一天评估，若届时断权则预防性停售
        versions = db.execute(
            "SELECT * FROM product_versions WHERE artwork_id=?", (arow["id"],)
        ).fetchall()
        for vrow in versions:
            version = dict(vrow)
            chk = rights.evaluate_version(db, version, on_date=new_start)
            if not chk["ok"]:
                _log_action(db, incident_id, "suspend_sale", "version",
                            version["code"],
                            f"按新展期开始日 {new_start} 评估权利不足："
                            f"{'；'.join(chk['reasons'])}（临期仍未续签则执行停售）",
                            status="pending")
                actions.append({"action": "suspend_sale", "entity": version["code"],
                                "effective_on": new_start})
    db.execute("UPDATE incidents SET status='processed', processed_at=? WHERE id=?",
               (now(), incident_id))
    db.commit()
    return {"incident_id": incident_id, "exhibition": exhibition_code,
            "actions": actions}


# ---------- 供应商延期 ----------

def supplier_delay(db, po_code: str, new_expected_at: str,
                   reason: str | None = None) -> dict:
    db.execute("BEGIN IMMEDIATE")
    po = dict(_one(db, "SELECT * FROM purchase_orders WHERE code=?", (po_code,)))
    db.execute("UPDATE purchase_orders SET status='delayed', expected_at=?, note=?"
               " WHERE id=?", (new_expected_at, reason or po["note"], po["id"]))
    incident_id = _create_incident(
        db, "supplier_delay", po_code,
        {"po": po_code, "new_expected_at": new_expected_at, "reason": reason},
        f"采购单 {po_code} 供应商延期至 {new_expected_at}",
    )
    actions = [{"action": "preserve_commitment", "entity": po_code}]
    _log_action(db, incident_id, "preserve_commitment", "purchase_order", po_code,
                f"采购单已延期（原交期 {po['expected_at']} → {new_expected_at}），"
                "产能锁定承诺继续有效，按延期条款与供应商协商")
    version = dict(_one(db, "SELECT * FROM product_versions WHERE id=?",
                        (po["version_id"],)))
    # 交期晚于任一在用许可到期：在断权日前无法完成交付，建议替代供应商/调减数量
    licenses = db.execute(
        "SELECT code, valid_until FROM licenses WHERE artwork_id=? AND status='active'",
        (version["artwork_id"],),
    ).fetchall()
    for lic in licenses:
        if new_expected_at > lic["valid_until"]:
            _log_action(db, incident_id, "rework_or_reorder", "purchase_order", po_code,
                        f"新交期 {new_expected_at} 晚于许可 {lic['code']} 到期日 "
                        f"{lic['valid_until']}，该批货无法在授权期内合法交付："
                        "应更换供应商提前交付、调减订单，或先完成续签",
                        status="pending")
            actions.append({"action": "rework_or_reorder", "entity": po_code,
                            "license": lic["code"], "valid_until": lic["valid_until"]})
    db.execute("UPDATE incidents SET status='processed', processed_at=? WHERE id=?",
               (now(), incident_id))
    db.commit()
    return {"incident_id": incident_id, "po": po_code, "actions": actions}


# ---------- 质检不合格 ----------

def qc_failure(db, batch_code: str, passed_qty: int, failed_qty: int,
               inspector: str, note: str | None = None) -> dict:
    db.execute("BEGIN IMMEDIATE")
    result = _record_qc(db, batch_code, passed_qty, failed_qty, inspector, note)
    if failed_qty == 0:
        # 全部合格：仅登记质检结果，不产生风险事件
        db.commit()
        return {"incident_id": None, "batch": batch_code, "qc": result, "actions": []}
    batch = dict(_one(db, "SELECT * FROM production_batches WHERE code=?",
                      (batch_code,)))
    incident_id = _create_incident(
        db, "qc_failed", batch_code,
        {"batch": batch_code, "passed": passed_qty, "failed": failed_qty},
        f"批次 {batch_code} 质检：合格 {passed_qty}，不合格 {failed_qty}",
    )
    actions: list[dict] = []
    _log_action(db, incident_id, "rework_or_reorder", "production_batch", batch_code,
                f"{failed_qty} 件不合格已隔离，不得入库；需安排返工或向供应商重新下单",
                status="pending")
    actions.append({"action": "rework_or_reorder", "entity": batch_code})
    # 库存敞口：以 ATP（在手+在途未废数量−已占用）衡量
    atp = sales._version_atp(db, batch["version_id"])
    if atp["atp"] < 0:
        items = db.execute(
            "SELECT oi.*, o.channel FROM order_items oi JOIN orders o ON o.id=oi.order_id"
            " WHERE oi.version_id=? AND oi.status IN ('open','partial')"
            " ORDER BY o.created_at, oi.id",
            (batch["version_id"],),
        ).fetchall()
        shortage = -atp["atp"]
        for item in items:
            if shortage <= 0:
                break
            unshipped = item["allocated_qty"] - item["shipped_qty"]
            cover = min(unshipped, shortage)
            _log_action(db, incident_id, "refund_or_substitute", "order_item",
                        str(item["id"]),
                        f"批次 {batch_code} 不合格导致缺口 {cover} 件"
                        f"（渠道 {item['channel']} 订单行 {item['id']}），"
                        "可等待返工补货或退款/替代",
                        status="pending")
            actions.append({"action": "refund_or_substitute",
                            "item_id": item["id"], "shortage": cover})
            shortage -= cover
    db.execute("UPDATE incidents SET status='processed', processed_at=? WHERE id=?",
               (now(), incident_id))
    db.commit()
    return {"incident_id": incident_id, "batch": batch_code, "qc": result,
            "actions": actions}


# ---------- 待决动作的人工执行 ----------

def resolve_action(db, action_id: int, decision: str,
                   substitute_version: str | None = None,
                   new_until: str | None = None, doc_ref: str | None = None) -> dict:
    """decision: refund / substitute / renew / halt / decline"""
    db.execute("BEGIN IMMEDIATE")
    action = dict(_one(db, "SELECT * FROM incident_actions WHERE id=?", (action_id,)))
    if action["status"] not in ("pending",):
        raise DomainError(f"动作 {action_id} 状态为 {action['status']}，无需处理")
    if decision == "refund" and action["action_type"] == "refund_or_substitute":
        out = refund_item(db, int(action["entity_ref"]), None,
                          f"事件处置退款（{action['detail']}）")
    elif decision == "substitute" and action["action_type"] == "refund_or_substitute":
        if not substitute_version:
            raise DomainError("替代处置需要 substitute_version")
        out = substitute_item(db, int(action["entity_ref"]), substitute_version)
    elif decision == "renew" and action["action_type"] == "renew_license":
        if not new_until or not doc_ref:
            raise DomainError("续签处置需要 new_until 与 doc_ref")
        from .catalog import renew_license
        renew_license(db, action["entity_ref"], new_until, doc_ref)
        out = {"renewed": action["entity_ref"], "valid_until": new_until}
    elif decision == "decline":
        out = {"declined": True}
    else:
        raise DomainError(f"动作类型 {action['action_type']} 不支持决策 {decision}")
    db.execute(
        "UPDATE incident_actions SET status='resolved', resolved_at=? WHERE id=?",
        (now(), action_id),
    )
    _audit(db, "incident_action", str(action_id), f"resolve_{decision}", out or {})
    db.commit()
    return {"action_id": action_id, "decision": decision, "result": out}


def list_pending_actions(db) -> list[dict]:
    rows = db.execute(
        "SELECT a.*, i.code AS incident_code FROM incident_actions a"
        " JOIN incidents i ON i.id=a.incident_id WHERE a.status='pending'"
        " ORDER BY a.id"
    ).fetchall()
    return [dict(r) for r in rows]
