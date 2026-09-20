"""The engine-agnostic half of database access.

Almost all of the SQL this tool emits is the same on every engine; what differs is the type
names, the placeholder style, the auto-increment keyword and the "insert unless it's already
there" spelling.  Those are the abstract bits.  Everything built on top of them -- DDL
rendering, batched selects, inserts and updates -- lives here so that a new engine is a short
file rather than a reimplementation.

A subclass can be handed straight to :func:`dce2sql.importer.import_file`, which is the
extension point INSTRUCTIONS.md asks for.
"""

from __future__ import annotations

import json as jsonlib
from abc import ABC, abstractmethod
from typing import Any, Iterable, Sequence

from .. import schema
from ..schema import BOOL, JSON, Column, Table
from ..util import chunked

#: How many values to put in one ``IN (...)``.  SQLite's default ceiling is 999 bound
#: parameters; the other engines are far more generous, but there is nothing to gain from
#: longer lists, so one conservative number serves all of them.
PARAM_CHUNK = 900

#: How many rows to hand to a single ``executemany``.
ROW_CHUNK = 1000


class Adapter(ABC):
    """A database engine."""

    #: Name used by ``--engine``
    name: str = "abstract"

    #: Parameter marker for this engine's DBAPI driver
    placeholder: str = "?"

    #: Logical type -> engine type
    types: dict[str, str] = {}

    #: Appended to the primary key of an auto-incrementing table
    autoincrement: str = "AUTOINCREMENT"

    def __init__(self) -> None:
        self.connection: Any = None

    # -- lifecycle ---------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """Open the connection, creating the database if it does not exist."""

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def __enter__(self) -> "Adapter":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()

    # -- raw access --------------------------------------------------------------------

    def execute(self, sql: str, params: Sequence = ()) -> Any:
        cursor = self.connection.cursor()
        cursor.execute(sql, params)
        return cursor

    def executemany(self, sql: str, rows: Sequence[Sequence]) -> int:
        """Run one statement over many rows, and report how many were actually written.

        The count matters for the statements that quietly skip rows -- an "insert unless it is
        already there" offered a thousand rows may store none of them -- so that the report
        says what changed rather than what was attempted.
        """
        cursor = self.connection.cursor()
        written = 0
        for batch in chunked(rows, ROW_CHUNK):
            cursor.executemany(sql, batch)
            # A driver that declines to say returns -1; fall back to assuming all of them
            written += cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else len(batch)
        return written

    def fetchall(self, sql: str, params: Sequence = ()) -> list[tuple]:
        return list(self.execute(sql, params).fetchall())

    # -- identifiers and literals ------------------------------------------------------

    def quote(self, name: str) -> str:
        return f'"{name}"'

    def marks(self, count: int) -> str:
        return ", ".join([self.placeholder] * count)

    def encode(self, column: Column, value: Any) -> Any:
        """Convert a Python value into whatever this engine stores for that logical type."""
        if value is None:
            return None
        if column.type == BOOL:
            return 1 if value else 0
        if column.type == JSON:
            # Sorted keys, because the column is compared as text: the same object can come out
            # of two exports with its keys in a different order (a normalized document has its
            # references resolved at a different point), and that is not an edit.
            return (
                value
                if isinstance(value, str)
                else jsonlib.dumps(
                    value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
            )
        return value

    def encode_row(self, table: Table, columns: Sequence[str], row: Sequence) -> tuple:
        return tuple(self.encode(table.column(c), v) for c, v in zip(columns, row))

    # -- DDL ---------------------------------------------------------------------------

    def column_type(self, column: Column) -> str:
        rendered = self.types[column.type]
        if column.size and "{size}" in rendered:
            return rendered.format(size=column.size)
        return rendered.replace("{size}", str(column.size or 255))

    def column_ddl(self, table: Table, column: Column) -> str:
        parts = [self.quote(column.name), self.column_type(column)]
        if column.pk:
            parts.append("PRIMARY KEY")
            if column.auto and self.autoincrement:
                parts.append(self.autoincrement)
        elif not column.null:
            parts.append("NOT NULL")
        if column.default is not None:
            parts.append(f"DEFAULT {self.encode(column, column.default)}")
        return " ".join(parts)

    def table_ddl(self, table: Table) -> list[str]:
        """``CREATE TABLE`` plus every index and unique constraint, all idempotent."""
        columns = ",\n  ".join(self.column_ddl(table, c) for c in table.columns)
        statements = [
            f"CREATE TABLE IF NOT EXISTS {self.quote(table.name)} (\n  {columns}\n)"
        ]

        for group in table.unique:
            # An expression can't be quoted as an identifier; a plain column name can
            rendered = ", ".join(g if "(" in g else self.quote(g) for g in group)
            label = "_".join(_slug(g) for g in group)
            statements.append(
                f"CREATE UNIQUE INDEX IF NOT EXISTS "
                f"{self.quote(f'ux_{table.name}_{label}')} "
                f"ON {self.quote(table.name)} ({rendered})"
            )

        for name in table.indexed():
            statements.append(
                f"CREATE INDEX IF NOT EXISTS {self.quote(f'ix_{table.name}_{name}')} "
                f"ON {self.quote(table.name)} ({self.quote(name)})"
            )

        return statements

    def create_schema(self) -> None:
        for table in schema.TABLES:
            for statement in self.table_ddl(table):
                self.execute(statement)
        self.seed()
        self.commit()

    def seed(self) -> None:
        """Fill the enumeration tables.  Safe to repeat, and updates a renamed value."""
        from ..enums import CHANNEL_TYPES, MESSAGE_TYPES, REFERENCE_TYPES

        for table, values in (
            (schema.CHANNEL_TYPES, CHANNEL_TYPES),
            (schema.MESSAGE_TYPES, MESSAGE_TYPES),
            (schema.REFERENCE_TYPES, REFERENCE_TYPES),
        ):
            rows = [(k, v) for k, v in sorted(values.items())]
            self.upsert(table, ("id", "name"), rows, key=("id",))

    # -- queries -----------------------------------------------------------------------

    def select_by_ids(
        self, table: Table, ids: Iterable[int], columns: Sequence[str] | None = None
    ) -> dict[int, tuple]:
        """Fetch rows by primary key, keyed by that key.  Chunked to stay within limits."""
        columns = tuple(columns or table.names)
        ids = list(dict.fromkeys(i for i in ids if i is not None))
        if not ids:
            return {}

        selected = ", ".join(self.quote(c) for c in columns)
        key_index = columns.index(table.primary_key.name)
        out: dict[int, tuple] = {}

        for batch in chunked(ids, PARAM_CHUNK):
            sql = (
                f"SELECT {selected} FROM {self.quote(table.name)} "
                f"WHERE {self.quote(table.primary_key.name)} IN ({self.marks(len(batch))})"
            )
            for row in self.fetchall(sql, batch):
                out[row[key_index]] = row

        return out

    def select_keyed(
        self,
        table: Table,
        key_columns: Sequence[str],
        keys: Iterable[tuple],
        columns: Sequence[str] | None = None,
    ) -> dict[tuple, tuple]:
        """Fetch rows by a composite key, keyed by it.

        Used wherever a row has no Discord ID and therefore has to be found again by its
        identity -- an embed by ``(message_id, ordinal)``, a member by ``(user_id, guild_id)``.
        The keys are matched one column at a time rather than with a row constructor, because
        not every engine indexes ``(a, b) IN (...)`` the way one would hope.
        """
        columns = tuple(columns or table.names)
        keys = list(dict.fromkeys(keys))
        if not keys:
            return {}

        selected = ", ".join(self.quote(c) for c in columns)
        key_indexes = [columns.index(c) for c in key_columns]
        out: dict[tuple, tuple] = {}

        # Group by the leading column so each query is a single IN-list
        per_lead: dict[Any, list[tuple]] = {}
        for key in keys:
            per_lead.setdefault(key[0], []).append(key)

        lead = key_columns[0]
        rest = key_columns[1:]
        for batch in chunked(per_lead.keys(), PARAM_CHUNK):
            sql = (
                f"SELECT {selected} FROM {self.quote(table.name)} "
                f"WHERE {self.quote(lead)} IN ({self.marks(len(batch))})"
            )
            wanted = {k for value in batch for k in per_lead[value]}
            for row in self.fetchall(sql, list(batch)):
                key = tuple(row[i] for i in key_indexes)
                if not rest or key in wanted:
                    out[key] = row

        return out

    # -- writes ------------------------------------------------------------------------

    def insert(self, table: Table, columns: Sequence[str], rows: Sequence[Sequence]) -> int:
        if not rows:
            return 0
        sql = (
            f"INSERT INTO {self.quote(table.name)} "
            f"({', '.join(self.quote(c) for c in columns)}) "
            f"VALUES ({self.marks(len(columns))})"
        )
        return self.executemany(sql, [self.encode_row(table, columns, r) for r in rows])

    @abstractmethod
    def insert_ignore(
        self, table: Table, columns: Sequence[str], rows: Sequence[Sequence]
    ) -> int:
        """Insert, skipping rows that violate a unique constraint; return how many landed.

        This is what makes re-importing a file a no-op on the junction tables.
        """

    @abstractmethod
    def upsert(
        self,
        table: Table,
        columns: Sequence[str],
        rows: Sequence[Sequence],
        key: Sequence[str],
    ) -> int:
        """Insert, or overwrite the non-key columns of a row that is already there."""

    def update(
        self,
        table: Table,
        columns: Sequence[str],
        rows: Sequence[Sequence],
        key: Sequence[str] = ("id",),
    ) -> int:
        """Update rows by key.  Each row supplies the updated columns, then the key values."""
        if not rows:
            return 0
        assignments = ", ".join(f"{self.quote(c)} = {self.placeholder}" for c in columns)
        condition = " AND ".join(f"{self.quote(c)} = {self.placeholder}" for c in key)
        sql = f"UPDATE {self.quote(table.name)} SET {assignments} WHERE {condition}"

        encoded = []
        for row in rows:
            values = self.encode_row(table, columns, row[: len(columns)])
            keys = self.encode_row(table, key, row[len(columns) :])
            encoded.append(values + keys)

        return self.executemany(sql, encoded)

    # -- introspection ------------------------------------------------------------------

    def count(self, table_name: str) -> int:
        return self.fetchall(f"SELECT COUNT(*) FROM {self.quote(table_name)}")[0][0]

    def max_id(self, table_name: str, below: int) -> int:
        """Largest primary key under a ceiling, used to allocate synthetic emoji IDs."""
        rows = self.fetchall(
            f"SELECT MAX({self.quote('id')}) FROM {self.quote(table_name)} "
            f"WHERE {self.quote('id')} < {self.placeholder}",
            (below,),
        )
        return rows[0][0] or 0


def _slug(expression: str) -> str:
    """Turn a column name or expression into something usable inside an index name."""
    return "".join(c if c.isalnum() else "_" for c in expression).strip("_").lower()
