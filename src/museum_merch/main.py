"""美术馆文创授权履约系统 HTTP 入口（标准库实现，无外部依赖）。"""

import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import catalog, incidents, production, reporting, rights, sales
from .database import connect, init_db


class DomainError(Exception):
    pass


# 路由表：(method, 正则) → 处理函数(db, match, body, query)
def _ok(value):
    return 200, value


ROUTES: list[tuple[str, re.Pattern, callable]] = []


def route(method: str, pattern: str):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def register(fn):
        ROUTES.append((method, regex, fn))
        return fn

    return register


# ---------- 档案 ----------

@route("POST", r"/holders")
def h_holder(db, m, body, q):
    return _ok({"id": catalog.create_holder(
        db, body["code"], body["name"], body["kind"], body.get("contact"))})


@route("POST", r"/exhibitions")
def h_exhibition(db, m, body, q):
    return _ok({"id": catalog.create_exhibition(
        db, body["code"], body["title"], body["start_date"], body["end_date"])})


@route("POST", r"/artworks")
def h_artwork(db, m, body, q):
    return _ok({"id": catalog.create_artwork(
        db, body["code"], body["title"], body["artist"], body["holder"],
        body.get("exhibition"))})


# ---------- 授权与证据 ----------

@route("POST", r"/licenses")
def h_license(db, m, body, q):
    return _ok({"id": catalog.record_license(
        db, body["code"], body["artwork"], body["uses"], body["valid_from"],
        body["valid_until"], body.get("channels"), body.get("regions"),
        body.get("sublicensable", False), body.get("royalty_rate"),
        body.get("royalty_unit_fee"), body.get("signed_at"), body.get("notes"))})


@route("POST", r"/licenses/{code}/documents")
def h_license_doc(db, m, body, q):
    return _ok({"id": catalog.add_license_document(
        db, m["code"], body["doc_type"], body["doc_ref"], body.get("checksum"),
        body.get("issued_by"), body.get("received_at"))})


@route("POST", r"/licenses/{code}/revoke")
def h_revoke(db, m, body, q):
    catalog.revoke_license(db, m["code"], body.get("revoked_at"), body.get("doc_ref"))
    result = incidents.license_revoked(db, m["code"], body.get("auto_refund", False))
    return _ok(result)


@route("POST", r"/licenses/{code}/renew")
def h_renew(db, m, body, q):
    catalog.renew_license(db, m["code"], body["valid_until"], body["doc_ref"],
                          body.get("received_at"))
    return _ok({"renewed": m["code"], "valid_until": body["valid_until"]})


# ---------- 版本与审核 ----------

@route("POST", r"/versions")
def h_version(db, m, body, q):
    return _ok({"id": catalog.create_version(
        db, body["code"], body["name"], body["artwork"],
        body.get("is_derivative", False))})


@route("POST", r"/versions/{code}/review")
def h_review(db, m, body, q):
    code = m["code"]
    catalog.submit_for_review(db, code)
    catalog.decide_review(db, code, body["decision"], body["reviewer"],
                          body.get("notes"), body.get("doc_ref"))
    return _ok({"version": code, "decision": body["decision"]})


@route("GET", r"/versions/{code}/gate")
def h_gate(db, m, body, q):
    return _ok(catalog.gate(db, m["code"], q.get("channel", [None])[0],
                            q.get("region", [None])[0], q.get("date", [None])[0]))


@route("GET", r"/artworks/{code}/promotion-check")
def h_promo(db, m, body, q):
    return _ok(catalog.check_promotion(
        db, m["code"], q.get("channel", [None])[0], q.get("region", [None])[0],
        q.get("date", [None])[0]))


# ---------- 供应、生产、质检 ----------

@route("POST", r"/suppliers")
def h_supplier(db, m, body, q):
    return _ok({"id": production.create_supplier(
        db, body["code"], body["name"], body.get("lead_time_days", 30))})


@route("POST", r"/purchase-orders")
def h_po(db, m, body, q):
    targets = [tuple(t) for t in body.get("targets", [])] or None
    return _ok({"id": production.create_po(
        db, body["code"], body["supplier"], body["version"], int(body["qty"]),
        str(body["unit_cost"]), body["expected_at"], targets, body.get("date"))})


@route("POST", r"/purchase-orders/{code}/lock")
def h_po_lock(db, m, body, q):
    targets = [tuple(t) for t in body.get("targets", [])] or None
    production.lock_po(db, m["code"], targets, body.get("date"))
    return _ok({"locked": m["code"], "committed": True})


@route("POST", r"/batches")
def h_batch(db, m, body, q):
    return _ok({"id": production.start_batch(
        db, body["code"], body["purchase_order"], int(body["qty_planned"]),
        body.get("note"))})


