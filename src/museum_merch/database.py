"""SQLite 数据连接。"""

import os
import sqlite3


def connect(path: str | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path or os.getenv("DATABASE_PATH", "museum-merch.db"),
        isolation_level=None,  # 事务由 Store 显式管理（BEGIN IMMEDIATE）
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
