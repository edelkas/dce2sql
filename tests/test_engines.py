"""The engine layer, checked without needing a server.

Most of what goes wrong with a new adapter is in the SQL it renders, and that can be inspected
without connecting to anything.  The rest -- whether the server actually accepts it -- is in
``test_server_engines.py``, which runs only when there is a server to run against.
"""

from __future__ import annotations

import pytest

from dce2sql import schema
from dce2sql.adapters import ENGINES, MysqlAdapter, PostgresAdapter, SqliteAdapter, create, parse_url
from dce2sql.adapters.server import ServerAdapter
from dce2sql.schema import BOOL, JSON, TS, Column

#: One instance of each engine, built without connecting.
ADAPTERS = {
    "sqlite": SqliteAdapter("unused.db"),
    "mysql": MysqlAdapter("unused"),
    "postgres": PostgresAdapter("unused"),
}


@pytest.fixture(params=sorted(ADAPTERS), ids=sorted(ADAPTERS))
def adapter(request):
    return ADAPTERS[request.param]


class TestDdl:
    def test_every_table_renders(self, adapter):
        for table in schema.TABLES:
            ddl = adapter.create_table_ddl(table)
            assert ddl.startswith("CREATE TABLE IF NOT EXISTS")
            for column in table.columns:
                assert adapter.quote(column.name) in ddl

    def test_every_logical_type_is_mapped(self, adapter):
        used = {c.type for t in schema.TABLES for c in t.columns}
        assert used <= set(adapter.types), f"{adapter.name} is missing a type"

    def test_the_primary_key_can_assign_itself(self, adapter):
        """An auto-incrementing key has to be declared as one, however the engine spells it."""
        ddl = adapter.create_table_ddl(schema.MENTIONS)
        assert "PRIMARY KEY" in ddl
        spelling = adapter.autoincrement_type or adapter.autoincrement
        assert spelling and spelling in ddl

    def test_a_discord_id_gets_eight_bytes(self, adapter):
        # A snowflake does not fit in four, and silently truncating one would be catastrophic
        rendered = adapter.column_type(schema.MESSAGES.column("id"))
        assert rendered in ("INTEGER", "BIGINT"), rendered

    def test_every_reference_column_gets_eight_bytes(self, adapter):
        """A reference points at a key, and every key in this schema is 8 bytes.

        SQLite hides a mistake here -- its INTEGER is 8 bytes however the column is declared --
        so this is the guard that would have caught 'emoji_id' being an INT holding a snowflake.
        """
        for table in schema.TABLES:
            for column in table.columns:
                if column.name.endswith("_id"):
                    rendered = adapter.column_type(column)
                    assert rendered in ("INTEGER", "BIGINT"), (table.name, column.name, rendered)

    def test_index_names_fit_the_engines_limit(self, adapter):
        for table in schema.TABLES:
            for statement in _index_statements(adapter, table):
                name = statement.split("INDEX")[1].split("ON")[0]
                name = name.replace("IF NOT EXISTS", "").strip().strip('"`')
                assert len(name) <= adapter.max_identifier, (adapter.name, name)

    def test_index_names_are_stable(self, adapter):
        # They have to be, or every run would try to create the same index under a new name
        first = [_index_statements(adapter, t) for t in schema.TABLES]
        second = [_index_statements(adapter, t) for t in schema.TABLES]
        assert first == second

    def test_an_expression_key_part_is_parenthesised(self, adapter):
        """MySQL requires it around a functional key part; the others tolerate it."""
        statements = _index_statements(adapter, schema.REACTIONS)
        expression = [s for s in statements if "COALESCE" in s]
        assert expression, "the reactions table has a COALESCE unique index"

        # The key part itself is parenthesised, inside the parentheses around the key list:
        #   ... ON "reactions" ("message_id", "emoji_id", (COALESCE("user_id", 0)))
        rendered = expression[0].replace(" ", "").replace('"', "").replace("`", "")
        assert "(COALESCE(user_id,0))" in rendered

    def test_a_null_sentinel_has_the_type_of_its_column(self, adapter):
        """``COALESCE(timestamp, 0)`` is a type error wherever timestamps are a real type."""
        statements = _index_statements(adapter, schema.MESSAGE_HISTORY)
        expression = [s for s in statements if "COALESCE" in s][0]
        if adapter.name == "sqlite":
            assert ", 0)" in expression
        else:
            assert ", 0)" not in expression, "a timestamp column needs a timestamp sentinel"

    def test_unique_constraints_are_all_rendered(self, adapter):
        for table in schema.TABLES:
            statements = _index_statements(adapter, table)
            assert sum("UNIQUE" in s for s in statements) == len(table.unique), table.name

    def test_mysql_asks_the_server_what_already_exists(self):
        # ...because it has no CREATE INDEX IF NOT EXISTS to lean on
        assert not MysqlAdapter.supports_index_if_not_exists
        assert SqliteAdapter.supports_index_if_not_exists
        assert PostgresAdapter.supports_index_if_not_exists

    def test_only_the_engines_that_need_a_guard_omit_it(self, adapter):
        statements = _index_statements(adapter, schema.MENTIONS)
        has_guard = all("IF NOT EXISTS" in s for s in statements)
        assert has_guard == adapter.supports_index_if_not_exists


