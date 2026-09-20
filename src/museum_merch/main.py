"""美术馆文创服务入口。

启动前可用 museum_merch.seed.load_demo 写入演示数据（DATABASE_SEED=1）。
"""

import os

from . import seed
from .api import make_server
from .database import connect
from .store import Store


def build_store() -> Store:
    conn = connect()
    store = Store(conn)
    store.init_schema()
    if os.getenv("DATABASE_SEED") == "1":
        seed.load_demo(store)
    return store


def run() -> None:
    store = build_store()
    server = make_server(
        "0.0.0.0", int(os.getenv("PORT", "8080")), store)
    server.serve_forever()


if __name__ == "__main__":
    run()
