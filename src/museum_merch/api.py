"""JSON HTTP API：把领域服务暴露为 REST 路由。"""

from __future__ import annotations

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import analytics, cases as cases_svc, catalog, fulfillment, rights, royalty
from .store import DomainError, Store, now


def _today() -> str:
    return now()[:10]


class Api:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.routes: list[tuple[str, re.Pattern[str], Callable]] = []

    def route(self, method: str, pattern: str) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    def handle(self, method: str, path: str, body: dict[str, Any] | None,
               qs: dict[str, str]) -> tuple[int, Any]:
        for m, regex, fn in self.routes:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                return fn(self, body or {}, qs, **match.groupdict())
        return 404, {"error": "not-found", "path": path}


def build_api(store: Store) -> Api:
    api = Api(store)
    r = api.route

    # ------------------------------------------------------------ 策展/权利

    @r("POST", r"/api/exhibitions")
    def _(api, b, q):
        return 201, {"id": rights.create_exhibition(
            api.store, id=b["id"], title=b["title"], start_date=b["start_date"],
            end_date=b["end_date"], status=b.get("status", "scheduled"))}

    @r("POST", r"/api/exhibitions/(?P<eid>[^/]+)/reschedule")
    def _(api, b, q, eid):
        event_id = rights.reschedule_exhibition(
            api.store, eid, start_date=b["start_date"], end_date=b["end_date"])
        case_ids = cases_svc.process_events(api.store, b.get("on") or _today())
        return 200, {"event_id": event_id, "case_ids": case_ids}

    @r("POST", r"/api/holders")
    def _(api, b, q):
        return 201, {"id": rights.create_holder(
            api.store, id=b["id"], name=b["name"], kind=b["kind"],
            contact=b.get("contact"))}

    @r("POST", r"/api/artworks")
    def _(api, b, q):
        return 201, {"id": rights.create_artwork(
            api.store, id=b["id"], exhibition_id=b.get("exhibition_id"),
            title=b["title"], catalog_no=b.get("catalog_no"),
            holder_splits=b.get("holder_splits"))}

    @r("POST", r"/api/licenses")
    def _(api, b, q):
        return 201, {"id": rights.create_license(
            api.store, id=b["id"], code=b["code"], artwork_id=b["artwork_id"],
            holder_id=b["holder_id"], uses=b["uses"], channels=b["channels"],
            regions=b["regions"], valid_from=b["valid_from"],
            valid_until=b["valid_until"], royalty_rate_bps=b.get("royalty_rate_bps"),
            royalty_per_unit_cents=b.get("royalty_per_unit_cents"),
            sublicensable=b.get("sublicensable", False), signed_at=b.get("signed_at"),
            evidences=b.get("evidences"))}

    @r("POST", r"/api/licenses/renew")
    def _(api, b, q):
        lid = rights.renew_license(
            api.store, code=b["code"], old_license_id=b["old_license_id"],
            valid_from=b["valid_from"], valid_until=b["valid_until"],
            royalty_rate_bps=b.get("royalty_rate_bps"),
            royalty_per_unit_cents=b.get("royalty_per_unit_cents"))
        return 201, {"id": lid}

    @r("POST", r"/api/licenses/(?P<lid>[^/]+)/withdraw")
    def _(api, b, q, lid):
        event_id = rights.withdraw_license(api.store, lid, b.get("reason", ""))
        case_ids = cases_svc.process_events(api.store, b.get("on") or _today())
        return 200, {"event_id": event_id, "case_ids": case_ids}

    # ------------------------------------------------------------ 产品/版本

    @r("POST", r"/api/suppliers")
    def _(api, b, q):
        return 201, {"id": catalog.create_supplier(
            api.store, id=b["id"], name=b["name"],
            lead_time_days=b.get("lead_time_days"))}

    @r("POST", r"/api/products")
    def _(api, b, q):
        return 201, {"id": catalog.create_product(
            api.store, id=b["id"], sku=b["sku"], name=b["name"],
            exhibition_id=b.get("exhibition_id"))}

    @r("POST", r"/api/products/(?P<pid>[^/]+)/versions")
    def _(api, b, q, pid):
        return 201, {"id": catalog.create_version(
            api.store, product_id=pid, artwork_id=b["artwork_id"],
            use_type=b["use_type"], title=b["title"], price_cents=b["price_cents"],
            spec=b.get("spec"), version_no=b.get("version_no"),
            version_id=b.get("id"))}

    @r("POST", r"/api/versions/(?P<vid>[^/]+)/markets")
    def _(api, b, q, vid):
        for m in b["markets"]:
            catalog.add_version_market(api.store, vid, m["channel"], m["region"])
        return 200, {"ok": True}

    @r("POST", r"/api/versions/(?P<vid>[^/]+)/quotas")
    def _(api, b, q, vid):
        for quota in b["quotas"]:
            catalog.set_channel_quota(
                api.store, vid, quota["channel"], int(quota["quota_qty"]))
        return 200, {"ok": True}

    @r("POST", r"/api/versions/(?P<vid>[^/]+)/content-review")
    def _(api, b, q, vid):
        rights.review_content(api.store, vid, b["decision"],
                              b.get("reviewer", ""), b.get("notes", ""))
        return 200, {"ok": True}

    @r("GET", r"/api/versions/(?P<vid>[^/]+)/gate")
    def _(api, b, q, vid):
        return 200, rights.evaluate_version(
            api.store, vid, q.get("stage", "sale"), q.get("on", _today()),
            q.get("channel") or None, q.get("region") or None)

    @r("POST", r"/api/versions/(?P<vid>[^/]+)/listings")
    def _(api, b, q, vid):
        return 201, {"id": catalog.list_version(
            api.store, vid, b["channel"], b["region"], b.get("on") or _today())}

    @r("POST", r"/api/versions/(?P<vid>[^/]+)/resume")
    def _(api, b, q, vid):
        markets = [(m["channel"], m["region"]) for m in b.get("markets", [])] or None
        return 200, {"relisted": catalog.resume_version(
            api.store, vid, b.get("on") or _today(), markets)}

    # ------------------------------------------------------------ 采购/生产/质检

    @r("POST", r"/api/pos")
    def _(api, b, q):
        return 201, {"id": fulfillment.create_po(
            api.store, po_no=b["po_no"], version_id=b["version_id"],
            supplier_id=b["supplier_id"], qty_ordered=int(b["qty_ordered"]),
            unit_cost_cents=int(b["unit_cost_cents"]),
            expected_at=b.get("expected_at"), on_date=b_date(b, q))}

    @r("POST", r"/api/pos/(?P<poid>[^/]+)/confirm")
    def _(api, b, q, poid):
        fulfillment.confirm_po(api.store, poid)
        return 200, {"ok": True}

    @r("POST", r"/api/pos/(?P<poid>[^/]+)/delay")
    def _(api, b, q, poid):
        event_id = fulfillment.report_po_delay(
            api.store, poid, b["new_expected_at"], b.get("reason", ""))
        case_ids = cases_svc.process_events(api.store, b.get("on") or _today())
        return 200, {"event_id": event_id, "case_ids": case_ids}

    @r("POST", r"/api/pos/(?P<poid>[^/]+)/batches")
    def _(api, b, q, poid):
        return 201, {"id": fulfillment.produce_batch(
            api.store, po_id=poid, qty_produced=int(b["qty_produced"]))}

    @r("POST", r"/api/batches/(?P<bid>[^/]+)/qc")
    def _(api, b, q, bid):
        event_id = fulfillment.record_qc(
            api.store, batch_id=bid, qty_inspected=int(b["qty_inspected"]),
            qty_passed=int(b["qty_passed"]), notes=b.get("notes", ""))
        case_ids = cases_svc.process_events(api.store, b.get("on") or _today()) \
            if event_id else []
        return 200, {"event_id": event_id, "case_ids": case_ids}

    # ------------------------------------------------------------ 订单/履约

    @r("POST", r"/api/orders")
    def _(api, b, q):
        return 201, {"id": fulfillment.place_order(
            api.store, order_no=b["order_no"], channel=b["channel"],
            region=b["region"], items=b["items"], on_date=b_date(b, q),
            customer_ref=b.get("customer_ref"))}

    @r("POST", r"/api/order-items/(?P<iid>[^/]+)/ship")
    def _(api, b, q, iid):
        ids = fulfillment.ship_item(
            api.store, item_id=iid, qty=int(b["qty"]), on=b_date(b, q))
        return 200, {"shipment_ids": ids}

    @r("POST", r"/api/order-items/(?P<iid>[^/]+)/cancel")
    def _(api, b, q, iid):
        out = fulfillment.cancel_unshipped(
            api.store, item_id=iid, qty=int(b["qty"]),
            reason=b.get("reason", "客户/渠道取消"))
        return 200, out

    @r("POST", r"/api/order-items/(?P<iid>[^/]+)/return")
    def _(api, b, q, iid):
        return 200, fulfillment.return_shipped(
            api.store, item_id=iid, qty=int(b["qty"]),
            reason=b.get("reason", "退货"), restock=b.get("restock", False))

    # ------------------------------------------------------------ 事件/工单/版税

    @r("POST", r"/api/events/process")
    def _(api, b, q):
        return 200, {"case_ids": cases_svc.process_events(
            api.store, b.get("on") or _today())}

    @r("POST", r"/api/expiries/sweep")
    def _(api, b, q):
        return 200, {"case_ids": cases_svc.sweep_expiries(
            api.store, b.get("on") or _today())}

    @r("GET", r"/api/cases")
    def _(api, b, q):
        rows = api.store.all(
            "SELECT * FROM case_record ORDER BY created_at DESC LIMIT 200")
        return 200, [dict(r) for r in rows]

    @r("GET", r"/api/cases/(?P<cid>[^/]+)")
    def _(api, b, q, cid):
        case = api.store.one("SELECT * FROM case_record WHERE id=?", (cid,))
        if case is None:
            return 404, {"error": "not-found"}
        actions = api.store.all(
            "SELECT * FROM case_action WHERE case_id=? ORDER BY created_at,rowid", (cid,))
        return 200, {**dict(case), "actions": [dict(a) for a in actions]}

    @r("POST", r"/api/cases/(?P<cid>[^/]+)/substitute")
    def _(api, b, q, cid):
        return 200, cases_svc.execute_substitution(
            api.store, cid, b["replacement_version_id"])

    @r("POST", r"/api/cases/(?P<cid>[^/]+)/decline-substitution")
    def _(api, b, q, cid):
        return 200, {"executed": cases_svc.decline_substitution(
            api.store, cid, b.get("note", ""))}

    @r("POST", r"/api/case-actions/(?P<aid>[^/]+)/resolve")
    def _(api, b, q, aid):
        """人工动作处置：skip（放弃）或 note（已线下完成，如续展已签、已重新下单）。"""
        with api.store.transaction() as conn:
            conn.execute("UPDATE case_action SET status='skipped', result=?, executed_at=?"
                         " WHERE id=? AND status='proposed'",
                         (b.get("note", "人工处置完成"), now(), aid))
        return 200, {"ok": True}

    @r("POST", r"/api/royalties/settle")
    def _(api, b, q):
        return 200, royalty.settle(
            api.store, holder_id=b["holder_id"], period_start=b["period_start"],
            period_end=b["period_end"], mark_paid=b.get("mark_paid", False))

    # ------------------------------------------------------------ 看板/追溯

    @r("GET", r"/api/renewals")
    def _(api, b, q):
        return 200, analytics.renewals_due(
            api.store, q.get("on", _today()), within_days=int(q.get("within", "60")))

    @r("GET", r"/api/exhibitions/(?P<eid>[^/]+)/risk")
    def _(api, b, q, eid):
        return 200, analytics.exhibition_risk(
            api.store, eid, q.get("on", _today()))

    @r("GET", r"/api/stock/exposure")
    def _(api, b, q):
        return 200, analytics.stock_exposure(api.store, q.get("on", _today()))

    @r("GET", r"/api/versions/(?P<vid>[^/]+)/trace")
    def _(api, b, q, vid):
        return 200, analytics.trace_version(api.store, vid)

    @r("GET", r"/api/versions/(?P<vid>[^/]+)/stock")
    def _(api, b, q, vid):
        totals = fulfillment.stock_totals(api.store.conn, vid)
        quotas = [dict(r) for r in api.store.all(
            "SELECT channel,quota_qty FROM channel_quota WHERE version_id=?", (vid,))]
        return 200, {**totals, "quotas": quotas}

    return api


