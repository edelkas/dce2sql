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
    what the inverter is trying to reconstruct -- byte for byte, with nothing normalized away
    on either side. Anything short of a perfect match is the inverter approximating, which is
    the one thing it must not do.
    """

    def _unresolved(self):
        index = ChannelIndex()
        index.add_listing(fixture("channels.txt"))
        doc = Document(open_document(Source.of(fixture("ext.json"))), Unresolver(index))
        return {m["id"]: m["content"] for m in doc.messages()}

    def test_it_reproduces_what_dce_writes_raw(self):
        raw = json.loads(fixture("raw.json").read_text(encoding="utf-8"))
        theirs = {m["id"]: m["content"] for m in raw["messages"]}
        ours = self._unresolved()

        assert ours.keys() == theirs.keys()
        mismatched = [(i, theirs[i], ours[i]) for i in theirs if theirs[i] != ours[i]]
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
        assert len(re.findall(r"<a?:\w+:\d+>", bodies)) >= 20


class TestEmoji:
    """Custom emoji, the fourth kind, recovered from the message's own ``inlineEmojis``.

    Same bug as the rest: DCE writes ``<:goldheart:711809267547766806>`` as ``:goldheart:``,
    so renaming the emoji rewrites every message that ever used it.
    """

    STATIC = {
        "id": "711809267547766806",
        "name": "goldheart",
        "code": "goldheart",
        "isAnimated": False,
    }
    ANIMATED = {
        "id": "434937930247700482",
        "name": "victory",
        "code": "victory",
        "isAnimated": True,
    }
    #: A standard Unicode emoji, which DCE writes as the character itself -- already raw
    STANDARD = {"id": "", "name": "\U0001f642", "code": ":slight_smile:", "isAnimated": False}

    def test_a_custom_emoji_becomes_its_raw_form(self, unresolver):
        assert unresolver.unresolve("nice :goldheart:", emojis=[self.STATIC]) == (
            "nice <:goldheart:711809267547766806>"
        )

    def test_an_animated_one_keeps_its_marker(self, unresolver):
        assert unresolver.unresolve(":victory:", emojis=[self.ANIMATED]) == (
            "<a:victory:434937930247700482>"
        )

    def test_two_of_them_side_by_side(self, unresolver):
        """The commonest shape there is, and the one with no separator to lean on."""
        assert unresolver.unresolve(
            ":victory::goldheart:", emojis=[self.ANIMATED, self.STATIC]
        ) == "<a:victory:434937930247700482><:goldheart:711809267547766806>"

    def test_a_standard_emoji_is_left_alone(self, unresolver):
        # It has no ID and no raw form: the character *is* what Discord stores
        assert unresolver.unresolve("\U0001f642 hello", emojis=[self.STANDARD]) == (
            "\U0001f642 hello"
        )

    def test_one_the_message_does_not_use_is_left_alone(self, unresolver):
        """Scoped to the body's own emoji, exactly as user mentions are to its own mentions."""
        assert unresolver.unresolve("typing :goldheart: as text", emojis=[]) == (
            "typing :goldheart: as text"
        )

    def test_an_already_raw_body_is_untouched(self, unresolver):
        """The guard that matters: '<' before a token means it is already raw."""
        for raw in (
            "<:goldheart:711809267547766806>",
            "<a:victory:434937930247700482>",
            "<a:victory:434937930247700482><:goldheart:711809267547766806>",
        ):
            assert unresolver.unresolve(raw, emojis=[self.STATIC, self.ANIMATED]) == raw

    def test_a_colon_inside_a_word_is_not_an_emoji(self, unresolver):
        # Conservative by design: this would have come from 'mynote<:goldheart:id>', so the
        # match is missed rather than risked. Missing one is safe; inventing one is not.
        assert unresolver.unresolve("mynote:goldheart:", emojis=[self.STATIC]) == (
            "mynote:goldheart:"
        )

    def test_but_a_colon_ending_a_word_is(self, unresolver):
        # 'note:<:goldheart:id>' renders as 'note::goldheart:'
        assert unresolver.unresolve("note::goldheart:", emojis=[self.STATIC]) == (
            "note:<:goldheart:711809267547766806>"
        )

    def test_it_is_counted_separately(self, unresolver):
        unresolver.unresolve(":goldheart:", emojis=[self.STATIC])
        assert unresolver.recovered["emoji"] == 1

    def test_an_embed_uses_its_own_emoji_list(self, tmp_path):
        """An embed carries its own ``inlineEmojis``, and its text goes through the same
        formatter a body does."""
        document = {
            "mod": {"extended": True},
            "guild": {"id": "1", "name": "g"},
            "channel": {"id": "2", "type": "GuildTextChat", "name": "c"},
            "messages": [
                {
                    "id": "10",
                    "type": "Default",
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "content": "",
                    "author": {"id": "3", "name": "a", "discriminator": "0000",
                               "nickname": "a", "isBot": False, "avatarUrl": "u"},
                    "embeds": [
                        {
                            "title": "",
                            "description": "look :goldheart:",
                            "images": [],
                            "fields": [],
                            "inlineEmojis": [self.STATIC],
                        }
                    ],
                }
            ],
        }
        path = tmp_path / "embed.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)

        doc = Document(open_document(Source.of(path)), Unresolver(ChannelIndex()))
        message = next(iter(doc.messages()))
        assert message["embeds"][0]["description"] == (
            "look <:goldheart:711809267547766806>"
        )


