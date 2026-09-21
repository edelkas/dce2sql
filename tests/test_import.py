"""End-to-end imports.

The headline assertion is that the *shape of the export does not reach the database*: the same
conversation exported six different ways has to land as the same rows.  Everything else here is
a property of the merge policy -- re-importing changes nothing, a poorer export never erases a
richer one, an edit is recorded rather than overwritten.
"""

from __future__ import annotations

import json

import pytest
from conftest import fixture
from support import diff, import_files, regressions, rows, snapshot, with_rich_json

#: The four exports carrying the full field set, grouped by how they write people.  Within a
#: group the files differ only in whether entities are inlined or referenced from lookup
#: tables, which is supposed to make no difference at all.
MERGED = ["ext.json", "extnorm.json"]
SPLIT = ["split.json", "splitnorm.json"]
EXTENDED_SHAPES = MERGED + SPLIT


@pytest.fixture
def imported(tmp_path):
    """Import one fixture into a database of its own."""

    def _import(name, *extra):
        db = tmp_path / f"{name.replace('.json', '')}-{len(extra)}.db"
        import_files(db, fixture(name), *[fixture(e) for e in extra])
        return db

    return _import


class TestShapeEquivalence:
    """The same messages, exported several ways, become the same database.

    Three columns fall short of equality, and all three are losses in the *merged* shape
    rather than disagreements between the two:

    ``members.display``
        a merged person object has already collapsed nickname -> display name -> username into
        one string, so someone whose nickname equals their global display name is
        indistinguishable from someone who set none.
    ``users.avatar``, ``users.banner``
        a member's guild-specific image stands *in place of* the global one in a merged object
        rather than beside it, so for anyone who has set one the global image is invisible.
    """

    #: What a merged export cannot state. It must be silent about these, never contradictory.
    MERGED_CANNOT_SAY = {"display", "avatar", "banner"}

    @pytest.mark.parametrize("group", [MERGED, SPLIT], ids=["merged", "split"])
    def test_normalizing_changes_nothing(self, imported, group):
        # --normal moves entities into lookup tables and references them by ID. It is a
        # different way of writing the same document, and must not survive the import.
        problems = diff(snapshot(imported(group[0])), snapshot(imported(group[1])))
        assert not problems, "\n".join(problems)

    @pytest.mark.parametrize("merged", MERGED)
    @pytest.mark.parametrize("split", SPLIT)
    def test_splitting_changes_nothing_it_does_not_have_to(self, imported, merged, split):
        """Merged and split agree everywhere a merged export can speak at all."""
        left = snapshot(imported(merged), skip_columns=self.MERGED_CANNOT_SAY)
        right = snapshot(imported(split), skip_columns=self.MERGED_CANNOT_SAY)

        problems = diff(left, right)
        assert not problems, "\n".join(problems)

    @pytest.mark.parametrize("merged", MERGED)
    @pytest.mark.parametrize("split", SPLIT)
    def test_and_what_it_cannot_say_it_leaves_empty(self, imported, merged, split):
        """Where they differ, the merged one is silent -- never contradictory."""
        left, right = imported(merged), imported(split)

        # A merged export omits the role list for reaction authors, so in general it can know
        # less here; it must never know something different
        merged_roles = set(rows(left, "SELECT user_id, role_id FROM user_roles"))
        split_roles = set(rows(right, "SELECT user_id, role_id FROM user_roles"))
        assert merged_roles <= split_roles

        for table, columns in (("members", ("display",)), ("users", ("avatar", "banner"))):
            key = "user_id" if table == "members" else "id"
            selected = ", ".join((key, *columns))
            known = {r[0]: r[1:] for r in rows(right, f"SELECT {selected} FROM {table}")}
            for row in rows(left, f"SELECT {selected} FROM {table}"):
                for i, column in enumerate(columns):
                    assert row[1 + i] in (known[row[0]][i], None), f"{table}.{column}"

    @pytest.mark.parametrize("merged", MERGED)
    @pytest.mark.parametrize("split", SPLIT)
    def test_importing_both_converges_on_the_richer_one(self, tmp_path, merged, split):
        """Whichever order they arrive in, the archive ends up with everything."""
        reference = tmp_path / "reference.db"
        import_files(reference, fixture(split))

        for order in ((merged, split), (split, merged)):
            db = tmp_path / f"{order[0][0]}{order[1][0]}.db"
            import_files(db, fixture(order[0]), fixture(order[1]))
            problems = diff(snapshot(reference), snapshot(db))
            assert not problems, f"{order}\n" + "\n".join(problems)

    @pytest.mark.parametrize("poorer", ["upstream.json", "vanilla.json"])
    def test_vanilla_is_a_consistent_subset_of_extended(self, imported, poorer):
        """A vanilla export is a subset, never a different answer.

        It knows fewer people -- an extended export resolves the guild owner and can prove
        membership from a join date where a vanilla one can only infer it -- and it cannot fill
        the columns ``--extended`` adds.  What it *does* say has to agree exactly.
        """
        extended_only = {
            # guilds
            "description", "url", "banner", "splash", "boost_level", "boost_count", "owner_id",
            # channels
            "position", "members", "archived", "locked",
            # users and members: the nickname needs the global display name to separate it out
            "display", "joined_at", "boosting_since",
            # messages
            "components",
        }
        vanilla = snapshot(imported(poorer), skip_columns=extended_only)
        extended = snapshot(imported("ext.json"), skip_columns=extended_only)

        problems = regressions(vanilla, extended)
        assert not problems, "\n".join(problems)

    def test_messages_are_identical_across_all_shapes(self, imported):
        query = "SELECT id, type, timestamp, edited_timestamp, pinned, user_id, content FROM messages ORDER BY id"
        baseline = rows(imported("ext.json"), query)
        assert len(baseline) > 0
        for name in EXTENDED_SHAPES[1:] + ["vanilla.json", "upstream.json"]:
            assert rows(imported(name), query) == baseline, name


