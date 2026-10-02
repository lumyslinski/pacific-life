"""PostgreSQL access: one pool, one transaction per request.

Only this module imports psycopg. The rest of the package uses two things from
a connection: `execute(sql, params)` returning a cursor with `fetchone()` and
`fetchall()` that yield dict rows, and database errors that carry `sqlstate`
and `diag`. Every parameter in queries.py is a string, int, bool or None with
an explicit cast in the SQL, so nothing depends on driver-side type adaptation.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DatabaseError = psycopg.Error


class Cursor(Protocol):
    def fetchone(self) -> dict[str, Any] | None: ...
    def fetchall(self) -> list[dict[str, Any]]: ...


class Connection(Protocol):
    def execute(self, query: str, params: dict[str, Any] | None = None) -> Cursor: ...


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 10, statement_timeout_ms: int = 15_000):
        self._pool = ConnectionPool(
            conninfo=url, min_size=min_size, max_size=max_size, open=False,
            kwargs={"row_factory": dict_row, "options": f"-c statement_timeout={statement_timeout_ms}"},
        )

    def open(self) -> None:
        self._pool.open(wait=True, timeout=30)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        """Commit when the block ends normally, roll back when it raises."""
        with self._pool.connection() as connection:
            yield connection