class TestNamedByTheExport:
    """``channelMentions`` and ``roleMentions``, which ``--extended`` writes per message.

    These are the best source there is: named by the very export whose body is being read, so
    the names are contemporaneous by construction, and scoped to one message rather than to a
    pooled index that has to serve everything. With them, a channel mention needs no
    ``--channels`` listing and no database at all.
    """

    CHANNEL = {
        "id": "218819289266913281",
        "type": "GuildTextChat",
        "categoryId": "449367495544143892",
        "category": "Support",
        "name": "support",
    }
    ROLE = {"id": "198374136001593344", "name": "Moderator", "color": "#E67E22", "position": 51}

    def test_a_channel_it_names_needs_no_index_at_all(self):
        empty = Unresolver(ChannelIndex())
        assert empty.unresolve("pinned in #support", channels=[self.CHANNEL]) == (
            "pinned in <#218819289266913281>"
        )

    def test_a_role_it_names_likewise(self):
        empty = Unresolver(ChannelIndex())
        assert empty.unresolve("ask a @Moderator", roles=[self.ROLE]) == (
            "ask a <@&198374136001593344>"
        )

    def test_a_voice_channel_it_names_loses_its_marker(self):
        empty = Unresolver(ChannelIndex())
        voice = {**self.CHANNEL, "type": "GuildVoiceChat", "name": "General Voice"}
        assert empty.unresolve("in #General Voice [voice]", channels=[voice]) == (
            "in <#218819289266913281>"
        )

    def test_the_message_beats_the_pooled_index(self, unresolver):
        """The index may hold a name from another moment; the message never does."""
        stale = {**self.CHANNEL, "id": "999", "name": "general"}
        assert unresolver.unresolve("see #general", channels=[stale]) == "see <#999>"

    def test_it_is_still_timid(self):
        empty = Unresolver(ChannelIndex())
        assert empty.unresolve("in #support-tickets", channels=[self.CHANNEL]) == (
            "in #support-tickets"
        )

    def test_a_document_reads_them_however_they_are_written(self, tmp_path):
        """Inline in one shape, references into a root table in the other."""
        inline = {
            "mod": {"extended": True},
            "guild": {"id": "1", "name": "g"},
            "channel": {"id": "2", "type": "GuildTextChat", "name": "c"},
            "messages": [
                {
                    "id": "10",
                    "type": "Default",
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "content": "see #support and @Moderator",
                    "author": {"id": "3", "name": "a", "discriminator": "0000",
                               "nickname": "a", "isBot": False, "avatarUrl": "u"},
                    "channelMentions": [self.CHANNEL],
                    "roleMentions": [self.ROLE],
                }
            ],
        }
        normalized = {
            **inline,
            "mod": {"extended": True, "normal": True},
            "messages": [
                {**inline["messages"][0],
                 "authorId": "3",
                 "channelMentionIds": [self.CHANNEL["id"]],
                 "roleMentionIds": [self.ROLE["id"]]}
            ],
            "users": [inline["messages"][0]["author"]],
            "channels": [self.CHANNEL],
            "roles": [self.ROLE],
        }
        for key in ("author", "channelMentions", "roleMentions"):
            normalized["messages"][0].pop(key, None)

        for name, document in (("inline.json", inline), ("normal.json", normalized)):
            path = tmp_path / name
            with path.open("w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False)

            doc = Document(open_document(Source.of(path)), Unresolver(ChannelIndex()))
            message = next(iter(doc.messages()))

            assert [c["id"] for c in message["channelMentions"]] == [self.CHANNEL["id"]], name
            assert [r["id"] for r in message["roleMentions"]] == [self.ROLE["id"]], name
            assert message["content"] == (
                "see <#218819289266913281> and <@&198374136001593344>"
            ), name

    def test_they_are_stored(self, tmp_path):
        document = {
            "mod": {"extended": True},
            "guild": {"id": "1", "name": "g"},
            "channel": {"id": "2", "type": "GuildTextChat", "name": "c"},
            "messages": [
                {
                    "id": "10",
                    "type": "Default",
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "content": "see #support and @Moderator",
                    "author": {"id": "3", "name": "a", "discriminator": "0000",
                               "nickname": "a", "isBot": False, "avatarUrl": "u"},
                    "channelMentions": [self.CHANNEL],
                    "roleMentions": [self.ROLE],
                }
            ],
        }
        path = tmp_path / "doc.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)

        db = tmp_path / "archive.db"
        import_files(db, path)

        assert rows(db, "SELECT message_id, channel_id FROM channel_mentions") == [
            (10, 218819289266913281)
        ]
        assert rows(db, "SELECT message_id, role_id FROM role_mentions") == [
            (10, 198374136001593344)
        ]
        # A mention is often the only place an archive hears of a channel at all
        assert rows(
            db, "SELECT name, type FROM channels WHERE id = 218819289266913281"
        ) == [("support", 0)]
        assert rows(db, "SELECT name FROM roles WHERE id = 198374136001593344") == [
            ("Moderator",)
        ]


class TestTheWholePoint:
    """A rename must stop looking like an edit."""

    def _pair(self, tmp_path):
        """The same 230 messages before and after somebody renamed things.

        A year later: every channel the bodies mention has been renamed, and everyone's
        nickname has gained a suffix. The renames are applied to the mention arrays as well as
        to the bodies, because that is what a real re-export produces -- the export names
        things as they are at the time, not as they were.
        """
        with fixture("ext.json").open(encoding="utf-8") as handle:
            before = json.load(handle)
        after = json.loads(json.dumps(before))

        renames = {
            c["name"]: c["name"] + "-renamed"
            for m in before["messages"]
            for c in m.get("channelMentions") or []
            if c.get("name")
        }
        assert renames, "the fixture should mention some channels"

        for message in after["messages"]:
            body = message.get("content") or ""
            for old, new in renames.items():
                body = body.replace(f"#{old}", f"#{new}")
            for p in message.get("mentions") or []:
                body = body.replace(f"@{p['nickname']}", f"@{p['nickname']} the Second")
            message["content"] = body

            for p in message.get("mentions") or []:
                p["nickname"] += " the Second"
            for c in message.get("channelMentions") or []:
                if c.get("name") in renames:
                    c["name"] = renames[c["name"]]

        self.renames = renames
        paths = []
        for name, document in (("before.json", before), ("after.json", after)):
            path = tmp_path / name
            with path.open("w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False)
            paths.append(path)
        return paths

    def _index(self, names):
        """A pooled index of the given names, keyed by the IDs the fixture really uses."""
        with fixture("ext.json").open(encoding="utf-8") as handle:
            document = json.load(handle)
        ids = {
            c["name"]: c["id"]
            for m in document["messages"]
            for c in m.get("channelMentions") or []
            if c.get("name")
        }

        index = ChannelIndex()
        for original, name in zip(ids, names):
            index.add(ids[original], name, "test")
        return index

    def test_without_it_a_rename_reads_as_hundreds_of_edits(self, tmp_path):
        before, after = self._pair(tmp_path)
        db = tmp_path / "plain.db"

        import_files(db, before)
        import_files(db, after)

        assert rows(db, "SELECT COUNT(*) FROM message_history")[0][0] > 0

    def test_with_it_the_archive_does_not_move(self, tmp_path):
        """Each export is matched against names of its own vintage, as a real run would be.

        The channels table is left out of the comparison, and only that: a channel really was
        renamed, so its own row really should change. What must not change is anything about
        the *messages*, which is the whole complaint — nobody edited them.
        """
        before, after = self._pair(tmp_path)

        db = tmp_path / "fixed.db"
        self._import(db, before, self._index(list(self.renames)))
        settled = snapshot(db)

        self._import(db, after, self._index(list(self.renames.values())))
        moved = snapshot(db)

        assert rows(db, "SELECT COUNT(*) FROM message_history") == [(0,)]

        settled.pop("channels")
        moved.pop("channels")
        problems = diff(settled, moved)
        assert not problems, "\n".join(problems)

    def test_and_the_rename_itself_is_recorded(self, tmp_path):
        """The bodies hold still; the channel's own row is where a rename belongs."""
        before, after = self._pair(tmp_path)

        db = tmp_path / "renamed.db"
        self._import(db, before, self._index(list(self.renames)))
        self._import(db, after, self._index(list(self.renames.values())))

        names = {n for (n,) in rows(db, "SELECT name FROM channels WHERE name IS NOT NULL")}
        assert set(self.renames.values()) <= names
        assert not (set(self.renames) & names), "the old names should have been updated away"

    def test_and_what_is_stored_is_the_raw_form(self, tmp_path):
        before, _ = self._pair(tmp_path)
        db = tmp_path / "raw.db"
        self._import(db, before, self._index(list(self.renames)))

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
        self._import(db, before, self._index(list(self.renames)))

        assert rows(db, "SELECT unresolved FROM imports") == [(1,)]
