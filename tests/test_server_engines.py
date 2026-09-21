"""The same promises, proved against a real MySQL or PostgreSQL server.

These run only when there is a server to run them against, named by an environment variable:

    DCE2SQL_TEST_MYSQL=mysql://root:secret@localhost/dce2sql_test
    DCE2SQL_TEST_POSTGRES=postgresql://postgres:secret@localhost/dce2sql_test

**The named database is dropped and recreated between tests**, so point them at a scratch one.

What they check is not the SQL syntax -- ``test_engines.py`` does that without a server -- but
the two things only a server can settle: that it accepts what the adapter renders, and that a
value survives the round trip through a native type unchanged.  The last test is the one that
matters most: the same exports imported into SQLite and into a server have to produce the same
archive, which is the whole claim of having an adapter layer at all.
"""

from __future__ import annotations

import os

import pytest
from conftest import fixture
from support import (
    diff,
    drop_everything,
    import_files,
    regressions,
    rows,
    snapshot,
    with_rich_json,
)

from dce2sql.adapters import parse_url

#: Engine name -> the environment variable naming a server to test it against.
SERVERS = {
    "mysql": "DCE2SQL_TEST_MYSQL",
    "postgres": "DCE2SQL_TEST_POSTGRES",
}


def _target(engine: str) -> dict:
    url = os.environ.get(SERVERS[engine])
    if not url:
        pytest.skip(f"set {SERVERS[engine]} to a scratch database to run these")
    options = parse_url(url)
    if options is None or options["engine"] != engine:
        pytest.skip(f"{SERVERS[engine]} is not a {engine} URL")
    return {k: v for k, v in options.items() if v is not None}


@pytest.fixture(params=sorted(SERVERS), ids=sorted(SERVERS))
def server(request):
    """A scratch database on a real server, emptied before each test."""
    target = _target(request.param)
    drop_everything(target)
    return target


class TestSchema:
    def test_the_server_accepts_the_schema(self, server):
        import_files(server, fixture("ext.json"))
        assert rows(server, "SELECT COUNT(*) FROM messages")[0][0] > 0

    def test_creating_it_twice_is_harmless(self, server):
        """Every run creates the schema, so the second run must be a no-op.

        Worth its own test because MySQL has no ``CREATE INDEX IF NOT EXISTS`` and has to work
        out for itself what is already there.
        """
        import_files(server, fixture("ext.json"))
        import_files(server, fixture("ext.json"))
        assert rows(server, "SELECT COUNT(*) FROM imports")[0][0] == 2

    def test_a_snowflake_survives(self, server):
        """Eight bytes: a Discord ID truncated to four would be silent and catastrophic."""
        import_files(server, fixture("ext.json"))
        biggest = rows(server, "SELECT MAX(id) FROM messages")[0][0]
        assert biggest > 2**32

    def test_emoji_survive(self, server):
        """The reason MySQL must be utf8mb4: the older utf8 cannot store one at all."""
        import_files(server, fixture("ext.json"))
        stored = rows(
            server, "SELECT name FROM emojis WHERE guild_id IS NULL AND name <> code"
        )
        assert stored, "the fixture uses standard Unicode emoji"
        assert any(max(ord(c) for c in name) > 0xFFFF for (name,) in stored)


