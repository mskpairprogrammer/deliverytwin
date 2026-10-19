"""Dialect-aware database access used by the portfolio data layer.

Every implementation uses positional ``?`` parameters, so callers never build SQL from
values. Identifiers are validated against the schema before interpolation.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Iterator, Sequence

from .schema import TABLES, check_identifier, render_create_table


class Database:
    """Minimal interface shared by the SQLite, DuckDB, and Databricks backends."""

    dialect = "sqlite"
    catalog: str | None = None
    schema: str | None = None

    def batch(self) -> Any:
        """Group statements into one transaction where the backend supports it."""
        return nullcontext()

    def table(self, name: str) -> str:
        if name not in TABLES:
            raise ValueError(f"Unknown table: {name!r}")
        parts = [part for part in (self.catalog, self.schema) if part]
        return ".".join([*map(check_identifier, parts), name])

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def create_tables(self) -> None:
        for table in TABLES.values():
            self.execute(render_create_table(table, self.dialect, self.table(table.name)))

    def coerce(self, column_type: str, value: Any) -> Any:
        if value is None or value == "":
            return None
        if column_type == "int":
            return int(float(value))
        if column_type == "double":
            return float(value)
        if column_type == "date" and self.dialect == "databricks":
            return date.fromisoformat(str(value))
        return str(value) if column_type in ("string", "date") else value

    def upsert(self, table_name: str, rows: Iterable[dict[str, Any]]) -> int:
        table = TABLES[table_name]
        columns = [column.name for column in table.columns]
        types = {column.name: column.type for column in table.columns}
        count = 0
        for row in rows:
            values = [self.coerce(types[name], row.get(name)) for name in columns]
            sql, params = self._upsert_statement(table_name, columns, list(table.key), values)
            self.execute(sql, params)
            count += 1
        return count

    def _upsert_statement(
        self, table_name: str, columns: list[str], keys: list[str], values: list[Any]
    ) -> tuple[str, list[Any]]:
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in keys)
        sql = (
            f"INSERT INTO {self.table(table_name)} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}"
        )
        return sql, values


class SQLiteDatabase(Database):
    dialect = "sqlite"

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._batch_depth = 0

    @contextmanager
    def batch(self) -> Iterator[None]:
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self.connection.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = self.connection.execute(sql, tuple(params))
        names = [description[0] for description in cursor.description or ()]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.connection.execute(sql, tuple(params))
        if self._batch_depth == 0:
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class DuckDBDatabase(Database):
    """In-process stand-in for tests and offline development; never used in production."""

    dialect = "duckdb"

    def __init__(self, path: str = ":memory:") -> None:
        import duckdb

        self.connection = duckdb.connect(path)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = self.connection.execute(sql, list(params))
        names = [description[0] for description in cursor.description or ()]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.connection.execute(sql, list(params))

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True)
class DatabricksSettings:
    server_hostname: str
    http_path: str
    catalog: str = "dpm_dev"
    schema: str = "mlops"
    access_token: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "DatabricksSettings":
        source = os.environ if env is None else env
        host = source.get("DATABRICKS_SERVER_HOSTNAME") or source.get("DATABRICKS_HOST", "")
        host = host.replace("https://", "").replace("http://", "").rstrip("/")
        http_path = source.get("DATABRICKS_HTTP_PATH", "")
        if not host or not http_path:
            raise RuntimeError(
                "Set DATABRICKS_SERVER_HOSTNAME (or DATABRICKS_HOST) and DATABRICKS_HTTP_PATH."
            )
        return cls(
            host, http_path,
            source.get("DELIVERYTWIN_CATALOG", "dpm_dev"),
            source.get("DELIVERYTWIN_SCHEMA", "mlops"),
            source.get("DATABRICKS_TOKEN") or None,
        )


class DatabricksDatabase(Database):
    """Databricks SQL warehouse adapter; only the connection needs a live workspace."""

    dialect = "databricks"

    def __init__(self, settings: DatabricksSettings, connection: Any = None) -> None:
        self.catalog = check_identifier(settings.catalog)
        self.schema = check_identifier(settings.schema)
        self.connection = connection or self._connect(settings)

    @staticmethod
    def _connect(settings: DatabricksSettings) -> Any:
        from databricks import sql as databricks_sql

        kwargs: dict[str, Any] = {
            "server_hostname": settings.server_hostname,
            "http_path": settings.http_path,
        }
        if settings.access_token:
            kwargs["access_token"] = settings.access_token
        else:
            from databricks.sdk.core import Config

            config = Config(host=f"https://{settings.server_hostname}")
            kwargs["credentials_provider"] = lambda: config.authenticate
        return databricks_sql.connect(**kwargs)

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        cursor = self.connection.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(sql, list(params))
            names = [description[0] for description in cursor.description or ()]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._cursor() as cursor:
            cursor.execute(sql, list(params))

    def close(self) -> None:
        self.connection.close()

    def _upsert_statement(
        self, table_name: str, columns: list[str], keys: list[str], values: list[Any]
    ) -> tuple[str, list[Any]]:
        source = ", ".join(f"? AS {c}" for c in columns)
        match = " AND ".join(f"t.{k} = s.{k}" for k in keys)
        updates = ", ".join(f"t.{c} = s.{c}" for c in columns if c not in keys)
        inserted = ", ".join(f"s.{c}" for c in columns)
        update_clause = f"WHEN MATCHED THEN UPDATE SET {updates} " if updates else ""
        sql = (
            f"MERGE INTO {self.table(table_name)} AS t USING (SELECT {source}) AS s ON {match} "
            f"{update_clause}WHEN NOT MATCHED THEN INSERT ({', '.join(columns)}) VALUES ({inserted})"
        )
        return sql, values


def open_database(backend: str, **options: Any) -> Database:
    """Open the configured backend: ``sqlite``, ``duckdb`` (tests), or ``databricks``."""
    if backend == "sqlite":
        return SQLiteDatabase(options["connection"])
    if backend == "duckdb":
        return DuckDBDatabase(options.get("path", ":memory:"))
    if backend == "databricks":
        return DatabricksDatabase(options.get("settings") or DatabricksSettings.from_env())
    raise ValueError(f"Unsupported backend: {backend!r}")
