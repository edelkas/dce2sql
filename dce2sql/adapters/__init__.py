"""Database engines.

Each adapter subclasses :class:`~dce2sql.adapters.base.Adapter`.  ``--engine`` picks one by
name; :func:`create` is the only place that knows which names exist, so adding an engine means
adding a module and one entry here.
"""

from __future__ import annotations

from .base import Adapter
from .sqlite import SqliteAdapter

#: Engines available to ``--engine``.  MySQL and PostgreSQL join this once written; the
#: abstraction exists so that they are a new file rather than a new code path everywhere.
ENGINES: dict[str, type[Adapter]] = {
    SqliteAdapter.name: SqliteAdapter,
}

DEFAULT_ENGINE = SqliteAdapter.name


def create(engine: str, database: str, **options) -> Adapter:
    """Build the adapter for ``engine``, pointed at ``database``."""
    try:
        cls = ENGINES[engine]
    except KeyError:
        available = ", ".join(sorted(ENGINES))
        raise ValueError(f"unknown engine {engine!r}; available: {available}") from None
    return cls(database, **options)


__all__ = ["Adapter", "SqliteAdapter", "ENGINES", "DEFAULT_ENGINE", "create"]
