"""SQLite 数据连接。"""

import os
import sqlite3


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(
        os.getenv("DATABASE_PATH", "museum-merch.db"), check_same_thread=False
    )
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
