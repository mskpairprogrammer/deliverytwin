"""Dialect-aware database access used by the portfolio data layer.

Every implementation uses positional ``?`` parameters, so callers never build SQL from
values. Identifiers are validated against the schema before interpolation.

DuckDB is the default local runtime (a persistent, thread-safe analytical engine that mirrors the
Databricks SQL surface); SQLite remains available, and Databricks hosts the portable app tables.
"""
from __future__ import annotations

import atexit
import os
import re
import sqlite3
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .schema import ALL_TABLES, TABLES, check_identifier, render_create_table


def _error_types(name: str) -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [getattr(sqlite3, "Error" if name == "Error" else "IntegrityError")]
    try:
        import duckdb

        types.append(duckdb.Error if name == "Error" else duckdb.ConstraintException)
    except ImportError:  # SQLite-only installs
        pass
    return tuple(types)


# Catch these instead of backend-specific exceptions so no caller assumes one engine.
DatabaseError = _error_types("Error")
IntegrityError = _error_types("IntegrityError")


@dataclass
class Result:
    names: list[str]
    rows: list[tuple[Any, ...]]


class Database:
    """Minimal interface shared by the SQLite, DuckDB, and Databricks backends."""

    dialect = "sqlite"
    catalog: str | None = None
    schema: str | None = None

    def batch(self) -> Any:
        """Group statements into one transaction where the backend supports it."""
        return nullcontext()

    def table(self, name: str) -> str:
        if name not in ALL_TABLES:
            raise ValueError(f"Unknown table: {name!r}")
        parts = [part for part in (self.catalog, self.schema) if part]
        return ".".join([*map(check_identifier, parts), name])

    def run(self, sql: str, params: Sequence[Any] = ()) -> Result:
        raise NotImplementedError

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        result = self.run(sql, params)
        return [dict(zip(result.names, row)) for row in result.rows]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.run(sql, params)

    def begin(self) -> None:
        """Open an explicit transaction (no-op where the backend has none)."""

    def commit(self) -> None:
        """Commit the explicit transaction."""

    def rollback(self) -> None:
        """Roll back the explicit transaction."""

    def close(self) -> None:
        raise NotImplementedError

    def clear_table(self, name: str) -> None:
        self.execute(f"DELETE FROM {self.table(name)}")

    def create_tables(self, include_app: bool = True, names: tuple[str, ...] | None = None) -> None:
        if names is not None:
            tables = [ALL_TABLES[name] for name in names]
        else:
            tables = list((ALL_TABLES if include_app else TABLES).values())
        for table in tables:
            self.execute(render_create_table(table, self.dialect, self.table(table.name)))

    def coerce(self, column_type: str, value: Any) -> Any:
        if value is None or value == "":
            return None
        if column_type == "int":
            return int(float(value))
        if column_type == "double":
            return float(value)
        if self.dialect == "databricks" and column_type == "date":
            return date.fromisoformat(str(value))
        if self.dialect == "databricks" and column_type == "timestamp":
            return datetime.fromisoformat(str(value))
        return str(value) if column_type in ("string", "date", "timestamp") else value

    def upsert(self, table_name: str, rows: Iterable[dict[str, Any]]) -> int:
        table = ALL_TABLES[table_name]
        columns = [column.name for column in table.columns]
        types = {column.name: column.type for column in table.columns}
        keys = list(table.key)
        prepared = [[self.coerce(types[name], row.get(name)) for name in columns] for row in rows]
        if self.upsert_chunk <= 1:
            for values in prepared:
                sql, params = self._upsert_statement(table_name, columns, keys, values)
                self.execute(sql, params)
            return len(prepared)
        positions = [columns.index(key) for key in keys]
        for start in range(0, len(prepared), self.upsert_chunk):
            # Within one statement the last row for a key wins, as it would if the rows ran one by one.
            chunk = list({tuple(values[i] for i in positions): values
                          for values in prepared[start:start + self.upsert_chunk]}.values())
            sql, params = self._upsert_rows_statement(table_name, columns, keys, chunk)
            self.execute(sql, params)
        return len(prepared)

    # Rows per upsert statement: one multi-row statement is far cheaper than many single-row ones on DuckDB.
    upsert_chunk = 100

    def _upsert_rows_statement(
        self, table_name: str, columns: list[str], keys: list[str], rows: list[list[Any]]
    ) -> tuple[str, list[Any]]:
        group = "(" + ", ".join("?" for _ in columns) + ")"
        updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in keys)
        action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
        sql = (
            f"INSERT INTO {self.table(table_name)} ({', '.join(columns)}) VALUES {', '.join([group] * len(rows))} "
            f"ON CONFLICT ({', '.join(keys)}) {action}"
        )
        return sql, [value for values in rows for value in values]

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

    # Introspection and scripts are only needed for the local engines that hold the source-system tables.
    def list_tables(self) -> list[str]:
        raise NotImplementedError

    def indexed_columns(self, table: str) -> set[str]:
        """Foreign-key and unique columns of a table (empty where the engine does not need to know)."""
        return set()

    def table_columns(self, name: str) -> list[str]:
        raise NotImplementedError

    def run_script(self, script: str) -> None:
        for statement in translate_schema(script, self.dialect):
            self.execute(statement)


