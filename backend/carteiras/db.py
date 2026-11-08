"""Camada fina sobre o sqlite3. Uma conexão por thread, transações aninhadas por savepoint."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .settings import TZ

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

Row = dict[str, Any]


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    return json.loads(text)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()

    # --- conexão -----------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = conn
            self._local.depth = 0
        return conn

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def init_schema(self) -> None:
        self._conn().executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    # --- consultas ---------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self._conn().execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self._conn().executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[Row]:
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Row | None:
        row = self._conn().execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = None) -> Any:
        row = self._conn().execute(sql, params).fetchone()
        if row is None or row[0] is None:
            return default
        return row[0]

    def insert(self, table: str, values: Row, *, or_ignore: bool = False, or_replace: bool = False) -> int:
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT OR REPLACE" if or_replace else "INSERT"
        cols = ", ".join(values)
        marks = ", ".join(f":{k}" for k in values)
        cur = self.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", values)
        return int(cur.lastrowid or 0)

    # --- transações --------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[None]:
        conn = self._conn()
        depth: int = self._local.depth
        name = f"sp_{depth}"
        conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {name}")
        self._local.depth = depth + 1
        try:
            yield
        except BaseException:
            self._local.depth = depth
            conn.execute("ROLLBACK" if depth == 0 else f"ROLLBACK TO {name}")
            if depth > 0:
                conn.execute(f"RELEASE {name}")
            raise
        else:
            self._local.depth = depth
            conn.execute("COMMIT" if depth == 0 else f"RELEASE {name}")

    # --- chave-valor ---------------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        return self.scalar("SELECT value FROM kv WHERE key = ?", (key,), default)

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now_iso()),
        )


def open_db(path: str | Path | None = None) -> Database:
    from . import settings

    db = Database(path if path is not None else settings.db_path())
    db.init_schema()
    return db
