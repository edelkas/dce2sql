"""Helpers shared by the tests.

Most of what this project has to prove is a relation between two databases -- the same export
imported twice, the same conversation exported several ways, a range imported whole versus in
pieces, a poorer export arriving after a richer one.  :func:`snapshot` reduces a database to
something comparable, and :func:`diff` and :func:`regressions` state the two relations that
matter: *the same*, and *never worse*.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from dce2sql import schema
from dce2sql.adapters import create
from dce2sql.documents import Document
from dce2sql.importer import Importer
from dce2sql.reader import Source, open_document
from dce2sql.util import unsigned_url

#: Columns that record when an import happened rather than what was imported.
VOLATILE = {"created_at", "updated_at"}

#: 'imports' is expected to differ: it grows by one row every time a file is read, by design.
SKIP_TABLES = {"imports"}


@dataclass(frozen=True)
class Snap:
    """One table, reduced to comparable form."""

    columns: tuple[str, ...]
    rows: list[tuple]

    def indexed(self, key: tuple[str, ...]) -> dict[tuple, tuple]:
        positions = [self.columns.index(c) for c in key]
        return {tuple(row[i] for i in positions): row for row in self.rows}


def import_files(db_path: Path, *json_paths) -> Importer:
    """Import one or more exports into a database, creating it if needed."""
    adapter = create("sqlite", str(db_path))
    adapter.connect()
    try:
        adapter.create_schema()
        importer = Importer(adapter)
        for path in json_paths:
            source = Source.of(path)
            doc = Document(open_document(source))
            result = importer.import_document(doc, source)
            assert result.error is None, f"{path}: {result.error}"
        return importer
    finally:
        adapter.close()


def snapshot(db_path: Path, tables=None, skip_columns=frozenset()) -> dict[str, Snap]:
    """Reduce a database to ``{table: Snap}``, leaving out bookkeeping.

    Rows come back sorted rather than in insertion order, because two imports that produce the
    same data need not produce it in the same sequence -- a normalized export resolves its
    lookup tables at the end, an inline one as it goes.  Auto-assigned keys are dropped for the
    same reason: they are an artifact of insertion order, not data.
    """
    tables = tables or [t for t in schema.DATA_TABLES if t not in SKIP_TABLES]
    connection = sqlite3.connect(str(db_path))
    try:
        out = {}
        for name in tables:
            table = schema.BY_NAME[name]
            columns = tuple(
                c.name
                for c in table.columns
                if c.name not in VOLATILE and c.name not in skip_columns and not c.auto
            )
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM {name}"
            ).fetchall()
            out[name] = Snap(columns, sorted(_unsign(table, columns, rows), key=_sort_key))
        return out
    finally:
        connection.close()


def natural_key(table_name: str, available: tuple[str, ...]) -> tuple[str, ...]:
    """The columns that identify a row, ignoring any auto-assigned surrogate key."""
    table = schema.BY_NAME[table_name]

    if not table.primary_key.auto:
        candidate = (table.primary_key.name,)
    elif table.unique:
        # Unwrap COALESCE(col, 0) back to the bare column name
        candidate = tuple(
            group.split("(")[1].split(",")[0].strip() if "(" in group else group
            for group in table.unique[0]
        )
    else:
        candidate = tuple(c.name for c in table.columns if not c.auto)

    # A comparison may have excluded one of them, in which case the rest still identifies the
    # row well enough for the purpose
    return tuple(c for c in candidate if c in available) or available


def diff(left: dict[str, Snap], right: dict[str, Snap], limit: int = 12) -> list[str]:
    """Every difference between two snapshots."""
    problems: list[str] = []

    for name in sorted(set(left) | set(right)):
        a, b = left.get(name), right.get(name)
        if a is None or b is None:
            problems.append(f"{name}: present on only one side")
            continue
        if a.rows == b.rows:
            continue
        if len(a.rows) != len(b.rows):
            problems.append(f"{name}: {len(a.rows)} rows != {len(b.rows)} rows")
        for row in [r for r in a.rows if r not in b.rows][:limit]:
            problems.append(f"{name}: only on the left  {row!r}")
        for row in [r for r in b.rows if r not in a.rows][:limit]:
            problems.append(f"{name}: only on the right {row!r}")

    return problems


def regressions(before: dict[str, Snap], after: dict[str, Snap]) -> list[str]:
    """Places where the right-hand side lost or changed something the left-hand side had.

    This is the project's central promise stated as an assertion: nothing is ever deleted, and
    a poorer export never erases a richer one.  *Filling* a column that was empty is fine and
    expected; changing or dropping one that was not is the failure being looked for.

    It doubles as a subset check -- "``before`` is a consistent subset of ``after``" -- which is
    the right relation between a vanilla export and an extended one.
    """
    problems: list[str] = []

    for name, snap in before.items():
        later = after.get(name)
        if later is None:
            problems.append(f"{name}: missing entirely")
            continue

        key = natural_key(name, snap.columns)
        indexed = later.indexed(key)
        positions = [snap.columns.index(c) for c in key]

        for row in snap.rows:
            row_key = tuple(row[i] for i in positions)
            other = indexed.get(row_key)
            if other is None:
                problems.append(f"{name}: row disappeared {row!r}")
                continue
            for i, column in enumerate(snap.columns):
                value = row[i]
                if value is None:
                    continue
                if other[later.columns.index(column)] != value:
                    problems.append(
                        f"{name}.{column}: {value!r} became "
                        f"{other[later.columns.index(column)]!r} (key {row_key})"
                    )

    return problems


def rows(db_path: Path, sql: str, params=()) -> list[tuple]:
    connection = sqlite3.connect(str(db_path))
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def _unsign(table, columns, rows):
    """Strip Discord's CDN signature before comparing.

    Two exports of the same attachment never agree on it -- it is re-signed on every run and
    expires within a day -- so leaving it in would make every cross-export comparison fail on
    something that is not a difference. The importer already declines to *update* a row over
    it; this is the same judgement applied to two databases built independently.
    """
    signed = [i for i, name in enumerate(columns) if table.column(name).signed_url]
    if not signed:
        return rows
    out = []
    for row in rows:
        row = list(row)
        for i in signed:
            if isinstance(row[i], str):
                row[i] = unsigned_url(row[i])
        out.append(tuple(row))
    return out


def _sort_key(row):
    # NULLs and mixed types make a plain sort raise, so everything is compared as text
    return tuple("" if v is None else str(v) for v in row)
