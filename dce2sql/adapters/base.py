"""The engine-agnostic half of database access.

Almost all of the SQL this tool emits is the same on every engine; what differs is the type
names, the placeholder style, the auto-increment spelling and how each says "insert unless it
is already there".  Those are the abstract bits.  Everything built on top of them -- DDL
rendering, batched selects, inserts, updates and the comparison that decides whether a row has
changed -- lives here so that a new engine is a short file rather than a reimplementation.

A subclass can be handed straight to :class:`dce2sql.importer.Importer`, which is the extension
point INSTRUCTIONS.md asks for.

One thing deserves saying out loud, because it is the reason this layer has a ``differs``
method at all.  The engines do not agree on how to store a value, and the importer's whole
merge policy rests on being able to ask "has this actually changed?".  SQLite has no boolean
and no timestamp, so both become integers; MySQL and PostgreSQL have all three natively, plus
a real JSON type that normalizes what it is given.  A value therefore does not necessarily come
back out looking like it went in, and comparing the two naively would report an edit on every
single row.  ``encode`` maps a Python value into the engine's world and ``differs`` compares
within it.
"""

from __future__ import annotations

import hashlib
import json as jsonlib
from abc import ABC, abstractmethod
from typing import Any, Iterable, Sequence

from .. import schema
from ..schema import BOOL, JSON, TS, Column, NullSafe, Table
from ..util import chunked, same_url

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

    #: Engine type for an auto-incrementing primary key, when it differs from the plain one
    #: (PostgreSQL spells it as a type, the others as a keyword on the column)
    autoincrement_type: str | None = None

    #: Appended to the primary key of an auto-incrementing table
    autoincrement: str = "AUTOINCREMENT"

    #: Whether ``CREATE INDEX`` accepts ``IF NOT EXISTS``.  MySQL does not.
    supports_index_if_not_exists: bool = True

    #: Longest identifier the engine will accept; index names are capped to it
    max_identifier: int = 63

    #: True for an engine that talks to a server, and so needs a host and credentials
    is_server: bool = False

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

    # -- comparison --------------------------------------------------------------------

    def differs(self, column: Column, stored: Any, incoming: Any) -> bool:
        """Whether a value has really changed, as opposed to merely being written again.

        This is what the merge policy asks before updating a row, so everything that is not a
        genuine difference has to be filtered out here.  Two things are not:

        * a Discord CDN link that has simply been re-signed -- the ``ex``, ``is`` and ``hm``
          parameters are regenerated on every export and expire within a day;
        * a JSON document the engine stored natively and handed back in its own normal form,
          reordered or reformatted but meaning exactly the same thing.
        """
        if self.equivalent(column, stored, incoming):
            return False
        if column.signed_url and same_url(stored, incoming):
            return False
        return True

    def equivalent(self, column: Column, stored: Any, incoming: Any) -> bool:
        """Whether two values of this column mean the same thing to this engine."""
        if stored == incoming:
            return True
        if column.type == JSON:
            return _json_equal(stored, incoming)
        if column.type == TS:
            return _instant_equal(stored, incoming)
        return False

    # -- DDL ---------------------------------------------------------------------------

    def column_type(self, column: Column) -> str:
        if column.auto and self.autoincrement_type:
            return self.autoincrement_type
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

    def create_table_ddl(self, table: Table) -> str:
        columns = ",\n  ".join(self.column_ddl(table, c) for c in table.columns)
        return f"CREATE TABLE IF NOT EXISTS {self.quote(table.name)} (\n  {columns}\n)"

    def index_expression(self, table: Table, part) -> str:
        """Render one key part of an index.

        A plain column name is quoted as an identifier.  A :class:`NullSafe` part becomes an
        expression, wrapped in its own parentheses: MySQL requires them around a functional key
        part, and the others tolerate a redundant pair.
        """
        if not isinstance(part, NullSafe):
            return self.quote(part)
        column = table.column(part.column)
        return f"(COALESCE({self.quote(column.name)}, {self.null_sentinel(column)}))"

    def null_sentinel(self, column: Column) -> str:
        """The stand-in a NULL becomes inside a unique index.

        Zero for anything numeric: no Discord ID is ever 0 except the synthetic "Direct
        Messages" guild, which never appears in one of these positions.  A timestamp needs a
        literal of its own type, which is why this is the adapter's business.
        """
        return "0"

    def index_name(self, prefix: str, table: str, label: str) -> str:
        """An index name the engine will accept.

        Names are derived from the columns, which can run past the 63 or 64 characters engines
        allow.  An over-long one is truncated and given a short digest of the full name, so it
        stays unique and stays the same on every run -- an index that changed name between runs
        would be created afresh each time.
        """
        name = f"{prefix}_{table}_{label}"
        if len(name) <= self.max_identifier:
            return name
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        return name[: self.max_identifier - 9] + "_" + digest

    def index_ddl(self, table: Table) -> list[str]:
        """Every index and unique constraint for a table, skipping those already there."""
        existing = self.existing_indexes(table.name)
        guard = "IF NOT EXISTS " if self.supports_index_if_not_exists else ""
        statements = []

        for group in table.unique:
            name = self.index_name("ux", table.name, "_".join(_slug(g) for g in group))
            if name in existing:
                continue
            parts = ", ".join(self.index_expression(table, g) for g in group)
            statements.append(
                f"CREATE UNIQUE INDEX {guard}{self.quote(name)} "
                f"ON {self.quote(table.name)} ({parts})"
            )

        for column in table.indexed():
            name = self.index_name("ix", table.name, column)
            if name in existing:
                continue
            statements.append(
                f"CREATE INDEX {guard}{self.quote(name)} "
                f"ON {self.quote(table.name)} ({self.quote(column)})"
            )

        return statements

    def existing_indexes(self, table_name: str) -> set[str]:
        """Indexes already on a table.

        Only needed by an engine without ``CREATE INDEX IF NOT EXISTS``; the others let the
        server decide and return nothing here.
        """
        return set()

    def table_ddl(self, table: Table) -> list[str]:
        """Everything needed to bring one table into being, in order."""
        return [self.create_table_ddl(table), *self.index_ddl(table)]

    def create_schema(self) -> None:
        # Indexes are created after their table rather than alongside it, because an engine
        # that cannot say IF NOT EXISTS has to ask the server what is already there
        for table in schema.TABLES:
            self.execute(self.create_table_ddl(table))
            for statement in self.index_ddl(table):
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


