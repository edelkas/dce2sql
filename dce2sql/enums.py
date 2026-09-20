"""Discord enumerations, and the mapping from the names DCE writes to their official values.

DCE serializes ``type`` fields as the *name* of its own C# enum rather than as the number
Discord sends.  Two consequences drive everything in this module:

* DCE's enums are deliberately incomplete -- it only names the values it has a use for.  The
  parser casts the raw integer (``(MessageKind)t``), so an unrecognized value survives the round
  trip as a bare number string.  A ``type`` field is therefore *either* a DCE name *or* a number.
* DCE's names are not Discord's names.  ``GuildTextChat`` is Discord's ``GUILD_TEXT``.  The
  mapping runs name -> integer -> official name, and only the integer is stored in the data
  tables; the official names live in the seed tables.
"""

from __future__ import annotations

# --------------------------------------------------------------------------------------------
# Channel types
# --------------------------------------------------------------------------------------------

# https://docs.discord.com/developers/resources/channel#channel-object-channel-types
# Values 6-9 have never existed.  Seeded into the 'channel_types' table.
CHANNEL_TYPES: dict[int, str] = {
    0: "GUILD_TEXT",
    1: "DM",
    2: "GUILD_VOICE",
    3: "GROUP_DM",
    4: "GUILD_CATEGORY",
    5: "GUILD_ANNOUNCEMENT",
    10: "ANNOUNCEMENT_THREAD",
    11: "PUBLIC_THREAD",
    12: "PRIVATE_THREAD",
    13: "GUILD_STAGE_VOICE",
    14: "GUILD_DIRECTORY",
    15: "GUILD_FORUM",
    16: "GUILD_MEDIA",
}

# DiscordChatExporter.Core/Discord/Data/ChannelKind.cs
DCE_CHANNEL_KINDS: dict[str, int] = {
    "GuildTextChat": 0,
    "DirectTextChat": 1,
    "GuildVoiceChat": 2,
    "DirectGroupTextChat": 3,
    "GuildCategory": 4,
    "GuildNews": 5,
    "GuildNewsThread": 10,
    "GuildPublicThread": 11,
    "GuildPrivateThread": 12,
    "GuildStageVoice": 13,
    "GuildDirectory": 14,
    "GuildForum": 15,
}

# The type a parent channel is assumed to have when it is only known from a child's 'categoryId'
CATEGORY_TYPE = 4

# --------------------------------------------------------------------------------------------
# Message types
# --------------------------------------------------------------------------------------------

# https://docs.discord.com/developers/resources/message#message-object-message-types
# The gaps (13, 30, 33-35, 40-43, 45) are values Discord has never assigned or has withdrawn.
MESSAGE_TYPES: dict[int, str] = {
    0: "DEFAULT",
    1: "RECIPIENT_ADD",
    2: "RECIPIENT_REMOVE",
    3: "CALL",
    4: "CHANNEL_NAME_CHANGE",
    5: "CHANNEL_ICON_CHANGE",
    6: "CHANNEL_PINNED_MESSAGE",
    7: "USER_JOIN",
    8: "GUILD_BOOST",
    9: "GUILD_BOOST_TIER_1",
    10: "GUILD_BOOST_TIER_2",
    11: "GUILD_BOOST_TIER_3",
    12: "CHANNEL_FOLLOW_ADD",
    14: "GUILD_DISCOVERY_DISQUALIFIED",
    15: "GUILD_DISCOVERY_REQUALIFIED",
    16: "GUILD_DISCOVERY_GRACE_PERIOD_INITIAL_WARNING",
    17: "GUILD_DISCOVERY_GRACE_PERIOD_FINAL_WARNING",
    18: "THREAD_CREATED",
    19: "REPLY",
    20: "CHAT_INPUT_COMMAND",
    21: "THREAD_STARTER_MESSAGE",
    22: "GUILD_INVITE_REMINDER",
    23: "CONTEXT_MENU_COMMAND",
    24: "AUTO_MODERATION_ACTION",
    25: "ROLE_SUBSCRIPTION_PURCHASE",
    26: "INTERACTION_PREMIUM_UPSELL",
    27: "STAGE_START",
    28: "STAGE_END",
    29: "STAGE_SPEAKER",
    31: "STAGE_TOPIC",
    32: "GUILD_APPLICATION_PREMIUM_SUBSCRIPTION",
    36: "GUILD_INCIDENT_ALERT_MODE_ENABLED",
    37: "GUILD_INCIDENT_ALERT_MODE_DISABLED",
    38: "GUILD_INCIDENT_REPORT_RAID",
    39: "GUILD_INCIDENT_REPORT_FALSE_ALARM",
    44: "PURCHASE_NOTIFICATION",
    46: "POLL_RESULT",
}

# DiscordChatExporter.Core/Discord/Data/MessageKind.cs
DCE_MESSAGE_KINDS: dict[str, int] = {
    "Default": 0,
    "RecipientAdd": 1,
    "RecipientRemove": 2,
    "Call": 3,
    "ChannelNameChange": 4,
    "ChannelIconChange": 5,
    "ChannelPinnedMessage": 6,
    "GuildMemberJoin": 7,
    "ThreadCreated": 18,
    "Reply": 19,
    "ThreadStarterMessage": 21,
    "PollResult": 46,
}

# --------------------------------------------------------------------------------------------
# Message reference types
# --------------------------------------------------------------------------------------------

# https://docs.discord.com/developers/resources/message#message-reference-types
REFERENCE_TYPES: dict[int, str] = {
    0: "DEFAULT",
    1: "FORWARD",
}

# DiscordChatExporter.Core/Discord/Data/MessageReferenceKind.cs
DCE_REFERENCE_KINDS: dict[str, int] = {
    "Default": 0,
    "Forward": 1,
}

# A reference written before DCE knew about forwards carries no type at all
DEFAULT_REFERENCE_TYPE = 0

# --------------------------------------------------------------------------------------------
# Sticker formats
# --------------------------------------------------------------------------------------------

# Stored as an uppercase string rather than an integer, per the schema.  DCE writes the C# enum
# name, which is capitalized ('Png'), so only the case has to be normalized.
STICKER_FORMATS: dict[str, str] = {
    "png": "PNG",
    "apng": "APNG",
    "lottie": "LOTTIE",
    "gif": "GIF",
}

# --------------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------------


def resolve_kind(value: str | int | None, names: dict[str, int]) -> int | None:
    """Turn a DCE ``type`` field into the official integer.

    Accepts a DCE enum name, a bare number (DCE's fallback for a value its enum doesn't name),
    or an integer.  Returns ``None`` for anything else, which the importer counts as a warning
    rather than treating as fatal: a future DCE release naming a new value must not be able to
    abort an import of an otherwise perfectly good archive.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value

    text = value.strip()
    if text in names:
        return names[text]

    # DCE fell back to the raw number, either because Discord added a value or because the
    # export predates the name
    try:
        return int(text)
    except ValueError:
        return None


def resolve_channel_type(value: str | int | None) -> int | None:
    return resolve_kind(value, DCE_CHANNEL_KINDS)


def resolve_message_type(value: str | int | None) -> int | None:
    return resolve_kind(value, DCE_MESSAGE_KINDS)


def resolve_reference_type(value: str | int | None) -> int | None:
    if value is None:
        return DEFAULT_REFERENCE_TYPE
    return resolve_kind(value, DCE_REFERENCE_KINDS)


def resolve_sticker_format(value: str | None) -> str | None:
    if not value:
        return None
    return STICKER_FORMATS.get(value.strip().lower(), value.strip().upper())
