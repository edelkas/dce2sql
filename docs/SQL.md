# SQL database schema

This file details the SQL database schema and documents each of the fields. The information comes from DCE's JSON exports. Some fields come from [my extended fork](https://github.com/edelkas/DiscordChatExporter) which adds additional capabilities. If a vanilla export is used instead, those fields may simply be ignored.

## Table of contents

- [Overview](#overview)
- [Base tables](#base-tables)
   * [Guilds table](#guilds-table)
   * [Channels table](#channels-table)
   * [Users table](#users-table)
   * [Messages table](#messages-table)
   * [Attachments table](#attachments-table)
   * [Roles table](#roles-table)
   * [Emojis table](#emojis-table)
   * [Stickers table](#stickers-table)
   * [Interactions table](#interactions-table)
- [Auxiliary tables](#auxiliary-tables)
   * [Channel types table](#channel-types-table)
   * [Message types table](#message-types-table)
   * [Reference types table](#reference-types-table)
   * [Message history table](#message-history-table)
   * [Embeds table](#embeds-table)
   * [Resources table](#resources-table)
- [Junction tables](#junction-tables)
   * [Members table](#members-table)
   * [User roles table](#user-roles-table)
   * [Reactions table](#reactions-table)
   * [Mentions table](#mentions-table)
   * [Message emojis table](#message-emojis-table)
   * [Message stickers table](#message-stickers-table)
- [Metadata tables](#metadata-tables)
   * [Imports table](#imports-table)

## Overview

The database is able to store an arbitrary amount of chat dumps, even from different guilds. It uses UTF-8 encoding and all ID fields are be 8 bytes, as Discord's own IDs are used whenever available (e.g. guild IDs, channel IDs, message IDs, etc) instead of the default auto-incremental IDs.

The database contains the following [**base tables**](#base-tables), which are the ones that map to actual Discord objects: [`guilds`](#guilds-table), [`channels`](#channels-table), [`messages`](#messages-table), [`users`](#users-table), [`attachments`](#attachments-table), [`roles`](#roles-table), [`emojis`](#emojis-table), [`stickers`](#stickers-table) and [`interactions`](#interactions-table). It also contains the following [**auxiliary tables**](#auxiliary-tables) which don't map to actual Discord objects: [`channel_types`](#channel-types-table), [`message_types`](#message-types-table), [`reference_types`](#reference-types-table), [`message_history`](#message-history-table), [`embeds`](#embeds-table) and [`resources`](#resources-table). Furthermore, it contains the following [**junction tables**](#junction-tables): [`members`](#members-table), [`user_roles`](#user-roles-table), [`reactions`](#reactions-table), [`mentions`](#mentions-table), [`message_emojis`](#message-emojis-table) and [`message_stickers`](#message-stickers-table). Finally, it contains the following [**metadata tables**](#metadata-tables): [`imports`](#imports-table). All base tables' ID fields are the actual 8-byte IDs used by Discord and taken from the JSON exports. The other tables, which don't map to actual Discord objects, use the standard auto-increment ID scheme instead.

Some notes about specific common fields:

- **Timestamps** are to be stored with native types for engines that support it, otherwise (e.g. SQLite) they'll be stored as integers instead (UNIX timestamp). Everything is UTC: PostgreSQL uses `TIMESTAMPTZ`, MySQL a `DATETIME` with the session pinned to `+00:00`, since its `DATETIME` carries no offset of its own.
- **Booleans** shall be stored with native types for engines that support it, otherwise integers (0 and 1) will be used instead (if possible, 1-byte integers).
- **Colors** will be casted and packed as integers: `R << 16 | G << 8 | B` for RGB colors.
- **Strings**, for engines that support it (e.g. MySQL), should be stored in place when short enough (e.g. VARCHAR), and by reference when long enough (e.g. TEXT). Other engines, like SQLite, handle this on their own. I'll usually specify the max length of most string fields here so the type can be chosen suitably. Examples of (potentially) long strings include message content or channel topics.

  **URLs are not short strings**, despite appearances, and are stored by reference along with them. Every bound in this schema is one Discord itself enforces — a username is 32 characters because Discord will not accept a 33rd — but a URL inside an embed is simply whatever somebody typed into a message, so its only real limit is the 4,000-character message body. One found in the wild ran to 1,995 characters: a CDN link with an essay appended as a `?comment=` parameter. Any width chosen for it would be a guess, and a wrong guess loses data, so there is no width.

  Should a value ever exceed a column that *is* bounded, the import of that file fails and says which column, what the limit is and how long the value was, rather than truncating. A truncated name or URL is worse than a missing file, because it still looks like data.
- For some rich fields we may use **JSON** column types if supported. Otherwise they'll simply be stored serialized as strings.

For performance, simple **indexes** are used for all primary and foreign key fields. Foreign keys are all columns whose name ends in `_id`. Indexes will also be placed in all timestamp fields, as well as whenever explicitly mentioned.

Every column whose name ends in `_id` is 8 bytes wide, whether it points at a Discord snowflake or at an auto-assigned key, since all the keys here are 8 bytes. SQLite will not catch a mistake in this — its `INTEGER` is 8 bytes however the column is declared — but MySQL and PostgreSQL will, by rejecting a snowflake outright.

Those references are **not** enforced with foreign key constraints, however. An archive routinely points outside itself: a reply whose parent predates the export range, a sticker from a server that was never exported, a channel that no longer exists. Constraints would turn every one of those into an import failure, which is precisely backwards for a tool whose job is to never lose anything.

Several tables have no natural key, and need one anyway so that re-importing a file doesn't duplicate its rows. Where that is the case it's noted on the table. Unique constraints containing a nullable column are declared over a `COALESCE` of it: every SQL engine treats NULLs as distinct inside a unique index, so without it the constraint would fail to deduplicate exactly the rows a re-import would otherwise double. The stand-in value has the column's own type — zero for a number, the Unix epoch for a timestamp — which is why it is rendered per engine rather than written into the schema.

## Base tables

The **base tables** are those that map precisely to objects in Discord's platform, such as guilds, channels, users and messages. For this reason, their `ID` fields will always match their actual Discord ID, which is an 8-byte integer, instead of using the default auto-increment approach. These IDs are encoded as strings in the JSON exports, so they need to be casted first.

Additionally, all base tables have two timestamp fields, `created_at` and `updated_at`. The former will be set once the record is created, and the latter will be updated every time the record is actually changed.

### Guilds table

The `guilds` table stores all servers present in the database. Apart from the common 3 columns it shall also have the following fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| name | String | name | Guild name (max. 100 chars) |
| description | String | description | Description of the server |
| url | String | vanityUrl | Human-readable clickable link to join the server |
| icon | String | iconUrl | URL of the guild's icon image |
| banner | String | bannerUrl | URL of the guild's banner |
| splash | String | splashUrl | URL of the guild's splash screen |
| boost_level | Integer | premiumTier | Server's boost level |
| boost_count | Integer | premiumSubscriptionCount | Amount of boosts |
| owner_id | Integer | ownerId | User ID of the guild's owner |

Note that `description`, `vanityUrl`, `bannerUrl`, `splashUrl`, `premiumTier`, `premiumSubscriptionCount` and `ownerId` are only present in the extended fork.

### Channels table

The `channels` table shall have the following fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| guild_id | Integer | - | ID of the guild this channel belongs to |
| name | String | name | Channel name (max. 100 chars) |
| type | Integer | type | Channel type (currently 0-16, see the [official docs](https://docs.discord.com/developers/resources/channel#channel-object-channel-types)) |
| topic | String | topic | Channel description or current topic (max. 4000 chars) |
| parent_id | Integer | categoryId | ID of parent channel |
| position | Integer | position | The channel's listing order |
| members | Integer | memberCount | Amount of members in the thread |
| archived | Boolean | isArchived | Whether the thread is archived or not |
| locked | Boolean | isLocked | Whether the thread is locked or not |
| owner_id | Integer | ownerId | ID of the user who created the thread |

Note that `position`, `memberCount`, `isArchived`, `isLocked` and `ownerId` are only present in the extended fork. Also, the last four fields will be NULL for regular channels, which don't carry that information.

The `guild_id` isn't present in the JSON's channel object, but rather, in its guild object (recall each export file corresponds to a single guild and channel).

The `name` and `topic` fields are self-explanatory. As explained in [Channel object](JSON.md#channel-object), the `type` field must be converted from its string representation (as given by the JSON exports) to the official integer values. The [Channel types](#channel-types-table) table must be seeded first.

Regarding the `parent_id` field, internally channels can be laid out hierarchically in a guild, and the parent_id field is the ID of the parent channel, or NULL if it's a top-level channel. Discord categories are just a special type of channel that must be top-level (i.e. not a subchannel itself) and can only hold subchannels, not messages. Initially, only categories could have subchannels, and that's why in the JSON exports the parent of a channel is encoded in the `categoryId` and `category` keys, containing the parent channel's ID and name, respectively. However, that's no longer the case, as regular channels can now have "threads", which internally are simply subchannels as well. Discord also supports "forums", which are channels that can only hold threads (called "posts" in the context of forums), but not messages themselves (so, forums are somewhat similar to categories, but they don't need to be top-level). In summary, the key names "categoryId" and "category" are nowadays misnomers to a degree, and instead, they should be conceived as encoding the parent channel's ID and name in general. Before creating a channel, we must thus ensure the parent channel exists as well, even if we don't have exports for it (which will always be the case for categories or forums, but could also happen for regular channels or threads we haven't reached yet in the export list). When creating a channel this way, via the category keys, its parent_id and topic should be set to NULL, and its type should be set to 4 (corresponding to the category type). If, in a later JSON export, we find out the channel wasn't actually a category, then we'll simply update these 3 fields with their correct values.

### Users table

Users are Discord accounts. They differ from [Members](#members-table) in that the latter are the guild-specific user profiles. Thus, each user is associated with one member object per guild they're part of. The `users` table shall have the following additional fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| name | String | name | The user name |
| discriminator | Integer | discriminator | 4-digit value to distinguish users with the same name |
| display | String | displayName | The user display name / nick name |
| bot | Boolean | isBot | Whether this user is a bot account |
| avatar | String | avatarUrl | The avatar's image URL |
| banner | String | bannerUrl | The banner's image URL |
| deleted | Boolean | - | Whether this user has been deleted since first being archived |

The `avatar` and `banner` here are the account's *global* images. A member who has set a guild-specific avatar or banner is shown wearing it in a merged export, in place of the global one, so for those people a merged export cannot supply this column at all and leaves it empty rather than storing the guild image in it. The guild image goes to [`members`](#members-table), from either kind of export: it is recognisable by its CDN path, `/guilds/{guild}/users/{user}/avatars/...` rather than `/avatars/{user}/...`.

Note that the banner URL is only included in the extended fork, and that the display name is only recoverable at all from an export that states it separately from the guild nickname — that is, `--split-users`, a member export, or (since the field was added) `--extended`. A merged `nickname` is the whole cascade collapsed into one string and so cannot be trusted as a global display name; rather than guess, the column is left NULL and the merged value goes to [`members.display`](#members-table) only. See [Names](JSON.md#names) for the full picture.

Note that the JSON exports encode the discriminator as a string, so it needs to be casted. They are being phased out by Discord, which means most of them will usually be 0. It also encodes the color as a hex string, so it needs to be packed. The color may be NULL, which denotes the default one.

The `deleted` field, which should default to `false`, isn't present in the JSON exports, as in fact, it's impossible to detect it from one export, a comparison between different exports is required. The reason is that all messages by deleted users are automatically assigned to a special user with ID `456226577798135808` and name `Deleted User` by Discord. Thus, all deleted users which weren't previously archived will be lumped into this special user.

For performance, a simple index shall be placed in the `name`, `display` and `bot` fields.

### Messages table

The `messages` table shall have the following additional fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| channel_id | Integer | - | ID of the channel the message was posted to |
| type | Integer | type | Message type (currently 0-46, see the [official docs](https://docs.discord.com/developers/resources/message#message-object-message-types)) |
| timestamp | Timestamp / Integer | timestamp | When the message was first posted |
| edited_timestamp | Timestamp / Integer | timestampEdited | When the message was last edited |
| pinned | Boolean | isPinned | Whether this message is pinned in the channel |
| user_id | Integer | author['id'] | The ID of the user who posted the message |
| content | Text | content | The message's full content (max. 4000 chars). |
| reference_id | Integer | reference['messageId'] | ID of the message being referenced |
| reference_type | Integer | reference['type'] | Type of the reference |
| interaction_id | Integer | interaction['id'] | ID of the interaction being executed |
| interaction_user_id | Integer | interaction['user'] | ID of the user who triggered the interaction |
| components | JSON / String | components | Array of component objects |
| forwarded | JSON / String | forwardedMessage | The forwarded message, kept whole |

Note that `components` is only present in the extended fork. Since their structure is deeply nested, prone to change and rarely queried, we won't normalize it and instead leave it serialized as a JSON string.

The `forwarded` column is handled the same way, for a different reason: a forwarded message carries its own content, attachments, embeds and stickers, but has no ID and no author, so there is nothing to key its children by and nobody to attribute them to. It is stored verbatim rather than flattened into tables that could not hold it properly. Any reference arrays inside it are resolved first, so the stored JSON is self-contained.

The `timestampEdited` may be NULL if the message has never been edited. The `channel_id` isn't present in each message's JSON object, instead it's inferred from the file itself, since each export file corresponds to a single channel.

Just like for channels, and as explained in [Message object](JSON.md#message-object), the `type` field must be converted from its string representation (as given by the JSON exports) to the official integer values. The [Message types](#message-types-table) table must be seeded first.

A message may reference another one. This manifests as the `reference` object being present in the JSON exports, which may otherwise not be there. Note the `reference_type` should be converted from string to integer following the official values and DCE's enum, first seeding the [Reference types](#reference-types-table) table. It doesn't appear in older exports and should default to 0.

A message may contain an interaction, such as the execution of a slash command (which is the only one currently supported by DCE), otherwise the `interaction` object may not be in the JSON exports. Note the `interaction_id` doesn't correspond to the message author (usually a bot), but the user who triggered the interaction.

When parsing a message, entries for each attachment, embed, inline emoji, sticker, mention and reaction must be created in the corresponding tables as well.

As per our merge policy, when a future export detects a message has changed, its corresponding fields will be updated (notably `edited_timestamp` and `content`, perhaps also `pinned`). In order to prevent losing historical records, however, when this happens we shall record the old content in the [Message history](#message-history-table) table.

Again owing to our archival policy to never delete anything, the user ID of a message will never be updated. This is because message authors can only change in one circumstance: when the user has been deleted. In that case, Discord reassigns all their messages to the special user with ID `456226577798135808` (see [Users table](#users-table) for more details), and thus this information gets lost. When we detect this user ID in a message that was already archived, instead of updating the user ID, we simply set the `deleted` field to true for the corresponding user, to prevent losing the real message authors.

For performance, apart from the standard indexes in all foreign key and timestamp fields, they shall also be placed on the `type` and `pinned` fields.

### Attachments table

The `attachments` table contains all files uploaded to the exported chats. The same file being uploaded multiple times does not get de-duplicated and gets assigned a new ID, at least at the API level. Thus, attachments are strictly tied to their parent message object. The table has the following fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| message_id | Integer | - | ID of the message this was attached to |
| name | String | fileName | The file name |
| size | Integer | fileSizeBytes | The file size in bytes |
| url | String | url | Attachment URL in Discord's CDN |

The `message_id` doesn't come from a specific JSON key, but rather from the message object that contains the attachment.

Be aware that the `url` is signed: Discord appends `ex`, `is` and `hm` parameters that are regenerated on every export and expire within about a day. Two exports of the same attachment therefore never agree on it, and the importer ignores those three parameters when deciding whether the row has changed — otherwise re-importing an archive would rewrite every attachment row and make `updated_at` meaningless. The part before the query string never changes and is what identifies the file.

### Roles table

The `roles` table stores all user roles. A server can have at most 250 different roles. It'll have the following additional fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| guild_id | Integer | - | The server this role belongs to |
| name | String | name | Role name (max. 100 chars) |
| color | Integer | color | Color in which usernames with this role render |
| position | Integer | position | Precedence of this role |

The `guild_id` doesn't come from the JSON object, its inferred from the export file. Again, the color must be packed first, and may be NULL.

### Emojis table

The `emojis` table stores all emojis found in the exports, including both standard Unicode emojis and custom server emojis. Each server can only have 100 custom emojis (50 static and 50 animated), with additional slots when boosted. The table has the following fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| guild_id | Integer | - | The guild this emoji belongs to (custom emojis only) |
| name | String | name | The emoji's UTF-8 representation (standard) or name (custom) |
| code | String | code | The emoji's shorthand (standard) or name (custom) |
| animated | Boolean | isAnimated | Whether the emoji is animated or static |
| url | String | imageUrl | URL to the emoji's image |

As before, the `guild_id` is inferred from the export file. Since standard Unicode emojis don't have an assigned ID in Discord (indeed, it's empty in the JSON exports), nor are part of any server in particular, their ID should follow the standard auto-increment scheme, and their guild ID should be NULL.

Note that this means finding emojis in the database from the JSON exports isn't trivial. Custom emojis can be fetched by ID, but standard emojis need to be fetched by name or code, ideally by code (since the name is Unicode and thus sensitive to encoding).

The synthetic IDs are allocated from 1 upwards. Real snowflakes are all above 4×10¹⁵, so the two can share the column without any risk of collision, and anything below 2³² can be taken to be a standard emoji. That test is what identifies them, not a NULL guild: a *custom* emoji seen only in a message also has no guild, for the reason given above about stickers.

### Stickers table

The `stickers` table stores all stickers used in the exported chats. It has the following fields.

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| guild_id | Integer | - | The guild that owns this sticker, when known |
| name | String | name | Sticker name (max. 30 chars) |
| format | String | format | Sticker image format |
| url | String | sourceUrl | URL to the image file in Discord's CDN |

A sticker seen on a message says nothing about where it came from, so `guild_id` stays NULL for those. It is only filled from the guild's own sticker inventory, which extended exports include — that being the one place ownership is actually asserted.

The `format` is capitalized and can currently be `PNG`, `APNG`, `LOTTIE` and `GIF`.

### Interactions table

The `interactions` table contains all interactions available in the server that have been executed at least once in the exported chats.  It has the following fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| name | String | name | Slash command name |

## Auxiliary tables

These tables encode objects that don't map to actual objects in Discord's platform. Thus, their ID follows the standard auto-increment scheme and they lack the timestamp fields `created_at` and `updated_at`.

### Channel types table

The `channel_types` table encodes all available channel types according to the [official documentation](https://docs.discord.com/developers/resources/channel#channel-object-channel-types), and should be seeded before the [`channels` table](#channels-table) so that types are known. It only has the following 2 columns:

| Name | Type | Description |
| --- | --- | --- |
| ID | Integer | Channel type ID as per the docs |
| name | String | Channel type name |

Both are taken from the cited documentation table: the `ID` field corresponds to the `ID` column, and the `name` field corresponds to the `Type` column.

At the time of writing these docs the message type can range from 0 to 16.

### Message types table

The `message_types` table encodes all available message types according to the [official documentation](https://docs.discord.com/developers/resources/message#message-object-message-types), and should be seeded before the [`messages` table](#messages-table) so that types are known. It only has the following 2 columns:

| Name | Type | Description |
| --- | --- | --- |
| ID | Integer | Message type ID as per the docs |
| name | String | Message type name |

Both are taken from the cited documentation table: the `ID` field corresponds to the `Value` column, and the `name` field corresponds to the `Type` column.

At the time of writing these docs the message type can range from 0 to 46.

### Reference types table

The `reference_types` table encodes all available message reference types according to the [official documentation](https://docs.discord.com/developers/resources/message#message-reference-types), and should be seeded before the [`messages` table](#messages-table). It only has the following 2 columns:

| Name | Type | Description |
| --- | --- | --- |
| ID | Integer | Reference type ID as per the docs |
| name | String | Reference type name |

Both are taken from the cited documentation table: the `ID` field corresponds to the `Value` column, and the `name` field corresponds to the `Type` column.

At the time of writing these docs there are only two supported reference types: 0 (default) and 1 (forward).

### Message history table

The `message_history` table contains all old contents of messages that were archived but have been changed by a newer edit. Messages that have had a single version found so far, the one currently present in the [Messages table](#messages-table), do not need an entry in this table. It has the following fields:

| Name | Type | Description |
| --- | --- | --- |
| message_id | Integer | ID of the message object in the [Messages table](#messages-table)
| timestamp | Timestamp / Integer | When this version was posted |
| content | Text | The content of this version of the message |

Note that we do not need to store any additional metadata, such as the user ID, since that information is available in the `messages` table.

The timestamp deserves a clarification. If this version was the message's first one (i.e. `edited_timestamp` was NULL), then it corresponds to the `timestamp` field of the old message. Otherwise, it corresponds to the `edited_timestamp` field of the old message.

### Embeds table

The `embeds` table stores the information for the embeds used in all exported messages. A message may contain up to 10 embeds, and they are deduplicated by URL per-message. All embed's fields are optional and thus may be NULL here. Since their structure is so rich, we flatten out most fields here. The table has the following fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| message_id | Integer | - | ID of the message containing this embed |
| ordinal | Integer | - | Position in the message's embed array |
| title | String | title | Embed's title (max. 256 chars) |
| url | String | url | Embed's link |
| description | Text | description | Embed's long description (max. 4096 chars) |
| timestamp | Timestamp / Integer | timestamp | Embed's timestamp |
| color | Integer | color | Color of the embed bar |
| thumbnail_id | Integer | thumbnail | ID of the thumbnail resource |
| author_name | String | author | Embed's author name (max. 256 chars) |
| author_url | String | author | Embed's author link |
| author_icon_id | Integer | author | ID of the author icon resource |
| image_id | Integer | image | ID of the embed's main image resource |
| video_id | Integer | video | ID of the embed's video resource |
| footer | String | footer | Text in the embed's footer (max. 2048 chars) |
| footer_icon_id | Integer | footer | ID of the footer icon resource |
| fields | JSON / String | fields | Field array of the embed |

As usual, the color needs to be packed. Note the `timestamp` need not match the actual message's timestamp, it's manually selectable.

An embed has no ID of its own and its URL may be NULL or repeated, so `(message_id, ordinal)` is its identity and is declared unique — that being what a re-import matches on.

All resource IDs (`thumbnail_id`, `author_icon_id`, `image_id`, `video_id`, `footer_icon_id`) refer to the [Resource](#resources-table) table. One resource is created for each image or video in the embed. Entries in the [Emoji](#emojis-table) table are also created for each inline emoji.

We store the fields array serialized in a JSON column to avoid further unnecessary normalization of the database. For engines that don't support JSON columns this will simply be a string. Each field contains 3 keys: the name, the value and whether it's an inline field or not.

### Resources table

The `resources` table is used to store all embedded resources. For now, these may be either images or videos. They correspond to the embed's `thumbnail`, `image`, `video` and `images` objects in the JSON exports. Embeds' icons, both from the `author` and `footer` fields, also gain their own resource, except they lack width and height information, so those fields are NULL here. The table has the following fields:

| Name | Type | Description |
| --- | --- | --- |
| embed_id | Integer | ID of the embed containing this resource |
| slot | String | Which of the embed's fields this filled |
| url | String | Original URL of the resource |
| proxied_url | String | URL of the resource in Discord's CDN |
| width | Integer | Width of the image or video in pixels |
| height | Integer | Height of the image or video in pixels |

The `slot` is one of `thumbnail`, `image`, `video`, `author_icon`, `footer_icon` or `images[n]`. It serves two purposes: it says what the row meant without having to look back at the embed, and together with `embed_id` it gives the row the identity it otherwise lacks, so `(embed_id, slot)` is declared unique.

Discord stores copies of the resources in their own servers for performance and safety. These are usually distinguished as the "proxy_url" in their API documentation. In DCE, however, the proxied URL is usually stored as `url` or `iconUrl`, whereas the original url is stored as `canonicalUrl` or `iconCanonicalUrl`, depending on the object. The original URL wasn't present in older exports, so it can be NULL here.

The same resource (same `url`) could appear multiple times in the database as long as the `proxied_url` is different, which means it's appeared in multiple embeds and Discord hasn't deduplicated them. The same resource may even appear twice in the same embed (e.g. as `image` and as one of `images`).

Note that not all resources are explicitly referenced as columns in the [Embed](#embeds-table). For instance, if an embed contains multiple images (i.e. the `images` field isn't empty in the JSON exports), each of those images will corresponds to a row in this table, but potentially only the main image (i.e. the `image` field) will be referenced directly via the embed's `image_id` column. This is why we also need the `embed_id` column, even though it will sometimes be redundant.

## Junction tables

Junction tables, or bridge tables, store many-to-many relationships between objects. We use them to store things such as message reactions, user roles, etc.

### Members table

Members are the guild-specific user profiles. Thus, this table bridges the [User](#users-table) and [Guild](#guilds-table) tables, and adds the guild-specific user information. Entries in this table should be unique by (user ID, guild ID) pair. It has the following fields:

| Name | Type | JSON key | Description |
| --- | --- | --- | --- |
| user_id | Integer | id | ID of the user this member corresponds to |
| guild_id | Integer | - | ID of the guild this member corresponds to |
| display | String | nickname | User's display name on this server |
| color | Integer | color | Color of the user name in the guild |
| avatar | String | avatarUrl | URL of the member avatar image |
| banner | String | bannerUrl | URL of the member banner image |
| joined_at | Timestamp / Integer | joinedAt | When the user joined the guild |
| boosting_since | Timestamp / Integer | premiumSince | When user started boosting the guild |

The `user_id` comes from the `id` field in vanilla JSON exports, and also from `userId` in the fork's user export. The `guild_id` is inferred from the export file. The last 3 fields are only present in the extended exports.

Unlike the other junction tables this one carries real, mutable data, so it also gets `created_at` and `updated_at`, which the merge policy needs in order to say when a nickname or colour last changed.

The `nickname` field from vanilla exports incorporates both user and member info to compute the final display name, so it's not reliable. The fork's JSON exports which split between user and member info is preferred to get the guild-specific display names.

Accordingly, a merged export may set this column when it *creates* the row, so that an archive of nothing but vanilla exports still has names in it, but it may never change one afterwards. That includes clearing it: a NULL here means "this member set no nickname", which is a claim only a document that states the nickname in its own right — a split export, or a member export — is in a position to make. Without that rule the two kinds of export overwrite each other in turn and the column ends up depending on which file happened to be imported last.

### User roles table

The `user_roles` table stores all users' assigned roles. It bridges the [User](#users-table) and [Role](#roles-table) tables. It has the following fields:

| Name | Type | Description |
| --- | --- | --- |
| user_id | Integer | ID of the user |
| role_id | Integer | ID of the role |

### Reactions table

The `reactions` table contains all users' reactions to all messages. Since a user may attach multiple reactions to any given message, this table bridges the [Message](#messages-table), [User](#users-table) and [Emoji](#emojis-table) tables. It has the following fields:

| Name | Type | Description |
| --- | --- | --- |
| message_id | Integer | ID of the message that was reacted |
| user_id | Integer | ID of the user who reacted, or NULL |
| emoji_id | Integer | ID of the emoji used in the reaction |
| count | Integer | How many reactions this row accounts for |

See the note in [Emojis table](#emojis-table) about how to find emojis by ID or code in the database.

Fetching the list of users behind a reaction is by far the most expensive thing an export does — one request per 100 users, per emoji, per message — so it can be turned off, and a rate-limited run may not manage it in full. The JSON then gives a `count` with an empty `users` array, which would otherwise make the reaction disappear from the database entirely.

Hence the `count` column and the nullable `user_id`. A row naming its user counts 1; a single row with `user_id` NULL carries whatever the count is in excess of the users actually named. **`SUM(count)` grouped by `(message_id, emoji_id)` is therefore the true total in every case**, whichever way the export was made.

The anonymous remainder shrinks as later exports name more of the reactors, so unlike every other row in this database it is replaced rather than accumulated. It has to be measured against the names already in the database rather than against the ones in the file being imported: an export made without reaction users lists none at all, and must not re-anonymize reactors that an earlier export already named.

### Mentions table

The `mentions` table stores all users' mentions in all messages. Each message contains a `mentions` array in the JSON exports which is a list of users that were mentioned in it. Thus, this table bridges the [User](#users-table) and [Message](#messages-table) tables. It has the following fields:

| Name | Type | Description |
| --- | --- | --- |
| message_id | Integer | ID of the message containing the mention |
| user_id | Integer | ID of the user that was mentioned |

### Message emojis table

The `message_emojis` table encodes which emojis have been used in the content of each message or in any of its embeds. It bridges the [Message](#messages-table) and [Emoji](#emojis-table) tables, and optionally also the [Embed](#embeds-table) table. It has the following fields:

| Name | Type | Description |
| --- | --- | --- |
| message_id | Integer | ID of the message containing the emoji |
| emoji_id | Integer | ID of the emoji used |
| embed_id | Integer | ID of the embed the emoji was in |

See the note in [Emojis table](#emojis-table) about how to find emojis by ID or code in the database. If an emoji appears in the content of a message its `embed_id` will be NULL.

### Message stickers table

The `message_stickers` table encodes which stickers have been used in each message. Regular users are limited to a single sticker per message, but the API actually supports up to 3. It bridges the [Message](#messages-table) and [Sticker](#stickers-table) tables. It has the following fields:

| Name | Type | Description |
| --- | --- | --- |
| message_id | Integer | ID of the message containing the sticker |
| sticker_id | Integer | ID of the sticker used |

## Metadata tables

These tables encode information about the export processes themselves, rather than Discord data.

### Imports table

The `imports` table stores information about each JSON export file imported into the database. A new entry must be created for every imported JSON file, even if the same file is imported twice (which should otherwise result in a no-op on the remaining tables). It has the following fields:

| Name | Type | Description |
| --- | --- | --- |
| sha1 | String | SHA1 digest of the JSON file |
| kind | String | `messages` for a channel export, `members` for a roster |
| guild_id | Integer | ID of the guild the JSON export corresponds to |
| channel_id | Integer | ID of the channel, NULL for a member export |
| start_timestamp | Timestamp / Integer | Start of export data range |
| end_timestamp | Timestamp / Integer | End of export data range |
| exported_at | Timestamp / Integer | When the JSON export was generated |
| imported_at | Timestamp / Integer | When the JSON file was imported to the database |
| first_message_id | Integer | ID of the first message in the export |
| last_message_id | Integer | ID of the last message in the export |
| total_message_count | Integer | Number of messages in the export |
| new_message_count | Integer | Number of messages that weren't already in the database when importing the file |
| edit_message_count | Integer | Number of messages that were already in the database, but had changed |

A member export has no channel and no date range, so those columns are NULL for it and `total_message_count` holds the roster size instead; `kind` says which of the two a row describes.

Note that some of the fields come directly from the JSON exports, such as the `channel_id` (which comes from `channel['id']` in the JSON), the first 2 timestamps (which come from the `dateRange` object), the `exported_at` timestamp (which comes from `exportedAt`) or the `total_message_count` (which comes from `messageCount`).

Others can also be inferred directly from the JSON exports, such as the IDs of the first and last message in the exports, or the file's SHA1 hash. However, others are computed during the import process, such as the amount of new / changed messages, or naturally, the `imported_at` timestamp.