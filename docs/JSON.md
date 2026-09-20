# JSON exports schema

This file contains detailed documentation about the format of DCE's JSON exports. It does **not** contain much documentation about the underlying Discord objects themselves, other than sparse links to the official [API Reference](https://docs.discord.com/developers/reference). Its contents are used to seed the SQL database (see [SQL.md](SQL.md)).

This document also includes fields only present in [my fork](https://github.com/edelkas/DiscordChatExporter) for extended exports. This fork also add the ability to export a guild's member list and corresponding user objects, which will be useful for us. A JSON file produced by this fork can be recognized by the presence of a top-level key `mod`.

## Table of contents

- [Overview](#overview)
- [Mod object](#mod-object)
- [Normalized output](#normalized-output)
- [Guild object](#guild-object)
- [Channel object](#channel-object)
- [Date range object](#date-range-object)
- [Message object](#message-object)
   * [User object](#user-object)
      + [Role object](#role-object)
   * [Attachment object](#attachment-object)
   * [Emoji object](#emoji-object)
   * [Embed object](#embed-object)
      + [Resource object](#resource-object)
      + [Embed author object](#embed-author-object)
      + [Embed footer object](#embed-footer-object)
      + [Embed field object](#embed-field-object)
   * [Sticker object](#sticker-object)
   * [Reaction object](#reaction-object)
   * [Mention object](#mention-object)
   * [Reference object](#reference-object)
   * [Interaction object](#interaction-object)
   * [Forwarded message object](#forwarded-message-object)
- [Member export](#member-export)
   * [Member object](#member-object)

## Overview

Each JSON file contains the export for a single channel, usually within a given date range. Its top-level structure is the following:

```
{
  "guild": Object,
  "channel": Object,
  "dateRange": Object,
  "exportedAt": String,
  "messages": Array<Object>,
  "messageCount": Integer
}
```

`dateRange` is always written by current versions, with `after` and `before` set to NULL when the export wasn't restricted; older exports may omit the object entirely. `exportedAt` contains the precise timestamp of when the export operation was carried out. The `messageCount` should match the size of the `message` array.

The extended fork also includes the following fields:

```
{
  "mod": Object
}
```

The `mod` object identifies this JSON file as being exported with the extended fork, and is detailed in [Mod object](#mod-object). Note that the fork writes it unconditionally, even when no fork-specific option was used, so its *presence* distinguishes fork from upstream while its *contents* distinguish one fork export from another.

The JSON is usually completely denormalized, so there's a large amount of duplication as we'll see. For instance, the entire user information is duplicated on each posted message. This leads to very large yet very compressible files, an issue we'll solve in our SQL schema. The fork's `--normal` option changes this, adding several more top-level keys; see [Normalized output](#normalized-output).

Some notes about specific common fields:

- **IDs** are always encoded as strings, but they correspond to 8-byte integers, and represent the objects' IDs in Discord's platform.
- **Timestamps** are stored as strings in ISO-8601 format, usually `YYYY-MM-DDThh:mm:ss+hh:mm`, but sometimes including floating part for the seconds.
- **Colors** are stored as hex strings in the usual `"#RRGGBB"` format.
- **CDN links** to attachments and embedded media are signed. Discord appends `ex`, `is` and `hm` parameters which are regenerated on every export and expire within about a day, so two exports of the same attachment never agree on the URL. The part before the query string is stable and is what identifies the file.
- **Enumerations** (channel types, message types, reference types, sticker formats) are written as the *name* of DCE's own C# enum rather than Discord's numeric value, and DCE's enums deliberately cover only the values it has a use for. The parser casts the raw integer, so an unrecognized value survives as a bare number string. Every such field is therefore either a name or a number.

Some of the object keys may not always be present, either because they're optional, or because they were added in later versions of DCE. I've tried to make some of those explicit, but I may have missed some. In those cases, parsing should be robust, defaulting to NULL (or empty of arrays).

## Mod object

The `mod` object is written by the fork on every export, and says which of its options produced the file. Each key is a boolean:

```
{
  "normal": Boolean,
  "extended": Boolean,
  "splitUsers": Boolean,
  "reactionUsers": Boolean,
  "cache": Boolean
}
```

| Key | Meaning |
| --- | --- |
| `normal` | Entities were written to lookup tables and referenced by ID. See [Normalized output](#normalized-output). |
| `extended` | The extra fields marked throughout this document are present. |
| `splitUsers` | User and member were written as two objects. See [User object](#user-object). |
| `reactionUsers` | The reacting users were fetched. When false, every reaction's `users` array is empty although its `count` is not. |
| `cache` | Member data may come from a cross-run cache, and so may be up to the cache's TTL out of date. |

Keys are added over time, so an export made before a given option existed simply lacks its key. Absent should be read as false, with one exception: `reactionUsers` defaults to **true**, because before the flag existed there was no way for the users not to be fetched.

A member export (see [Member export](#member-export)) writes a smaller `mod` object with a different key, `fullUsers`.

## Normalized output

With the fork's `--normal` option, entities that have an identity are written once into lookup tables at the root of the document and referenced by ID elsewhere. The document gains these top-level keys:

```
{
  "users": Array<Object>,
  "members": Array<Object>,
  "roles": Array<Object>,
  "emojis": Array<Object>,
  "stickers": Array<Object>
}
```

`members` only appears together with `splitUsers`. Wherever the denormalized form embeds an object, the normalized form puts a reference in its place:

| Denormalized | Normalized | Table |
| --- | --- | --- |
| `message.author` | `message.authorId` | `users` |
| `message.mentions` | `message.mentionIds` | `users` |
| `message.stickers` | `message.stickerIds` | `stickers` |
| `message.inlineEmojis` | `message.inlineEmojiKeys` | `emojis` |
| `embed.inlineEmojis` | `embed.inlineEmojiKeys` | `emojis` |
| `reaction.emoji` | `reaction.emojiKey` | `emojis` |
| `reaction.users` | `reaction.userIds` | `users` |
| `interaction.user` | `interaction.userId` | `users` |
| `user.roles`, `member.roles`, `guild.roles` | `roleIds` | `roles` |
| `guild.emojis` | `guild.emojiKeys` | `emojis` |
| `guild.stickers` | `guild.stickerIds` | `stickers` |
| `guild.owner`, `channel.owner` | *(omitted; only `ownerId` remains)* | `users` |

Two things about this matter when parsing:

**Emoji are keyed by a string, not an ID.** Standard Unicode emoji have no Discord ID at all, and a custom emoji renamed over its lifetime is two distinct records that must not collapse into one. So each entry in the `emojis` table carries an extra `key` field, which is what everything else references: the snowflake for a custom emoji, the character for a standard one, suffixed with `~2`, `~3` and so on to separate records that would otherwise collide.

**The lookup tables are written after the messages.** Member data is resolved on demand as an export progresses, so deferring the tables to the postamble gives every entry the richest data the export ever saw. The practical consequence is that a consumer has to reach the end of the file before the beginning of it means anything — the tables cannot be read first.

Order within the tables is stable across runs rather than dependent on the order entities happened to be encountered: users by ID, roles by descending position, emoji by key, stickers by ID.

## Guild object

The `guild` object contains basic information about the server the export belongs to. It has the following schema:

```
{
  "id": String,
  "name": String,
  "iconUrl": String
}
```

The extended fork also includes the following fields:

```
{
  "description": String,
  "vanityUrl": String,
  "bannerUrl": String,
  "splashUrl": String,
  "premiumTier": Integer,
  "premiumSubscriptionCount": Integer,
  "approximateMemberCount": Integer,
  "approximatePresenceCount": Integer,
  "ownerId": String,
  "owner": Object,
  "roles": Array<Object>,
  "emojis": Array<Object>,
  "stickers": Array<Object>
}
```

The owner is a [User](#user-object) object, the roles are [Role](#role-object) objects, the emojis are [Emoji](#emoji-object) objects, and the stickers are [Sticker](#sticker-object) objects. They're all exported so the entire collection is known even when they haven't been used in the actual exported chats.

## Channel object

The `channel` object contains basic information about the channel the export belongs to. It has the following schema:

```
{
  "id": String,
  "type": String,
  "categoryId": String,
  "category": String,
  "name": String,
  "topic": String
}
```

Note that the **type** is provided as a custom string, but we'll want to recover Discord's internal integer types, which can be consulted in the [official documentation](https://docs.discord.com/developers/resources/channel#channel-object-channel-types). The enum used by DCE can be consulted in this [source file](https://github.com/Tyrrrz/DiscordChatExporter/blob/prime/DiscordChatExporter.Core/Discord/Data/ChannelKind.cs). DCE outputs the type ID as a string whenever the type is unrecognized. For instance, channel type 16 (corresponding to media channels) isn't supported by DCE yet, so the type would show as "16" instead of something like "GuildMedia". Therefore, we need to be prepared to support both methods. The same situation happens for [Message objects](#message-object).

The extended fork also includes the following fields:

```
{
  "position": Integer,
  "memberCount": Integer,
  "isArchived": Boolean,
  "isLocked": Boolean,
  "ownerId": String
}
```

The last four only apply to threads, regular channels don't hold that information.

## Date range object

The `dateRange` object specifies the date range the export operation was restricted to. It won't appear if it wasn't specified when exporting. It has the following schema:

```
{
  "after": String,
  "before": String
}
```

## Message object

The `messages` array contains the list of all exported messages from the channel within the given date range. Each message object has the following schema:

```
{
  "id": String,
  "type": String,
  "timestamp": String,
  "timestampEdited": String,
  "callEndedTimestamp": String,
  "isPinned": Boolean,
  "content": String,
  "author": Object,
  "attachments": Array<Object>,
  "embeds": Array<Object>,
  "stickers": Array<Object>,
  "reactions": Array<Object>,
  "mentions": Array<Object>,
  "reference": Object,
  "forwardedMessage": Object,
  "inlineEmojis": Array<Object>,
  "interaction": Object
}
```

Note that `timestampEdited` and `callEndedTimestamp` can be NULL if the message has never been edited or doesn't correspond to a voice call message, respectively. `reference` is only present if the message is a reply to another message, `forwardedMessage` only if it is a forward, and `interaction` only if it is the result of a slash command.

Regarding the **type**, the same note made for [Channel objects](#channel-object) applies here: DCE uses an enum with custom strings (see [source code](https://github.com/Tyrrrz/DiscordChatExporter/blob/prime/DiscordChatExporter.Core/Discord/Data/MessageKind.cs)) and only supports _some_ of the types (displaying the rest as numeric strings), and we'll instead want to recover Discord's internal integer values (see [official docs](https://docs.discord.com/developers/resources/message#message-object-message-types)).

The extended fork also includes the following fields:

```
{
  "flags": Array<String>,
  "components": Array<Object>
}
```

### User object

The `author` object in each message contains the information about the user / member that posted the message. The user is the actual Discord account, whereas the member is the guild-specific user profile. Thus, each user is associated with one member per guild they're part of.

In vanilla DCE exports, both user and member information are combined, and it has the following schema:

```
{
  "id": String,
  "name": String,
  "discriminator": String,
  "nickname": String,
  "color": String,
  "isBot": Boolean,
  "roles": Array<Object>,
  "avatarUrl": String
}
```

Note that `color` can be NULL, which denotes the user names takes the default color. Deleted users always show with ID 456226577798135808 and name "Deleted User".

The extended fork also includes the following fields, all related to the member except `displayName`:

```
{
  "displayName": String,
  "joinedAt": String,
  "premiumSince": String,
  "isPending": Boolean,
  "flags": Array<String>,
  "bannerUrl": String
}
```

#### Names

Four fields carry a name, and they are three underlying Discord values arranged as a cascade:

| Discord field | Where it lives | What it is |
| --- | --- | --- |
| `username` | user | The unique handle. |
| `global_name` | user | The account-wide display name from profile settings. Optional. |
| `nick` | member | The per-guild override. Optional. |

What a client renders is `nick ?? global_name ?? username`. The export fields map onto that as follows:

| Field | Value |
| --- | --- |
| `user.name` | `username`, raw |
| `user.displayName` | `global_name ?? username` |
| `member.nickname` | `nick`, raw, NULL when unset |
| `member.displayName` | `nick ?? user.displayName` |

The rule is the same for both objects: the raw field is that object's own datum with nothing folded in, and `displayName` is "what to render", resolved as far as that object can resolve it.

The merged object has only one name field beyond `name`, and it is a misnomer: **`nickname` in a merged object is the whole cascade**, `nick ?? global_name ?? username`, not the nickname. It equals `member.displayName` when there is a member record and `user.displayName` when there isn't. With `--extended` the user's own `displayName` sits beside it, which is what makes the real nickname recoverable by comparing the two; without it, the guild nickname and the global display name cannot be told apart. Even with it, someone whose nickname happens to equal their global display name is indistinguishable from someone who set no nickname at all.

Two further fields are merged the same way:

| Field | Value |
| --- | --- |
| `avatarUrl` | `member.avatarUrl ?? user.avatarUrl` |
| `bannerUrl` | `member.bannerUrl ?? user.bannerUrl ?? null` |

Which of the two a merged object is showing can always be worked out, because Discord serves a member's guild-specific image from a different path (`/guilds/{guild}/users/{user}/avatars/...`) than a global one (`/avatars/{user}/...`). What cannot be worked out is the *global* image of someone who has set a guild one: the override stands in its place, and the global image is not in the document at all. Only a split export or a member export carries both.

The `--media` option defeats the distinction entirely, since it rewrites every URL to a local path.

#### Split users

With the fork's `splitUsers` option, the two objects are written apart. The user is the outer object, with a nullable `member` on it:

```
{
  "id": String,
  "name": String,
  "discriminator": String,
  "displayName": String,
  "isBot": Boolean,
  "avatarUrl": String,
  "bannerUrl": String,
  "member": Object
}
```

`member` follows the schema in [Member object](#member-object) and is **NULL** when the document has no guild profile for that person: they left, were never in the guild, or nothing in the export ever caused them to be looked up. Written the other way round they would get a member object full of nulls, which reads as "a member who set no nickname and holds no roles" — a claim the export is in no position to make.

The nesting is deliberately the opposite of what the member export uses, where every entry *is* a member and the user is nested inside it. Both documents use the same two objects with the same field names, so one parser covers both.

Under `--normal`, the root `users` table holds plain user objects with no `member` on them, and a separate `members` table holds the member objects, keyed by the same `userId`. Anyone the guild has no record of is simply absent from it.

#### Role object

The `roles` array contains the list of roles the user has. Each role object has the following schema:

```
{
  "id": String,
  "name": String,
  "color": String,
  "position": Integer
}
```

Note that `color` can be NULL if the role has no special color assigned, in which case the default one is used.

### Attachment object

The `attachments` array contains the list of files attached to the message. Each attachment object has the following schema:

```
{
  "id": String,
  "url": String,
  "fileName": String,
  "fileSizeBytes": Integer
}
```

### Emoji object

The `inlineEmojis` array contains the list of emojis that were used in the message. Each emoji object has the following schema:

```
{
  "id": String,
  "name": String,
  "code": String,
  "isAnimated": Boolean,
  "imageUrl": String
}
```

Note that for standard Unicode emojis the `id` is empty and the `name` is the actual UTF-8 character(s). The `code` is the ASCII shorthand that can be used within colons to obtain the same emoji when drafting the message. For custom server emojis, the `ìd` is its 8-byte ID, and the `name` and `code` both match.

### Embed object

The `embeds` array contains the list of embeds in the message. Embeds are generated automatically by Discord for certain types of attachments or links, such as images. They can also be generated manually by bots. Each embed object has the following schema:

```
{
  "title": String,
  "url": String,
  "timestamp": String,
  "description": String,
  "thumbnail": Object,
  "author": Object,
  "image": Object,
  "video": Object,
  "fields": Array<Object>,
  "images": Array<Object>,
  "color": String,
  "footer": Object,
  "inlineEmojis": Array<Object>
}
```

In actual embeds, all fields are optional. See the [official docs](https://docs.discord.com/developers/resources/message#embed-object) for more details. In the exports, some of them will always appear, but potentially empty. The URL and timestamp may be NULL. The objects within the `inlineEmojis` list follow the schema described in [Emoji object](#emoji-object).

#### Resource object

An embed may have optional image / video resources via `thumbnail`, `image` `video` and `images`. They all follow the same schema:

```
{
  "url": String,
  "canonicalUrl": String,
  "width": Integer,
  "height": Integer
}
```

Note that the `canonicalUrl` points to the original URL of the image or video, whereas the `url` points to the reupload (and, often, reencode) in Discord's CDN. Older exports didn't have the canonical version. For embed's with multiple `images`, `image` may be one of them, the main one.

#### Embed author object

The embed's optional `author` field has the following schema:

```
{
  "name": String,
  "url": String,
  "iconUrl": String,
  "iconCanonicalUrl": String
}
```

The last two may not appear if the author field lacks an icon. The distinction between the canonical and non-canonical icon URL is the same as for [Resource objects](#resource-object). Older exports didn't have the canonical version.

#### Embed footer object

The embed's optional `footer` field has the following schema:

```
{
  "text": String,
  "iconUrl": String,
  "iconCanonicalUrl": String
}
```

The same observations about the [Embed author](#embed-author-object)'s icon apply to the footer's icon. Older exports didn't have the canonical version.

#### Embed field object

Fields provide additional details about an embed's content. Short ones may appear in columns, whereas longer ones usually have their own line. The `fields` array contains the list of fields in the embed. Each embed object has the following schema:

```
{
  "name": String,
  "value": String,
  "isInline": Boolean
}
```

### Sticker object

The `stickers` array contains the list of stickers used in the message. Each sticker object has the following schema:

```
{
  "id": String,
  "name": String,
  "format": String,
  "sourceUrl": String
}
```

### Reaction object

The `reactions` array contains the list of reactions that have been attached to this message by users. Each reaction object has the following schema:

```
{
  "emoji": Object,
  "count": Integer,
  "users": Array<Object>
}
```

The `emoji` object follows the schema described in [Emoji object](#emoji-object). Each user object in the `users` list follows the schema described in [User object](#user-object), except it lacks the `roles` member.

### Mention object

The `mentions` array contains the list of users that were mentioned (a.k.a. pinged) in this message. Each mention object follows the schema described in [User object](#user-object).

### Reference object

The `reference` object is only present for messages that reference another (single) message, and contains basic information to identify said message. In has the following schema:

```
{
  "type": String,
  "messageId": String,
  "channelId": String,
  "guildId": String
}
```

The `type` field should be considered optional as it wasn't present in older exports. Newer exports support two values: "Default" (standard replies) and "Forward". When not present we should default to the former, which used to be the only reference type.

### Interaction object

The `interaction` object is only present for messages that resulted from the execution of a slash command. For this reason, they correspond to bot messages. It contains information about the slash command itself and the user who triggered it. It has the following schema:

```
{
  "id": String,
  "name": String,
  "user": Object
}
```

The `user` object follows the schema described in [User object](#user-object) also present in messages.

### Forwarded message object

The `forwardedMessage` object is only present for messages that forward another one, which the `reference` object identifies with `type` set to "Forward". It carries the forwarded content inline, because the original may well be outside the export — or in a channel the exporting account cannot read. It has the following schema:

```
{
  "timestamp": String,
  "timestampEdited": String,
  "content": String,
  "attachments": Array<Object>,
  "embeds": Array<Object>,
  "stickers": Array<Object>,
  "components": Array<Object>
}
```

The children follow the same schemas as a message's own, and are normalized on the same terms (so `stickerIds` under `--normal`). `components` only appears with the extended fork. Note what is *absent*: a forward carries no ID and no author of its own, so there is nothing to key it by and nobody to attribute it to.

## Member export

The fork's `exportusers` command writes a different document: the guild's entire member list, including everyone who never posted. It has the following top-level structure:

```
{
  "mod": Object,
  "guild": Object,
  "exportedAt": String,
  "members": Array<Object>,
  "memberCount": Integer
}
```

There is no `channel` and no `dateRange`, a roster not being about either, and that absence is how the two kinds of document are told apart. Beware that a normalized message export made with `splitUsers` *also* has a top-level `members` array, but there it is a lookup table rather than the subject of the document — check for `channel` first.

The `mod` object here has just two keys:

```
{
  "normal": Boolean,
  "fullUsers": Boolean
}
```

`fullUsers` is provenance for one field. The user object nested in a roster entry is a *partial* one: Discord sends `banner` as null on it regardless of whether the account has one. Only a dedicated per-user fetch carries it, which costs one request per member and so is opt-in. When `fullUsers` is false, a null `bannerUrl` on a user means "not known" rather than "not set".

The `guild` object carries the extended fields described in [Guild object](#guild-object), minus the emoji and sticker inventories — this document is about people. Under `--normal` the roster's users go to a root `users` table and the guild's full role inventory to a root `roles` table, exactly as in a message export.

### Member object

Each entry in the `members` array is a member with the user nested inside it, which is the opposite nesting to [Split users](#split-users). It has the following schema:

```
{
  "userId": String,
  "nickname": String,
  "displayName": String,
  "color": String,
  "avatarUrl": String,
  "bannerUrl": String,
  "joinedAt": String,
  "premiumSince": String,
  "isPending": Boolean,
  "flags": Array<String>,
  "roles": Array<Object>,
  "user": Object
}
```

A member has no identity of its own — it is a user's profile within one guild — so `userId` is both its key and its reference to the user. `avatarUrl` and `bannerUrl` are the guild-specific overrides *only*, so NULL means "wears the global one", which is on the user object rather than duplicated here. `nickname` is NULL when the member never set one; see [Names](#names) for how it relates to `displayName`.

Roles are ordered highest first, matching what a client renders, and Discord leaves `@everyone` out of a member's role list so it is absent here too. The `color` is that of the highest-positioned role carrying one.

Under `--normal`, `roles` becomes `roleIds` and `user` is dropped in favour of the root `users` table, which the `userId` indexes.

The same member schema is used by message exports made with `splitUsers`, where it appears as the `member` field of a user rather than as the outer object — minus `user`, which would be circular there.
