"""PostgreSQL engine.

Works with PostgreSQL 9.5 or newer, the floor being ``INSERT ... ON CONFLICT``.  Prefers the
``psycopg`` 3 driver and falls back to ``psycopg2``.

Two choices worth explaining.  Timestamps are ``TIMESTAMPTZ``, so the instant is stored rather
than a wall-clock reading that means nothing without knowing the server's timezone -- which
matters here, since an archive is assembled from exports taken on whatever machine happened to
be running and in whatever timezone it was set to.  And rich fields are ``JSONB`` rather than
``JSON``: it is the type worth having, being indexable and queryable, at the cost of not
round-tripping byte for byte.  That cost is paid in :meth:`Adapter.equivalent`, which compares
JSON documents by what they say rather than how they are written.
"""

from __future__ import annotations

from typing import Sequence

from ..schema import BOOL, ID, INT, JSON, STR, TEXT, TS, Column, Table
from .server import ServerAdapter, missing_driver

#: SQLSTATE for "database does not exist".
UNDEFINED_DATABASE = "3D000"


class PostgresAdapter(ServerAdapter):
    name = "postgres"
    default_port = 5432
    maintenance_database = "postgres"

    max_identifier = 63

    # PostgreSQL spells an auto-incrementing key as a type rather than a column attribute
    autoincrement = ""
    autoincrement_type = "BIGSERIAL"

    types = {
        ID: "BIGINT",
        INT: "INTEGER",
        BOOL: "BOOLEAN",
        TS: "TIMESTAMPTZ",
        STR: "VARCHAR({size})",
        TEXT: "TEXT",
        JSON: "JSONB",
    }

    # -- DDL ---------------------------------------------------------------------------

    def null_sentinel(self, column: Column) -> str:
        # to_timestamp is IMMUTABLE, which an index expression has to be; casting a text
        # literal to timestamptz is only STABLE and would be rejected
        if column.type == TS:
            return "to_timestamp(0)"
        return super().null_sentinel(column)

    # -- connecting --------------------------------------------------------------------

    def _driver(self):
        try:
            import psycopg

            return psycopg, 3
        except ImportError:
            pass
        try:
            import psycopg2

            return psycopg2, 2
        except ImportError:
            raise missing_driver("postgres", ("psycopg", "psycopg2"), "postgres") from None

    def _connect(self, database: str):
        driver, version = self._driver()
        self._driver_version = version
        return driver.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=database,
            **self.options,
        )

    def _is_missing_database(self, exc: Exception) -> bool:
        """Tell "no such database" apart from every other reason a connection can fail.

        The SQLSTATE would settle it, but libpq does not carry one out of a *connection*
        failure -- only out of a query -- so psycopg reports ``sqlstate`` as None here.  Hence
        the fallback to what the server actually said, narrowed by the database name so that it
        cannot match a failure about something else.
        """
        for attribute in ("sqlstate", "pgcode"):
            if getattr(exc, attribute, None) == UNDEFINED_DATABASE:
                return True
        message = str(exc)
        return f'database "{self.database}" does not exist' in message

    def _create_database(self) -> None:
        connection = self._connect(self.maintenance_database)
        try:
            # CREATE DATABASE cannot run inside a transaction block
            connection.autocommit = True
            connection.cursor().execute(
                f"CREATE DATABASE {self.quote(self.database)} ENCODING 'UTF8'"
            )
        finally:
            connection.close()

    def _configure(self) -> None:
        # Everything in the database is UTC. TIMESTAMPTZ stores the instant either way, but
        # this makes anything read back come out in the timezone it was written in.
        self.execute("SET TIME ZONE 'UTC'")
        self.commit()

    # -- raw access --------------------------------------------------------------------

    def executemany(self, sql: str, rows: Sequence[Sequence]) -> int:
        """Batched writes, using whatever the installed driver does well.

        ``psycopg2.executemany`` sends one round trip per row, which over a real archive is the
        difference between minutes and hours, so ``execute_batch`` is used there instead.  It
        reports no useful row count, hence the fallback: the figure in the report is then rows
        offered rather than rows stored, which is only ever an over-count on the junction
        tables.  psycopg 3 has none of these problems.
        """
        if getattr(self, "_driver_version", 3) >= 3:
            return super().executemany(sql, rows)

        from psycopg2.extras import execute_batch

        from .base import ROW_CHUNK
        from ..util import chunked

        cursor = self.connection.cursor()
        written = 0
        for batch in chunked(rows, ROW_CHUNK):
            execute_batch(cursor, sql, batch, page_size=len(batch))
            written += len(batch)
        return written

    # -- writes ------------------------------------------------------------------------

    def insert_ignore(
        self, table: Table, columns: Sequence[str], rows: Sequence[Sequence]
    ) -> int:
        # No conflict target: the row is skipped whichever unique constraint it collides with,
        # which matters because two of them are over an expression
        if not rows:
            return 0
        sql = (
            f"INSERT INTO {self.quote(table.name)} "
            f"({', '.join(self.quote(c) for c in columns)}) "
            f"VALUES ({self.marks(len(columns))}) "
            f"ON CONFLICT DO NOTHING"
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
        assignments = ", ".join(
            f"{self.quote(c)} = EXCLUDED.{self.quote(c)}" for c in updated
        )
        sql = (
            f"INSERT INTO {self.quote(table.name)} "
            f"({', '.join(self.quote(c) for c in columns)}) "
            f"VALUES ({self.marks(len(columns))}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
        )
        return self.executemany(sql, [self.encode_row(table, columns, r) for r in rows])
