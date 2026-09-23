"""Files that are unusual, degenerate, or not exports at all.

An archive built over years will contain all of these: channels that were empty for the range,
direct messages, channel and message types newer than the tool, and the odd file that got into
the directory by mistake. None of them may take down a run.
"""

from __future__ import annotations

import json

import pytest
from conftest import fixture
from support import import_files, rows

from dce2sql.documents import Document, load
from dce2sql.reader import Source, open_document


@pytest.fixture
def make(tmp_path):
    """Write a doctored copy of a fixture and hand back its path."""

    def _make(name, mutate, as_name=None):
        with fixture(name).open(encoding="utf-8") as handle:
            document = json.load(handle)
        mutate(document)
        path = tmp_path / (as_name or f"doctored-{name}")
        with path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)
        return path

    return _make


def _empty(document):
    document["messages"] = []
    document["messageCount"] = 0


class TestDegenerateExports:
    def test_a_channel_with_no_messages(self, tmp_path, make):
        """`--skip-empty` is off by default, so these are common in a date-ranged run."""
        db = tmp_path / "archive.db"
        import_files(db, make("ext.json", _empty))

        assert rows(db, "SELECT COUNT(*) FROM messages") == [(0,)]
        # The channel and guild are still worth having
        assert rows(db, "SELECT COUNT(*) FROM channels")[0][0] >= 1
        assert rows(db, "SELECT total_message_count FROM imports") == [(0,)]

    def test_a_direct_message_export(self, tmp_path, make):
        """DCE files DMs under a synthetic guild with ID 0."""

        def to_dm(document):
            document["guild"] = {
                "id": "0",
                "name": "Direct Messages",
                "iconUrl": "https://cdn.discordapp.com/embed/avatars/0.png",
            }
            document["channel"] = {
                "id": "999888777666555444",
                "type": "DirectTextChat",
                "categoryId": None,
                "category": None,
                "name": "someone",
                "topic": None,
            }

        db = tmp_path / "archive.db"
        import_files(db, make("ext.json", to_dm))

        assert rows(db, "SELECT id, name FROM guilds") == [(0, "Direct Messages")]
        assert rows(
            db, "SELECT guild_id, type FROM channels WHERE id = 999888777666555444"
        ) == [(0, 1)]
        # A DM names no parent, so none should have been invented for it. The other rows in
        # this table are channels the doctored bodies mention, which bring their own parents.
        assert rows(
            db, "SELECT parent_id FROM channels WHERE id = 999888777666555444"
        ) == [(None,)]


class TestUnknownTypes:
    def test_a_channel_type_dce_has_no_name_for(self, tmp_path, make):
        """DCE writes the bare number when its enum doesn't cover the value."""
        db = tmp_path / "archive.db"

        def media_channel(document):
            document["channel"]["type"] = "16"  # GUILD_MEDIA

        import_files(db, make("ext.json", media_channel))
        assert rows(
            db, "SELECT type FROM channels WHERE id = ?", (int(_channel_id("ext.json")),)
        ) == [(16,)]

    def test_a_message_type_this_tool_has_never_heard_of(self, tmp_path, make):
        """A future DCE naming a new value must not be able to abort an import."""

        def rename(document):
            for message in document["messages"][:3]:
                message["type"] = "SomethingDiscordAddedLater"

        db = tmp_path / "archive.db"
        import_files(db, make("ext.json", rename))

        assert rows(db, "SELECT COUNT(*) FROM messages WHERE type IS NULL") == [(3,)]
        assert rows(db, "SELECT COUNT(*) FROM messages")[0][0] > 3

    def test_and_it_is_reported_rather_than_swallowed(self, tmp_path, make):
        def rename(document):
            document["messages"][0]["type"] = "SomethingDiscordAddedLater"

        db = tmp_path / "archive.db"
        importer = import_files(db, make("ext.json", rename))
        assert importer.stats.unknown_types["message:SomethingDiscordAddedLater"] == 1


class TestNotAnExport:
    @pytest.mark.parametrize(
        "document", [{}, {"hello": "world"}, {"messages": []}], ids=["empty", "alien", "bare"]
    )
    def test_valid_json_that_is_not_a_dce_export_is_rejected(self, tmp_path, document):
        """Otherwise it imports as a silent no-op and claims a file was read."""
        path = tmp_path / "alien.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle)

        doc = Document(open_document(Source.of(path)))
        with pytest.raises(ValueError, match="does not look like a DCE export"):
            doc.validate()

    def test_a_failing_file_leaves_the_database_untouched(self, tmp_path, make):
        db = tmp_path / "archive.db"
        import_files(db, fixture("ext.json"))
        before = rows(db, "SELECT COUNT(*) FROM messages")

        alien = tmp_path / "alien.json"
        with alien.open("w", encoding="utf-8") as handle:
            json.dump({"hello": "world"}, handle)

        from dce2sql.adapters import create

        adapter = create("sqlite", str(db))
        adapter.connect()
        try:
            from dce2sql.importer import Importer

            source = Source.of(alien)
            result = Importer(adapter).import_document(
                Document(open_document(source)), source
            )
        finally:
            adapter.close()

        assert result.error is not None
        assert rows(db, "SELECT COUNT(*) FROM messages") == before
        assert rows(db, "SELECT COUNT(*) FROM imports") == [(1,)], "the failure is not recorded"


class TestStreaming:
    @pytest.mark.parametrize("name", ["ext.json", "extnorm.json", "roster.json"])
    def test_the_streaming_reader_agrees_with_the_direct_one(self, name):
        """Forcing the threshold to zero exercises the path only huge files normally take."""
        source = Source.of(fixture(name))
        whole = Document(open_document(source, threshold=1 << 40))
        streamed = Document(open_document(source, threshold=0))

        assert whole.kind == streamed.kind
        assert whole.mod == streamed.mod
        assert whole.header == streamed.header

    def test_a_roster_is_recognized_without_parsing_it(self):
        """The deciding key sits past the guild's role inventory, so a prefix will not do."""
        streamed = open_document(Source.of(fixture("roster.json")), threshold=0)
        assert streamed.array_key == "members"
        assert len(list(streamed.items())) > 0

    def test_streamed_and_parsed_imports_produce_the_same_rows(self, tmp_path):
        from dce2sql.adapters import create
        from dce2sql.importer import Importer
        from support import diff, snapshot

        outcomes = []
        for threshold in (1 << 40, 0):
            db = tmp_path / f"{threshold}.db"
            adapter = create("sqlite", str(db))
            adapter.connect()
            try:
                adapter.create_schema()
                source = Source.of(fixture("extnorm.json"))
                result = Importer(adapter).import_document(
                    Document(open_document(source, threshold=threshold)), source
                )
                assert result.error is None
            finally:
                adapter.close()
            outcomes.append(snapshot(db))

        problems = diff(*outcomes)
        assert not problems, "\n".join(problems)


def _channel_id(name):
    return load(Source.of(fixture(name))).channel["id"]
