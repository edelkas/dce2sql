"""MySQL engine.

Works with MySQL 8.0.13 or newer, and with MariaDB 10.8 or newer.  The floor is set by
functional indexes: two of the unique constraints in the schema are declared over
``COALESCE(column, 0)``, because every SQL engine treats NULLs as distinct inside a unique
index and so would fail to deduplicate precisely the rows a re-import doubles.

``utf8mb4`` throughout, and not as a formality: the older ``utf8`` in MySQL is three bytes per
character and cannot store an emoji at all, which in a Discord archive would be a spectacular
way to lose data.
"""

from __future__ import annotations

from datetime import timezone
from typing import Sequence

from ..schema import BOOL, ID, INT, JSON, STR, TEXT, TS, Column, Table
from .server import ServerAdapter, missing_driver

#: Character set and collation for the database and every table in it.
CHARSET = "utf8mb4"
COLLATION = "utf8mb4_unicode_ci"

#: MySQL error numbers worth telling apart.
ER_BAD_DB_ERROR = 1049


class MysqlAdapter(ServerAdapter):
    name = "mysql"
    default_port = 3306
    maintenance_database = "mysql"

    # MySQL has no CREATE INDEX ... IF NOT EXISTS, so what is already there has to be asked for
    supports_index_if_not_exists = False
    max_identifier = 64

    autoincrement = "AUTO_INCREMENT"
    # BIGINT rather than the INT the schema declares: an auto key on a junction table counts
    # rows, and four bytes is a ceiling an archive could conceivably reach
    autoincrement_type = "BIGINT"

    types = {
        # Discord IDs are 8-byte and unsigned in principle, but signed BIGINT covers every
        # snowflake that will exist for the next several centuries
        ID: "BIGINT",
        INT: "INT",
        BOOL: "BOOLEAN",
        TS: "DATETIME",
        STR: "VARCHAR({size})",
        # MEDIUMTEXT rather than TEXT: TEXT is 64 KiB, which in utf8mb4 is only 16k characters,
        # and a channel topic or an embed description has no business being truncated
        TEXT: "MEDIUMTEXT",
        JSON: "JSON",
    }

    def quote(self, name: str) -> str:
        return f"`{name}`"

    # -- values ------------------------------------------------------------------------

    def encode(self, column, value):
        value = super().encode(column, value)
        # DATETIME has no timezone, so the offset would simply be dropped; it is made explicit
        # here instead, and everything in the database is UTC
        if column.type == TS and value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    # -- connecting --------------------------------------------------------------------

    def _driver(self):
        try:
            import pymysql

            return pymysql
        except ImportError:
            pass
        try:
            import MySQLdb

            return MySQLdb
        except ImportError:
            raise missing_driver("mysql", ("PyMySQL", "mysqlclient"), "mysql") from None

    def _connect(self, database: str):
        driver = self._driver()
        return driver.connect(
            host=self.host,
            port=self.port,
            user=self.user or "root",
            password=self.password or "",
            database=database,
            charset=CHARSET,
            autocommit=False,
            **self.options,
        )

    def _is_missing_database(self, exc: Exception) -> bool:
        return ER_BAD_DB_ERROR in getattr(exc, "args", ())

    def _create_database(self) -> None:
        connection = self._connect(self.maintenance_database)
        try:
            connection.cursor().execute(
                f"CREATE DATABASE IF NOT EXISTS {self.quote(self.database)} "
                f"CHARACTER SET {CHARSET} COLLATE {COLLATION}"
            )
            connection.commit()
        finally:
            connection.close()

    def _configure(self) -> None:
        cursor = self.connection.cursor()
        cursor.execute(f"SET NAMES {CHARSET} COLLATE {COLLATION}")
        # Everything in the database is UTC; this keeps NOW() and friends honest for anyone
        # querying it later, and stops a server-local timezone leaking into comparisons
        cursor.execute("SET time_zone = '+00:00'")

    # -- DDL ---------------------------------------------------------------------------

    def create_table_ddl(self, table: Table) -> str:
        return (
            super().create_table_ddl(table)
            + f" ENGINE=InnoDB DEFAULT CHARSET={CHARSET} COLLATE={COLLATION}"
        )

    def null_sentinel(self, column: Column) -> str:
        # A bare literal rather than FROM_UNIXTIME(0), which depends on the session timezone
        # and so is not deterministic enough for a functional index
        if column.type == TS:
            return "'1970-01-01 00:00:00'"
        return super().null_sentinel(column)

    def existing_indexes(self, table_name: str) -> set[str]:
        rows = self.fetchall(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (table_name,),
        )
        return {row[0] for row in rows}

    # -- writes ------------------------------------------------------------------------

    def insert_ignore(
        self, table: Table, columns: Sequence[str], rows: Sequence[Sequence]
    ) -> int:
        """Insert, leaving any row that is already there exactly as it is.

        Deliberately not ``INSERT IGNORE``, which downgrades *every* error to a warning --
        including data truncation.  In an archiving tool that is a licence to lose data
        quietly.  Assigning a column to itself has the same effect on a duplicate key and no
        effect on anything else, and it leaves real errors raising.
        """
        if not rows:
            return 0
        first = self.quote(columns[0])
        sql = (
            f"INSERT INTO {self.quote(table.name)} "
            f"({', '.join(self.quote(c) for c in columns)}) "
            f"VALUES ({self.marks(len(columns))}) "
            f"ON DUPLICATE KEY UPDATE {first} = {first}"
        )
        # A no-op update reports 0 affected rows, so the count is rows genuinely inserted
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
        assignments = ", ".join(
            f"{self.quote(c)} = VALUES({self.quote(c)})" for c in updated
        )
        sql = (
            f"INSERT INTO {self.quote(table.name)} "
            f"({', '.join(self.quote(c) for c in columns)}) "
            f"VALUES ({self.marks(len(columns))}) "
            f"ON DUPLICATE KEY UPDATE {assignments}"
        )
        return self.executemany(sql, [self.encode_row(table, columns, r) for r in rows])