def _slug(part) -> str:
    """Turn a key part into something usable inside an index name."""
    if isinstance(part, NullSafe):
        return f"{part.column}_or_null".lower()
    return "".join(c if c.isalnum() else "_" for c in part).strip("_").lower()


def _json_equal(stored: Any, incoming: Any) -> bool:
    """Compare two JSON documents by what they say, not by how they are written.

    A native JSON column hands back its own normal form -- PostgreSQL's ``jsonb`` reorders keys
    and rewrites numbers, MySQL's ``JSON`` drops insignificant whitespace -- so the text that
    comes out is rarely the text that went in even when nothing has changed.
    """
    return _as_json(stored) == _as_json(incoming)


def _as_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return jsonlib.loads(value)
        except ValueError:
            return value
    return value


def _instant_equal(stored: Any, incoming: Any) -> bool:
    """Compare two instants across the forms the engines store them in.

    SQLite keeps Unix seconds and the others keep a native timestamp, so a comparison can find
    an int on one side and a datetime on the other -- when reading a database written by an
    older version of this tool, or simply when the driver returns naive local time for a column
    written as UTC.
    """
    left, right = _epoch(stored), _epoch(incoming)
    return left is not None and left == right


def _epoch(value: Any) -> int | None:
    from datetime import datetime, timezone

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return None
