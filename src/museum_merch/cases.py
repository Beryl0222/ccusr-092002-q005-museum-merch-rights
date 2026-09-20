"""事件处置引擎。

特展改期、授权撤回/到期、供应商延期、质检不合格统一进入「事件 → 工单 → 处置动作」：
每个动作都有类型、对象、解释与执行结果；确定性动作自动执行，需要商业判断的
（续展、替代版本、重新下单）保留为待人工裁决。已确认的采购承诺一律保留不删。
"""

from __future__ import annotations

import json
from typing import Any

from . import fulfillment
from .rights import evaluate_version
from .store import DomainError, Store, new_id, now

AUTO_ACTIONS = {"delist", "refund-unshipped", "cancel-po", "halt-production",
                "quarantine-stock", "await"}
MANUAL_ACTIONS = {"renew-license", "substitute", "reorder", "keep-commitment"}


def _add_action(conn, case_id: str, action_type: str, target_ref: str | None,
                detail: str, status: str = "proposed", result: str = "") -> str:
    action_id = new_id("act")
    conn.execute(
        "INSERT INTO case_action(id,case_id,action_type,target_ref,detail,status,"
        "result,created_at,executed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (action_id, case_id, action_type, target_ref, detail, status, result,
         now(), now() if status == "executed" else None),
    )
    return action_id


def _create_case(conn, *, event_id: str | None, version_id: str | None, kind: str,
                 title: str, explanation: str, scope: dict[str, Any]) -> str:
    case_id = new_id("case")
    case_no = f"C-{kind[:2].upper()}-{new_id('n')[:6]}"
    conn.execute(
        "INSERT INTO case_record(id,case_no,event_id,version_id,kind,title,explanation,"
        "scope,status,created_at) VALUES(?,?,?,?,?,?,?,?,'open',?)",
        (case_id, case_no, event_id, version_id, kind, title, explanation,
         json.dumps(scope, ensure_ascii=False), now()),
    )
    return case_id


# ---------------------------------------------------------------- 授权失效（撤回/到期）

def _affect_version(conn, case_id: str, version, markets_failed: list[str],
                    reason_text: str, on_date: str) -> None:
    """对一个受影响版本生成处置动作。markets_failed 为空表示全部市场失守。"""
    version_id = version["id"]
    all_markets = [r["channel"] + "/" + r["region"] for r in conn.execute(
        "SELECT channel,region FROM version_market WHERE version_id=?", (version_id,))]
    if not markets_failed or set(markets_failed) >= set(all_markets):
        conn.execute("UPDATE product_version SET status='blocked', blocked_reason=?"
                     " WHERE id=?", (reason_text, version_id))
        listings = conn.execute(
            "SELECT id,channel,region FROM listing WHERE version_id=? AND status='active'",
            (version_id,)).fetchall()
    else:
        listings = conn.execute(
            "SELECT id,channel,region FROM listing WHERE version_id=? AND status='active'"
            " AND channel||'/'||region IN (%s)"
            % ",".join("?" for _ in markets_failed),
            (version_id, *markets_failed)).fetchall()
    for lst in listings:
        _add_action(conn, case_id, "delist", lst["id"],
                    f"停止 {lst['channel']}/{lst['region']} 在售（{reason_text}）")

    # 未发货订单：先退款取消、释放预留，已发货部分不动（版税已按当时有效授权计提）
    open_items = conn.execute(
        "SELECT i.id AS item_id, i.qty-i.qty_shipped-i.qty_cancelled AS open_qty,"
        " o.channel, o.region, o.order_no FROM order_item i JOIN sales_order o"
        " ON o.id=i.order_id WHERE i.version_id=? AND i.status='open'",
        (version_id,)).fetchall()
    for it in open_items:
        market = f"{it['channel']}/{it['region']}"
        if markets_failed and market not in markets_failed:
            continue
        if it["open_qty"] > 0:
            _add_action(conn, case_id, "refund-unshipped", it["item_id"],
                        f"订单 {it['order_no']} 未发货 {it['open_qty']} 件退款并取消")

    # 采购单：未承诺可取消；已承诺只停产、保留合同
    for po in conn.execute(
            "SELECT * FROM purchase_order WHERE version_id=?", (version_id,)):
        if po["status"] == "draft":
            _add_action(conn, case_id, "cancel-po", po["id"],
                        f"采购单 {po['po_no']} 尚未确认，直接取消，不构成承诺")
        elif po["status"] in fulfillment.OPEN_PO_STATUSES:
            _add_action(conn, case_id, "halt-production", po["id"],
                        f"采购单 {po['po_no']} 已对供应商确认（合同承诺保留），"
                        "停止后续生产，已产出合格品隔离待议")
        elif po["status"] == "halted":
            _add_action(conn, case_id, "keep-commitment", po["id"],
                        f"采购单 {po['po_no']} 已处于停产状态，合同承诺继续保留",
                        status="executed", result="无需重复停产")

    # 替代版本候选：同产品下仍通过闸门的其他版本
    candidate = conn.execute(
        "SELECT v2.id FROM product_version v1 JOIN product_version v2"
        " ON v1.product_id=v2.product_id WHERE v1.id=? AND v2.id!=v1.id"
        " AND v2.status!='blocked' AND v2.content_status='approved'"
        " ORDER BY v2.version_no DESC LIMIT 1", (version_id,)).fetchone()
    if candidate:
        _add_action(conn, case_id, "substitute", candidate["id"],
                    "可将未发货订单替代为同产品的有效版本，需人工确认")
    else:
        _add_action(conn, case_id, "renew-license", version_id,
                    "无替代版本；完成续展/重新取得授权后可恢复采购与上架，需人工跟进")

    # 全部市场失守时，剩余合格库存隔离，避免被其他渠道继续售卖
    if not markets_failed or set(markets_failed) >= set(all_markets):
        totals = fulfillment.stock_totals(conn, version_id)
        if totals["sellable"] > 0:
            _add_action(conn, case_id, "quarantine-stock", version_id,
                        f"隔离剩余合格库存 {totals['sellable']} 件，等待权利恢复或处置")


