"""Database engines.

Each adapter subclasses :class:`~dce2sql.adapters.base.Adapter`.  ``--engine`` picks one by
name; :func:`create` is the only place that knows which names exist, so adding an engine means
adding a module and one entry here.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

from .base import Adapter
from .mysql import MysqlAdapter
from .postgres import PostgresAdapter
from .server import ServerAdapter
from .sqlite import SqliteAdapter

#: Engines available to ``--engine``.
ENGINES: dict[str, type[Adapter]] = {
    SqliteAdapter.name: SqliteAdapter,
    MysqlAdapter.name: MysqlAdapter,
    PostgresAdapter.name: PostgresAdapter,
}

DEFAULT_ENGINE = SqliteAdapter.name

#: URL schemes understood by :func:`parse_url`, mapped onto engine names.  The aliases are the
#: ones SQLAlchemy and libpq have taught everyone to expect.
SCHEMES: dict[str, str] = {
    "sqlite": "sqlite",
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgres": "postgres",
    "postgresql": "postgres",
    "psql": "postgres",
}


def parse_url(url: str) -> dict | None:
    """Pull connection details out of a database URL, or return None if it isn't one.

    ``postgresql://user:secret@host:5432/archive`` and the like.  Accepted because it is what
    everyone already has in a note somewhere, and because it keeps a password out of the shell
    history rather better than a ``--password`` flag does -- though not much better, which is
    why the environment variable exists too.
    """
    if "://" not in url:
        return None

    parsed = urlparse(url)
    engine = SCHEMES.get(parsed.scheme.split("+")[0].lower())
    if engine is None:
        return None

    database = unquote(parsed.path.lstrip("/"))
    if engine == "sqlite":
        # sqlite:///relative/path or sqlite:////absolute/path, as everyone else spells it
        return {"engine": engine, "database": database or parsed.netloc}

    return {
        "engine": engine,
        "database": database,
        "host": parsed.hostname or "localhost",
        "port": parsed.port,
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
    }


def create(engine: str, database: str, **options) -> Adapter:
    """Build the adapter for ``engine``, pointed at ``database``.

    Options that only make sense for a server -- host, port, credentials -- are dropped for an
    engine that has no server, so that a single set of CLI flags can be passed through whatever
    the engine turns out to be.
    """
    try:
        cls = ENGINES[engine]
    except KeyError:
        available = ", ".join(sorted(ENGINES))
        raise ValueError(f"unknown engine {engine!r}; available: {available}") from None

    if not issubclass(cls, ServerAdapter):
        options = {}

    return cls(database, **{k: v for k, v in options.items() if v is not None})


__all__ = [
    "Adapter",
    "ServerAdapter",
    "SqliteAdapter",
    "MysqlAdapter",
    "PostgresAdapter",
    "ENGINES",
    "DEFAULT_ENGINE",
    "SCHEMES",
    "create",
    "parse_url",
]
