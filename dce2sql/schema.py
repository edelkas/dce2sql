"""The database schema, declared once as metadata.

Each adapter renders DDL from these objects rather than keeping its own ``CREATE TABLE``
script, so adding an engine cannot silently drift from the schema the importer writes against.
See ``docs/SQL.md`` for what every column means; this file is the machine-readable copy.

Two conventions, both from SQL.md:

* Tables that map to real Discord objects use Discord's own 8-byte ID as the primary key and
  carry ``created_at``/``updated_at``.  Everything else auto-increments.
* Any column whose name ends in ``_id`` is a reference: it gets an index, and it is declared
  as an 8-byte ``ID`` whether it points at a Discord snowflake or at an auto-assigned key.
  SQLite hides a mistake here, since its INTEGER is 8 bytes whatever the declaration says; the
  other engines do not, and a truncated snowflake would be silent and unrecoverable.

References are **not** enforced with foreign keys.  An archive routinely points outside itself:
a reply whose parent predates the export range, a sticker whose guild was never exported, a
message quoting a channel that no longer exists.  Constraints would turn all of those into
import failures, which is exactly backwards for a tool whose job is to never lose anything.
"""

from __future__ import annotations

from dataclasses import dataclass

# Logical column types.  Adapters map these onto whatever the engine actually offers.
ID = "id"  # 8-byte Discord snowflake
INT = "int"
BOOL = "bool"
TS = "ts"  # instant; native type where available, Unix seconds otherwise
STR = "str"  # short, bounded; VARCHAR where available
TEXT = "text"  # potentially long, or of no knowable length -- see URL below
JSON = "json"  # native JSON column where available, serialized string otherwise