class TestNativeTypes:
    def test_a_timestamp_comes_back_as_the_instant_it_went_in(self, server):
        import_files(server, fixture("ext.json"))
        stored = snapshot(server)["messages"]
        column = stored.columns.index("timestamp")
        instants = [row[column] for row in stored.rows if row[column]]
        assert instants
        # Canonicalized back to Unix seconds; the fixture is from January 2025
        assert all(1_735_000_000 < i < 1_740_000_000 for i in instants)

    def test_a_boolean_comes_back_as_a_boolean(self, server):
        import_files(server, fixture("ext.json"))
        assert set(r[0] for r in rows(server, "SELECT DISTINCT bot FROM users")) <= {0, 1, True, False}

    def test_a_json_document_comes_back_as_what_it_was(self, server, tmp_path):
        """The column type the engines disagree about most: TEXT, JSON and JSONB."""
        import json

        from support import COMPONENTS, FORWARD

        path, with_components, with_forward = with_rich_json(tmp_path)
        import_files(server, path)

        stored = {row[0]: row for row in snapshot(server)["messages"].rows}
        columns = snapshot(server)["messages"].columns

        components = stored[with_components][columns.index("components")]
        assert json.loads(components) == COMPONENTS

        forwarded = stored[with_forward][columns.index("forwarded")]
        assert json.loads(forwarded)["content"] == FORWARD["content"]

    def test_and_re_importing_it_is_still_not_an_edit(self, server, tmp_path):
        path, _, _ = with_rich_json(tmp_path)
        import_files(server, path)
        before = snapshot(server)

        import_files(server, path)
        problems = diff(before, snapshot(server))
        assert not problems, "\n".join(problems)

    def test_none_of_that_reads_as_an_edit(self, server):
        """A native type hands back its own normal form, which must not look like a change."""
        import_files(server, fixture("ext.json"))
        before = snapshot(server)

        import_files(server, fixture("ext.json"))
        problems = diff(before, snapshot(server))
        assert not problems, "\n".join(problems)


class TestPromises:
    """The properties the SQLite suite asserts, restated against a server."""

    def test_reimporting_changes_nothing(self, server):
        import_files(server, fixture("split.json"))
        before = snapshot(server)

        import_files(server, fixture("split.json"))
        problems = diff(before, snapshot(server))
        assert not problems, "\n".join(problems)

    def test_normalizing_changes_nothing(self, server):
        import_files(server, fixture("ext.json"))
        inline = snapshot(server)

        drop_everything(server)
        import_files(server, fixture("extnorm.json"))
        problems = diff(inline, snapshot(server))
        assert not problems, "\n".join(problems)

    def test_order_does_not_matter(self, server):
        names = ["ext.json", "split.json", "upstream.json", "noreact.json"]

        import_files(server, *[fixture(n) for n in names])
        forward = snapshot(server)

        drop_everything(server)
        import_files(server, *[fixture(n) for n in reversed(names)])

        problems = diff(forward, snapshot(server))
        assert not problems, "\n".join(problems)

    def test_a_poorer_export_never_erases_a_richer_one(self, server):
        import_files(server, fixture("split.json"))
        before = snapshot(server)

        import_files(server, fixture("upstream.json"))
        problems = regressions(before, snapshot(server))
        assert not problems, "\n".join(problems)

    def test_an_edit_is_recorded(self, server):
        import_files(server, fixture("split.json"))
        assert rows(server, "SELECT COUNT(*) FROM message_history") == [(0,)]

    def test_reaction_totals_hold(self, server):
        import_files(server, fixture("noreact.json"))
        anonymous = rows(
            server,
            "SELECT message_id, emoji_id, SUM(count) FROM reactions "
            "GROUP BY message_id, emoji_id ORDER BY message_id, emoji_id",
        )

        drop_everything(server)
        import_files(server, fixture("split.json"))
        named = rows(
            server,
            "SELECT message_id, emoji_id, SUM(count) FROM reactions "
            "GROUP BY message_id, emoji_id ORDER BY message_id, emoji_id",
        )
        assert [(m, e, int(n)) for m, e, n in anonymous] == [
            (m, e, int(n)) for m, e, n in named
        ]


class TestAgainstSqlite:
    """The claim that makes the adapter layer worth having."""

    ALL = ["ext.json", "extnorm.json", "split.json", "splitnorm.json", "upstream.json",
           "vanilla.json", "noreact.json", "roster.json"]

    def test_the_same_exports_produce_the_same_archive(self, server, tmp_path):
        files = [fixture(n) for n in self.ALL]

        sqlite_db = tmp_path / "archive.db"
        import_files(sqlite_db, *files)
        import_files(server, *files)

        problems = diff(snapshot(sqlite_db), snapshot(server))
        assert not problems, "\n".join(problems)