_AUTOINCREMENT = re.compile(r"INTEGER PRIMARY KEY AUTOINCREMENT", re.I)
_CREATE_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)", re.I)
_REAL = re.compile(r"\bREAL\b", re.I)


def translate_schema(script: str, dialect: str) -> list[str]:
    """Split the SQLite-flavoured schema into statements and adapt them to the target engine.

    DuckDB needs sequences instead of AUTOINCREMENT, 8-byte floats instead of REAL, and no PRAGMA.
    Foreign keys, CHECK and UNIQUE constraints are kept.
    """
    statements: list[str] = []
    for raw in script.split(";"):
        statement = raw.strip()
        if not statement:
            continue
        if dialect == "duckdb":
            if statement.upper().startswith("PRAGMA"):
                continue
            created = _CREATE_TABLE.search(statement)
            if created and _AUTOINCREMENT.search(statement):
                name = created.group(1)
                statements.append(f"CREATE SEQUENCE IF NOT EXISTS {name}_seq START 1")
                statement = _AUTOINCREMENT.sub(f"BIGINT PRIMARY KEY DEFAULT nextval('{name}_seq')", statement)
            statement = _REAL.sub("DOUBLE", statement)
        statements.append(statement)
    return statements


class _LocalEngine(Database):
    """Shared transaction handling for the in-process engines (SQLite, DuckDB).

    A re-entrant lock serialises statements across threads. Transactions nest: only the outermost
    ``begin``/``commit`` issues BEGIN/COMMIT, and a rollback anywhere makes the whole unit roll back.
    """

    lock: threading.RLock
    connection: Any

    def _init_transactions(self) -> None:
        self._depth = 0
        self._failed = False

    def _execute_control(self, statement: str) -> None:
        self.connection.execute(statement)

    @contextmanager
    def batch(self) -> Iterator[None]:
        self.begin()
        try:
            yield
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

    def begin(self) -> None:
        self.lock.acquire()
        try:
            if self._depth == 0:
                self._execute_control("BEGIN")
            self._depth += 1
        except BaseException:
            self.lock.release()
            raise

    def commit(self) -> None:
        try:
            self._depth -= 1
            if self._depth == 0:
                failed, self._failed = self._failed, False
                self._execute_control("ROLLBACK" if failed else "COMMIT")
        finally:
            self.lock.release()

    def rollback(self) -> None:
        try:
            self._failed = True
            self._depth -= 1
            if self._depth == 0:
                self._failed = False
                self._execute_control("ROLLBACK")
        finally:
            self.lock.release()

    def run(self, sql: str, params: Sequence[Any] = ()) -> Result:
        with self.lock:
            cursor = self.connection.execute(sql, self._params(params))
            names = [description[0] for description in cursor.description or ()]
            return Result(names, [tuple(row) for row in cursor.fetchall()] if names else [])

    def _params(self, params: Sequence[Any]) -> Any:
        return list(params)

    def _deletion_order(self, tables: list[str]) -> list[str]:
        return tables

    def clear_all(self) -> int:
        """Delete every row of every table, keeping the schema; returns the number of rows removed.

        Each table is cleared in its own statement, children first: DuckDB cannot delete a child and then
        its parent in one transaction while foreign keys are enforced.
        """
        deleted = 0
        for name in self._deletion_order(self.list_tables()):
            quoted = '"' + check_identifier(name).replace('"', '""') + '"'
            deleted += self.run(f"SELECT COUNT(*) FROM {quoted}").rows[0][0]
            self.execute(f"DELETE FROM {quoted}")
        return deleted


