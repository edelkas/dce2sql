"""Putting resolved mentions back, and not putting back what was never one.

The failure this exists for: DCE writes ``#general`` where the message said ``<#123>``, so
renaming the channel changes the body of every message that ever mentioned it, and an importer
diffing old against new sees hundreds of edits that never happened.

Two halves are tested here.  That the inversion works is the easy half.  The hard half is that
it is *timid* -- a tool that rewrites message bodies has to be far more afraid of changing
something it shouldn't than of missing something it could have caught, because a wrong
substitution is indistinguishable from the real thing afterwards.
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import fixture
from support import diff, import_files, rows, snapshot

from dce2sql.documents import Document, Person
from dce2sql.mentions import ChannelIndex, Unresolver
from dce2sql.reader import Source, open_document


def person(user_id: str, rendered: str) -> Person:
    return Person(user={"id": user_id}, rendered=rendered)


@pytest.fixture
def unresolver():
    channels = ChannelIndex()
    channels.add("449367560878686208", "nplusplus", "test")
    channels.add("111", "general", "test")
    channels.add("222", "Voice Chat", "test", is_voice=True)

    u = Unresolver(channels)
    u.add_roles(
        [
            {"id": "198374136001593344", "name": "Moderator"},
            {"id": "300", "name": "Not Commander Salamander"},
            # Discord's everyone role is literally named '@everyone'
            {"id": "197765375503368192", "name": "@everyone"},
        ]
    )
    return u


class TestInversion:
    def test_a_user_mention_becomes_its_id(self, unresolver):
        bob = person("200775143860076545", "raigan#7701")
        assert unresolver.unresolve("@raigan#7701 hello", [bob]) == (
            "<@200775143860076545> hello"
        )

    def test_a_channel_mention_becomes_its_id(self, unresolver):
        assert unresolver.unresolve("see #general") == "see <#111>"

    def test_a_role_mention_becomes_its_id(self, unresolver):
        assert unresolver.unresolve("@Moderator help") == "<@&198374136001593344> help"

    def test_a_name_with_spaces_in_it(self, unresolver):
        assert unresolver.unresolve("@Not Commander Salamander") == "<@&300>"

    def test_a_voice_channel_loses_its_marker_too(self, unresolver):
        """DCE writes ' [voice]' after the name, which is part of the rendering, not the text."""
        assert unresolver.unresolve("join #Voice Chat [voice] now") == "join <#222> now"

    def test_several_in_one_body(self, unresolver):
        bob = person("1", "bob")
        assert unresolver.unresolve("@bob see #general and #nplusplus", [bob]) == (
            "<@1> see <#111> and <#449367560878686208>"
        )

    def test_it_counts_what_it_did(self, unresolver):
        unresolver.unresolve("@bob #general", [person("1", "bob")])
        assert unresolver.recovered["user"] == 1
        assert unresolver.recovered["channel"] == 1


class TestTimidity:
    """What it must refuse to touch."""

    def test_at_everyone_and_at_here_stay_as_they_are(self, unresolver):
        # They are already raw, and Discord's everyone *role* is named '@everyone', so taking
        # it for a role would produce '@@everyone'
        assert unresolver.unresolve("@everyone and @here") == "@everyone and @here"

    def test_a_name_that_is_only_the_start_of_a_longer_one(self, unresolver):
        """The bug this guards: '@bob' pulled out of '@bobby', leaving '<@1>by'."""
        assert unresolver.unresolve("@bobby said so", [person("1", "bob")]) == "@bobby said so"

    def test_the_longer_of_two_candidates_wins(self, unresolver):
        people = [person("1", "bob"), person("2", "bobby")]
        assert unresolver.unresolve("@bobby said so", people) == "<@2> said so"

    def test_a_channel_name_that_is_only_a_prefix(self, unresolver):
        # '#general-chat' is a channel this run has never heard of, not '#general' plus text
        assert unresolver.unresolve("in #general-chat") == "in #general-chat"

    def test_something_that_merely_looks_like_a_mention(self, unresolver):
        assert unresolver.unresolve("read #rules and ask @nobody") == (
            "read #rules and ask @nobody"
        )

    def test_a_sigil_in_the_middle_of_a_word(self, unresolver):
        """An e-mail address is the obvious case, and there is no shortage of others."""
        bob = person("1", "raigan#7701")
        assert unresolver.unresolve("mail me at a@raigan#7701", [bob]) == (
            "mail me at a@raigan#7701"
        )

    def test_a_name_belonging_to_someone_this_message_does_not_mention(self, unresolver):
        """The whole reason user matching is scoped to the message's own mentions array."""
        assert unresolver.unresolve("@bob was not pinged", []) == "@bob was not pinged"

    def test_what_dce_writes_when_it_could_not_resolve_one(self, unresolver):
        # There is no ID behind any of these, so there is nothing to put back
        text = "@Unknown #deleted-channel @deleted-role"
        assert unresolver.unresolve(text, [person("1", "Unknown")]) == text

    def test_content_already_raw_is_left_exactly_as_it_is(self, unresolver):
        raw = "<@200775143860076545> see <#111> and <:goldheart:711809267547766806>"
        assert unresolver.unresolve(raw, [person("200775143860076545", "raigan")]) == raw