#: A URL.  Stored as TEXT rather than a bounded VARCHAR, which is a deliberate departure from
#: SQL.md's "names or URLs are short strings": a URL in an embed is whatever somebody typed into
#: a message, so its only real limit is Discord's 4,000-character message body, and one found in
#: the wild ran to 1,995 characters (a CDN link with an essay appended as a ?comment= parameter).
#: Everything else bounded here is bounded by Discord itself -- a username is 32 characters
#: because Discord will not accept a 33rd -- and those stay VARCHAR.
URL = TEXT


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    null: bool = True
    pk: bool = False
    auto: bool = False  # auto-incrementing primary key
    size: int | None = None  # max length, for engines that want one
    index: bool = False  # forced index, on top of the _id/timestamp rule
    default: object = None
    #: Holds a Discord CDN link, whose signature is re-generated on every export.  Two values
    #: differing only in that are the same link, and must not read as an edit.
    signed_url: bool = False


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    #: Column tuples that must be unique.  A part may be a plain column name, or a
    #: :class:`NullSafe` wrapper around one -- see that class for why some of them have to be.
    unique: tuple[tuple, ...] = ()
    comment: str = ""

    def column(self, name: str) -> Column:
        for col in self.columns:
            if col.name == name:
                return col
        raise KeyError(f"{self.name} has no column {name!r}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    @property
    def primary_key(self) -> Column:
        for col in self.columns:
            if col.pk:
                return col
        raise KeyError(f"{self.name} has no primary key")

    def indexed(self) -> list[str]:
        """Every column that should get a plain index, per the convention above."""
        out = []
        for col in self.columns:
            if col.pk:
                continue  # already indexed by being the primary key
            if col.index or col.type == TS or col.name.endswith("_id"):
                out.append(col.name)
        return out


def _timestamps() -> tuple[Column, ...]:
    """The pair every base table carries, so the merge policy has something to record into."""
    return (
        Column("created_at", TS, null=False),
        Column("updated_at", TS, null=False),
    )


def _base(name: str, *columns: Column, unique=(), comment="") -> Table:
    """A table keyed by a real Discord ID."""
    return Table(
        name,
        (Column("id", ID, null=False, pk=True), *columns, *_timestamps()),
        unique=unique,
        comment=comment,
    )


def _aux(name: str, *columns: Column, unique=(), comment="") -> Table:
    """A table for something Discord has no object for; auto-incrementing, no timestamps."""
    return Table(
        name,
        (Column("id", INT, null=False, pk=True, auto=True), *columns),
        unique=unique,
        comment=comment,
    )


def _seed(name: str, comment: str) -> Table:
    """A lookup table of Discord's own enumeration values, keyed by the official number."""
    return Table(
        name,
        (
            Column("id", INT, null=False, pk=True),
            Column("name", STR, null=False, size=64),
        ),
        comment=comment,
    )


@dataclass(frozen=True)
class NullSafe:
    """A unique-constraint key part that treats NULL as a value.

    SQLite, MySQL and PostgreSQL all treat NULLs as *distinct* inside a unique index, so a
    constraint containing a nullable column cannot deduplicate the rows where it is NULL --
    precisely the rows a re-import would otherwise double.  Substituting a sentinel makes the
    constraint do what it reads as.

    What the sentinel *is* has to be left to the adapter, because it has to have the column's
    own type: zero serves for an ID or a count, but a timestamp column is a real date on the
    engines that have one, and ``COALESCE(timestamp, 0)`` is a type error there.
    """

    column: str


# ------------------------------------------------------------------------------------------
# Base tables
# ------------------------------------------------------------------------------------------

GUILDS = _base(
    "guilds",
    Column("name", STR, size=100),
    Column("description", TEXT),
    Column("url", URL),
    Column("icon", URL),
    Column("banner", URL),
    Column("splash", URL),
    Column("boost_level", INT),
    Column("boost_count", INT),
    Column("owner_id", ID),
    comment="Discord servers.",
)

CHANNELS = _base(
    "channels",
    Column("guild_id", ID),
    Column("name", STR, size=100),
    Column("type", INT),
    Column("topic", TEXT),
    Column("parent_id", ID),
    Column("position", INT),
    Column("members", INT),
    Column("archived", BOOL),
    Column("locked", BOOL),
    Column("owner_id", ID),
    comment="Channels, categories, forums and threads alike; 'parent_id' nests them.",
)

USERS = _base(
    "users",
    Column("name", STR, size=32, index=True),
    Column("discriminator", INT),
    Column("display", STR, size=32, index=True),
    Column("bot", BOOL, index=True),
    Column("avatar", URL),
    Column("banner", URL),
    Column("deleted", BOOL, null=False, default=False),
    comment="Discord accounts, global. Guild-specific profiles live in 'members'.",
)

MESSAGES = _base(
    "messages",
    Column("channel_id", ID),
    Column("type", INT, index=True),
    Column("timestamp", TS),
    Column("edited_timestamp", TS),
    Column("pinned", BOOL, index=True),
    Column("user_id", ID),
    Column("content", TEXT),
    Column("reference_id", ID),
    Column("reference_type", INT),
    Column("interaction_id", ID),
    Column("interaction_user_id", ID),
    Column("components", JSON),
    Column("forwarded", JSON),
    comment="One row per message. Superseded content goes to 'message_history'.",
)

ATTACHMENTS = _base(
    "attachments",
    Column("message_id", ID),
    Column("name", STR, size=255),
    Column("size", INT),
    Column("url", URL, signed_url=True),
    comment="Files uploaded with a message. Discord does not deduplicate these.",
)

ROLES = _base(
    "roles",
    Column("guild_id", ID),
    Column("name", STR, size=100),
    Column("color", INT),
    Column("position", INT),
    comment="Guild roles.",
)

EMOJIS = _base(
    "emojis",
    Column("guild_id", ID),
    Column("name", STR, size=64, index=True),
    Column("code", STR, size=64, index=True),
    Column("animated", BOOL),
    Column("url", URL),
    comment=(
        "Custom emoji keyed by their Discord ID; standard Unicode emoji have none, so they get "
        "a low synthetic ID that cannot collide with a snowflake, and a NULL guild."
    ),
)

STICKERS = _base(
    "stickers",
    Column("guild_id", ID),
    Column("name", STR, size=30),
    Column("format", STR, size=16),
    Column("url", URL),
    comment="Stickers. 'guild_id' is only recoverable from an extended export's inventory.",
)

INTERACTIONS = _base(
    "interactions",
    Column("name", STR, size=100, index=True),
    comment=(
        "One row per slash-command invocation: Discord's interaction ID identifies the "
        "invocation, not the command. SELECT DISTINCT name for the command list."
    ),
)

# ------------------------------------------------------------------------------------------
# Auxiliary tables
# ------------------------------------------------------------------------------------------

CHANNEL_TYPES = _seed("channel_types", "Discord's official channel types.")
MESSAGE_TYPES = _seed("message_types", "Discord's official message types.")
REFERENCE_TYPES = _seed("reference_types", "Discord's official message reference types.")

MESSAGE_HISTORY = _aux(
    "message_history",
    Column("message_id", ID, null=False),
    Column("timestamp", TS),
    Column("content", TEXT),
    unique=(("message_id", NullSafe("timestamp")),),
    comment="Superseded message contents, so an edit seen by a later export loses nothing.",
)

EMBEDS = _aux(
    "embeds",
    Column("message_id", ID, null=False),
    # Embeds have no ID of their own and their URL may be NULL or repeated, so their position
    # in the message's array is the only stable identity available for a re-import to match on
    Column("ordinal", INT, null=False),
    Column("title", STR, size=256),
    Column("url", URL),
    Column("description", TEXT),
    Column("timestamp", TS),
    Column("color", INT),
    Column("thumbnail_id", ID),
    Column("author_name", STR, size=256),
    Column("author_url", URL),
    Column("author_icon_id", ID),
    Column("image_id", ID),
    Column("video_id", ID),
    Column("footer", STR, size=2048),
    Column("footer_icon_id", ID),
    Column("fields", JSON),
    unique=(("message_id", "ordinal"),),
    comment="Message embeds, flattened. Rich images and videos go to 'resources'.",
)

RESOURCES = _aux(
    "resources",
    Column("embed_id", ID, null=False),
    # Which of the embed's slots this filled: thumbnail, image, video, author_icon, footer_icon,
    # or images[n]. Gives a re-import something to match on, and says what the row meant.
    Column("slot", STR, null=False, size=32),
    Column("url", URL, signed_url=True),
    Column("proxied_url", URL, signed_url=True),
    Column("width", INT),
    Column("height", INT),
    unique=(("embed_id", "slot"),),
    comment="Images and videos inside an embed. 'url' is canonical, 'proxied_url' is Discord's.",
)

# ------------------------------------------------------------------------------------------
# Junction tables
# ------------------------------------------------------------------------------------------

#: 'members' is a junction in shape only -- it carries real, mutable, guild-specific data, so
#: it gets the same created_at/updated_at treatment as a base table for the merge policy's sake.
MEMBERS = Table(
    "members",
    (
        Column("id", INT, null=False, pk=True, auto=True),
        Column("user_id", ID, null=False),
        Column("guild_id", ID, null=False),
        Column("display", STR, size=32, index=True),
        Column("color", INT),
        Column("avatar", URL),
        Column("banner", URL),
        Column("joined_at", TS),
        Column("boosting_since", TS),
        *_timestamps(),
    ),
    unique=(("user_id", "guild_id"),),
    comment="A user's profile inside one guild. 'display' is the nickname, NULL when unset.",
)

USER_ROLES = _aux(
    "user_roles",
    Column("user_id", ID, null=False),
    Column("role_id", ID, null=False),
    unique=(("user_id", "role_id"),),
    comment="Role assignments. The role already carries the guild.",
)

REACTIONS = _aux(
    "reactions",
    Column("message_id", ID, null=False),
    Column("user_id", ID),
    Column("emoji_id", ID, null=False),
    # 1 on a row that names its reacting user. When the export was made without fetching the
    # user list, a single row stands in for the whole reaction with the total here and a NULL
    # user, so SUM(count) per (message_id, emoji_id) is the true total in either case.
    Column("count", INT, null=False, default=1),
    unique=(("message_id", "emoji_id", NullSafe("user_id")),),
    comment="Who reacted with what, or how many did when the names weren't exported.",
)

MENTIONS = _aux(
    "mentions",
    Column("message_id", ID, null=False),
    Column("user_id", ID, null=False),
    unique=(("message_id", "user_id"),),
    comment="Users mentioned in a message.",
)

MESSAGE_EMOJIS = _aux(
    "message_emojis",
    Column("message_id", ID, null=False),
    Column("emoji_id", ID, null=False),
    Column("embed_id", ID),
    unique=(("message_id", "emoji_id", NullSafe("embed_id")),),
    comment="Emoji used in a message's content, or inside one of its embeds.",
)

MESSAGE_STICKERS = _aux(
    "message_stickers",
    Column("message_id", ID, null=False),
    Column("sticker_id", ID, null=False),
    unique=(("message_id", "sticker_id"),),
    comment="Stickers attached to a message.",
)

# ------------------------------------------------------------------------------------------
# Metadata
# ------------------------------------------------------------------------------------------

IMPORTS = _aux(
    "imports",
    Column("sha1", STR, null=False, size=40, index=True),
    # 'messages' for a channel export, 'members' for an exportusers roster
    Column("kind", STR, null=False, size=16),
    Column("guild_id", ID),
    Column("channel_id", ID),
    Column("start_timestamp", TS),
    Column("end_timestamp", TS),
    Column("exported_at", TS),
    Column("imported_at", TS, null=False),
    Column("first_message_id", ID),
    Column("last_message_id", ID),
    Column("total_message_count", INT),
    Column("new_message_count", INT),
    Column("edit_message_count", INT),
    # Whether resolved mentions in this file's message bodies were put back into their raw
    # form on the way in. It changes what is stored, so the archive should say so.
    Column("unresolved", BOOL, null=False, default=False),
    comment=(
        "One row per file imported, deliberately never deduplicated: re-importing the same "
        "file is a no-op everywhere else, and the row is the evidence that it happened."
    ),
)

#: Creation order, which is also dependency order for anything that reads it.
TABLES: tuple[Table, ...] = (
    CHANNEL_TYPES,
    MESSAGE_TYPES,
    REFERENCE_TYPES,
    GUILDS,
    CHANNELS,
    USERS,
    MEMBERS,
    ROLES,
    USER_ROLES,
    EMOJIS,
    STICKERS,
    INTERACTIONS,
    MESSAGES,
    MESSAGE_HISTORY,
    ATTACHMENTS,
    EMBEDS,
    RESOURCES,
    MENTIONS,
    REACTIONS,
    MESSAGE_EMOJIS,
    MESSAGE_STICKERS,
    IMPORTS,
)

BY_NAME: dict[str, Table] = {t.name: t for t in TABLES}

#: Tables holding archived data, as opposed to seeds and bookkeeping.  Used by the tests that
#: assert two differently-shaped exports produce the same database.
DATA_TABLES: tuple[str, ...] = tuple(
    t.name
    for t in TABLES
    if t.name not in {"channel_types", "message_types", "reference_types", "imports"}
)

#: Columns excluded when comparing two databases for equality: they record when the import
#: happened, not what was imported.
BOOKKEEPING_COLUMNS: frozenset[str] = frozenset({"created_at", "updated_at"})