def _handle_rights_loss(store: Store, conn, event, *, kind: str, on_date: str) -> str:
    payload = json.loads(event["payload"])
    license_id = event["aggregate_ref"]
    lic = conn.execute("SELECT * FROM license WHERE id=?", (license_id,)).fetchone()
    artwork_id = lic["artwork_id"]
    versions = conn.execute("SELECT * FROM product_version WHERE artwork_id=?",
                            (artwork_id,)).fetchall()
    if not versions:
        # 没有关联商品：只记一个解释性工单，便于权利方看到影响面为零
        case_id = _create_case(
            conn, event_id=event["id"], version_id=None, kind=kind,
            title=f"授权{ '撤回' if kind == 'rights-withdrawal' else '到期' }（无关联商品）",
            explanation=payload.get("reason", "授权失效"),
            scope={"license_id": license_id, "versions": []})
        _add_action(conn, case_id, "await", license_id,
                    "该授权当前无关联产品版本，无需停产停售",
                    status="executed", result="无影响")
        return case_id

    first_case = None
    reason_text = ("授权撤回：" + payload.get("reason", "")) if kind == "rights-withdrawal" \
        else f"授权已于 {lic['valid_until']} 到期"
    for version in versions:
        review = evaluate_version(store, version["id"], "sale", on_date)
        failed = [m["channel"] + "/" + m["region"]
                  for m in review["markets"] if m["decision"] == "fail"]
        passed = [m["channel"] + "/" + m["region"]
                  for m in review["markets"] if m["decision"] == "pass"]
        if not failed:
            continue  # 另有授权覆盖，该版本不受影响
        case_id = _create_case(
            conn, event_id=event["id"], version_id=version["id"], kind=kind,
            title=f"{'授权撤回' if kind == 'rights-withdrawal' else '授权到期'}："
                  f"{version['title']}（{lic['code']}）",
            explanation=f"授权 {lic['code']} 失效，缺口市场 {failed or '全部市场'}；"
                        f"仍有效市场 {passed or '无'}。宣传许可不等于商品复制许可。",
            scope={"license_id": license_id, "failed_markets": failed,
                   "passed_markets": passed})
        _affect_version(conn, case_id, dict(version), failed, reason_text, on_date)
        first_case = first_case or case_id
    if first_case is None:
        case_id = _create_case(
            conn, event_id=event["id"], version_id=None, kind=kind,
            title="授权失效但产品仍被其他授权覆盖",
            explanation=f"授权 {lic['code']} 失效，但所有关联版本另有有效授权覆盖。",
            scope={"license_id": license_id, "versions": [v["id"] for v in versions]})
        _add_action(conn, case_id, "await", license_id,
                    "各版本仍在其他有效授权范围内，维持现状并持续监控",
                    status="executed", result="无影响")
        first_case = case_id
    return first_case