class TestBounds:
    """Which columns have a length limit, and which must not.

    A real export turned up an ``embeds.url`` of 1,995 characters: a Discord CDN link with an
    essay appended as a ``?comment=`` parameter. Every VARCHAR width is a guess, and for a URL
    there is nothing to guess from -- it is whatever somebody typed into a message.
    """

    #: Every column holding a URL. None of them may be bounded.
    URL_COLUMNS = [
        ("guilds", "url"), ("guilds", "icon"), ("guilds", "banner"), ("guilds", "splash"),
        ("users", "avatar"), ("users", "banner"),
        ("members", "avatar"), ("members", "banner"),
        ("attachments", "url"), ("emojis", "url"), ("stickers", "url"),
        ("embeds", "url"), ("embeds", "author_url"),
        ("resources", "url"), ("resources", "proxied_url"),
    ]

    @pytest.mark.parametrize("table,column", URL_COLUMNS)
    def test_a_url_column_is_never_bounded(self, table, column):
        assert schema.BY_NAME[table].column(column).size is None

    @pytest.mark.parametrize("table,column", URL_COLUMNS)
    def test_and_renders_as_unbounded_text(self, adapter, table, column):
        rendered = adapter.column_type(schema.BY_NAME[table].column(column))
        assert "VARCHAR" not in rendered, f"{table}.{column} is {rendered}"

    def test_every_remaining_bound_is_one_discord_itself_enforces(self):
        """A bounded column has to be bounded by the platform, not by our guess at it.

        Listed explicitly so that adding a bounded column is a decision rather than an
        oversight -- the one that let a 1,995-character URL into a VARCHAR(1024).
        """
        expected = {
            # Discord's own limits
            ("guilds", "name"): 100, ("channels", "name"): 100, ("roles", "name"): 100,
            ("users", "name"): 32, ("users", "display"): 32, ("members", "display"): 32,
            ("emojis", "name"): 64, ("emojis", "code"): 64,
            ("stickers", "name"): 30, ("interactions", "name"): 100,
            ("attachments", "name"): 255,
            ("embeds", "title"): 256, ("embeds", "author_name"): 256,
            ("embeds", "footer"): 2048,
            # Ours, and short by construction
            ("stickers", "format"): 16, ("resources", "slot"): 32,
            ("imports", "sha1"): 40, ("imports", "kind"): 16,
            ("channel_types", "name"): 64, ("message_types", "name"): 64,
            ("reference_types", "name"): 64,
        }
        found = {
            (t.name, c.name): c.size or 255
            for t in schema.TABLES
            for c in t.columns
            if c.type == schema.STR
        }
        assert found == expected

    def test_an_overflow_says_which_column_and_by_how_much(self, adapter):
        """The engines say "Data too long for column 'url' at row 1" and nothing more."""
        with pytest.raises(ValueError) as caught:
            adapter.encode_row(schema.USERS, ("id", "name"), (1, "x" * 40))

        message = str(caught.value)
        assert "users.name" in message
        assert "at most 32" in message and "of 40" in message

    def test_a_value_within_its_bound_passes_through(self, adapter):
        assert adapter.encode_row(schema.USERS, ("name",), ("x" * 32,)) == ("x" * 32,)