#: Every message export of the same conversation, richest and poorest alike.
ALL_MESSAGE_SHAPES = EXTENDED_SHAPES + ["vanilla.json", "upstream.json", "noreact.json"]


class TestMixedArchives:
    """An archive built from several kinds of export must not depend on their order.

    This is the strictest form of the promise, and the easiest one to break: any rule of the
    form "a poorer export may fill in what a richer one left empty" makes the last file read
    decide, because emptiness is a claim too.
    """

    def test_order_does_not_matter(self, tmp_path):
        forward, backward = tmp_path / "forward.db", tmp_path / "backward.db"
        import_files(forward, *[fixture(n) for n in ALL_MESSAGE_SHAPES])
        import_files(backward, *[fixture(n) for n in reversed(ALL_MESSAGE_SHAPES)])

        problems = diff(snapshot(forward), snapshot(backward))
        assert not problems, "\n".join(problems)

    def test_running_it_all_again_settles(self, tmp_path):
        once, twice = tmp_path / "once.db", tmp_path / "twice.db"
        files = [fixture(n) for n in ALL_MESSAGE_SHAPES]
        import_files(once, *files)
        import_files(twice, *files)
        import_files(twice, *files)

        problems = diff(snapshot(once), snapshot(twice))
        assert not problems, "\n".join(problems)

    def test_a_roster_in_the_middle_changes_nothing_about_that(self, tmp_path):
        a, b = tmp_path / "a.db", tmp_path / "b.db"
        files = [fixture(n) for n in ALL_MESSAGE_SHAPES]
        roster = fixture("roster.json")

        import_files(a, *files, roster)
        import_files(b, roster, *files)

        problems = diff(snapshot(a), snapshot(b))
        assert not problems, "\n".join(problems)


class TestIdempotency:
    def test_reimporting_a_file_changes_nothing(self, tmp_path):
        once, twice = tmp_path / "once.db", tmp_path / "twice.db"
        import_files(once, fixture("split.json"))
        import_files(twice, fixture("split.json"), fixture("split.json"))

        problems = diff(snapshot(once), snapshot(twice))
        assert not problems, "\n".join(problems)

    def test_but_it_is_recorded_in_imports(self, tmp_path):
        db = tmp_path / "archive.db"
        import_files(db, fixture("split.json"), fixture("split.json"))

        entries = rows(db, "SELECT sha1, new_message_count FROM imports ORDER BY id")
        assert len(entries) == 2
        assert entries[0][0] == entries[1][0], "the same file, so the same digest"
        assert entries[1][1] == 0, "the second pass found nothing new"