# ---------------------------------------------------------------- 特展改期

def _handle_reschedule(store: Store, conn, event, on_date: str) -> str:
    payload = json.loads(event["payload"])
    exhibition_id = event["aggregate_ref"]
    versions = conn.execute(
        "SELECT v.* FROM product_version v JOIN product p ON p.id=v.product_id"
        " WHERE p.exhibition_id=?", (exhibition_id,)).fetchall()
    case_id = _create_case(
        conn, event_id=event["id"], version_id=None, kind="reschedule",
        title=f"特展改期：{payload['old_start']}~{payload['old_end']} → "
              f"{payload['new_start']}~{payload['new_end']}",
        explanation="改期后需复核授权期限是否覆盖新展期；已确认的采购承诺继续保留。",
        scope={"exhibition_id": exhibition_id, "versions": [v["id"] for v in versions]})
    for version in versions:
        v = dict(version)
        # 新展期结束时点仍须有有效权利；否则要么续展，要么到期自动停售
        review = evaluate_version(store, v["id"], "sale", payload["new_end"])
        failed = [m["channel"] + "/" + m["region"]
                  for m in review["markets"] if m["decision"] == "fail"]
        if failed:
            _add_action(conn, case_id, "renew-license", v["id"],
                        f"{v['title']} 在新展期末 {payload['new_end']} 缺口市场 "
                        f"{failed}，须在到期前完成续展，否则到期自动停售退款")
        else:
            _add_action(conn, case_id, "await", v["id"],
                        f"{v['title']} 的授权覆盖新展期，无需变更",
                        status="executed", result="覆盖新展期")
        for po in conn.execute(
                "SELECT po_no,status FROM purchase_order WHERE version_id=? AND committed=1",
                (v["id"],)):
            _add_action(conn, case_id, "keep-commitment", None,
                        f"采购单 {po['po_no']}（{po['status']}）合同承诺保留，"
                        "按新档期与供应商协商交期，不撤单",
                        status="executed", result="承诺保留")
    return case_id


# ---------------------------------------------------------------- 供应商延期

def _handle_supplier_delay(store: Store, conn, event, on_date: str) -> str:
    payload = json.loads(event["payload"])
    po = conn.execute("SELECT * FROM purchase_order WHERE id=?",
                      (event["aggregate_ref"],)).fetchone()
    case_id = _create_case(
        conn, event_id=event["id"], version_id=po["version_id"], kind="supplier-delay",
        title=f"供应商延期：采购单 {po['po_no']} → {payload['new_expected_at']}",
        explanation=f"延期原因：{payload['reason']}。采购承诺保留，"
                    "复核新交期是否仍在授权期内。",
        scope={"po_id": po["id"], "new_expected_at": payload["new_expected_at"]})
    review = evaluate_version(store, po["version_id"], "sale",
                              payload["new_expected_at"])
    failed = [m["channel"] + "/" + m["region"]
              for m in review["markets"] if m["decision"] == "fail"]
    if failed:
        _add_action(conn, case_id, "renew-license", po["version_id"],
                    f"新交期 {payload['new_expected_at']} 已超出授权期限（缺口 {failed}），"
                    "到货也无法上架；须先续展，否则该批货物只能隔离/转授权期内渠道")
        _add_action(conn, case_id, "halt-production", po["id"],
                    "交期落在授权期外，暂停继续投产等待续展结果（合同承诺保留）")
    else:
        _add_action(conn, case_id, "await", po["id"],
                    "交期仍在授权有效期内，维持采购承诺并跟踪到货，到货质检合格后入库",
                    status="executed", result="继续等待交货")
    return case_id


# ---------------------------------------------------------------- 质检不合格

