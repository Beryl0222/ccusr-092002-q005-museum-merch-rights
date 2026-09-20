"""HTTP API 端到端测试。"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from museum_merch.database import connect, init_db
from museum_merch.main import Handler


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self.tmp.name, "api.db")
        db = connect()
        init_db(db)
        db.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        conn.request(method, path, payload,
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_health(self):
        status, data = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_full_flow_over_api(self):
        post = lambda p, b: self.call("POST", p, b)
        status, _ = post("/holders", {"code": "h1", "name": "艺术家", "kind": "artist"})
        self.assertEqual(status, 200)
        self.assertEqual(post("/exhibitions", {
            "code": "ex1", "title": "特展", "start_date": "2026-09-01",
            "end_date": "2026-11-30"})[0], 200)
        self.assertEqual(post("/artworks", {
            "code": "w1", "title": "星夜", "artist": "艺术家",
            "holder": "h1", "exhibition": "ex1"})[0], 200)
        self.assertEqual(post("/licenses", {
            "code": "l1", "artwork": "w1",
            "uses": ["product-reproduction", "exhibition-promotion"],
            "valid_from": "2026-08-01", "valid_until": "2026-12-31",
            "channels": ["museum-store", "livestream"], "regions": ["CN"],
            "royalty_rate": "0.12"})[0], 200)
        self.assertEqual(post("/licenses/l1/documents", {
            "doc_type": "contract", "doc_ref": "DOC-1"})[0], 200)
        self.assertEqual(post("/versions", {
            "code": "v1", "name": "丝巾", "artwork": "w1"})[0], 200)
        self.assertEqual(post("/versions/v1/review", {
            "decision": "approved", "reviewer": "张审"})[0], 200)
        status, gate = self.call("GET", "/versions/v1/gate")
        self.assertTrue(gate["ok"])
        self.assertEqual(post("/suppliers", {"code": "s1", "name": "厂"})[0], 200)
        self.assertEqual(post("/purchase-orders", {
            "code": "po1", "supplier": "s1", "version": "v1", "qty": 100,
            "unit_cost": "30", "expected_at": "2026-09-20"})[0], 200)
        self.assertEqual(post("/purchase-orders/po1/lock", {})[0], 200)
        self.assertEqual(post("/batches", {
            "code": "b1", "purchase_order": "po1", "qty_planned": 100})[0], 200)
        self.assertEqual(post("/batches/b1/qc", {
            "passed_qty": 100, "failed_qty": 0, "inspector": "李检"})[0], 200)
        self.assertEqual(post("/batches/b1/receive", {})[0], 200)
        self.assertEqual(post("/reservations", {
            "version": "v1", "channel": "livestream",
            "reserved_qty": 60})[0], 200)
        self.assertEqual(post("/listings", {
            "version": "v1", "channel": "livestream", "region": "CN"})[0], 200)
        self.assertEqual(post("/orders", {
            "code": "o1", "channel": "livestream", "region": "CN",
            "items": [{"version": "v1", "qty": 60, "unit_price": "100"}]})[0], 200)
        # 超卖返回 422 且带可解释原因
        status, err = post("/orders", {
            "code": "o2", "channel": "livestream", "region": "CN",
            "items": [{"version": "v1", "qty": 1, "unit_price": "100"}]})
        self.assertEqual(status, 422)
        self.assertTrue(err["reasons"])
        # 看板与追溯可访问
        self.assertEqual(self.call("GET", "/dashboard?exhibition=ex1")[0], 200)
        status, trace = self.call("GET", "/versions/v1/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["sales"][0]["order"], "o1")

    def test_promotion_only_license_blocks_po(self):
        post = lambda p, b: self.call("POST", p, b)
        post("/holders", {"code": "h1", "name": "艺术家", "kind": "artist"})
        post("/exhibitions", {"code": "ex1", "title": "展",
                              "start_date": "2026-09-01", "end_date": "2026-11-30"})
        post("/artworks", {"code": "w1", "title": "作品", "artist": "艺术家",
                           "holder": "h1", "exhibition": "ex1"})
        post("/licenses", {"code": "l1", "artwork": "w1",
                           "uses": ["exhibition-promotion"],
                           "valid_from": "2026-08-01", "valid_until": "2026-12-31"})
        post("/licenses/l1/documents", {"doc_type": "email", "doc_ref": "D1"})
        post("/versions", {"code": "v1", "name": "商品", "artwork": "w1"})
        post("/versions/v1/review", {"decision": "approved", "reviewer": "张审"})
        post("/suppliers", {"code": "s1", "name": "厂"})
        status, err = post("/purchase-orders", {
            "code": "po1", "supplier": "s1", "version": "v1", "qty": 10,
            "unit_cost": "1", "expected_at": "2026-10-01"})
        self.assertEqual(status, 422)
        self.assertTrue(any("product-reproduction" in r for r in err["reasons"]))


if __name__ == "__main__":
    unittest.main()