class TestChannelIndex:
    LISTING = (
        "197765375503368192  | Chat / nplusplus\n"
        "1081289718685237248 | Chat / custom-tabs\n"
        " * 1312447806480453715 | Thread / EON | Active\n"
        " * 1110998853634773073 | Thread / Duality | Archived\n"
        "\n"
        "not a channel line at all\n"
    )

    def test_it_reads_a_channels_listing(self, tmp_path):
        path = tmp_path / "channels.txt"
        path.write_text(self.LISTING, encoding="utf-8")

        index = ChannelIndex()
        index.add_listing(path)

        assert index.by_name["nplusplus"] == "197765375503368192"
        assert index.by_name["custom-tabs"] == "1081289718685237248"
        assert index.by_name["EON"] == "1312447806480453715"
        assert index.by_name["Duality"] == "1110998853634773073"
        assert len(index) == 4

    def test_a_thread_named_with_the_separators_in_it(self, tmp_path):
        """A thread title is free text, so it may contain the very characters we split on."""
        path = tmp_path / "channels.txt"
        path.write_text(" * 999 | Thread / A / B | C | Active\n", encoding="utf-8")

        index = ChannelIndex()
        index.add_listing(path)
        assert index.by_name == {"A / B | C": "999"}

    def test_an_export_contributes_its_own_channel_and_its_parent(self):
        index = ChannelIndex()
        index.add_export_header(
            {
                "channel": {
                    "id": "1",
                    "name": "nplusplus",
                    "type": "GuildTextChat",
                    "categoryId": "2",
                    "category": "Chat",
                }
            }
        )
        assert index.by_name == {"nplusplus": "1", "Chat": "2"}

    def test_a_voice_channel_is_recognised_either_way_it_is_written(self):
        index = ChannelIndex()
        index.add_export_header({"channel": {"id": "1", "name": "a", "type": "GuildVoiceChat"}})
        index.add_database([("2", "b", 2), ("3", "c", 0)])

        assert index.is_voice("1") and index.is_voice("2")
        assert not index.is_voice("3")

    def test_the_first_source_to_claim_a_name_keeps_it(self):
        """An export's names are of the right vintage; the database's are merely the latest."""
        index = ChannelIndex()
        index.add("1", "general", "exports")
        index.add("2", "general", "database")

        assert index.by_name["general"] == "1"
        assert index.collisions == 1

    def test_deleted_channel_is_not_a_name(self):
        index = ChannelIndex()
        index.add("1", "deleted-channel", "exports")
        assert not index.by_name


class TestDocuments:
    def test_a_document_unresolves_its_bodies(self, unresolver):
        source = Source.of(fixture("ext.json"))
        doc = Document(open_document(source), unresolver)

        bodies = [m["content"] for m in doc.messages()]
        assert any("<@" in b for b in bodies), "the fixture has user mentions"
        assert not any("@raigan#7701" in b for b in bodies)

    def test_an_export_already_raw_is_left_alone(self, tmp_path, unresolver):
        """``--markdown false`` exports say so, and there is nothing in them to put back."""
        with fixture("ext.json").open(encoding="utf-8") as handle:
            document = json.load(handle)
        document["mod"]["markdown"] = False
        path = tmp_path / "raw.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)

        doc = Document(open_document(Source.of(path)), unresolver)
        assert doc.unresolver is None
        list(doc.messages())
        assert unresolver.total == 0

    def test_the_mod_block_reports_it(self, tmp_path):
        with fixture("ext.json").open(encoding="utf-8") as handle:
            document = json.load(handle)
        document["mod"]["markdown"] = False
        path = tmp_path / "raw.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)

        doc = Document(open_document(Source.of(path)))
        assert doc.mod.markdown is False
        assert "raw" in doc.describe()

    def test_an_export_predating_the_flag_reads_as_resolved(self):
        # Which it was: --markdown has always defaulted to on
        assert Document(open_document(Source.of(fixture("upstream.json")))).mod.markdown