class TestMergePolicy:
    @pytest.mark.parametrize("poorer", ["upstream.json", "vanilla.json", "noreact.json"])
    def test_a_poorer_export_never_erases_a_richer_one(self, tmp_path, poorer):
        """A later, less informative export may add, but must never take away.

        Filling a column the richer export left empty is fine: a vanilla file does know *a*
        name for a member, just not which kind of name it is.  Changing or dropping a value
        that was already there is the failure being looked for.
        """
        db = tmp_path / "archive.db"
        import_files(db, fixture("split.json"))
        before = snapshot(db)

        import_files(db, fixture(poorer))
        problems = regressions(before, snapshot(db))
        assert not problems, "\n".join(problems)

    def test_a_richer_export_fills_in_what_was_missing(self, tmp_path):
        """Importing split after vanilla must reach the same place as split alone."""
        split_only = tmp_path / "a.db"
        vanilla_first = tmp_path / "b.db"
        import_files(split_only, fixture("split.json"))
        import_files(vanilla_first, fixture("upstream.json"), fixture("split.json"))

        problems = diff(snapshot(split_only), snapshot(vanilla_first))
        assert not problems, "\n".join(problems)

    def test_an_edit_is_recorded_rather_than_overwritten(self, tmp_path):
        db = tmp_path / "archive.db"
        import_files(db, fixture("split.json"))

        original = rows(db, "SELECT id, content, timestamp FROM messages ORDER BY id LIMIT 1")[0]
        message_id, old_content, old_timestamp = original

        edited = _edit_message(tmp_path, "split.json", message_id, "a later version")
        import_files(db, edited)

        assert rows(db, "SELECT content FROM messages WHERE id = ?", (message_id,)) == [
            ("a later version",)
        ]
        # The superseded version is dated by when it was posted, this being the first edit
        assert rows(
            db, "SELECT timestamp, content FROM message_history WHERE message_id = ?",
            (message_id,),
        ) == [(old_timestamp, old_content)]

    def test_an_unchanged_reimport_writes_no_history(self, tmp_path):
        db = tmp_path / "archive.db"
        import_files(db, fixture("split.json"), fixture("split.json"))
        assert rows(db, "SELECT COUNT(*) FROM message_history") == [(0,)]


class TestIncrementalRanges:
    def test_two_halves_equal_the_whole(self, tmp_path):
        """Splitting an export by date range is the workflow this has to survive."""
        first, second = _halve(tmp_path, "ext.json")

        whole, pieces = tmp_path / "whole.db", tmp_path / "pieces.db"
        import_files(whole, fixture("ext.json"))
        import_files(pieces, first, second)

        problems = diff(snapshot(whole), snapshot(pieces))
        assert not problems, "\n".join(problems)

    def test_overlapping_ranges_do_not_duplicate(self, tmp_path):
        first, second = _halve(tmp_path, "ext.json")
        db = tmp_path / "archive.db"
        import_files(db, first, second, fixture("ext.json"))

        whole = tmp_path / "whole.db"
        import_files(whole, fixture("ext.json"))
        assert not diff(snapshot(db), snapshot(whole))


class TestReactions:
    def test_totals_survive_an_export_without_reaction_users(self, tmp_path):
        """``--reaction-users false`` keeps the totals even though it loses the names."""
        with_users, without = tmp_path / "a.db", tmp_path / "b.db"
        import_files(with_users, fixture("split.json"))
        import_files(without, fixture("noreact.json"))

        query = (
            "SELECT message_id, emoji_id, SUM(count) FROM reactions "
            "GROUP BY message_id, emoji_id ORDER BY message_id, emoji_id"
        )
        assert rows(without, query) == rows(with_users, query)

    def test_the_names_are_only_there_when_the_export_had_them(self, tmp_path):
        with_users, without = tmp_path / "a.db", tmp_path / "b.db"
        import_files(with_users, fixture("split.json"))
        import_files(without, fixture("noreact.json"))

        named = "SELECT COUNT(*) FROM reactions WHERE user_id IS NOT NULL"
        assert rows(with_users, named)[0][0] > 0
        assert rows(without, named) == [(0,)]

    def test_naming_them_later_replaces_the_anonymous_total(self, tmp_path):
        db = tmp_path / "archive.db"
        import_files(db, fixture("noreact.json"), fixture("split.json"))

        reference = tmp_path / "reference.db"
        import_files(reference, fixture("split.json"))
        assert not diff(snapshot(db), snapshot(reference))


