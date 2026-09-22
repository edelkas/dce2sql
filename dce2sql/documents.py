"""Turning any shape of DCE export into one canonical form.

DCE can write the same conversation a dozen ways: vanilla or this fork's ``--extended``,
inline or ``--normal``, merged or ``--split-users``, with or without reaction authors.  Rather
than let every combination reach the importer, everything is reduced here to a single internal
shape, and the importer is written once against that.

The two reductions that do the work:

**Rehydration.**  Under ``--normal``, entities with an identity are written to lookup tables at
the root and referenced by ID from the messages.  Those references are resolved back into whole
objects, so a normalized document becomes indistinguishable from an inline one.

**Splitting.**  Discord has two objects where DCE's original schema has one: a *user* (the
global account) and a *member* (that account's profile inside one guild).  ``--split-users``
writes them apart; everything else writes them merged, with the member's nickname, colour,
roles and avatar folded into the user.  Here they always come apart, because the database keeps
them in separate tables.

Taking a merged object apart is not quite free, and the two places it isn't are worth knowing:

* ``nickname`` in a merged object has already collapsed *nickname -> display name -> username*
  into one string.  With ``--extended`` the user's own ``displayName`` is present alongside it,
  and the nickname is usually recoverable by comparing them -- though never for someone whose
  nickname happens to equal their global display name, who reads as having set none.  Without
  ``--extended`` the two cannot be told apart at all, and only the member gets a name.
* ``avatarUrl`` and ``bannerUrl`` are likewise a guild override falling back to the global one.
  These *are* recoverable whatever the flags, because Discord serves a member's guild-specific
  image from a different path -- ``/guilds/{guild}/users/{user}/...`` rather than
  ``/avatars/{user}/...``.  The exception is ``--media``, which rewrites every URL to a local
  file and erases the distinction; then the image is attributed to the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from .reader import MEMBERS, MESSAGES, RawDocument, Source

#: Matches the CDN path Discord uses for a member's guild-specific avatar or banner.
_GUILD_ASSET = re.compile(r"/guilds/\d+/users/\d+/(avatars|banners)/", re.IGNORECASE)


@dataclass(frozen=True)
class Mod:
    """The ``mod`` block: which of this fork's options produced the file.

    A vanilla DiscordChatExporter export has no ``mod`` key at all, which is itself the signal.
    Every flag defaults to the vanilla behaviour, so an older export of this fork that predates
    a flag reads as though the flag was off -- which it was.
    """

    vanilla: bool = True
    normal: bool = False
    extended: bool = False
    split_users: bool = False
    reaction_users: bool = True
    cache: bool = False
    full_users: bool = False
    #: Whether mentions, custom emoji and timestamps in a message body were resolved into
    #: names.  True is DCE's default and what every export before the flag existed did.
    markdown: bool = True

    @classmethod
    def parse(cls, header: dict) -> "Mod":
        block = header.get("mod")
        if not isinstance(block, dict):
            return cls()
        return cls(
            vanilla=False,
            normal=bool(block.get("normal", False)),
            extended=bool(block.get("extended", False)),
            split_users=bool(block.get("splitUsers", False)),
            # Predates the flag means the users were fetched, because there was no way not to
            reaction_users=bool(block.get("reactionUsers", True)),
            cache=bool(block.get("cache", False)),
            full_users=bool(block.get("fullUsers", False)),
            markdown=bool(block.get("markdown", True)),
        )

    def describe(self) -> str:
        if self.vanilla:
            return "vanilla"
        flags = [name for name, on in (
            ("normal", self.normal),
            ("extended", self.extended),
            ("split", self.split_users),
            ("no-reaction-users", not self.reaction_users),
            ("cached", self.cache),
            ("full-users", self.full_users),
            ("raw", not self.markdown),
        ) if on]
        return "+".join(flags) if flags else "default"


@dataclass
class Person:
    """One participant, taken apart into the two objects Discord actually has.

    ``member`` is ``None`` when this document holds no guild profile for them: they left, were
    never in the guild, or nothing in the export ever caused them to be looked up.
    """

    user: dict
    member: dict | None = None
    roles: list[dict] = field(default_factory=list)

    #: The name DCE would have printed for this person in a resolved mention: the guild
    #: nickname if there is one, else the account's display name.  Kept because it is the only
    #: thing a resolved '@...' in a message body can be matched back against, and because a
    #: vanilla export has nowhere else to put it -- see dce2sql.mentions.
    rendered: str | None = None

    @property
    def id(self) -> Any:
        return self.user.get("id")


class Lookups:
    """The root lookup tables of a ``--normal`` document, indexed for resolution.

    Absent entirely from an inline document, in which case every lookup misses and the objects
    found inline are used as they are.
    """

    def __init__(self, header: dict) -> None:
        self.users = {u.get("id"): u for u in header.get("users") or []}
        self.members = {m.get("userId"): m for m in header.get("members") or []}
        self.roles = {r.get("id"): r for r in header.get("roles") or []}
        self.stickers = {s.get("id"): s for s in header.get("stickers") or []}
        # Emoji are keyed by a string DCE assigns, not by ID: standard emoji have no ID, and a
        # custom emoji renamed over its lifetime is two distinct records
        self.emojis = {e.get("key"): e for e in header.get("emojis") or []}

    def role(self, role_id) -> dict:
        return self.roles.get(role_id) or {"id": role_id}

    def emoji(self, key) -> dict:
        return self.emojis.get(key) or {"name": key, "code": key}

    def sticker(self, sticker_id) -> dict:
        return self.stickers.get(sticker_id) or {"id": sticker_id}


class Document:
    """A DCE export reduced to one shape, whichever flags produced it."""

    def __init__(self, raw: RawDocument, unresolver=None) -> None:
        self.source: Source = raw.source
        self.header = raw.header
        self.kind = raw.array_key
        self.mod = Mod.parse(raw.header)
        self.lookups = Lookups(raw.header)
        self._raw = raw

        # An export made with markdown off already holds the raw form, so there is nothing to
        # put back and nothing to be gained by looking
        self.unresolver = unresolver if self.mod.markdown else None
        if self.unresolver is not None:
            # This document's own role names, which are the ones in force when it was written
            self.unresolver.add_roles(self._roles_of(raw.header.get("guild") or {}))

        #: What the message being reduced says it mentions, for the unresolver
        self._mentioned: list = []

        self.guild: dict = raw.header.get("guild") or {}
        self.channel: dict | None = raw.header.get("channel")
        self.date_range: dict = raw.header.get("dateRange") or {}
        self.exported_at = raw.header.get("exportedAt")
        self.declared_count = raw.header.get(
            "messageCount" if self.kind == MESSAGES else "memberCount"
        )

    # -- identity ----------------------------------------------------------------------

    @property
    def guild_id(self):
        return self.guild.get("id")

    @property
    def is_roster(self) -> bool:
        """True for an ``exportusers`` document, which has members but no channel."""
        return self.kind == MEMBERS

    def validate(self) -> None:
        """Reject a file that is not a DCE export, before it imports as a silent no-op.

        Any JSON object parses into a Document with everything empty, and would then import
        cleanly while doing nothing -- leaving a row in the imports table claiming a file was
        read that in truth contributed nothing. Better to say so.
        """
        if not self.guild.get("id"):
            raise ValueError("no guild object; this does not look like a DCE export")
        if not self.is_roster and not (self.channel or {}).get("id"):
            raise ValueError("no channel object; this does not look like a DCE export")

    def describe(self) -> str:
        """A short label for the report, e.g. ``messages:extended+split``."""
        what = "members" if self.is_roster else "messages"
        return f"{what}:{self.mod.describe()}"

    # -- guild-level inventories -------------------------------------------------------

    def guild_roles(self) -> list[dict]:
        """Every role the guild has, when the export carries the inventory."""
        return self._roles_of(self.guild)

    def guild_emojis(self) -> list[dict]:
        if "emojiKeys" in self.guild:
            return [self.lookups.emoji(k) for k in self.guild["emojiKeys"] or []]
        return list(self.guild.get("emojis") or [])

    def guild_stickers(self) -> list[dict]:
        if "stickerIds" in self.guild:
            return [self.lookups.sticker(i) for i in self.guild["stickerIds"] or []]
        return list(self.guild.get("stickers") or [])

    def owners(self) -> list[Person]:
        """The guild owner and the thread owner, when the export resolved them."""
        out = []
        for container in (self.guild, self.channel or {}):
            inline = container.get("owner")
            if inline is not None:
                out.append(self.person(inline))
                continue
            # Normalized: only the ID is written inline, and the user went to the table
            owner_id = container.get("ownerId")
            if owner_id is not None and owner_id in self.lookups.users:
                out.append(self.person_by_id(owner_id))
        return out

    # -- people ------------------------------------------------------------------------

    def person_by_id(self, user_id) -> Person:
        """Resolve a reference from a normalized document."""
        user = self.lookups.users.get(user_id)
        if user is None:
            # A reference with nothing behind it: keep the ID so the message still has an
            # author, rather than dropping the row
            return Person(user={"id": user_id})

        if self.mod.split_users:
            return self._assemble(user, self.lookups.members.get(user_id))
        return self._unmerge(user)

    def person(self, obj: dict | None) -> Person | None:
        """Normalize a person written inline, in either shape."""
        if obj is None:
            return None
        if "member" in obj:
            # --split-users: the user is the outer object, with a nullable member on it
            user = {k: v for k, v in obj.items() if k != "member"}
            return self._assemble(user, obj["member"])
        return self._unmerge(obj)

    def _assemble(self, user: dict, member: dict | None) -> Person:
        """Already split by the exporter; only the roles need resolving."""
        roles = self._roles_of(member) if member else []
        rendered = member.get("displayName") if member else user.get("displayName")
        return Person(
            user=dict(user),
            member=dict(member) if member else None,
            roles=roles,
            rendered=rendered,
        )

    def _unmerge(self, merged: dict) -> Person:
        """Take apart the single object vanilla DCE writes.

        See the module docstring for which parts of this are exact and which are not.
        """
        roles = self._roles_of(merged)

        # Only --extended carries the account-wide display name. Without it there is no honest
        # way to fill the users table's, so it stays empty rather than being guessed at.
        global_display = merged.get("displayName")

        avatar, member_avatar = _split_asset(merged.get("avatarUrl"))
        banner, member_banner = _split_asset(merged.get("bannerUrl"))

        # Only keys the document actually carries are set, never keys it merely implies.  A
        # field missing because the export was made without --extended must not be written as
        # NULL over a value some other export already established -- absent and null are
        # different claims, and DCE is careful to make them different in the JSON too.
        user = {
            "id": merged.get("id"),
            "name": merged.get("name"),
            "discriminator": merged.get("discriminator"),
            "isBot": merged.get("isBot"),
        }
        if "displayName" in merged:
            user["displayName"] = global_display

        # A guild override stands *in place of* the global image in a merged object, rather
        # than beside it, so when one is present the global one is unknown -- not absent. The
        # key is left out entirely, or importing a merged export after a split one would erase
        # a perfectly good global avatar that the merged export simply could not see.
        if member_avatar is None:
            user["avatarUrl"] = avatar
        if "bannerUrl" in merged and member_banner is None:
            user["bannerUrl"] = banner

        resolved = merged.get("nickname")
        # Whatever DCE printed for this person is 'nickname', collapsed and all -- which is
        # exactly what a resolved mention in a message body says
        rendered = merged.get("nickname")

        if not _is_member(merged, roles, member_avatar, member_banner):
            return Person(user=user, member=None, roles=[], rendered=rendered)

        # The merged 'nickname' is nickname -> display name -> username already collapsed. When
        # the user's own display name is known, whichever of the two it isn't equal to is the
        # real nickname; when it is equal, no nickname was set. Without --extended nothing can
        # be compared, so the collapsed value is kept as the best available.
        if global_display is not None:
            nickname = resolved if resolved != global_display else None
        else:
            nickname = resolved

        member = {
            "nickname": nickname,
            "displayName": resolved,
            "color": merged.get("color"),
            "avatarUrl": member_avatar,
        }
        if "bannerUrl" in merged:
            member["bannerUrl"] = member_banner
        for extended_key in ("joinedAt", "premiumSince", "isPending", "flags"):
            if extended_key in merged:
                member[extended_key] = merged[extended_key]

        return Person(user=user, member=member, roles=roles, rendered=rendered)

    def _roles_of(self, obj: dict | None) -> list[dict]:
        if not obj:
            return []
        if "roleIds" in obj:
            return [self.lookups.role(i) for i in obj["roleIds"] or []]
        return list(obj.get("roles") or [])

    # -- the main array ----------------------------------------------------------------

    def messages(self) -> Iterator[dict]:
        """Walk the messages, each one already rehydrated and with its people split."""
        if self.is_roster:
            return iter(())
        return (self._message(m) for m in self._raw.items())

    def members(self) -> Iterator[Person]:
        """Walk an ``exportusers`` roster.

        A roster entry is the other way round from a message export's: the member is the outer
        object, because every entry *is* a member, with the user nested inside it.
        """
        if not self.is_roster:
            return iter(())
        return (self._roster_entry(m) for m in self._raw.items())

    def _roster_entry(self, entry: dict) -> Person:
        user = entry.get("user")
        if user is None:
            # Normalized rosters put the users in a table of their own
            user = self.lookups.users.get(entry.get("userId")) or {
                "id": entry.get("userId")
            }
        user = dict(user)
        if not self.mod.full_users:
            # The user nested in a roster entry is a partial one: Discord sends 'banner' as
            # null on it whether or not the account has one, so without --full-users the field
            # means "not known" rather than "not set", and must not be written as a value
            user.pop("bannerUrl", None)

        member = {k: v for k, v in entry.items() if k not in ("user", "roles", "roleIds")}
        return Person(
            user=user,
            member=member,
            roles=self._roles_of(entry),
            rendered=member.get("displayName"),
        )

    def _text(self, value):
        """A body as it should be stored, with resolved mentions put back if asked."""
        if self.unresolver is None or not value:
            return value
        return self.unresolver.unresolve(value, self._mentioned)

    def _message(self, raw: dict) -> dict:
        message = dict(raw)

        message["author"] = (
            self.person_by_id(raw["authorId"])
            if "authorId" in raw
            else self.person(raw.get("author"))
        )

        if "mentionIds" in raw:
            message["mentions"] = [self.person_by_id(i) for i in raw["mentionIds"] or []]
        else:
            message["mentions"] = [
                p for p in (self.person(m) for m in raw.get("mentions") or []) if p
            ]

        # Whatever the message says it mentions is what a resolved '@name' in its body may be
        # turned back into, so it has to be resolved before the body is touched
        self._mentioned = message["mentions"]

        if self.unresolver is not None:
            # A vanilla export has no guild role inventory, but every person in it carries the
            # roles they hold, with the IDs -- which between them name most of the roles the
            # server has. Pooled as they are met, so a role mention later in the file can be
            # put back even though nothing ever listed the roles outright.
            for person in (message["author"], *self._mentioned):
                if person is not None and person.roles:
                    self.unresolver.add_roles(person.roles)

        message["stickers"] = self._stickers(raw)
        message["reactions"] = [self._reaction(r) for r in raw.get("reactions") or []]
        message["inlineEmojis"] = self._inline_emojis(raw)
        message["embeds"] = [self._embed(e) for e in raw.get("embeds") or []]

        if self.unresolver is not None:
            message["content"] = self._text(raw.get("content"))

        interaction = raw.get("interaction")
        if interaction:
            interaction = dict(interaction)
            interaction["user"] = (
                self.person_by_id(interaction["userId"])
                if "userId" in interaction
                else self.person(interaction.get("user"))
            )
            message["interaction"] = interaction

        # A forward's payload is stored verbatim, so its own references are resolved to keep
        # the stored JSON self-contained rather than full of dangling keys
        forwarded = raw.get("forwardedMessage")
        if forwarded:
            resolved = dict(forwarded)
            resolved["stickers"] = self._stickers(forwarded)
            resolved["embeds"] = [self._embed(e) for e in forwarded.get("embeds") or []]
            resolved.pop("stickerIds", None)
            if self.unresolver is not None:
                resolved["content"] = self._text(forwarded.get("content"))
            message["forwardedMessage"] = resolved

        return message

    def _stickers(self, container: dict) -> list[dict]:
        if "stickerIds" in container:
            return [self.lookups.sticker(i) for i in container["stickerIds"] or []]
        return list(container.get("stickers") or [])

    def _inline_emojis(self, container: dict) -> list[dict]:
        if "inlineEmojiKeys" in container:
            return [self.lookups.emoji(k) for k in container["inlineEmojiKeys"] or []]
        return list(container.get("inlineEmojis") or [])

    def _embed(self, raw: dict) -> dict:
        embed = dict(raw)
        # An embed's description can mention custom emoji too, lifted into the same root table
        embed["inlineEmojis"] = self._inline_emojis(raw)
        embed.pop("inlineEmojiKeys", None)

        # An embed's text goes through the same formatter a message body does, so it carries
        # the same resolved mentions and drifts for the same reasons
        if self.unresolver is not None:
            for key in ("title", "description"):
                if embed.get(key):
                    embed[key] = self._text(embed[key])
            if embed.get("fields"):
                embed["fields"] = [
                    {**f, "name": self._text(f.get("name")), "value": self._text(f.get("value"))}
                    for f in embed["fields"]
                ]

        return embed

    def _reaction(self, raw: dict) -> dict:
        emoji = (
            self.lookups.emoji(raw["emojiKey"])
            if "emojiKey" in raw
            else raw.get("emoji") or {}
        )

        if "userIds" in raw:
            users = [self.person_by_id(i) for i in raw["userIds"] or []]
        else:
            users = [p for p in (self.person(u) for u in raw.get("users") or []) if p]

        return {"emoji": emoji, "count": raw.get("count"), "users": users}


def load(source: Source, **kwargs) -> Document:
    """Open and normalize one export."""
    from .reader import open_document

    return Document(open_document(source, **kwargs))


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _split_asset(url: str | None) -> tuple[str | None, str | None]:
    """Decide whether a merged avatar or banner URL is the global one or a guild override.

    Returns ``(user_url, member_url)``, exactly one of which is set.  Under ``--media`` every
    URL has been rewritten to a local path and the two are no longer distinguishable, so the
    image is credited to the user -- which is what it is, the overwhelming majority of the time.
    """
    if not url:
        return None, None
    if _GUILD_ASSET.search(url):
        return None, url
    return url, None


def _is_member(merged: dict, roles: list, member_avatar, member_banner) -> bool:
    """Decide whether a merged object describes someone this guild actually has a record of.

    ``--split-users`` says so outright.  A merged object does not, so this looks for something
    that could only have come from a member: a join date, a role, a name colour, or an image
    served from the guild's own path.  Someone with none of those is either a non-member or a
    member so plain that no guild-specific fact about them was ever recorded -- and in the
    second case there would be nothing to put in the row anyway.

    Getting this right is what keeps a merged export and a split one producing the same rows.
    """
    if merged.get("joinedAt") is not None:
        return True
    if roles:
        return True
    if merged.get("color") is not None:
        return True
    if member_avatar or member_banner:
        return True
    if merged.get("premiumSince") is not None:
        return True
    return bool(merged.get("flags"))
