"""Reading and normalizing exports, before any database is involved."""

from __future__ import annotations

import pytest
from conftest import fixture

from dce2sql import reader
from dce2sql.documents import Document, Mod, load
from dce2sql.reader import Source, open_document

ALL_SHAPES = [
    "upstream.json",
    "vanilla.json",
    "ext.json",
    "extnorm.json",
    "split.json",
    "splitnorm.json",
]


def doc(name) -> Document:
    return load(Source.of(fixture(name)))


class TestShapeDetection:
    def test_a_missing_mod_block_is_upstream_dce(self):
        assert Mod.parse({}).vanilla
        assert doc("upstream.json").mod.vanilla

    def test_the_fork_at_its_defaults_is_not_upstream_but_writes_the_same_shape(self):
        # It always writes the 'mod' block, which is the point of the block
        mod = doc("vanilla.json").mod
        assert not mod.vanilla
        assert not (mod.normal or mod.extended or mod.split_users)

    @pytest.mark.parametrize(
        "name,normal,extended,split",
        [
            ("ext.json", False, True, False),
            ("extnorm.json", True, True, False),
            ("split.json", False, True, True),
            ("splitnorm.json", True, True, True),
        ],
    )
    def test_the_mod_block_is_read_back(self, name, normal, extended, split):
        mod = doc(name).mod
        assert (mod.normal, mod.extended, mod.split_users) == (normal, extended, split)

    def test_an_option_that_postdates_an_export_reads_as_off(self):
        # ...except reaction users, which had no way of being off before the flag existed
        mod = Mod.parse({"mod": {"normal": False}})
        assert not mod.split_users and not mod.extended
        assert mod.reaction_users

    def test_a_roster_is_told_apart_from_a_message_export(self):
        assert doc("roster.json").is_roster
        assert not doc("split.json").is_roster

    def test_a_normalized_split_export_is_not_mistaken_for_a_roster(self):
        # It has a root 'members' table of its own, which is a lookup table rather than the
        # subject of the document
        document = doc("splitnorm.json")
        assert not document.is_roster
        assert document.kind == reader.MESSAGES


class TestRehydration:
    @pytest.mark.parametrize("name", ALL_SHAPES)
    def test_every_message_has_an_author_with_an_id(self, name):
        for message in doc(name).messages():
            assert message["author"] is not None
            assert message["author"].id

    def test_normalizing_does_not_change_what_a_message_says(self):
        inline = {m["id"]: m for m in doc("ext.json").messages()}
        normalized = {m["id"]: m for m in doc("extnorm.json").messages()}
        assert inline.keys() == normalized.keys()

        for message_id, a in inline.items():
            b = normalized[message_id]
            assert a["content"] == b["content"]
            assert a["author"].id == b["author"].id
            assert [p.id for p in a["mentions"]] == [p.id for p in b["mentions"]]
            assert [s.get("id") for s in a["stickers"]] == [
                s.get("id") for s in b["stickers"]
            ]

    def test_reaction_users_survive_the_lookup_tables(self):
        inline = {m["id"]: m for m in doc("ext.json").messages()}
        normalized = {m["id"]: m for m in doc("extnorm.json").messages()}
        for message_id, a in inline.items():
            for left, right in zip(a["reactions"], normalized[message_id]["reactions"]):
                assert left["count"] == right["count"]
                assert sorted(p.id for p in left["users"]) == sorted(
                    p.id for p in right["users"]
                )


class TestSplitting:
    def test_a_merged_export_yields_the_same_people_as_a_split_one(self):
        merged = {m["id"]: m["author"] for m in doc("ext.json").messages()}
        split = {m["id"]: m["author"] for m in doc("split.json").messages()}

        for message_id, a in merged.items():
            b = split[message_id]
            assert a.user.get("id") == b.user.get("id")
            assert a.user.get("displayName") == b.user.get("displayName")
            assert (a.member is None) == (b.member is None)
            if a.member is not None:
                assert a.member.get("color") == b.member.get("color")
                # The nickname is the one field a merged export cannot always recover: someone
                # whose nickname equals their global display name reads as having set none
                assert a.member.get("nickname") in (b.member.get("nickname"), None)

    def test_a_field_an_export_cannot_carry_is_absent_rather_than_null(self):
        """The difference is what stops a vanilla import erasing an extended one."""
        for message in doc("upstream.json").messages():
            assert "displayName" not in message["author"].user
            if message["author"].member is not None:
                assert "joinedAt" not in message["author"].member
            break

    def test_an_extended_export_states_them_explicitly(self):
        for message in doc("ext.json").messages():
            assert "displayName" in message["author"].user
            break


class TestReader:
    def test_streaming_and_whole_file_parsing_agree(self):
        """The same file, read both ways, has to come out the same.

        Only the threshold differs -- forcing it to zero puts a small fixture through the
        two-pass ijson path that normally only very large exports take.
        """
        source = Source.of(fixture("extnorm.json"))
        whole = open_document(source, threshold=1 << 40)
        streamed = open_document(source, threshold=0)

        assert whole.header == streamed.header
        assert list(whole.items()) == list(streamed.items())

    def test_the_header_carries_the_lookup_tables_written_after_the_messages(self):
        # They are in the postamble, so a streaming reader has to get to the end of the file
        # before the beginning of it means anything
        streamed = open_document(Source.of(fixture("extnorm.json")), threshold=0)
        assert streamed.header["users"]
        assert streamed.header["roles"]

    def test_the_declared_message_count_is_available_before_the_messages(self):
        # This is what makes the progress estimate honest within a file
        assert doc("ext.json").declared_count > 0

    def test_sha1_is_stable(self):
        source = Source.of(fixture("ext.json"))
        assert source.sha1 == Source.of(fixture("ext.json")).sha1
        assert len(source.sha1) == 40


class TestRoster:
    def test_entries_are_members_with_a_user_inside(self):
        people = list(doc("roster.json").members())
        assert people
        for person in people:
            assert person.member is not None
            assert person.user.get("id")
            assert person.user["id"] == person.member["userId"]

    def test_a_roster_carries_join_dates(self):
        assert any(p.member.get("joinedAt") for p in doc("roster.json").members())