class TestRichJson:
    """The two columns kept as JSON rather than flattened into tables.

    Neither appears in the fixtures -- no message in those two days carried a components tree
    or forwarded anything -- so the documents are built here instead of hoping for one.
    """

    def test_a_components_tree_round_trips(self, tmp_path):
        from support import COMPONENTS

        path, with_components, _ = with_rich_json(tmp_path)
        db = tmp_path / "archive.db"
        import_files(db, path)

        stored = rows(db, "SELECT components FROM messages WHERE id = ?", (with_components,))
        assert json.loads(stored[0][0]) == COMPONENTS

    def test_a_forward_is_kept_whole(self, tmp_path):
        """It has no ID and no author, so there is nothing to key its children by."""
        from support import FORWARD

        path, _, with_forward = with_rich_json(tmp_path)
        db = tmp_path / "archive.db"
        import_files(db, path)

        stored = rows(
            db, "SELECT forwarded, reference_id, reference_type FROM messages WHERE id = ?",
            (with_forward,),
        )[0]
        assert json.loads(stored[0])["content"] == FORWARD["content"]
        assert stored[1] == 1234567890123456789
        assert stored[2] == 1, "Forward"

    def test_and_neither_reads_as_an_edit_on_re_import(self, tmp_path):
        path, _, _ = with_rich_json(tmp_path)
        db = tmp_path / "archive.db"
        import_files(db, path)
        before = snapshot(db)

        import_files(db, path)
        problems = diff(before, snapshot(db))
        assert not problems, chr(10).join(problems)


class TestLongValues:
    def test_a_url_far_longer_than_any_limit_survives(self, tmp_path):
        """From a real export: a CDN link with an essay appended as a ?comment= parameter."""
        monstrous = (
            "https://cdn.discordapp.com/attachments/1/2/a.gif?comment="
            + "why_would_anyone_do_this_" * 100
        )
        assert len(monstrous) > 2000

        def give_it_an_embed(document):
            document["messages"][0]["embeds"] = [
                {"title": "", "url": monstrous, "timestamp": None, "description": "",
                 "images": [], "fields": [], "inlineEmojis": []}
            ]

        path = _doctor(tmp_path, "ext.json", give_it_an_embed, "monstrous.json")
        db = tmp_path / "archive.db"
        import_files(db, path)

        stored = rows(db, "SELECT url FROM embeds WHERE ordinal = 0 ORDER BY LENGTH(url) DESC")
        assert stored[0][0] == monstrous, "stored whole, not truncated"

    def test_an_over_long_bounded_value_fails_the_file_and_says_why(self, tmp_path):
        """Failing beats truncating: a truncated name looks like data."""
        from dce2sql.adapters import create
        from dce2sql.documents import Document
        from dce2sql.importer import Importer
        from dce2sql.reader import Source, open_document

        def absurd_name(document):
            document["messages"][0]["author"]["name"] = "n" * 40

        path = _doctor(tmp_path, "ext.json", absurd_name, "absurd.json")
        db = tmp_path / "archive.db"

        adapter = create("sqlite", str(db))
        adapter.connect()
        try:
            adapter.create_schema()
            source = Source.of(path)
            result = Importer(adapter).import_document(
                Document(open_document(source)), source
            )
        finally:
            adapter.close()

        assert result.error is not None
        assert "users.name" in result.error and "at most 32" in result.error
        # ...and the file was rolled back rather than half-written
        assert rows(db, "SELECT COUNT(*) FROM messages") == [(0,)]