class SQLiteDatabase(_LocalEngine):
    """SQLite in autocommit mode with explicit BEGIN/COMMIT, so transactions are always visible."""

    dialect = "sqlite"

    def __init__(self, connection: sqlite3.Connection | str = ":memory:") -> None:
        if isinstance(connection, str):
            connection = sqlite3.connect(connection, check_same_thread=False)
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys = ON")
        self.connection = connection
        self.lock = threading.RLock()
        self._init_transactions()

    def _params(self, params: Sequence[Any]) -> Any:
        return tuple(params)

    def clear_all(self) -> int:
        # Children may be deleted before their parents, so enforcement is paused for the wipe.
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            deleted = super().clear_all()
            if "sqlite_sequence" in {row["name"] for row in self.query("SELECT name FROM sqlite_master")}:
                self.execute("DELETE FROM sqlite_sequence")
            return deleted
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def list_tables(self) -> list[str]:
        return [
            row["name"] for row in self.query(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]

    def table_columns(self, name: str) -> list[str]:
        return [row["name"] for row in self.query(f"PRAGMA table_info({check_identifier(name)})")]


_DUCKDB_ROOTS: dict[str, tuple[Any, threading.RLock]] = {}
_DUCKDB_REGISTRY_LOCK = threading.Lock()


def _close_duckdb_roots() -> None:
    for root, _lock in list(_DUCKDB_ROOTS.values()):
        try:
            root.close()
        except Exception:  # best effort at interpreter exit
            pass
    _DUCKDB_ROOTS.clear()


atexit.register(_close_duckdb_roots)


class DuckDBDatabase(_LocalEngine):
    """Persistent, thread-safe DuckDB runtime.

    File databases are opened once per process and shared: each ``DuckDBDatabase`` gets its own cursor, and a
    per-file re-entrant lock serialises statements and transactions so concurrent HTTP requests never collide.
    ``:memory:`` gives every instance its own private database.
    """

    dialect = "duckdb"

    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        import duckdb

        target = str(path)
        self._owns_root = target == ":memory:"
        if self._owns_root:
            self._root, self.lock = duckdb.connect(":memory:"), threading.RLock()
        else:
            key = str(Path(target).resolve())
            with _DUCKDB_REGISTRY_LOCK:
                if key not in _DUCKDB_ROOTS:
                    Path(key).parent.mkdir(parents=True, exist_ok=True)
                    _DUCKDB_ROOTS[key] = (duckdb.connect(key), threading.RLock())
                self._root, self.lock = _DUCKDB_ROOTS[key]
        self.connection = self._root.cursor()
        self.path = target
        self._indexed: dict[str, set[str]] = {}
        self._init_transactions()

    def run_script(self, script: str) -> None:
        super().run_script(script)
        self._indexed.clear()

    def _execute_control(self, statement: str) -> None:
        self.connection.execute("BEGIN TRANSACTION" if statement == "BEGIN" else statement)

    def indexed_columns(self, table: str) -> set[str]:
        cached = self._indexed.get(table)
        if cached is None:
            cached = set()
            for row in self.query(
                "SELECT constraint_column_names AS names FROM duckdb_constraints() "
                "WHERE table_name = ? AND constraint_type IN ('FOREIGN KEY', 'UNIQUE')", [check_identifier(table)],
            ):
                cached.update(row["names"])
            self._indexed[table] = cached
        return cached

    def _deletion_order(self, tables: list[str]) -> list[str]:
        children: dict[str, set[str]] = {}
        for row in self.query(
            "SELECT table_name, referenced_table FROM duckdb_constraints() WHERE constraint_type = 'FOREIGN KEY'"
        ):
            children.setdefault(row["referenced_table"], set()).add(row["table_name"])
        remaining, ordered = set(tables), []
        while remaining:
            leaves = sorted(t for t in remaining if not ((children.get(t, set()) - {t}) & remaining))
            leaves = leaves or sorted(remaining)
            ordered.extend(leaves)
            remaining -= set(leaves)
        return ordered

    def close(self) -> None:
        self.connection.close()
        if self._owns_root:
            self._root.close()

    def list_tables(self) -> list[str]:
        return [
            row["table_name"] for row in self.query(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' ORDER BY table_name"
            )
        ]

    def table_columns(self, name: str) -> list[str]:
        return [
            row["column_name"] for row in self.query(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' "
                "AND table_name = ? ORDER BY ordinal_position", [check_identifier(name)],
            )
        ]


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
    upsert_chunk = 1  # MERGE INTO is issued one row at a time

    def __init__(self, settings: DatabricksSettings, connection: Any = None) -> None:
        self.catalog = check_identifier(settings.catalog)
        self.schema = check_identifier(settings.schema)
        self.connection = connection or self._connect(settings)
        self.lock = threading.RLock()

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

    def run(self, sql: str, params: Sequence[Any] = ()) -> Result:
        with self.lock, self._cursor() as cursor:
            cursor.execute(sql, list(params))
            names = [description[0] for description in cursor.description or ()]
            return Result(names, [tuple(row) for row in cursor.fetchall()] if names else [])

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
    """Open the configured backend: ``duckdb`` (default local runtime), ``sqlite``, or ``databricks``."""
    if backend == "sqlite":
        return SQLiteDatabase(options.get("connection") or options.get("path", ":memory:"))
    if backend == "duckdb":
        return DuckDBDatabase(options.get("path", ":memory:"))
    if backend == "databricks":
        return DatabricksDatabase(options.get("settings") or DatabricksSettings.from_env())
    raise ValueError(f"Unsupported backend: {backend!r}")
