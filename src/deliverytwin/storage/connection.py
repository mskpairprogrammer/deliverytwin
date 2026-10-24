"""A small, engine-neutral connection used for the app's relational tables.

The simulated source-system tables (Planview, Azure DevOps, ServiceNow, UKG, Oracle HCM) and the
historical reference projects use the configured database. Databricks stores all tables in Unity Catalog;
DuckDB remains the local default. Nothing outside ``data/`` imports ``duckdb``: callers use this class,
``DatabaseError`` and ``IntegrityError``.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Iterator, Sequence

from .database import (
    DatabaseError,
    Database,
    DatabricksDatabase,
    DatabricksSettings,
    DuckDBDatabase,
    IntegrityError,
    Result,
)
from .schema import check_identifier

DIALECTS = ("duckdb", "databricks")
DEFAULT_PATHS = {"duckdb": "deliverytwin.duckdb", "databricks": "/tmp/deliverytwin.duckdb"}

__all__ = [
    "Connection", "Cursor", "DatabaseError", "IntegrityError", "Row", "connect", "default_database_path",
    "resolve_dialect", "DIALECTS",
]


def resolve_dialect(explicit: str | None = None, env: dict[str, str] | None = None) -> str:
    """``--dialect`` wins, then the Databricks switch, then ``DELIVERYTWIN_DB_DIALECT``, then Databricks when its
    host is set, else DuckDB.

    With ``DELIVERYTWIN_DATABRICKS_ENABLED=0`` the data stays on DuckDB whatever else is set; with ``=1`` it moves
    to Databricks once the warehouse settings (host and ``DATABRICKS_HTTP_PATH``) are filled in.
    """
    from ..integrations import databricks_switch

    source = os.environ if env is None else env
    host = source.get("DATABRICKS_HOST") or source.get("DATABRICKS_SERVER_HOSTNAME")
    enabled = databricks_switch(source)
    chosen = (explicit or "").strip().lower()
    if not chosen and enabled is not None:
        chosen = "databricks" if enabled and host and source.get("DATABRICKS_HTTP_PATH") else "duckdb"
    if not chosen:
        chosen = source.get("DELIVERYTWIN_DB_DIALECT", "").strip().lower()
    if not chosen:
        chosen = "databricks" if host else "duckdb"
    if chosen not in DIALECTS:
        raise ValueError(f"Unsupported database dialect {chosen!r}. Choose one of: {', '.join(DIALECTS)}.")
    return chosen


def default_database_path(dialect: str, env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return source.get("DELIVERYTWIN_DB_PATH") or DEFAULT_PATHS[dialect]


class Row:
    """Mapping-and-sequence row: ``row["name"]``, ``row[0]``, ``dict(row)`` and iteration over values."""

    __slots__ = ("_names", "_values")

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        self._names = tuple(names)
        self._values = tuple(values)

    def keys(self) -> list[str]:
        return list(self._names)

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._names.index(key)]
            except ValueError:
                raise KeyError(key) from None
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._names, self._values))!r})"


class Cursor:
    def __init__(self, result: Result) -> None:
        self._rows = [Row(result.names, values) for values in result.rows]
        self._position = 0

    def fetchone(self) -> Row | None:
        if self._position >= len(self._rows):
            return None
        self._position += 1
        return self._rows[self._position - 1]

    def fetchall(self) -> list[Row]:
        rows, self._position = self._rows[self._position:], len(self._rows)
        return rows

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())


_MULTI_INSERT = re.compile(r"^\s*(INSERT\s+INTO\s+\w+\s*\(([^)]*)\)\s*VALUES\s*)\(([?,\s]+)\)(.*)$", re.I | re.S)
_CONFLICT_COLUMNS = re.compile(r"ON\s+CONFLICT\s*\(([^)]*)\)", re.I)
_UPSERT_TAIL = re.compile(r"\s*DO\s+UPDATE\s+SET\s+((?:\w+\s*=\s*excluded\.\w+\s*,?\s*)+)$", re.I)
_ASSIGNMENT = re.compile(r"(\w+)\s*=\s*excluded\.(\w+)", re.I)
_INSERT_TARGET = re.compile(r"INSERT\s+INTO\s+(\w+)", re.I)
EXECUTEMANY_CHUNK = 100


def _guard_upsert(sql: str, indexed: set[str]) -> str:
    """Make a DuckDB upsert safe for tables that children reference.

    DuckDB rewrites a row as delete-and-insert when an upsert touches an indexed column (foreign key or unique),
    which its foreign keys reject while children still point at the row. So indexed columns are left out of
    the update (they are the row's identity, not data to overwrite) and a row is only rewritten when a
    remaining column actually differs. Constraints stay enforced for every insert.
    """
    tail = _UPSERT_TAIL.search(sql)
    target = _INSERT_TARGET.search(sql)
    if not tail or not target:
        return sql
    assignments = [(c, s) for c, s in _ASSIGNMENT.findall(tail.group(1)) if c not in indexed]
    head = sql[: tail.start()].rstrip()
    if not assignments:
        return f"{head} DO NOTHING"
    sets = ", ".join(f"{column} = excluded.{source}" for column, source in assignments)
    checks = " OR ".join(f"{target.group(1)}.{column} IS DISTINCT FROM excluded.{source}" for column, source in assignments)
    return f"{head} DO UPDATE SET {sets} WHERE {checks}"


class Connection:
    """Engine-neutral facade: ``execute`` returns rows addressable by name; ``with`` opens a transaction.

    A *session* connection (one per HTTP request) is used as ``with connect(...) as connection:``; entering it
    only marks the request's scope and leaving it closes the connection. Everything inside that uses
    ``with connection:`` to group writes into a transaction, so slow work (such as an AI call) never holds
    the database lock.
    """

    def __init__(self, local: Database, dialect: str, session: bool = False) -> None:
        self.local = local
        self.dialect = dialect
        self._session = session
        self._session_open = False
        self._transactions = 0

    def _prepare(self, sql: str) -> str:
        if self.dialect != "duckdb":
            return sql
        target = _INSERT_TARGET.search(sql)
        if not target or "DO UPDATE" not in sql.upper():
            return sql
        return _guard_upsert(sql, self.local.indexed_columns(target.group(1)))

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        return Cursor(self.local.run(self._prepare(sql), params))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        """Run one statement per row; plain ``INSERT ... VALUES (?, ...)`` is sent as multi-row statements."""
        batch = [tuple(params) for params in rows]
        if not batch:
            return
        sql = self._prepare(sql)
        match = _MULTI_INSERT.match(sql)
        with self.local.batch():
            if not match:
                for params in batch:
                    self.local.run(sql, params)
                return
            head, column_list, placeholders, tail = match.groups()
            width = len([p for p in placeholders.split(",") if p.strip()])
            names = [name.strip() for name in column_list.split(",")]
            conflict = _CONFLICT_COLUMNS.search(tail)
            positions = [names.index(c.strip()) for c in conflict.group(1).split(",")] if conflict else []
            group = "(" + ", ".join("?" for _ in range(width)) + ")"
            for start in range(0, len(batch), EXECUTEMANY_CHUNK):
                chunk = batch[start:start + EXECUTEMANY_CHUNK]
                if positions:  # the last row for a key wins, as it would one row at a time
                    chunk = list({tuple(params[i] for i in positions): params for params in chunk}.values())
                statement = f"{head}{', '.join([group] * len(chunk))}{tail}"
                self.local.run(statement, [value for params in chunk for value in params])

    def executescript(self, script: str) -> None:
        self.local.run_script(script)

    def upsert_rows(
        self,
        table: str,
        columns: Sequence[str],
        keys: Sequence[str],
        rows: Iterable[Sequence[Any]],
        update_columns: Sequence[str] | None = None,
    ) -> None:
        """Idempotently insert rows using SQL portable across DuckDB and Databricks."""
        table = check_identifier(table)
        column_names = [check_identifier(name) for name in columns]
        key_names = [check_identifier(name) for name in keys]
        updates = [check_identifier(name) for name in (update_columns if update_columns is not None else column_names)]
        updates = [name for name in updates if name not in key_names]
        if not column_names or not key_names or not set(key_names) <= set(column_names):
            raise ValueError("Upsert columns and keys must be non-empty, with every key included in columns.")
        values = [tuple(row) for row in rows]
        if any(len(row) != len(column_names) for row in values):
            raise ValueError("Every upsert row must match the declared column count.")
        if not values:
            return
        if self.dialect == "databricks":
            key_positions = [column_names.index(name) for name in key_names]
            for start in range(0, len(values), 50):
                batch = values[start:start + 50]
                if len(batch) > 1:
                    batch = list({
                        tuple(row[position] for position in key_positions): row
                        for row in batch
                    }.values())
                source = ", ".join(f"? AS {name}" for name in column_names)
                match = " AND ".join(f"t.{name} = s.{name}" for name in key_names)
                update = f"WHEN MATCHED THEN UPDATE SET {', '.join(f't.{name} = s.{name}' for name in updates)} " if updates else ""
                insert_columns = ", ".join(column_names)
                insert_values = ", ".join(f"s.{name}" for name in column_names)
                if len(batch) == 1:
                    source_query = f"(SELECT {source})"
                    params = batch[0]
                else:
                    selects = [
                        "SELECT " + ", ".join(
                            f"? AS {name}" for name in column_names
                        )
                    ]
                    selects.extend(
                        "SELECT " + ", ".join("?" for _ in column_names)
                        for _ in batch[1:]
                    )
                    source_query = f"({' UNION ALL '.join(selects)}) AS s"
                    params = [value for row in batch for value in row]
                if len(batch) == 1:
                    source_query += " AS s"
                sql = (
                    f"MERGE INTO {table} AS t USING {source_query} ON {match} "
                    f"{update}WHEN NOT MATCHED THEN INSERT ({insert_columns}) VALUES ({insert_values})"
                )
                self.local.run(sql, params)
            return
        placeholders = ", ".join("?" for _ in column_names)
        conflict = ", ".join(key_names)
        action = (
            "DO UPDATE SET " + ", ".join(f"{name} = excluded.{name}" for name in updates)
            if updates else "DO NOTHING"
        )
        self.executemany(
            f"INSERT INTO {table} ({', '.join(column_names)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) {action}",
            values,
        )

    def __enter__(self) -> "Connection":
        if self._session and not self._session_open:
            self._session_open = True
            return self
        self.local.begin()
        self._transactions += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._transactions == 0:
            self._session_open = False
            self.close()
            return
        self._transactions -= 1
        if exc_type is None:
            self.local.commit()
        else:
            self.local.rollback()

    def commit(self) -> None:
        """Statements autocommit outside ``with``, so there is nothing to flush."""

    def close(self) -> None:
        self.local.close()

    def list_tables(self) -> list[str]:
        return self.local.list_tables()

    def table_columns(self, table: str) -> list[str]:
        return self.local.table_columns(table)

    def app_database(self) -> Database:
        """Where application tables live: with the rest of the configured database."""
        return self.local


def connect(
    database: str | os.PathLike[str] | None = None, dialect: str | None = None, session: bool = False
) -> Connection:
    """Open the configured engine for ``dialect``; DuckDB remains a private local database when in memory."""
    chosen = resolve_dialect(dialect)
    if chosen == "databricks":
        local: Database = DatabricksDatabase(DatabricksSettings.from_env())
    else:
        path = str(database) if database is not None else default_database_path(chosen)
        local = DuckDBDatabase(path)
    return Connection(local, chosen, session)