class TestGroundTruth:
    """The decisive check: DCE's own raw output for the very same messages.

    ``raw.json`` is ``ext.json`` exported again with ``--markdown false``, so it holds exactly
    what the inverter is trying to reconstruct. Anything short of a perfect match is the
    inverter approximating, which is the one thing it must not do.
    """

    def _unresolved(self):
        index = ChannelIndex()
        index.add_listing(fixture("channels.txt"))
        doc = Document(open_document(Source.of(fixture("ext.json"))), Unresolver(index))
        return {m["id"]: m["content"] for m in doc.messages()}

    @staticmethod
    def _comparable(text: str) -> str:
        # The inverter claims mentions and nothing else, so the two things it deliberately
        # leaves resolved are normalized away on both sides
        return re.sub(r"<a?:(\w+):\d+>", lambda m: f":{m.group(1)}:", text)

    def test_it_reproduces_what_dce_writes_raw(self):
        raw = json.loads(fixture("raw.json").read_text(encoding="utf-8"))
        theirs = {m["id"]: m["content"] for m in raw["messages"]}
        ours = self._unresolved()

        assert ours.keys() == theirs.keys()
        mismatched = [
            (i, theirs[i], ours[i])
            for i in theirs
            if self._comparable(theirs[i]) != self._comparable(ours[i])
        ]
        assert not mismatched, "\n".join(
            f"{i}\n  DCE raw: {t[:90]}\n  ours   : {o[:90]}"
            for i, t, o in mismatched[:5]
        )

    def test_and_there_was_something_to_reproduce(self):
        """Guards the test above against passing because nothing was mentioned at all."""
        raw = json.loads(fixture("raw.json").read_text(encoding="utf-8"))
        bodies = " ".join(m["content"] for m in raw["messages"])
        assert len(re.findall(r"<@\d+>", bodies)) >= 5
        assert len(re.findall(r"<#\d+>", bodies)) >= 3


class TestTheWholePoint:
    """A rename must stop looking like an edit."""

    RENAMES = {"support": "help-desk", "userlevels": "user-levels", "multiplayer": "versus"}

    def _pair(self, tmp_path):
        """The same 230 messages before and after somebody renamed things."""
        with fixture("ext.json").open(encoding="utf-8") as handle:
            before = json.load(handle)
        after = json.loads(json.dumps(before))

        for message in after["messages"]:
            body = message.get("content") or ""
            for old, new in self.RENAMES.items():
                body = body.replace(f"#{old}", f"#{new}")
            for p in message.get("mentions") or []:
                body = body.replace(f"@{p['nickname']}", f"@{p['nickname']} the Second")
            message["content"] = body
            for p in message.get("mentions") or []:
                p["nickname"] += " the Second"

        paths = []
        for name, document in (("before.json", before), ("after.json", after)):
            path = tmp_path / name
            with path.open("w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False)
            paths.append(path)
        return paths

    def _index(self, names):
        index = ChannelIndex()
        for i, name in enumerate(names, start=1):
            index.add(str(i), name, "test")
        return index

    def test_without_it_a_rename_reads_as_hundreds_of_edits(self, tmp_path):
        before, after = self._pair(tmp_path)
        db = tmp_path / "plain.db"

        import_files(db, before)
        import_files(db, after)

        assert rows(db, "SELECT COUNT(*) FROM message_history")[0][0] > 0

    def test_with_it_the_archive_does_not_move(self, tmp_path):
        """Each export is matched against names of its own vintage, as a real run would be."""
        before, after = self._pair(tmp_path)
        original = list(self.RENAMES)
        renamed = list(self.RENAMES.values())

        db = tmp_path / "fixed.db"
        self._import(db, before, self._index(original))
        settled = snapshot(db)

        self._import(db, after, self._index(renamed))

        assert rows(db, "SELECT COUNT(*) FROM message_history") == [(0,)]
        problems = diff(settled, snapshot(db))
        assert not problems, "\n".join(problems)

    def test_and_what_is_stored_is_the_raw_form(self, tmp_path):
        before, _ = self._pair(tmp_path)
        db = tmp_path / "raw.db"
        self._import(db, before, self._index(self.RENAMES))

        bodies = [b for (b,) in rows(db, "SELECT content FROM messages") if b]
        assert any("<@" in b for b in bodies)
        assert not any("@raigan#7701" in b for b in bodies)

    def _import(self, db, path, index):
        from dce2sql.adapters import create
        from dce2sql.importer import Importer

        adapter = create("sqlite", str(db))
        adapter.connect()
        try:
            adapter.create_schema()
            source = Source.of(path)
            doc = Document(open_document(source), Unresolver(index))
            result = Importer(adapter).import_document(doc, source)
            assert result.error is None, result.error
        finally:
            adapter.close()

    def test_the_import_records_that_it_happened(self, tmp_path):
        before, _ = self._pair(tmp_path)
        db = tmp_path / "recorded.db"
        self._import(db, before, self._index(self.RENAMES))

        assert rows(db, "SELECT unresolved FROM imports") == [(1,)]
