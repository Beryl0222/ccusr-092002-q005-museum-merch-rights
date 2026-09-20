"""美术馆文创服务入口。"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .database import connect


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        database = connect()
        try:
            database.execute("SELECT 1").fetchone()
        finally:
            database.close()
        body = json.dumps(
            {"status": "ok", "service": "美术馆文创授权履约系统"},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def run() -> None:
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    run()