class TestVolatileUrls:
    def test_a_re_signed_attachment_url_is_not_an_edit(self, tmp_path):
        """Discord signs CDN links per export, with a signature that expires within a day.

        Re-importing an archive would otherwise rewrite every attachment row and leave
        ``updated_at`` meaning nothing.
        """
        db = tmp_path / "archive.db"
        import_files(db, fixture("ext.json"))
        before = rows(db, "SELECT id, url, updated_at FROM attachments ORDER BY id")
        assert before, "the fixture should carry attachments"

        resigned = _resign(tmp_path, "ext.json")
        import_files(db, resigned)

        after = rows(db, "SELECT id, url, updated_at FROM attachments ORDER BY id")
        assert after == before

    def test_but_a_different_file_is(self, tmp_path):
        db = tmp_path / "archive.db"
        import_files(db, fixture("ext.json"))

        def rename(document):
            for message in document["messages"]:
                for attachment in message["attachments"]:
                    attachment["url"] = attachment["url"].replace(".png", ".jpg")

        moved = _doctor(tmp_path, "ext.json", rename, "moved.json")
        import_files(db, moved)

        assert rows(db, "SELECT COUNT(*) FROM attachments WHERE url LIKE '%.jpg%'")[0][0] > 0


class TestChannels:
    def test_a_parent_channel_is_created_from_the_category_keys(self, imported):
        db = imported("ext.json")
        parents = rows(
            db,
            "SELECT c.name, p.name, p.type FROM channels c JOIN channels p "
            "ON c.parent_id = p.id",
        )
        assert parents, "the exported channel should have a parent"
        # Nothing but the name is known about a parent seen only this way
        assert all(p_type == 4 for _, _, p_type in parents)

    def test_a_real_export_corrects_a_stub(self, tmp_path):
        """A parent stubbed as a category must give way to its own export."""
        db = tmp_path / "archive.db"
        import_files(db, fixture("ext.json"))

        channel_id, parent_id = rows(
            db, "SELECT id, parent_id FROM channels WHERE parent_id IS NOT NULL LIMIT 1"
        )[0]
        promoted = _retype_channel(tmp_path, "ext.json", parent_id, "GuildForum")
        import_files(db, promoted)

        assert rows(db, "SELECT type FROM channels WHERE id = ?", (parent_id,)) == [(15,)]
        # ...and the child is untouched by its parent being corrected
        assert rows(db, "SELECT parent_id FROM channels WHERE id = ?", (channel_id,)) == [
            (parent_id,)
        ]


class TestUsersAndMembers:
    def test_the_global_display_name_comes_from_the_user_object(self, imported):
        """It is whatever the export put on the user, and never anything guild-specific."""
        document = _load("split.json")
        expected = {
            int(m["author"]["id"]): m["author"]["displayName"]
            for m in document["messages"]
        }

        stored = dict(rows(imported("split.json"), "SELECT id, display FROM users"))
        for user_id, display in expected.items():
            assert stored[user_id] == display

    def test_an_export_that_cannot_know_it_leaves_it_empty(self, imported):
        # Guessing is exactly what the user/member split exists to avoid
        assert rows(
            imported("upstream.json"),
            "SELECT COUNT(*) FROM users WHERE display IS NOT NULL",
        ) == [(0,)]

    def test_roles_are_attached_to_the_members_that_wear_them(self, imported):
        db = imported("split.json")
        assert rows(db, "SELECT COUNT(*) FROM user_roles")[0][0] > 0
        orphans = rows(
            db,
            "SELECT COUNT(*) FROM user_roles ur LEFT JOIN roles r ON r.id = ur.role_id "
            "WHERE r.id IS NULL",
        )
        assert orphans == [(0,)]

    def test_a_deleted_account_flags_the_user_without_losing_the_author(self, tmp_path):
        db = tmp_path / "archive.db"
        import_files(db, fixture("split.json"))

        message_id, author_id = rows(
            db, "SELECT id, user_id FROM messages WHERE user_id IS NOT NULL ORDER BY id LIMIT 1"
        )[0]

        reassigned = _reassign_author(tmp_path, "split.json", message_id)
        import_files(db, reassigned)

        assert rows(db, "SELECT user_id FROM messages WHERE id = ?", (message_id,)) == [
            (author_id,)
        ], "the real author is kept"
        assert rows(db, "SELECT deleted FROM users WHERE id = ?", (author_id,)) == [(1,)]