class TestIdentifiers:
    def test_each_engine_quotes_its_own_way(self):
        assert ADAPTERS["sqlite"].quote("select") == '"select"'
        assert ADAPTERS["postgres"].quote("select") == '"select"'
        assert ADAPTERS["mysql"].quote("select") == "`select`"

    def test_placeholders_match_the_driver(self):
        assert ADAPTERS["sqlite"].marks(3) == "?, ?, ?"
        assert ADAPTERS["mysql"].marks(3) == "%s, %s, %s"
        assert ADAPTERS["postgres"].marks(3) == "%s, %s, %s"

    def test_an_over_long_name_is_truncated_and_kept_unique(self, adapter):
        a = adapter.index_name("ux", "t", "c" * 200)
        b = adapter.index_name("ux", "t", "d" * 200)
        assert len(a) <= adapter.max_identifier
        assert a != b


class TestValues:
    """What each engine stores, and whether it can tell a change from a re-write."""

    def test_sqlite_has_no_native_types_so_everything_is_an_integer(self):
        db = ADAPTERS["sqlite"]
        assert db.encode(Column("x", BOOL), True) == 1
        assert db.encode(Column("x", TS), 1_700_000_000) == 1_700_000_000
        assert isinstance(db.encode(Column("x", JSON), {"a": 1}), str)

    @pytest.mark.parametrize("name", ["mysql", "postgres"])
    def test_a_server_gets_native_values(self, name):
        from datetime import datetime

        db = ADAPTERS[name]
        assert db.encode(Column("x", BOOL), 1) is True
        stored = db.encode(Column("x", TS), 1_700_000_000)
        assert isinstance(stored, datetime)

    def test_mysql_strips_the_offset_because_datetime_has_no_room_for_it(self):
        stored = ADAPTERS["mysql"].encode(Column("x", TS), 1_700_000_000)
        assert stored.tzinfo is None, "a naive UTC value, not a local reading"
        # 1700000000 is 2023-11-14T22:13:20Z; the point is that it is not shifted into
        # whatever timezone the machine running the import happens to be set to
        assert (stored.year, stored.month, stored.day, stored.hour) == (2023, 11, 14, 22)

    def test_postgres_keeps_the_offset(self):
        stored = ADAPTERS["postgres"].encode(Column("x", TS), 1_700_000_000)
        assert stored.tzinfo is not None
        assert stored.timestamp() == 1_700_000_000

    def test_a_reordered_json_document_is_not_an_edit(self, adapter):
        """A native JSON column hands back its own normal form, not the text it was given."""
        column = Column("components", JSON)
        stored = '{"b": 2, "a": 1}'
        incoming = adapter.encode(column, {"a": 1, "b": 2})
        assert not adapter.differs(column, stored, incoming)

    def test_a_json_document_that_really_changed_is(self, adapter):
        column = Column("components", JSON)
        incoming = adapter.encode(column, {"a": 1, "b": 3})
        assert adapter.differs(column, '{"a": 1, "b": 2}', incoming)

    def test_a_parsed_json_document_compares_too(self, adapter):
        # psycopg hands back a dict rather than text for a jsonb column
        column = Column("components", JSON)
        incoming = adapter.encode(column, {"a": 1})
        assert not adapter.differs(column, {"a": 1}, incoming)

    def test_an_instant_compares_across_the_forms_it_is_stored_in(self, adapter):
        from datetime import datetime, timezone

        column = Column("timestamp", TS)
        aware = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
        assert not adapter.differs(column, aware, adapter.encode(column, 1_700_000_000))
        assert not adapter.differs(column, 1_700_000_000, adapter.encode(column, 1_700_000_000))

    def test_a_re_signed_cdn_link_is_not_an_edit_on_any_engine(self, adapter):
        column = schema.ATTACHMENTS.column("url")
        base = "https://cdn.discordapp.com/attachments/1/2/a.png"
        assert not adapter.differs(column, f"{base}?ex=aaa&is=bbb&hm=ccc", f"{base}?ex=111&is=222&hm=333")
        assert adapter.differs(column, f"{base}?ex=aaa", f"{base.replace('a.png', 'b.png')}?ex=aaa")


