"""SQLite engine.

The default, and the one that matches what this tool is for: a whole archive in a single file
that can be copied, backed up and queried anywhere without a server.

SQLite has no boolean and no timestamp type, so both become integers -- 0/1 and Unix seconds,
as INSTRUCTIONS.md specifies.  It also has no VARCHAR length semantics worth honouring, so the
declared sizes in the schema are ignored here; they exist for the engines that want them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from ..schema import BOOL, ID, INT, JSON, STR, TEXT, TS, Table
from .base import Adapter


class SqliteAdapter(Adapter):
    name = "sqlite"
    placeholder = "?"

    # 'INTEGER' rather than 'BIGINT' throughout, and not only because SQLite integers already
    # hold 8 bytes: only a column declared exactly 'INTEGER PRIMARY KEY' becomes the table's
    # rowid. Any other spelling leaves an auto-incrementing key unfilled.
    types = {
        ID: "INTEGER",
        INT: "INTEGER",
        BOOL: "INTEGER",
        TS: "INTEGER",
        STR: "TEXT",
        TEXT: "TEXT",
        JSON: "TEXT",
    }

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.execute("PRAGMA encoding = 'UTF-8'")
        # Durability is not worth much here: an interrupted import is rolled back as a whole and
        # simply re-run, and the file is a derived artifact rebuildable from the JSON
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        # Large negative values are a cache size in KiB, so this is 64 MiB
        self.connection.execute("PRAGMA cache_size = -65536")
        self.connection.execute("PRAGMA temp_store = MEMORY")

    def insert_ignore(
        self, table: Table, columns: Sequence[str], rows: Sequence[Sequence]
    ) -> int:
        if not rows:
            return 0
        sql = (
            f"INSERT OR IGNORE INTO {self.quote(table.name)} "
            f"({', '.join(self.quote(c) for c in columns)}) "
            f"VALUES ({self.marks(len(columns))})"
        )
        return self.executemany(sql, [self.encode_row(table, columns, r) for r in rows])

    def upsert(
        self,
        table: Table,
        columns: Sequence[str],
        rows: Sequence[Sequence],
        key: Sequence[str],
    ) -> int:
        if not rows:
            return 0
        updated = [c for c in columns if c not in key]
        conflict = ", ".join(self.quote(c) for c in key)
        sql = (
            f"INSERT INTO {self.quote(table.name)} "
            f"({', '.join(self.quote(c) for c in columns)}) "
            f"VALUES ({self.marks(len(columns))}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET "
            + ", ".join(f"{self.quote(c)} = excluded.{self.quote(c)}" for c in updated)
        )
        return self.executemany(sql, [self.encode_row(table, columns, r) for r in rows])
