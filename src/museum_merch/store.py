"""数据访问与事务支持。"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class DomainError(Exception):
    """业务规则冲突，code 供调用方程序化处理。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def open_store(path: str = ":memory:") -> "Store":
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return Store(conn)


class Store:
    """所有访问经同一把可重入锁串行化：SQLite 同一时刻只有一个写者，
    且单个连接不能被多线程交错使用；锁内事务可重入调用读方法。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()

    def init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # IMMEDIATE：下单/发货等写事务立即取库级写锁，配合外层锁杜绝并发超卖
        self._lock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        finally:
            self._lock.release()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def must_one(self, sql: str, params: tuple = (), label: str = "记录") -> sqlite3.Row:
        row = self.one(sql, params)
        if row is None:
            raise DomainError("not-found", f"{label}不存在", {"sql": sql, "params": list(params)})
        return row

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def insert(self, table: str, **fields: Any) -> str:
        row_id = fields.get("id") or new_id(table.replace("_", "-"))
        fields = {"id": row_id, **fields}
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        with self._lock, self.transaction():
            self.conn.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
        return row_id