def b_date(b: dict[str, Any], q: dict[str, str]) -> str:
    return b.get("on") or q.get("on") or _today()


# ---------------------------------------------------------------- HTTP 封装

class Handler(BaseHTTPRequestHandler):
    api: Api = None  # 由 make_server 注入

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _query(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlsplit
        parsed = urlsplit(self.path)
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def _dispatch(self, method: str) -> None:
        from urllib.parse import urlsplit
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._write(200, {"status": "ok", "service": "美术馆文创授权履约系统"})
            return
        try:
            body = self._read_body() if method in ("POST", "PUT") else {}
            code, payload = self.api.handle(method, path, body, self._query())
        except DomainError as exc:
            code = {"not-found": 404, "duplicate": 409, "already-listed": 409,
                    "not-listed": 409, "gate-failed": 422, "oversell": 409,
                    "rights-lapsed": 422, "invalid-state": 409,
                    "insufficient-stock": 409, "nothing-to-settle": 404,
                    "no-action": 404}.get(exc.code, 400)
            self._write(code, {"error": exc.code, "message": exc.message,
                               "detail": exc.detail})
            return
        except (json.JSONDecodeError, KeyError) as exc:
            self._write(400, {"error": "bad-request", "message": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - 服务边界统一兜底
            traceback.print_exc()
            self._write(500, {"error": "internal", "message": str(exc)})
            return
        self._write(code, payload)

    def _write(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        return


def make_server(host: str, port: int, store: Store) -> ThreadingHTTPServer:
    store.init_schema()
    Handler.api = build_api(store)

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return _Server((host, port), Handler)
