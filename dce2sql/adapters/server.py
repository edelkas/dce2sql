"""What MySQL and PostgreSQL have in common, and SQLite does not.

Both talk to a server rather than a file, which brings three things with it: connection
details, a database that may have to be created before it can be used, and native column types
for the three things SQLite has to fake.

That last one is the interesting half.  SQLite stores booleans and timestamps as integers and
JSON as text; the servers have all three natively, which SQL.md asks for.  Native types mean a
value does not come back out looking the way it went in -- a timestamp returns as a
``datetime``, a ``jsonb`` document returns reordered -- so the encoding has to be matched by
the comparison in :meth:`Adapter.differs`, or the merge policy would report an edit on every
row it looked at.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from ..schema import BOOL, TS, Column
from .base import Adapter


class ServerAdapter(Adapter):
    """An engine reached over the network."""

    is_server = True
    placeholder = "%s"

    #: Port used when none is given
    default_port: int = 0

    #: Database connected to in order to create another one
    maintenance_database: str = ""

    def __init__(
        self,
        database: str,
        host: str = "localhost",
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        create: bool = True,
        **options,
    ) -> None:
        super().__init__()
        self.database = database
        self.host = host
        self.port = port or self.default_port
        self.user = user
        self.password = password
        self.create = create
        self.options = options

    # -- values ------------------------------------------------------------------------

    def encode(self, column: Column, value: Any) -> Any:
        """Hand the driver a native value where the engine has a native type for it."""
        if value is None:
            return None
        if column.type == BOOL:
            return bool(value)
        if column.type == TS:
            # Stored as Unix seconds everywhere inside this tool; converted only at the edge
            if isinstance(value, datetime):
                return value
            return datetime.fromtimestamp(int(value), timezone.utc)
        return super().encode(column, value)

    # -- connecting --------------------------------------------------------------------

    def connect(self) -> None:
        try:
            self.connection = self._connect(self.database)
        except Exception as exc:  # noqa: BLE001 -- re-raised below if it is not what we think
            if not (self.create and self._is_missing_database(exc)):
                raise
            self._create_database()
            self.connection = self._connect(self.database)
        self._configure()

    def _configure(self) -> None:
        """Anything the session needs once it is open."""

    def _connect(self, database: str):
        raise NotImplementedError

    def _is_missing_database(self, exc: Exception) -> bool:
        raise NotImplementedError

    def _create_database(self) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        who = f"{self.user}@" if self.user else ""
        return f"{self.name}://{who}{self.host}:{self.port}/{self.database}"


def missing_driver(engine: str, packages: Sequence[str], extra: str) -> ImportError:
    """The error raised when an engine's driver is not installed.

    Worth its own function because the message is the whole of the user experience here: a bare
    ``ModuleNotFoundError: psycopg`` tells someone nothing about what to do next.
    """
    names = " or ".join(packages)
    return ImportError(
        f"the {engine} engine needs {names}, which is not installed.\n"
        f"  Install it with:  pip install 'dce2sql[{extra}]'"
    )
