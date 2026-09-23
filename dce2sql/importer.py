"""Writing a normalized document into the database.

Everything here works on the canonical shape :mod:`dce2sql.documents` produces, so there is one
import path rather than one per combination of DCE flags.

Three rules run through all of it:

**Nothing is ever deleted.**  A record that stops appearing in the exports stays.  An edit does
not overwrite history, it adds to it.  A message's author is never reassigned, because the only
thing that ever reassigns one is the account being deleted, and that would lose the real name.

**A field the document does not carry is not written.**  DCE distinguishes a key that is absent
from a key whose value is null, and so does this: ``joinedAt: null`` means "not a member", while
no ``joinedAt`` key at all means "this export was made without --extended".  Only the first is
written.  This is what lets a vanilla export and an extended one be imported in either order
without the poorer one erasing what the richer one established.

**Re-importing is a no-op.**  Every write is either a merge that compares before updating, or an
insert guarded by a unique constraint.  Importing the same file twice changes nothing but the
``imports`` table, which records that it happened.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Iterable, Sequence

from . import schema
from .adapters.base import PARAM_CHUNK, Adapter
from .documents import Document, Person
from .enums import (
    CATEGORY_TYPE,
    resolve_channel_type,
    resolve_message_type,
    resolve_reference_type,
    resolve_sticker_format,
)
from .reader import Source
from .schema import Table
from .stats import FileStats, Stats
from .util import (
    DELETED_USER_ID,
    boolean,
    chunked,
    color,
    discriminator,
    snowflake,
    timestamp,
)

#: Messages held in memory at once.  The people, emoji and children of a batch are resolved and
#: written together, which is where the batching INSTRUCTIONS.md asks for actually pays off.
BATCH_SIZE = 500

#: Standard Unicode emoji have no Discord ID, so they get one from a counter starting at 1.
#: Real snowflakes are all far above this, so the two can share a key column safely.
SYNTHETIC_ID_CEILING = 1 << 32

#: Which embed field each resource row came from.
EMBED_SLOTS = ("thumbnail", "image", "video", "author_icon", "footer_icon")


class Importer:
    """Imports documents into one database.  Reusable across files within a run."""

    def __init__(
        self, adapter: Adapter, stats: Stats | None = None, progress=None
    ) -> None:
        self.db = adapter
        self.stats = stats or Stats()
        self.progress = progress
        #: Member columns the document in hand can only approximate; see _member_insert_only
        self._insert_only: frozenset[str] = frozenset()
        self._standard_emojis: dict[str, int] | None = None
        self._next_emoji_id = 1

    # ----------------------------------------------------------------------------------
    # Entry point
    # ----------------------------------------------------------------------------------

    def import_document(self, doc: Document, source: Source) -> FileStats:
        """Import one file, atomically.  A failure leaves the database untouched."""
        started = time.time()
        now = int(started)
        fs = FileStats(path=str(source.path), shape=doc.describe(), bytes=source.size)

        try:
            doc.validate()
            if doc.is_roster:
                self._import_roster(doc, fs, now)
            else:
                self._import_messages(doc, fs, now)
            self._record_import(doc, source, fs, now)
            self.db.commit()
        except Exception as exc:  # noqa: BLE001 -- reported per file, never fatal to the run
            self.db.rollback()
            fs.error = f"{type(exc).__name__}: {exc}"

        self.stats.record(fs)
        return fs

    # ----------------------------------------------------------------------------------
    # Message exports
    # ----------------------------------------------------------------------------------

    def _member_insert_only(self, doc: Document) -> frozenset[str]:
        """Member columns this document may set when creating a row, but never change after.

        One column qualifies: the nickname, from any export that writes people merged.  Such an
        export gives the *resolved* display name -- nickname, else global display name, else
        username -- where the schema wants the raw nickname.  With --extended the user's own
        display name is there to compare against and the nickname can usually be recovered, but
        not always: someone whose nickname is identical to their global display name is
        indistinguishable from someone who set none.

        The value is therefore *derived*, and a derived value must not overwrite an observed
        one -- not even with NULL, which is a claim in its own right ("this member set no
        nickname") that only a split export or a roster is in a position to make.  Creating the
        column's first value is fine: there is nothing there to contradict.  So an archive of
        nothing but vanilla exports still gets names, and a split export arriving later still
        corrects them.

        Without this the two kinds of export fight over the column and whichever file was read
        last decides -- which is exactly the shape-dependence this importer exists to remove.
        """
        if doc.is_roster or doc.mod.split_users:
            return frozenset()
        return frozenset({"display"})

    def _import_messages(self, doc: Document, fs: FileStats, now: int) -> None:
        self._insert_only = self._member_insert_only(doc)
        guild_id = self._guild(doc, now)
        channel_id = self._channel(doc, guild_id, now)

        # The guild's own inventories, present only in an extended export.  Written before the
        # messages so that a role or emoji referenced later is already there with its real name.
        self._roles(doc.guild_roles(), guild_id, now)
        self._emojis(doc.guild_emojis(), guild_id, now)
        self._stickers(doc.guild_stickers(), guild_id, now)
        self._people(doc.owners(), guild_id, now)

        batch: list[dict] = []
        for message in doc.messages():
            batch.append(message)
            fs.messages += 1
            if self.progress is not None:
                self.progress.advance()
            if fs.first_message_id is None:
                fs.first_message_id = snowflake(message.get("id"))
            fs.last_message_id = snowflake(message.get("id"))

            if len(batch) >= BATCH_SIZE:
                self._flush(doc, batch, guild_id, channel_id, fs, now)
                batch = []

        if batch:
            self._flush(doc, batch, guild_id, channel_id, fs, now)

    def _flush(
        self,
        doc: Document,
        batch: list[dict],
        guild_id: int | None,
        channel_id: int | None,
        fs: FileStats,
        now: int,
    ) -> None:
        """Write one batch of messages and everything hanging off it."""
        people: dict[int, Person] = {}
        for message in batch:
            for person in people_in(message):
                key = snowflake(person.id)
                if key is None:
                    continue
                # The best-informed sighting wins, not the last one. The same person can turn
                # up in a batch as a message author and as a reaction author, and a merged
                # export writes no role list for the latter -- so picking by position would
                # make what the database ends up with depend on where the batch boundaries
                # happened to fall.
                previous = people.get(key)
                if previous is None or richness(person) > richness(previous):
                    people[key] = person

        self._people(people.values(), guild_id, now)
        # No guild is attributed to an emoji merely seen in a message: with Nitro it could
        # belong to any server. Ownership is only claimed from the guild's own inventory.
        emoji_ids = self._emojis(emojis_in(batch), None, now)
        self._stickers(stickers_in(batch), None, now)
        self._interactions(batch, now)
        self._messages(batch, channel_id, fs, now)
        self._attachments(batch, now)
        self._mentions(batch)
        self._mention_targets(batch, guild_id, now)
        self._message_stickers(batch)
        self._reactions(batch, emoji_ids)
        self._embeds(batch, emoji_ids, now)

    # ----------------------------------------------------------------------------------
    # Roster exports
    # ----------------------------------------------------------------------------------

    def _import_roster(self, doc: Document, fs: FileStats, now: int) -> None:
        self._insert_only = self._member_insert_only(doc)
        guild_id = self._guild(doc, now)
        self._roles(doc.guild_roles(), guild_id, now)
        self._people(doc.owners(), guild_id, now)

        batch: list[Person] = []
        for person in doc.members():
            batch.append(person)
            fs.members += 1
            if self.progress is not None:
                self.progress.advance()
            if len(batch) >= BATCH_SIZE * 4:
                self._people(batch, guild_id, now)
                batch = []
        if batch:
            self._people(batch, guild_id, now)

    # ----------------------------------------------------------------------------------
    # Guild, channel
    # ----------------------------------------------------------------------------------

    def _guild(self, doc: Document, now: int) -> int | None:
        guild = doc.guild
        guild_id = snowflake(guild.get("id"))
        if guild_id is None:
            return None

        record: dict[str, Any] = {"id": guild_id}
        _carry(
            record,
            guild,
            {
                "name": "name",
                "icon": "iconUrl",
                "description": "description",
                "url": "vanityUrl",
                "banner": "bannerUrl",
                "splash": "splashUrl",
                "boost_level": "premiumTier",
                "boost_count": "premiumSubscriptionCount",
            },
        )
        if "ownerId" in guild:
            record["owner_id"] = snowflake(guild["ownerId"])

        self._merge(schema.GUILDS, {guild_id: record}, now)
        return guild_id

    def _channel(self, doc: Document, guild_id: int | None, now: int) -> int | None:
        channel = doc.channel or {}
        channel_id = snowflake(channel.get("id"))
        if channel_id is None:
            return None

        parent_id = snowflake(channel.get("categoryId"))
        if parent_id is not None:
            self._channel_stub(parent_id, channel.get("category"), guild_id, now)

        record: dict[str, Any] = {
            "id": channel_id,
            "guild_id": guild_id,
            "name": channel.get("name"),
            "topic": channel.get("topic"),
            "parent_id": parent_id,
        }
        record["type"] = self._channel_type(channel.get("type"))
        _carry(record, channel, {"position": "position", "members": "memberCount"})
        if "isArchived" in channel:
            record["archived"] = boolean(channel["isArchived"])
        if "isLocked" in channel:
            record["locked"] = boolean(channel["isLocked"])
        if "ownerId" in channel:
            record["owner_id"] = snowflake(channel["ownerId"])

        self._merge(schema.CHANNELS, {channel_id: record}, now)
        return channel_id

    def _channel_stub(
        self, parent_id: int, name: str | None, guild_id: int | None, now: int
    ) -> None:
        """Make sure a channel's parent exists, even with no export of its own.

        DCE calls it ``category`` for historical reasons, but a parent is nowadays just as
        likely to be a forum, or an ordinary channel holding threads.  Category is the best
        guess available here; a later export of the parent itself corrects the type.  What this
        must never do is push a real channel's type *back* to 4, so an existing row only has its
        name and guild filled in.
        """
        known = self.db.select_by_ids(schema.CHANNELS, [parent_id], ("id",))
        record: dict[str, Any] = {"id": parent_id, "guild_id": guild_id, "name": name}
        if not known:
            record["type"] = CATEGORY_TYPE
        self._merge(schema.CHANNELS, {parent_id: record}, now)

    def _channel_type(self, raw) -> int | None:
        resolved = resolve_channel_type(raw)
        if resolved is None and raw is not None:
            self.stats.unknown_types[f"channel:{raw}"] += 1
        return resolved

    # ----------------------------------------------------------------------------------
    # People
    # ----------------------------------------------------------------------------------

    def _people(self, people: Iterable[Person], guild_id: int | None, now: int) -> None:
        users: dict[int, dict] = {}
        members: dict[tuple, dict] = {}
        roles: dict[int, dict] = {}
        assignments: set[tuple[int, int]] = set()

        for person in people:
            if person is None:
                continue
            user_id = snowflake(person.id)
            if user_id is None:
                continue

            users[user_id] = _user_record(user_id, person.user)

            for role in person.roles:
                role_id = snowflake(role.get("id"))
                if role_id is None:
                    continue
                roles[role_id] = _role_record(role_id, role, guild_id)
                assignments.add((user_id, role_id))

            if person.member is not None and guild_id is not None:
                members[(user_id, guild_id)] = _member_record(
                    user_id, guild_id, person.member
                )

        self._merge(schema.USERS, users, now)
        self._merge(schema.ROLES, roles, now)
        self._merge(
            schema.MEMBERS,
            members,
            now,
            key=("user_id", "guild_id"),
            insert_only=self._insert_only,
        )
        self._insert_ignore(
            schema.USER_ROLES, ("user_id", "role_id"), sorted(assignments)
        )

    def _roles(self, roles: Iterable[dict], guild_id: int | None, now: int) -> None:
        records = {}
        for role in roles:
            role_id = snowflake(role.get("id"))
            if role_id is not None:
                records[role_id] = _role_record(role_id, role, guild_id)
        self._merge(schema.ROLES, records, now)

    # ----------------------------------------------------------------------------------
    # Emoji and stickers
    # ----------------------------------------------------------------------------------

    def _emojis(
        self, emojis: Iterable[dict], guild_id: int | None, now: int
    ) -> dict[tuple, int]:
        """Register emoji and return a map from identity to database ID.

        Custom emoji are keyed by their snowflake.  Standard ones have none, so they are keyed
        by their shortcode -- ``:joy:`` rather than the character, because the character is the
        one thing here whose encoding could vary -- and given a synthetic ID.
        """
        records: dict[int, dict] = {}
        identities: dict[tuple, int] = {}

        for emoji in emojis:
            identity = _emoji_identity(emoji)
            if identity is None or identity in identities:
                continue

            kind, token = identity
            if kind == "custom":
                emoji_id = token
            else:
                emoji_id = self._standard_id(token)

            identities[identity] = emoji_id
            record: dict[str, Any] = {
                "id": emoji_id,
                "name": emoji.get("name"),
                "code": emoji.get("code"),
                "animated": boolean(emoji.get("isAnimated")),
                "url": emoji.get("imageUrl"),
            }
            # A standard emoji belongs to no guild; a custom one to the guild it was seen in,
            # which is only actually known when the export named it in the guild's inventory
            if kind == "custom" and guild_id is not None:
                record["guild_id"] = guild_id
            records[emoji_id] = record

        self._merge(schema.EMOJIS, records, now)
        return identities

    def _standard_id(self, code: str) -> int:
        index = self._standard_index()
        if code not in index:
            index[code] = self._next_emoji_id
            self._next_emoji_id += 1
        return index[code]

    def _standard_index(self) -> dict[str, int]:
        if self._standard_emojis is None:
            # A null guild is not enough to identify a standard emoji: a custom one seen in a
            # message has no guild either, because being used somewhere proves nothing about
            # where it came from. The synthetic ID range is what actually distinguishes them.
            rows = self.db.fetchall(
                f"SELECT {self.db.quote('id')}, {self.db.quote('code')}, "
                f"{self.db.quote('name')} FROM {self.db.quote('emojis')} "
                f"WHERE {self.db.quote('guild_id')} IS NULL "
                f"AND {self.db.quote('id')} < {self.db.placeholder}",
                (SYNTHETIC_ID_CEILING,),
            )
            self._standard_emojis = {(code or name): i for i, code, name in rows}
            self._next_emoji_id = self.db.max_id("emojis", SYNTHETIC_ID_CEILING) + 1
        return self._standard_emojis

    def _stickers(self, stickers: Iterable[dict], guild_id: int | None, now: int) -> None:
        records: dict[int, dict] = {}
        for sticker in stickers:
            sticker_id = snowflake(sticker.get("id"))
            if sticker_id is None:
                continue
            record: dict[str, Any] = {
                "id": sticker_id,
                "name": sticker.get("name"),
                "format": resolve_sticker_format(sticker.get("format")),
                "url": sticker.get("sourceUrl"),
            }
            # Only the guild's own inventory proves ownership; a sticker seen on a message
            # could have come from anywhere, so its guild is left alone
            if guild_id is not None:
                record["guild_id"] = guild_id
            records[sticker_id] = record
        self._merge(schema.STICKERS, records, now)

    def _interactions(self, batch: list[dict], now: int) -> None:
        records: dict[int, dict] = {}
        for message in batch:
            interaction = message.get("interaction")
            if not interaction:
                continue
            interaction_id = snowflake(interaction.get("id"))
            if interaction_id is not None:
                records[interaction_id] = {
                    "id": interaction_id,
                    "name": interaction.get("name"),
                }
        self._merge(schema.INTERACTIONS, records, now)

    # ----------------------------------------------------------------------------------
    # Messages
    # ----------------------------------------------------------------------------------

    def _messages(
        self, batch: list[dict], channel_id: int | None, fs: FileStats, now: int
    ) -> None:
        records: dict[int, dict] = {}
        for message in batch:
            message_id = snowflake(message.get("id"))
            if message_id is None:
                continue
            records[message_id] = self._message_record(message, message_id, channel_id)

        if not records:
            return

        table = schema.MESSAGES
        columns = _data_columns(table)
        index = {c: i for i, c in enumerate(columns)}
        existing = self.db.select_by_ids(table, records.keys(), columns)

        inserts: list[list] = []
        updates: dict[tuple, list[list]] = defaultdict(list)
        history: list[tuple] = []
        deleted_authors: set[int] = set()

        for message_id, fields in records.items():
            encoded = self._encode(table, fields)
            row = existing.get(message_id)

            if row is None:
                inserts.append(self._insert_values(table, columns, encoded) + [now, now])
                fs.new_messages += 1
                continue

            # Discord reassigns a deleted account's messages to one stand-in user. Recording
            # that would erase who actually wrote them, so the author is left as archived and
            # the account is flagged instead -- which is the only way to detect a deletion.
            new_author = encoded.get("user_id")
            old_author = row[index["user_id"]]
            if (
                new_author == DELETED_USER_ID
                and old_author not in (None, DELETED_USER_ID)
            ):
                deleted_authors.add(old_author)
            encoded.pop("user_id", None)

            old_content = row[index["content"]]
            if "content" in encoded and encoded["content"] != old_content:
                # The version being replaced is dated by when it was last edited, or by when it
                # was posted if this is the first edit anyone has seen
                history.append(
                    (
                        message_id,
                        row[index["edited_timestamp"]] or row[index["timestamp"]],
                        old_content,
                    )
                )

            changed = {c: v for c, v in encoded.items() if row[index[c]] != v}
            if changed:
                key = tuple(sorted(changed))
                updates[key].append([changed[c] for c in key] + [now, message_id])
                fs.edited_messages += 1

        self.db.insert(table, tuple(columns) + ("created_at", "updated_at"), inserts)
        self.stats.insert(table.name, len(inserts))

        for key, rows in updates.items():
            self.db.update(table, key + ("updated_at",), rows, key=("id",))
            self.stats.update(table.name, len(rows))

        self._insert_ignore(
            schema.MESSAGE_HISTORY, ("message_id", "timestamp", "content"), history
        )
        self._mark_deleted(deleted_authors, now)

    def _message_record(
        self, message: dict, message_id: int, channel_id: int | None
    ) -> dict[str, Any]:
        reference = message.get("reference") or {}
        interaction = message.get("interaction") or {}
        author: Person | None = message.get("author")

        record: dict[str, Any] = {
            "id": message_id,
            "channel_id": channel_id,
            "type": self._message_type(message.get("type")),
            "timestamp": timestamp(message.get("timestamp")),
            "edited_timestamp": timestamp(message.get("timestampEdited")),
            "pinned": boolean(message.get("isPinned")),
            "user_id": snowflake(author.id) if author else None,
            "content": message.get("content"),
            "reference_id": snowflake(reference.get("messageId")),
            "reference_type": (
                resolve_reference_type(reference.get("type")) if reference else None
            ),
            "interaction_id": snowflake(interaction.get("id")),
            "interaction_user_id": (
                snowflake(interaction["user"].id)
                if interaction.get("user") is not None
                else None
            ),
            # A forward carries its own content, attachments and embeds but has no ID and no
            # author of its own, so it is kept whole rather than flattened into tables that
            # would have nothing to key it by
            "forwarded": message.get("forwardedMessage"),
        }

        # Only --extended writes components at all, so their absence must not be read as
        # "this message has none"
        if "components" in message:
            record["components"] = message["components"] or None

        return record

    def _message_type(self, raw) -> int | None:
        resolved = resolve_message_type(raw)
        if resolved is None and raw is not None:
            self.stats.unknown_types[f"message:{raw}"] += 1
        return resolved

    def _mark_deleted(self, user_ids: set[int], now: int) -> None:
        if not user_ids:
            return
        table = schema.USERS
        existing = self.db.select_by_ids(table, user_ids, ("id", "deleted"))
        rows = [[1, now, uid] for uid, row in existing.items() if not row[1]]
        if rows:
            self.db.update(table, ("deleted", "updated_at"), rows, key=("id",))
            self.stats.update(table.name, len(rows))
            self.stats.users_deleted += len(rows)

    # ----------------------------------------------------------------------------------
    # Message children
    # ----------------------------------------------------------------------------------

    def _attachments(self, batch: list[dict], now: int) -> None:
        records: dict[int, dict] = {}
        for message in batch:
            message_id = snowflake(message.get("id"))
            for attachment in message.get("attachments") or []:
                attachment_id = snowflake(attachment.get("id"))
                if attachment_id is None:
                    continue
                records[attachment_id] = {
                    "id": attachment_id,
                    "message_id": message_id,
                    "name": attachment.get("fileName"),
                    "size": attachment.get("fileSizeBytes"),
                    "url": attachment.get("url"),
                }
        self._merge(schema.ATTACHMENTS, records, now)

    def _mentions(self, batch: list[dict]) -> None:
        rows = set()
        for message in batch:
            message_id = snowflake(message.get("id"))
            for person in message.get("mentions") or []:
                user_id = snowflake(person.id)
                if message_id is not None and user_id is not None:
                    rows.add((message_id, user_id))
        self._insert_ignore(schema.MENTIONS, ("message_id", "user_id"), sorted(rows))

    def _mention_targets(self, batch: list[dict], guild_id: int | None, now: int) -> None:
        """Channels and roles a message mentions, which only an extended export records.

        The channels are worth writing as rows in their own right, not just as a junction: a
        mention is often the only place an archive ever hears of a channel that was never
        exported, and losing its name would be a shame.  Only the fields a mention carries are
        written, so this can never overwrite a real export of that channel with less.
        """
        channels: dict[int, dict] = {}
        channel_rows, role_rows = set(), set()
        roles: dict[int, dict] = {}

        for message in batch:
            message_id = snowflake(message.get("id"))
            if message_id is None:
                continue

            for channel in message.get("channelMentions") or []:
                channel_id = snowflake(channel.get("id"))
                if channel_id is None:
                    continue
                channel_rows.add((message_id, channel_id))

                record: dict[str, Any] = {"id": channel_id}
                if channel.get("name"):
                    # Only a channel that actually resolved is known to be in this guild: the
                    # exporter looks a mention up in this guild's channels, so one it could not
                    # find may well belong to another server entirely
                    record["guild_id"] = guild_id
                    record["name"] = channel["name"]
                if channel.get("type") is not None:
                    record["type"] = self._channel_type(channel["type"])
                if channel.get("categoryId"):
                    record["parent_id"] = snowflake(channel["categoryId"])
                channels[channel_id] = record

            for role in message.get("roleMentions") or []:
                role_id = snowflake(role.get("id"))
                if role_id is None:
                    continue
                role_rows.add((message_id, role_id))
                if role.get("name"):
                    roles[role_id] = _role_record(role_id, role, guild_id)

        # A mentioned channel's parent is named only by ID here, so no stub is invented for it
        self._merge(schema.CHANNELS, channels, now)
        self._merge(schema.ROLES, roles, now)
        self._insert_ignore(
            schema.CHANNEL_MENTIONS, ("message_id", "channel_id"), sorted(channel_rows)
        )
        self._insert_ignore(
            schema.ROLE_MENTIONS, ("message_id", "role_id"), sorted(role_rows)
        )

    def _message_stickers(self, batch: list[dict]) -> None:
        rows = set()
        for message in batch:
            message_id = snowflake(message.get("id"))
            for sticker in message.get("stickers") or []:
                sticker_id = snowflake(sticker.get("id"))
                if message_id is not None and sticker_id is not None:
                    rows.add((message_id, sticker_id))
        self._insert_ignore(
            schema.MESSAGE_STICKERS, ("message_id", "sticker_id"), sorted(rows)
        )

    def _reactions(self, batch: list[dict], emoji_ids: dict[tuple, int]) -> None:
        """Write who reacted, or how many did when the export doesn't say who.

        A row naming its user counts 1; a row with no user carries the rest of the total.  The
        second kind appears when the export was made with ``--reaction-users false``, and also
        whenever the count exceeds the names actually listed.  Summing ``count`` over a
        ``(message_id, emoji_id)`` pair therefore gives the true total in every case.
        """
        named: set[tuple] = set()
        totals: dict[tuple, int] = {}

        for message in batch:
            message_id = snowflake(message.get("id"))
            if message_id is None:
                continue
            for reaction in message.get("reactions") or []:
                identity = _emoji_identity(reaction.get("emoji") or {})
                emoji_id = emoji_ids.get(identity) if identity else None
                if emoji_id is None:
                    continue

                totals[(message_id, emoji_id)] = reaction.get("count") or 0
                for person in reaction.get("users") or []:
                    user_id = snowflake(person.id)
                    if user_id is not None:
                        named.add((message_id, user_id, emoji_id, 1))

        if not totals:
            return

        columns = ("message_id", "user_id", "emoji_id", "count")
        self._insert_ignore(schema.REACTIONS, columns, sorted(named))

        # The unnamed remainder shrinks as later exports name more of the reactors, so unlike
        # every other row here it is replaced rather than accumulated. It is measured against
        # the names now in the database, not against the ones in this file: an export made
        # without reaction users lists none at all, and must not re-anonymize the reactors that
        # an earlier export already named.
        messages = {message_id for message_id, _ in totals}
        self._delete_summaries(messages)
        stored = self._named_reaction_counts(messages)

        summaries = [
            (message_id, None, emoji_id, remainder)
            for (message_id, emoji_id), total in sorted(totals.items())
            if (remainder := total - stored.get((message_id, emoji_id), 0)) > 0
        ]
        self._insert_ignore(schema.REACTIONS, columns, summaries)

    def _named_reaction_counts(self, message_ids: set[int]) -> dict[tuple, int]:
        """How many reactors each (message, emoji) already has by name."""
        table = self.db.quote(schema.REACTIONS.name)
        out: dict[tuple, int] = {}
        for group in chunked(sorted(message_ids), PARAM_CHUNK):
            rows = self.db.fetchall(
                f"SELECT {self.db.quote('message_id')}, {self.db.quote('emoji_id')}, COUNT(*) "
                f"FROM {table} WHERE {self.db.quote('user_id')} IS NOT NULL "
                f"AND {self.db.quote('message_id')} IN ({self.db.marks(len(group))}) "
                f"GROUP BY {self.db.quote('message_id')}, {self.db.quote('emoji_id')}",
                group,
            )
            for message_id, emoji_id, count in rows:
                out[(message_id, emoji_id)] = count
        return out

    def _delete_summaries(self, message_ids: set[int]) -> None:
        table = self.db.quote(schema.REACTIONS.name)
        for group in chunked(sorted(message_ids), PARAM_CHUNK):
            self.db.execute(
                f"DELETE FROM {table} WHERE {self.db.quote('user_id')} IS NULL "
                f"AND {self.db.quote('message_id')} IN ({self.db.marks(len(group))})",
                group,
            )

    def _embeds(self, batch: list[dict], emoji_ids: dict[tuple, int], now: int) -> None:
        """Flatten embeds, then their resources, then link the two together.

        An embed has no ID of its own, so its position in the message's array is its identity.
        Resources are written after the embeds exist and the embeds are then updated with the
        IDs, which is the only way round a reference that points both ways.
        """
        records: dict[tuple, dict] = {}
        for message in batch:
            message_id = snowflake(message.get("id"))
            if message_id is None:
                continue
            for ordinal, embed in enumerate(message.get("embeds") or []):
                records[(message_id, ordinal)] = _embed_record(
                    message_id, ordinal, embed
                )

        if not records:
            self._message_emojis(batch, emoji_ids, {})
            return

        self._merge(
            schema.EMBEDS, records, now, key=("message_id", "ordinal"), timestamps=False
        )

        found = self.db.select_keyed(
            schema.EMBEDS, ("message_id", "ordinal"), records.keys(), ("id", "message_id", "ordinal")
        )
        embed_ids = {key: row[0] for key, row in found.items()}

        resources: dict[tuple, dict] = {}
        for message in batch:
            message_id = snowflake(message.get("id"))
            for ordinal, embed in enumerate(message.get("embeds") or []):
                embed_id = embed_ids.get((message_id, ordinal))
                if embed_id is None:
                    continue
                for slot, resource in _resources_of(embed):
                    resources[(embed_id, slot)] = _resource_record(embed_id, slot, resource)

        if resources:
            self._merge(
                schema.RESOURCES,
                resources,
                now,
                key=("embed_id", "slot"),
                timestamps=False,
            )
            stored = self.db.select_keyed(
                schema.RESOURCES,
                ("embed_id", "slot"),
                resources.keys(),
                ("id", "embed_id", "slot"),
            )
            self._link_resources(stored, now)

        self._message_emojis(batch, emoji_ids, embed_ids)

    def _link_resources(self, stored: dict[tuple, tuple], now: int) -> None:
        """Point each embed at the resources filling its named slots."""
        by_embed: dict[int, dict[str, int]] = defaultdict(dict)
        for (embed_id, slot), row in stored.items():
            by_embed[embed_id][slot] = row[0]

        records: dict[int, dict] = {}
        for embed_id, slots in by_embed.items():
            fields = {f"{slot}_id": slots[slot] for slot in EMBED_SLOTS if slot in slots}
            if fields:
                records[embed_id] = fields

        self._merge(schema.EMBEDS, records, now, key=("id",), timestamps=False)

    def _message_emojis(
        self,
        batch: list[dict],
        emoji_ids: dict[tuple, int],
        embed_ids: dict[tuple, int],
    ) -> None:
        rows = set()
        for message in batch:
            message_id = snowflake(message.get("id"))
            if message_id is None:
                continue

            for emoji in message.get("inlineEmojis") or []:
                identity = _emoji_identity(emoji)
                emoji_id = emoji_ids.get(identity) if identity else None
                if emoji_id is not None:
                    rows.add((message_id, emoji_id, None))

            for ordinal, embed in enumerate(message.get("embeds") or []):
                embed_id = embed_ids.get((message_id, ordinal))
                for emoji in embed.get("inlineEmojis") or []:
                    identity = _emoji_identity(emoji)
                    emoji_id = emoji_ids.get(identity) if identity else None
                    if emoji_id is not None:
                        rows.add((message_id, emoji_id, embed_id))

        self._insert_ignore(
            schema.MESSAGE_EMOJIS,
            ("message_id", "emoji_id", "embed_id"),
            sorted(rows, key=lambda r: (r[0], r[1], r[2] or 0)),
        )

    # ----------------------------------------------------------------------------------
    # Bookkeeping
    # ----------------------------------------------------------------------------------

    def _record_import(
        self, doc: Document, source: Source, fs: FileStats, now: int
    ) -> None:
        """One row per file imported, whether or not it changed anything."""
        channel = doc.channel or {}
        columns = (
            "sha1",
            "kind",
            "guild_id",
            "channel_id",
            "start_timestamp",
            "end_timestamp",
            "exported_at",
            "imported_at",
            "first_message_id",
            "last_message_id",
            "total_message_count",
            "new_message_count",
            "edit_message_count",
            "unresolved",
        )
        row = (
            source.sha1,
            "members" if doc.is_roster else "messages",
            snowflake(doc.guild_id),
            snowflake(channel.get("id")),
            timestamp(doc.date_range.get("after")),
            timestamp(doc.date_range.get("before")),
            timestamp(doc.exported_at),
            now,
            fs.first_message_id,
            fs.last_message_id,
            fs.members if doc.is_roster else fs.messages,
            fs.new_messages,
            fs.edited_messages,
            doc.unresolver is not None,
        )
        self.db.insert(schema.IMPORTS, columns, [row])
        self.stats.insert(schema.IMPORTS.name, 1)

    # ----------------------------------------------------------------------------------
    # The merge primitive
    # ----------------------------------------------------------------------------------

    def _encode(self, table: Table, fields: dict[str, Any]) -> dict[str, Any]:
        return {c: self.db.encode(table.column(c), v) for c, v in fields.items()}

    def _insert_values(
        self, table: Table, columns: Sequence[str], encoded: dict[str, Any]
    ) -> list:
        """Fill in the columns this document said nothing about.

        Their declared default, not NULL: a column like ``users.deleted`` is NOT NULL and no
        export ever carries it, since it can only be worked out by comparing two of them.
        """
        out = []
        for name in columns:
            if name in encoded:
                out.append(encoded[name])
            else:
                column = table.column(name)
                out.append(self.db.encode(column, column.default))
        return out

    def _merge(
        self,
        table: Table,
        records: dict,
        now: int,
        key: Sequence[str] = ("id",),
        timestamps: bool | None = None,
        insert_only: frozenset[str] = frozenset(),
    ) -> None:
        """Insert what is new, update only what actually changed, touch nothing else.

        ``records`` maps a key -- the primary key, or a tuple for a composite one -- to the
        columns this document knows about.  Columns it does not mention are neither inserted nor
        compared, which is what stops a poorer export from erasing a richer one's work.

        ``insert_only`` names columns whose value this document can only derive rather than
        observe.  They are written when the row is created and never touched again, so that a
        derived value cannot overwrite one some better-informed export observed.
        """
        if not records:
            return

        if timestamps is None:
            timestamps = any(c.name == "created_at" for c in table.columns)

        key = tuple(key)
        columns = _data_columns(table)
        # The key columns have to come back from the database whether or not this document
        # writes them, since they are what the result is indexed by
        fetch = tuple(dict.fromkeys((*key, *columns)))
        index = {c: i for i, c in enumerate(fetch)}

        if key == ("id",):
            existing = self.db.select_by_ids(table, records.keys(), fetch)
        else:
            existing = self.db.select_keyed(table, key, records.keys(), fetch)

        inserts: list[list] = []
        updates: dict[tuple, list[list]] = defaultdict(list)

        for record_key, fields in records.items():
            encoded = self._encode(table, fields)
            row = existing.get(record_key)

            if row is None:
                values = self._insert_values(table, columns, encoded)
                inserts.append(values + ([now, now] if timestamps else []))
                continue

            changed = {
                c: v
                for c, v in encoded.items()
                if c not in insert_only
                and self.db.differs(table.column(c), row[index[c]], v)
            }
            if not changed:
                continue

            changed_key = tuple(sorted(changed))
            key_values = list(record_key) if isinstance(record_key, tuple) else [record_key]
            updates[changed_key].append(
                [changed[c] for c in changed_key] + ([now] if timestamps else []) + key_values
            )

        insert_columns = tuple(columns) + (("created_at", "updated_at") if timestamps else ())
        self.db.insert(table, insert_columns, inserts)
        self.stats.insert(table.name, len(inserts))

        for changed_key, rows in updates.items():
            update_columns = changed_key + (("updated_at",) if timestamps else ())
            self.db.update(table, update_columns, rows, key=key)
            self.stats.update(table.name, len(rows))

    def _insert_ignore(
        self, table: Table, columns: Sequence[str], rows: Sequence[Sequence]
    ) -> None:
        if not rows:
            return
        # The engine drops the duplicates and reports how many it kept, so the count is rows
        # actually stored -- which on a re-import is zero, as it should be
        self.stats.insert(table.name, self.db.insert_ignore(table, columns, rows))


# --------------------------------------------------------------------------------------
# Record builders
# --------------------------------------------------------------------------------------


def _data_columns(table: Table) -> list[str]:
    """Every column the importer supplies: not the bookkeeping pair, not an auto key."""
    return [
        c.name
        for c in table.columns
        if c.name not in ("created_at", "updated_at") and not c.auto
    ]


def _carry(record: dict, source: dict, mapping: dict[str, str]) -> None:
    """Copy the keys the document actually has, and only those."""
    for column, json_key in mapping.items():
        if json_key in source:
            record[column] = source[json_key]


def _user_record(user_id: int, user: dict) -> dict[str, Any]:
    record: dict[str, Any] = {"id": user_id}
    if "name" in user:
        record["name"] = user["name"]
    if "discriminator" in user:
        record["discriminator"] = discriminator(user["discriminator"])
    if "displayName" in user:
        record["display"] = user["displayName"]
    if "isBot" in user:
        record["bot"] = boolean(user["isBot"])
    if "avatarUrl" in user:
        record["avatar"] = user["avatarUrl"]
    if "bannerUrl" in user:
        record["banner"] = user["bannerUrl"]
    return record


def _member_record(user_id: int, guild_id: int, member: dict) -> dict[str, Any]:
    record: dict[str, Any] = {"user_id": user_id, "guild_id": guild_id}
    if "nickname" in member:
        record["display"] = member["nickname"]
    if "color" in member:
        record["color"] = color(member["color"])
    if "avatarUrl" in member:
        record["avatar"] = member["avatarUrl"]
    if "bannerUrl" in member:
        record["banner"] = member["bannerUrl"]
    if "joinedAt" in member:
        record["joined_at"] = timestamp(member["joinedAt"])
    if "premiumSince" in member:
        record["boosting_since"] = timestamp(member["premiumSince"])
    return record


def _role_record(role_id: int, role: dict, guild_id: int | None) -> dict[str, Any]:
    record: dict[str, Any] = {"id": role_id}
    if guild_id is not None:
        record["guild_id"] = guild_id
    if "name" in role:
        record["name"] = role["name"]
    if "color" in role:
        record["color"] = color(role["color"])
    if "position" in role:
        record["position"] = role["position"]
    return record


def _embed_record(message_id: int, ordinal: int, embed: dict) -> dict[str, Any]:
    author = embed.get("author") or {}
    footer = embed.get("footer") or {}
    return {
        "message_id": message_id,
        "ordinal": ordinal,
        "title": embed.get("title") or None,
        "url": embed.get("url"),
        "description": embed.get("description") or None,
        "timestamp": timestamp(embed.get("timestamp")),
        "color": color(embed.get("color")),
        "author_name": author.get("name") or None,
        "author_url": author.get("url"),
        "footer": footer.get("text") or None,
        "fields": embed.get("fields") or None,
    }


def _resources_of(embed: dict) -> list[tuple[str, dict]]:
    """Every image or video an embed carries, tagged with the slot it filled.

    The named slots are what the embed columns point at.  ``images[n]`` rows exist too, because
    an embed can carry several and only one of them is the main image; they are reachable
    through the resource's own ``embed_id``.
    """
    out: list[tuple[str, dict]] = []

    for slot, key in (("thumbnail", "thumbnail"), ("image", "image"), ("video", "video")):
        value = embed.get(key)
        if value:
            out.append((slot, value))

    author = embed.get("author") or {}
    if author.get("iconUrl") or author.get("iconCanonicalUrl"):
        out.append(
            (
                "author_icon",
                {
                    "url": author.get("iconCanonicalUrl"),
                    "proxiedUrl": author.get("iconUrl"),
                },
            )
        )

    footer = embed.get("footer") or {}
    if footer.get("iconUrl") or footer.get("iconCanonicalUrl"):
        out.append(
            (
                "footer_icon",
                {
                    "url": footer.get("iconCanonicalUrl"),
                    "proxiedUrl": footer.get("iconUrl"),
                },
            )
        )

    for i, image in enumerate(embed.get("images") or []):
        out.append((f"images[{i}]", image))

    return out


def _resource_record(embed_id: int, slot: str, resource: dict) -> dict[str, Any]:
    # DCE writes the CDN copy as 'url' and the original as 'canonicalUrl'; the icons of an
    # embed's author and footer use 'iconUrl' and 'iconCanonicalUrl', already unwrapped above
    canonical = resource.get("url") if "proxiedUrl" in resource else resource.get("canonicalUrl")
    proxied = resource.get("proxiedUrl", resource.get("url"))
    return {
        "embed_id": embed_id,
        "slot": slot,
        "url": canonical,
        "proxied_url": proxied,
        "width": resource.get("width"),
        "height": resource.get("height"),
    }


def _emoji_identity(emoji: dict) -> tuple[str, Any] | None:
    """What makes two emoji the same.

    A custom emoji is its snowflake.  A standard one has none, so it is its shortcode, falling
    back to the character itself when DCE has no shortcode for it.
    """
    if not emoji:
        return None
    emoji_id = snowflake(emoji.get("id"))
    if emoji_id is not None:
        return ("custom", emoji_id)
    token = emoji.get("code") or emoji.get("name")
    return ("standard", token) if token else None


def richness(person: Person) -> tuple:
    """How much one sighting of a person tells us, for picking between several.

    Knowing their guild profile beats not knowing it, and a longer role list beats a shorter
    one -- an absent role list means the export did not say, not that they hold none.
    """
    return (person.member is not None, len(person.roles), len(person.user))


def people_in(message: dict) -> Iterable[Person]:
    author = message.get("author")
    if author is not None:
        yield author
    for person in message.get("mentions") or []:
        yield person
    for reaction in message.get("reactions") or []:
        for person in reaction.get("users") or []:
            yield person
    interaction = message.get("interaction") or {}
    if interaction.get("user") is not None:
        yield interaction["user"]


def emojis_in(batch: list[dict]) -> Iterable[dict]:
    for message in batch:
        yield from message.get("inlineEmojis") or []
        for reaction in message.get("reactions") or []:
            emoji = reaction.get("emoji")
            if emoji:
                yield emoji
        for embed in message.get("embeds") or []:
            yield from embed.get("inlineEmojis") or []


def stickers_in(batch: list[dict]) -> Iterable[dict]:
    for message in batch:
        yield from message.get("stickers") or []
        forwarded = message.get("forwardedMessage") or {}
        yield from forwarded.get("stickers") or []