class TestRoster:
    def test_a_roster_enriches_members_without_disturbing_users(self, tmp_path):
        db = tmp_path / "archive.db"
        import_files(db, fixture("split.json"))
        before = rows(db, "SELECT id, name, display FROM users ORDER BY id")

        import_files(db, fixture("roster.json"))

        after = {u[0]: u for u in rows(db, "SELECT id, name, display FROM users ORDER BY id")}
        for user in before:
            assert after[user[0]] == user, "a roster must not rewrite a user it also names"

        assert rows(db, "SELECT COUNT(*) FROM members")[0][0] >= len(before)
        assert rows(db, "SELECT COUNT(*) FROM members WHERE joined_at IS NOT NULL")[0][0] > 0


class TestTypes:
    def test_every_type_in_the_corpus_resolved(self, imported):
        for name in EXTENDED_SHAPES + ["vanilla.json", "upstream.json"]:
            db = imported(name)
            assert rows(db, "SELECT COUNT(*) FROM messages WHERE type IS NULL") == [(0,)], name
            assert rows(db, "SELECT COUNT(*) FROM channels WHERE type IS NULL") == [(0,)], name

    def test_types_resolve_against_the_seed_tables(self, imported):
        db = imported("ext.json")
        assert rows(
            db,
            "SELECT COUNT(*) FROM messages m LEFT JOIN message_types t ON t.id = m.type "
            "WHERE t.id IS NULL",
        ) == [(0,)]
        assert rows(
            db,
            "SELECT COUNT(*) FROM channels c LEFT JOIN channel_types t ON t.id = c.type "
            "WHERE t.id IS NULL",
        ) == [(0,)]


# ------------------------------------------------------------------------------------
# Fixture surgery
# ------------------------------------------------------------------------------------


def _load(name):
    with fixture(name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _write(tmp_path, name, document):
    path = tmp_path / name
    with path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False)
    return path


def _halve(tmp_path, name):
    """Split an inline export into two adjacent ranges, as a date-split run would produce."""
    document = _load(name)
    messages = document["messages"]
    middle = len(messages) // 2

    first = dict(document, messages=messages[:middle], messageCount=middle)
    second = dict(document, messages=messages[middle:], messageCount=len(messages) - middle)
    return _write(tmp_path, "first.json", first), _write(tmp_path, "second.json", second)


def _edit_message(tmp_path, name, message_id, content):
    document = _load(name)
    for message in document["messages"]:
        if int(message["id"]) == message_id:
            message["content"] = content
            break
    else:  # pragma: no cover
        raise AssertionError(f"{message_id} not in {name}")
    return _write(tmp_path, "edited.json", document)


def _reassign_author(tmp_path, name, message_id):
    """Mimic what Discord does to the messages of a deleted account."""
    document = _load(name)
    for message in document["messages"]:
        if int(message["id"]) == message_id:
            author = message["author"]
            author["id"] = "456226577798135808"
            author["name"] = "Deleted User"
            break
    else:  # pragma: no cover
        raise AssertionError(f"{message_id} not in {name}")
    return _write(tmp_path, "deleted.json", document)


def _doctor(tmp_path, name, mutate, as_name):
    document = _load(name)
    mutate(document)
    return _write(tmp_path, as_name, document)


def _resign(tmp_path, name):
    """Re-sign every CDN link, the way a fresh export of the same messages would."""
    import re

    def resign(document):
        for message in document["messages"]:
            for attachment in message["attachments"]:
                attachment["url"] = re.sub(
                    r"(ex|is|hm)=[0-9a-f]+", lambda m: f"{m.group(1)}=deadbeef", attachment["url"]
                )

    return _doctor(tmp_path, name, resign, "resigned.json")


def _retype_channel(tmp_path, name, channel_id, dce_kind):
    """Turn an export into one *of* the given channel, so the stub gets corrected."""
    document = _load(name)
    document["channel"] = {
        "id": str(channel_id),
        "type": dce_kind,
        "categoryId": None,
        "category": None,
        "name": document["channel"]["category"],
        "topic": None,
    }
    document["messages"] = []
    document["messageCount"] = 0
    return _write(tmp_path, "parent.json", document)