@route("POST", r"/batches/{code}/qc")
def h_qc(db, m, body, q):
    # 不合格自动登记质检并生成隔离/补货/退款替代事件
    result = incidents.qc_failure(
        db, m["code"], int(body["passed_qty"]), int(body["failed_qty"]),
        body["inspector"], body.get("note"))
    return _ok(result)


@route("POST", r"/batches/{code}/receive")
def h_receive(db, m, body, q):
    production.receive_batch(db, m["code"],
                             int(body["qty"]) if body.get("qty") is not None else None)
    return _ok({"received": m["code"]})


@route("POST", r"/reservations")
def h_reservation(db, m, body, q):
    production.set_channel_reservation(
        db, body["version"], body["channel"], int(body["reserved_qty"]))
    return _ok({"version": body["version"], "channel": body["channel"],
                "reserved_qty": body["reserved_qty"]})


# ---------- 上架、订单、库存、版税 ----------

@route("POST", r"/listings")
def h_listing(db, m, body, q):
    return _ok({"id": sales.list_product(
        db, body["version"], body["channel"], body["region"], body.get("date"))})


@route("POST", r"/orders")
def h_order(db, m, body, q):
    return _ok({"id": sales.create_order(
        db, body["code"], body["channel"], body["region"], body["items"],
        body.get("customer_ref"), body.get("date"))})


@route("POST", r"/orders/{code}/ship")
def h_ship(db, m, body, q):
    return _ok({"shipments": sales.ship_order(
        db, m["code"], body.get("item_id"), body.get("qty"))})


@route("POST", r"/orders/{code}/cancel")
def h_cancel(db, m, body, q):
    return _ok(sales.cancel_order(
        db, m["code"], body.get("lines"), body.get("reason", "customer_cancel")))


@route("POST", r"/items/{id}/refund")
def h_refund(db, m, body, q):
    return _ok(sales.refund_item(
        int(m["id"]), body.get("qty"), body.get("reason", "manual_refund")))


@route("POST", r"/items/{id}/substitute")
def h_substitute(db, m, body, q):
    return _ok(sales.substitute_item(
        int(m["id"]), body["substitute_version"], body.get("qty")))


@route("GET", r"/inventory/{code}")
def h_inventory(db, m, body, q):
    return _ok(sales.inventory_view(db, m["code"]))


@route("POST", r"/royalty/settle")
def h_royalty(db, m, body, q):
    return _ok(sales.settle_royalty(
        db, body["license"], body["period_from"], body["period_to"]))


# ---------- 事件与处置 ----------

@route("POST", r"/incidents/exhibition-rescheduled")
def h_inc_reschedule(db, m, body, q):
    return _ok(incidents.exhibition_rescheduled(
        db, body["exhibition"], body["start_date"], body["end_date"]))


@route("POST", r"/incidents/supplier-delay")
def h_inc_delay(db, m, body, q):
    return _ok(incidents.supplier_delay(
        db, body["purchase_order"], body["new_expected_at"], body.get("reason")))


@route("POST", r"/incidents/license-expiry-sweep")
def h_expiry_sweep(db, m, body, q):
    return _ok(incidents.license_expiry_sweep(db, body.get("date")))


@route("GET", r"/incident-actions/pending")
def h_pending(db, m, body, q):
    return _ok({"actions": incidents.list_pending_actions(db)})


@route("POST", r"/incident-actions/{id}/resolve")
def h_resolve(db, m, body, q):
    return _ok(incidents.resolve_action(
        int(m["id"]), body["decision"], body.get("substitute_version"),
        body.get("valid_until"), body.get("doc_ref")))


# ---------- 看板与追溯 ----------

@route("GET", r"/dashboard")
def h_dashboard(db, m, body, q):
    return _ok(reporting.risk_dashboard(db, q.get("exhibition", [None])[0]))


@route("GET", r"/versions/{code}/trace")
def h_trace(db, m, body, q):
    return _ok(reporting.trace_version(db, m["code"]))


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"status": "ok", "service": "美术馆文创授权履约系统"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "请求体不是合法 JSON"})
            return
        query = parse_qs(parsed.query)
        db = connect()
        try:
            for verb, regex, fn in ROUTES:
                if verb != method:
                    continue
                match = regex.match(parsed.path)
                if match:
                    status, result = fn(db, match.groupdict(), body, query)
                    self._send(status, result)
                    return
            self._send(404, {"error": f"未找到路由 {method} {parsed.path}"})
        except catalog.DomainError as exc:
            db.rollback()
            self._send(422, {"error": str(exc), "reasons": exc.reasons})
        except KeyError as exc:
            db.rollback()
            self._send(400, {"error": f"请求缺少必填字段：{exc.args[0]}"})
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            traceback.print_exc()
            self._send(500, {"error": f"服务器内部错误：{exc}"})
        finally:
            db.close()

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, format: str, *args: object) -> None:
        return


def run() -> None:
    db = connect()
    init_db(db)
    db.close()
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    run()