def _handle_qc_failed(conn, event, on_date: str) -> str:
    payload = json.loads(event["payload"])
    batch_id = event["aggregate_ref"]
    batch = conn.execute("SELECT * FROM production_batch WHERE id=?",
                         (batch_id,)).fetchone()
    case_id = _create_case(
        conn, event_id=event["id"], version_id=batch["version_id"], kind="qc-failed",
        title=f"质检{ '整批判退' if payload['decision']=='fail' else '部分不合格' }："
              f"{payload['batch_no']}",
        explanation=f"送检 {payload['qty_passed']+payload['qty_failed']} 件，"
                    f"合格 {payload['qty_passed']}，不合格 {payload['qty_failed']}；"
                    f"备注：{payload.get('notes','')}。合格品已入共享库存，不合格品隔离。",
        scope={"batch_id": batch_id, "qty_passed": payload["qty_passed"],
               "qty_failed": payload["qty_failed"]})
    _add_action(conn, case_id, "quarantine-stock", batch_id,
                f"{payload['qty_failed']} 件不合格品已入隔离台账，不得销售",
                status="executed", result="已随质检入库自动隔离")
    if payload["qty_passed"] > 0:
        _add_action(conn, case_id, "await", batch_id,
                    f"{payload['qty_passed']} 件合格品已入共享库存，可正常销售",
                    status="executed", result="合格品入库")
    # 补产建议：缺口 = 已下单未交付（无合格批次覆盖的在途 PO 数量）
    open_pos = conn.execute(
        "SELECT po_no, qty_ordered FROM purchase_order WHERE version_id=?"
        " AND status IN ('confirmed','delayed','in-production','halted')",
        (batch["version_id"],)).fetchall()
    detail = ("质检不合格，建议重新下单补足缺口；重新采购会再次强制通过权利闸门"
              if open_pos else "质检不合格，如仍需销售可重新下单（须再次通过权利闸门）")
    _add_action(conn, case_id, "reorder", batch["version_id"], detail)
    return case_id


# ---------------------------------------------------------------- 事件轮询与执行

_HANDLERS = {
    "license.withdrawn": lambda store, conn, e, d:
        _handle_rights_loss(store, conn, e, kind="rights-withdrawal", on_date=d),
    "license.expired": lambda store, conn, e, d:
        _handle_rights_loss(store, conn, e, kind="rights-expired", on_date=d),
    "exhibition.rescheduled": _handle_reschedule,
    "supplier.delay": _handle_supplier_delay,
    "qc.failed": lambda store, conn, e, d: _handle_qc_failed(conn, e, d),
}


def process_events(store: Store, on_date: str) -> list[str]:
    """取出未处理事件，生成工单与动作，并自动执行确定性动作。"""
    case_ids: list[str] = []
    with store.transaction() as conn:
        events = conn.execute(
            "SELECT * FROM domain_event WHERE processed=0 ORDER BY created_at,rowid"
        ).fetchall()
        for event in events:
            handler = _HANDLERS.get(event["type"])
            if handler is None:
                continue
            case_id = handler(store, conn, event, on_date)
            if case_id:
                case_ids.append(case_id)
            conn.execute("UPDATE domain_event SET processed=1 WHERE id=?", (event["id"],))
    # 自动执行放在事务外按动作逐个跑（每个动作各自独立事务）
    for case_id in case_ids:
        execute_auto_actions(store, case_id, on_date)
    return case_ids


def execute_auto_actions(store: Store, case_id: str, on_date: str) -> list[str]:
    executed: list[str] = []
    pending = store.all(
        "SELECT * FROM case_action WHERE case_id=? AND status='proposed'", (case_id,))
    # 存在待决替代方案时，未发货订单先挂起：人工确认替代则转入新版本，
    # 明确拒绝替代后才走退款（见 decline_substitution）
    has_pending_substitute = any(
        a["action_type"] == "substitute" for a in pending)
    for action in pending:
        if action["action_type"] not in AUTO_ACTIONS:
            continue
        if has_pending_substitute and action["action_type"] == "refund-unshipped":
            continue
        result = _execute_one(store, action, on_date)
        executed.append(action["id"])
        with store.transaction() as conn:
            conn.execute("UPDATE case_action SET status='executed', result=?, executed_at=?"
                         " WHERE id=?", (result, now(), action["id"]))
    with store.transaction() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM case_action WHERE case_id=? AND status='proposed'",
            (case_id,)).fetchone()["n"]
        if remaining == 0:
            conn.execute("UPDATE case_record SET status='actioned', actioned_at=?"
                         " WHERE id=?", (now(), case_id))
    return executed


