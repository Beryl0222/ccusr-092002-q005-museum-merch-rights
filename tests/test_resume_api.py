"""授权恢复与 HTTP API 端到端测试。"""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from museum_merch import catalog, cases, fulfillment, rights
from museum_merch.api import build_api
from museum_merch.http_test import TestServer
from support import ON, build_world, restock


class ResumeTest(unittest.TestCase):
    def test_renew_then_resume_relists_and_ship_again(self):
        s = build_world()
        restock(s, qty=100)
        catalog.list_version(s, "v1", "museum-store", "CN", ON)
        rights.withdraw_license(s, "lic1", "撤回")
        cases.process_events(s, ON)
        # 撤回期间下单被拒
        from museum_merch.store import DomainError
        with self.assertRaises(DomainError):
            fulfillment.place_order(
                s, order_no="X", channel="museum-store", region="CN",
                items=[{"version_id": "v1", "qty": 1}], on_date=ON)
        # 续展到 2027 年中
        rights.renew_license(
            s, code="L1-2027", old_license_id="lic1",
            valid_from=ON, valid_until="2027-06-30")
        relisted = catalog.resume_version(
            s, "v1", ON, [("museum-store", "CN")])
        self.assertTrue(relisted)
        # 恢复后可正常下单发货
        oid = fulfillment.place_order(
            s, order_no="NEW", channel="museum-store", region="CN",
            items=[{"version_id": "v1", "qty": 2}], on_date=ON)
        item = s.one("SELECT id FROM order_item WHERE order_id=?", (oid,))["id"]
        fulfillment.ship_item(s, item_id=item, qty=2, on_date=ON)
        self.assertEqual(
            s.one("SELECT qty_shipped FROM order_item WHERE id=?", (item,))["qty_shipped"],
            2)

    def test_resume_blocked_while_market_still_uncovered(self):
        s = build_world()
        restock(s, qty=100)
        catalog.list_version(s, "v1", "pop-up", "CN", ON)
        cases.sweep_expiries(s, "2026-11-01")
        # 快闪授权到期、无续展，恢复必须失败
        with self.assertRaises(Exception):
            catalog.resume_version(s, "v1", "2026-11-02", [("pop-up", "CN")])


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = build_world()
        restock(s, qty=100)
        for ch in ("museum-store", "official-online-store", "livestream", "pop-up"):
            catalog.list_version(s, "v1", ch, "CN", ON)
        cls.server, cls.base, cls.store = TestServer.start(s)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_health(self):
        code, body = self.call("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")

    def test_gate_and_procurement_flow(self):
        # vbad 采购闸门失败 → 422
        code, body = self.call("POST", "/api/pos", {
            "po_no": "PO-BAD", "version_id": "vbad", "supplier_id": "sup1",
            "qty_ordered": 1, "unit_cost_cents": 1, "on": ON})
        self.assertEqual(code, 422)
        self.assertEqual(body["error"], "gate-failed")
        # 超卖下单 → 409
        code, body = self.call("POST", "/api/orders", {
            "order_no": "API-BIG", "channel": "livestream", "region": "CN",
            "items": [{"version_id": "v1", "qty": 999}], "on": ON})
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "oversell")

    def test_withdraw_and_trace_api(self):
        code, body = self.call("POST", "/api/orders", {
            "order_no": "API-M1", "channel": "museum-store", "region": "CN",
            "items": [{"version_id": "v1", "qty": 3}], "on": ON})
        self.assertEqual(code, 201)
        code, body = self.call(
            "POST", "/api/licenses/lic1/withdraw", {"reason": "API 撤回", "on": ON})
        self.assertEqual(code, 200)
        self.assertTrue(body["case_ids"])
        code, trace = self.call("GET", "/api/versions/v1/trace")
        self.assertEqual(code, 200)
        self.assertEqual(trace["product_version"]["sku"], "SKU1")
        self.assertTrue(any(l["code"] == "L1" and l["status"] == "withdrawn"
                            for l in trace["licenses"]))
        self.assertTrue(trace["gate_reviews"])
        self.assertTrue(trace["cases"])
        # 未发货 3 件已被工单退款
        refunds = [r for o in trace["orders"] for r in o["refunds"]
                   if o["order_no"] == "API-M1"]
        self.assertEqual(sum(r["qty"] for r in refunds), 3)


if __name__ == "__main__":
    unittest.main()
