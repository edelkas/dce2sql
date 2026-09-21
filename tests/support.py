"""Helpers shared by the tests.

Most of what this project has to prove is a relation between two databases -- the same export
imported twice, the same conversation exported several ways, a range imported whole versus in
pieces, a poorer export arriving after a richer one.  :func:`snapshot` reduces a database to
something comparable, and :func:`diff` and :func:`regressions` state the two relations that
matter: *the same*, and *never worse*.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from dce2sql import schema
from dce2sql.adapters import create
from dce2sql.documents import Document
from dce2sql.importer import Importer
from dce2sql.reader import Source, open_document
from dce2sql.schema import BOOL, JSON, TS
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


def open_adapter(target):
    """Build an adapter from either a SQLite path or a dict of connection options.

    Lets a test say *what* it wants to prove without saying which engine it is proving it on,
    which is the whole point of having an adapter layer.
    """
    if isinstance(target, dict):
        options = dict(target)
        return create(options.pop("engine"), options.pop("database"), **options)
    return create("sqlite", str(target))


def import_files(target, *json_paths) -> Importer:
    """Import one or more exports into a database, creating it if needed."""
    adapter = open_adapter(target)
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


def drop_everything(target) -> None:
    """Empty a server database between tests, since it outlives the process."""
    adapter = open_adapter(target)
    adapter.connect()
    try:
        for table in reversed(schema.TABLES):
            adapter.execute(f"DROP TABLE IF EXISTS {adapter.quote(table.name)}")
        adapter.commit()
    finally:
        adapter.close()


def snapshot(target, tables=None, skip_columns=frozenset()) -> dict[str, Snap]:
    """Reduce a database to ``{table: Snap}``, leaving out bookkeeping.

    Rows come back sorted rather than in insertion order, because two imports that produce the
    same data need not produce it in the same sequence -- a normalized export resolves its
    lookup tables at the end, an inline one as it goes.  Auto-assigned keys are dropped for the
    same reason: they are an artifact of insertion order, not data.

    Values are reduced to one canonical form as well, so that a snapshot means the same thing
    whichever engine produced it: SQLite has to fake booleans, timestamps and JSON as integers
    and text, while MySQL and PostgreSQL have all three natively.  Two archives holding the
    same thing must compare equal across that divide -- which is itself worth asserting, and
    ``test_server_engines.py`` does.
    """
    tables = tables or [t for t in schema.DATA_TABLES if t not in SKIP_TABLES]
    adapter = open_adapter(target)
    adapter.connect()
    try:
        out = {}
        for name in tables:
            table = schema.BY_NAME[name]
            columns = tuple(
                c.name
                for c in table.columns
                if c.name not in VOLATILE and c.name not in skip_columns and not c.auto
            )
            selected = ", ".join(adapter.quote(c) for c in columns)
            rows = adapter.fetchall(f"SELECT {selected} FROM {adapter.quote(name)}")
            rows = [_canonical(table, columns, row) for row in rows]
            out[name] = Snap(columns, sorted(rows, key=_sort_key))
        return out
    finally:
        adapter.close()


def natural_key(table_name: str, available: tuple[str, ...]) -> tuple[str, ...]:
    """The columns that identify a row, ignoring any auto-assigned surrogate key."""
    table = schema.BY_NAME[table_name]

    if not table.primary_key.auto:
        candidate = (table.primary_key.name,)
    elif table.unique:
        # A NullSafe part identifies the row by its underlying column
        candidate = tuple(
            part.column if isinstance(part, schema.NullSafe) else part
            for part in table.unique[0]
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


def rows(target, sql: str, params=()) -> list[tuple]:
    """Run a query against a database, whichever engine it is on.

    The SQL in the tests is written unquoted and in lower case, which every engine here accepts
    for these table and column names, so the same query serves all three.
    """
    adapter = open_adapter(target)
    adapter.connect()
    try:
        return adapter.fetchall(sql.replace("?", adapter.placeholder), params)
    finally:
        adapter.close()


def _canonical(table, columns, row):
    """Reduce one row to a form that does not depend on the engine that stored it.

    Three conversions, and one judgement:

    * a timestamp becomes Unix seconds, whether it arrived as an integer or a ``datetime``;
    * a boolean becomes 0 or 1, whether it arrived as one of those or as ``True``;
    * a JSON document becomes its sorted text, whether it arrived as text or already parsed;
    * a CDN link loses its signature, because Discord re-signs it on every export and two
      exports of the same attachment therefore never agree on it.
    """
    out = []
    for name, value in zip(columns, row):
        column = table.column(name)
        if value is None:
            out.append(None)
        elif column.type == TS:
            out.append(_epoch(value))
        elif column.type == BOOL:
            out.append(1 if value else 0)
        elif column.type == JSON:
            out.append(_json_text(value))
        elif column.signed_url and isinstance(value, str):
            out.append(unsigned_url(value))
        else:
            out.append(value)
    return tuple(out)


def _epoch(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return int(value)


def _json_text(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


#: A components tree and a forward, neither of which the fixtures happen to contain. Both are
#: stored as JSON, which is the one column type the engines disagree about most.
COMPONENTS = [
    {
        "type": 1,
        "components": [
            {"type": 2, "style": 5, "label": "Open", "url": "https://example.invalid/a"},
            {"type": 2, "style": 1, "label": "Vote", "customId": "vote:1"},
        ],
    }
]

FORWARD = {
    "timestamp": "2024-12-25T10:00:00+00:00",
    "timestampEdited": None,
    "content": "the original message, quoted from somewhere else",
    "attachments": [],
    "embeds": [],
    "stickers": [],
    "components": [],
}


def with_rich_json(tmp_path, name="ext.json"):
    """A copy of a fixture whose first two messages carry the JSON-valued columns."""
    import json

    from conftest import fixture

    with fixture(name).open(encoding="utf-8") as handle:
        document = json.load(handle)

    document["messages"][0]["components"] = COMPONENTS
    document["messages"][1]["forwardedMessage"] = FORWARD
    document["messages"][1]["reference"] = {
        "type": "Forward",
        "messageId": "1234567890123456789",
        "channelId": None,
        "guildId": None,
    }

    path = tmp_path / "rich.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False)
    return path, int(document["messages"][0]["id"]), int(document["messages"][1]["id"])


def _sort_key(row):
    # NULLs and mixed types make a plain sort raise, so everything is compared as text
    return tuple("" if v is None else str(v) for v in row)