def _execute_one(store: Store, action, on_date: str) -> str:
    t = action["action_type"]
    ref = action["target_ref"]
    if t == "delist":
        with store.transaction() as conn:
            row = conn.execute("SELECT version_id,channel,region FROM listing WHERE id=?",
                               (ref,)).fetchone()
            conn.execute("UPDATE listing SET status='stopped', stopped_at=?, case_id=?"
                         " WHERE id=?", (now(), action["case_id"], ref))
            return f"已停售 {row['channel']}/{row['region']}"
    if t == "refund-unshipped":
        item = store.must_one("SELECT * FROM order_item WHERE id=?", (ref,), "订单明细")
        open_qty = item["qty"] - item["qty_shipped"] - item["qty_cancelled"]
        if open_qty <= 0:
            return "无可退数量（可能已处置）"
        out = fulfillment.cancel_unshipped(
            store, item_id=ref, qty=open_qty, reason="授权失效/工单处置",
            case_id=action["case_id"])
        return f"已取消未发货 {open_qty} 件并退款 {out['amount_cents']} 分（退款单 {out['refund_id']}）"
    if t == "cancel-po":
        with store.transaction() as conn:
            conn.execute("UPDATE purchase_order SET status='cancelled' WHERE id=? AND"
                         " status='draft'", (ref,))
            return "未确认采购单已取消"
    if t == "halt-production":
        with store.transaction() as conn:
            conn.execute("UPDATE purchase_order SET status='halted' WHERE id=?"
                         " AND status IN ('confirmed','in-production','delayed')", (ref,))
            return "已停产；采购单记录与合同承诺保留"
    if t == "quarantine-stock":
        with store.transaction() as conn:
            if ref and ref.startswith("bat"):
                return action["detail"]  # 质检隔离在登记质检时已完成
            totals = fulfillment.stock_totals(conn, ref)
            qty = totals["on_hand"] - totals["reserved"]
            if qty > 0:
                conn.execute(
                    "INSERT INTO stock_ledger(id,version_id,batch_id,channel,"
                    "delta_on_hand,delta_reserved,delta_quarantine,reason,ref,created_at)"
                    " VALUES(?,?,NULL,NULL,?,0,?,?,?,?)",
                    (new_id("led"), ref, -qty, qty, "rights-quarantine",
                     action["case_id"], now()))
            return f"已隔离 {max(0,qty)} 件合格库存"
    if t == "await":
        return action["detail"]
    raise DomainError("manual-action", f"{t} 需人工裁决后执行")


