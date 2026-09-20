"""测试用内嵌 HTTP 服务。"""

from __future__ import annotations

import socket
from http.server import ThreadingHTTPServer

from .api import build_api
from .store import Store


class TestServer:
    def __init__(self, server: ThreadingHTTPServer, base: str, store: Store) -> None:
        self.server = server
        self.base = base
        self.store = store

    @classmethod
    def start(cls, store: Store) -> tuple[ThreadingHTTPServer, str, Store]:
        api = build_api(store)

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        from .api import Handler
        Handler.api = api
        server = _Server(("127.0.0.1", _free_port()), Handler)
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        return server, base, store


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