class TestRegistry:
    def test_every_engine_is_reachable_by_name(self):
        assert set(ENGINES) == {"sqlite", "mysql", "postgres"}

    def test_an_unknown_engine_says_what_there_is(self):
        with pytest.raises(ValueError, match="available: mysql, postgres, sqlite"):
            create("oracle", "x")

    def test_server_options_are_dropped_for_sqlite(self):
        """So one set of flags can be passed through whatever engine turns out to be chosen."""
        db = create("sqlite", "archive.db", host="nowhere", user="nobody", port=1234)
        assert isinstance(db, SqliteAdapter)
        assert str(db.path) == "archive.db"

    def test_server_options_reach_a_server(self):
        db = create("postgres", "archive", host="db.example", port=6000, user="me")
        assert isinstance(db, ServerAdapter)
        assert (db.host, db.port, db.user, db.database) == ("db.example", 6000, "me", "archive")

    def test_a_server_engine_defaults_its_port(self):
        assert create("mysql", "x").port == 3306
        assert create("postgres", "x").port == 5432

    def test_describe_does_not_leak_the_password(self):
        described = create("postgres", "archive", user="me", password="hunter2").describe()
        assert "hunter2" not in described
        assert "me" in described and "archive" in described


class TestUrls:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("postgresql://me:pw@db:5433/archive",
             {"engine": "postgres", "host": "db", "port": 5433, "user": "me",
              "password": "pw", "database": "archive"}),
            ("postgres://localhost/archive",
             {"engine": "postgres", "host": "localhost", "port": None, "user": None,
              "password": None, "database": "archive"}),
            ("mysql://root@127.0.0.1/dce",
             {"engine": "mysql", "host": "127.0.0.1", "port": None, "user": "root",
              "password": None, "database": "dce"}),
            ("mariadb://root@127.0.0.1/dce",
             {"engine": "mysql", "host": "127.0.0.1", "port": None, "user": "root",
              "password": None, "database": "dce"}),
        ],
    )
    def test_a_url_settles_everything_at_once(self, url, expected):
        assert parse_url(url) == expected

    def test_a_password_with_awkward_characters_survives(self):
        assert parse_url("postgres://me:p%40ss%3Aword@h/db")["password"] == "p@ss:word"

    def test_a_plain_name_is_not_a_url(self):
        assert parse_url("archive.db") is None
        assert parse_url("C:/archives/dce.db") is None

    def test_an_unrecognised_scheme_is_not_a_url_either(self):
        # ...so it falls through to being treated as a filename, which is the safe reading
        assert parse_url("oracle://host/db") is None

    def test_sqlite_urls_work_the_way_everyone_writes_them(self):
        assert parse_url("sqlite:///archive.db") == {"engine": "sqlite", "database": "archive.db"}


class TestDriverErrors:
    @pytest.mark.parametrize(
        "engine,expected", [("mysql", "PyMySQL"), ("postgres", "psycopg")]
    )
    def test_a_missing_driver_says_how_to_install_it(self, engine, expected, monkeypatch):
        """A bare ModuleNotFoundError tells nobody what to do next."""
        import builtins

        real = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name.split(".")[0] in ("pymysql", "MySQLdb", "psycopg", "psycopg2"):
                raise ImportError(name)
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)

        with pytest.raises(ImportError) as caught:
            create(engine, "x")._driver()

        assert expected in str(caught.value)
        assert "pip install" in str(caught.value)


def _index_statements(adapter, table):
    """Index DDL for a table, with the "what already exists" query stubbed out."""
    original = type(adapter).existing_indexes
    type(adapter).existing_indexes = lambda self, name: set()
    try:
        return adapter.index_ddl(table)
    finally:
        type(adapter).existing_indexes = original