def execute_substitution(store: Store, case_id: str, replacement_version_id: str) -> dict:
    """人工确认替代：把工单内未发货明细转到替代版本（同价、同渠道预留，不退款）。"""
    case = store.must_one("SELECT * FROM case_record WHERE id=?", (case_id,), "工单")
    scope = json.loads(case["scope"] or "{}")
    failed_markets = set(scope.get("failed_markets") or [])
    results = []
    with store.transaction() as conn:
        actions = conn.execute(
            "SELECT * FROM case_action WHERE case_id=? AND action_type='substitute'"
            " AND status='proposed'", (case_id,)).fetchall()
        if not actions:
            raise DomainError("no-action", "该工单没有待确认的替代动作")
        # 只替代工单受影响市场内的未发货明细；其他市场仍在有效授权下，原样保留
        old_version_id = case["version_id"]
        items = conn.execute(
            "SELECT i.*, o.channel, o.region, o.id AS oid FROM order_item i"
            " JOIN sales_order o ON o.id=i.order_id WHERE i.version_id=?"
            " AND i.status='open'", (old_version_id,)).fetchall()
        items = [it for it in items
                 if not failed_markets
                 or f"{it['channel']}/{it['region']}" in failed_markets]
        for it in items:
            qty = it["qty"] - it["qty_shipped"] - it["qty_cancelled"]
            if qty <= 0:
                continue
            listing = conn.execute(
                "SELECT 1 FROM listing WHERE version_id=? AND channel=? AND region=?"
                " AND status='active'",
                (replacement_version_id, it["channel"], it["region"])).fetchone()
            if not listing:
                raise DomainError("not-listed",
                                  f"替代版本未在 {it['channel']}/{it['region']} 在售，"
                                  "无法替代该渠道订单")
            cap = fulfillment.channel_cap(conn, replacement_version_id, it["channel"])
            if qty > cap:
                raise DomainError("oversell",
                                  f"替代版本在 {it['channel']} 额度不足（需 {qty}，余 {cap}）")
        for it in items:
            qty = it["qty"] - it["qty_shipped"] - it["qty_cancelled"]
            if qty <= 0:
                continue
            # 释放旧版本预留，占用新版本预留
            conn.execute(
                "INSERT INTO stock_ledger(id,version_id,batch_id,channel,delta_on_hand,"
                "delta_reserved,delta_quarantine,reason,ref,created_at)"
                " VALUES(?,?,NULL,?,0,?,0,'substitute-out',?,?)",
                (new_id("led"), old_version_id, it["channel"], -qty, it["id"], now()))
            conn.execute(
                "INSERT INTO stock_ledger(id,version_id,batch_id,channel,delta_on_hand,"
                "delta_reserved,delta_quarantine,reason,ref,created_at)"
                " VALUES(?,?,NULL,?,0,?,0,'substitute-in',?,?)",
                (new_id("led"), replacement_version_id, it["channel"], qty, it["id"], now()))
            conn.execute("UPDATE order_item SET version_id=?, status='open' WHERE id=?",
                         (replacement_version_id, it["id"]))
            conn.execute("UPDATE stock_reservation SET version_id=? WHERE item_id=?",
                         (replacement_version_id, it["id"]))
            results.append({"item_id": it["id"], "qty": qty})
        conn.execute(
            "UPDATE case_record SET replacement_version_id=? WHERE id=?",
            (replacement_version_id, case_id))
        for a in actions:
            conn.execute("UPDATE case_action SET status='executed', result=?,"
                         " executed_at=? WHERE id=?",
                         (f"已替代 {len(results)} 条未发货明细 → {replacement_version_id}",
                          now(), a["id"]))
        # 已转入替代版本，针对这些明细的退款动作不再执行
        conn.execute(
            "UPDATE case_action SET status='skipped', result='订单已替代，无需退款',"
            " executed_at=? WHERE case_id=? AND action_type='refund-unshipped'",
            (now(), case_id))
    execute_auto_actions(store, case_id, _today())
    return {"substituted": results}


def decline_substitution(store: Store, case_id: str, note: str = "") -> list[str]:
    """人工决定不替代：挂起的未发货明细立即退款取消。"""
    with store.transaction() as conn:
        conn.execute(
            "UPDATE case_action SET status='skipped', result=COALESCE(NULLIF(?,''),"
            "'人工确认不替代，转退款'), executed_at=? WHERE case_id=?"
            " AND action_type='substitute' AND status='proposed'",
            (note, now(), case_id))
    return execute_auto_actions(store, case_id, _today())


def _today() -> str:
    return now()[:10]


def sweep_expiries(store: Store, on_date: str) -> list[str]:
    """到期巡检：把已过 valid_until 的 active 授权置 expired 并产生事件，
    随后走与撤回完全相同的处置链（停售/退款/隔离），杜绝授权到期后线上仍在售。"""
    event_ids: list[str] = []
    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM license WHERE status='active' AND valid_until<?",
            (on_date,)).fetchall()
        for lic in rows:
            conn.execute("UPDATE license SET status='expired' WHERE id=?", (lic["id"],))
            event_id = new_id("evt")
            conn.execute(
                "INSERT INTO domain_event(id,type,aggregate_ref,payload,processed,created_at)"
                " VALUES(?,?,?,?,0,?)",
                (event_id, "license.expired", lic["id"],
                 json.dumps({"code": lic["code"]}, ensure_ascii=False), now()))
            event_ids.append(event_id)
    if event_ids:
        return process_events(store, on_date)
    return []
