# Architecture

How dce2sql is put together, and the reasoning behind the parts that are not obvious.
[SQL.md](SQL.md) covers the schema and [JSON.md](JSON.md) the input format; this is about the
code between them.

## Table of contents

- [The shape problem](#the-shape-problem)
- [Modules](#modules)
- [Reading](#reading)
- [Normalizing](#normalizing)
- [Importing](#importing)
- [The merge policy](#the-merge-policy)
- [Identity](#identity)
- [Engines](#engines)
- [Testing](#testing)

## The shape problem

DiscordChatExporter can write the same conversation many ways. Vanilla writes one shape; the
extended fork adds `--extended` (more fields), `--normal` (entities moved into lookup tables at
the root and referenced by ID), `--split-users` (the user and the guild member written as two
objects instead of one merged one) and `--reaction-users false` (reaction counts without the
names). Any combination is legal, and an archive built over years will contain several.

The design decision the whole tool rests on: **the shape is erased before anything reaches the
database.** `documents.py` reduces every variant to one internal form, and `importer.py` is
written once against that. The `mod` block never selects a code path — it only says which fields
are *known*.

That is what makes the central test possible: import the same conversation in six shapes and
assert the resulting databases are identical.

## Modules

| Module | Responsibility |
| --- | --- |
| `reader.py` | Get a document off disk, whole or streamed |
| `documents.py` | Reduce any shape to one canonical form |
| `enums.py` | DCE enum names → Discord's official values; the seed tables |
| `util.py` | Snowflakes, colours, timestamps, hashing |
| `schema.py` | The schema, declared once as metadata |
| `adapters/` | One class per engine, DDL rendered from the schema |
| `importer.py` | The per-file pipeline and the merge policy |
| `cli.py`, `progress.py`, `stats.py` | The command, the status bar, the report |

## Reading

Small files are parsed outright. Above a threshold (64 MiB) the reader streams with `ijson`
instead, in **two passes**:

1. everything except the document's one large array;
2. that array, one element at a time.

Two passes rather than one because of where DCE puts things. Under `--normal`, the lookup
tables that give meaning to `authorId`, `emojiKey` and the rest are written in the postamble,
*after* the messages — they are filled in as the export progresses, so they cannot be written
any earlier. A consumer has to reach the end of the file before the beginning of it means
anything.

A useful side effect: `messageCount` is also in the postamble, so it is known before the
messages are walked, which is what lets the progress bar interpolate honestly *within* a file
rather than jumping when the file ends.

## Normalizing

Two reductions:

**Rehydration** resolves `authorId`, `mentionIds`, `emojiKey`, `stickerIds`, `roleIds` and
`inlineEmojiKeys` against the root tables, so a normalized document becomes indistinguishable
from an inline one.

**Splitting** takes the merged person object apart into the user and the member. `--split-users`
already did; everything else has them folded together, and two of the fields do not come apart
cleanly:

- `nickname` in a merged object has already collapsed *nickname → display name → username* into
  one string. With `--extended` the user's own `displayName` sits next to it and the nickname is
  usually recoverable by comparison — though not always, since someone whose nickname equals
  their global display name reads as having set none. Without `--extended` the two cannot be
  told apart at all. (This is why the fork's merged user object gained `displayName` under
  `--extended`, and why `members.display` is treated as derived rather than observed below.)
- `avatarUrl` and `bannerUrl` are a guild override standing *in place of* the global one. Which
  of the two a merged object is showing can always be worked out, because Discord serves a
  member's guild-specific image from `/guilds/{guild}/users/{user}/…` rather than
  `/avatars/{user}/…`. What cannot be worked out is the global image of somebody who has set a
  guild one: it is not in the document at all. The key is then left out rather than written as
  NULL, so that importing a merged export after a split one does not erase it. (`--media`
  rewrites every URL to a local path and erases the distinction; the image is then credited to
  the user.)

A merged object also does not say whether the person is a member at all. It is taken to be one
when something could only have come from a member: a join date, a role, a name colour, a
guild-served image, a boost date, or member flags. Getting this right is what keeps merged and
split exports producing the same rows.

## Importing

One transaction per file. A failure rolls that file back and the run continues with the next.

Work is batched, in dependency order, over 500 messages at a time:

1. guild, channel, and the guild's role/emoji/sticker inventories
2. the batch's people → `users`, `members`, `user_roles`
3. emoji, stickers, interactions
4. messages, partitioned into new / changed / unchanged, with `message_history` for superseded
   content
5. attachments, mentions, stickers, reactions, embeds → resources
6. the `imports` row

Each step is one `executemany`, with `IN (…)` lists chunked to stay under the parameter limit.

Children are always reprocessed, not just for messages that changed, because a reaction or an
unfurled embed can appear long after the message itself stopped changing. They are written with
`INSERT OR IGNORE` against a unique constraint, so doing so costs one statement and never
duplicates.

## The merge policy

Three rules, and they are what most of the test suite is about.

**Nothing is deleted.** A record missing from a later export stays. This is an archive.

**A field the document does not carry is not written.** DCE distinguishes a key that is absent
from a key whose value is null, and so does this: `joinedAt: null` means "not a member", while
no `joinedAt` key at all means "exported without `--extended`". Only the first is written. This
is what lets a vanilla file and an extended one be imported in either order without the poorer
one erasing the richer one's work.

**A value a document can only *derive* is written when the row is created and never changed
afterwards.** Exactly one column qualifies: `members.display` from a merged export, which holds
the resolved display name where the schema wants the raw nickname. A derived value must not
overwrite an observed one — and that includes overwriting it with NULL, since "this member set
no nickname" is a claim in its own right that only a split export or a roster can make.

**A re-signed CDN link is not an edit.** Discord signs attachment and media URLs per export,
with `ex`, `is` and `hm` parameters that expire within a day. Comparing them literally would
rewrite every attachment row on every run and leave `updated_at` meaning nothing, so those three
parameters are ignored when deciding whether a URL changed. (`tools/compare_exports.py` in the
exporter's own repository strips the same ones.)

Getting this wrong is subtle and was worth a test of its own. An earlier version let a merged
export fill the column whenever it was empty, which sounds harmless and is not: emptiness is a
claim too, so a vanilla file and a split file then took turns overwriting each other and the
database ended up depending on which was read last.

Two further rules are specific to messages:

- A message's **author is never updated**. The only thing that ever reassigns one is the account
  being deleted, at which point Discord credits the message to a single stand-in user. Writing
  that would lose who actually wrote it, so the user is flagged `deleted` instead — which is
  also the only way to detect a deletion at all.
- Replaced **content goes to `message_history`** before the message row is updated, dated by the
  old `edited_timestamp`, or by the old `timestamp` if this is the first edit anyone has seen.

## Identity

Re-importing must not duplicate rows, which means everything needs a stable key. Most objects
have a Discord snowflake. The ones that do not:

| Row | Identity |
| --- | --- |
| Embeds | `(message_id, ordinal)` — an embed has no ID, and its URL may be null or repeated |
| Resources | `(embed_id, slot)`, slot being `thumbnail`, `image`, `video`, `author_icon`, `footer_icon` or `images[n]` |
| Message history | `(message_id, timestamp)` |
| Members | `(user_id, guild_id)` |
| Junction rows | their full column tuple |
| Standard emoji | their shortcode, with a synthetic ID below 2³² that cannot collide with a snowflake |

`imports` is the deliberate exception: one row per file read, never deduplicated, because the
row is the evidence that the import happened.

Unique constraints containing a nullable column are wrapped in `COALESCE(column, 0)`. Every SQL
engine treats NULLs as distinct inside a unique index, so without it the constraint would fail
to deduplicate exactly the rows a re-import would otherwise double — the anonymous reaction
totals, and emoji used outside an embed.

References are **not** enforced with foreign keys. An archive routinely points outside itself: a
reply whose parent predates the export range, a sticker from a server that was never exported, a
channel that no longer exists. Constraints would turn all of those into import failures.

## Engines

`schema.py` declares each table once as metadata — columns, logical types, unique constraints.
Adapters render DDL from that rather than keeping their own `CREATE TABLE` scripts, so an engine
cannot drift from the schema the importer writes against.

An adapter supplies the type map, the parameter style, the auto-increment keyword and the
spellings of "insert unless it's already there" and "insert or update". Everything else —
batched selects, inserts, updates, index creation — is shared. Adding MySQL or PostgreSQL is a
file of about fifty lines.

`Importer` takes the adapter as a constructor argument, so a subclass can be injected without
touching anything else.

## What a merged export cannot say

Three columns, and only these three, can be filled by a split export or a roster but not by a
merged one. All three are silences rather than disagreements — a merged export never contradicts
a split one, it only declines to speak:

| Column | Why |
| --- | --- |
| `members.display` | The nickname is collapsed into the resolved display name; someone whose nickname equals their global display name reads as having set none. |
| `users.avatar` | A guild avatar replaces the global one in the merged object, so the global one is not in the document. |
| `users.banner` | The same. |

Everything else matches exactly, which the tests assert over both the fixtures and a
thirty-channel live export.

## Testing

`tests/support.py` reduces a database to `{table: sorted rows}` with the bookkeeping columns and
auto-assigned keys removed. Most of what this project has to prove is an equality between two
such snapshots:

- the same conversation exported six ways
- a file imported once versus twice
- a range imported whole versus in halves
- every shape imported in order versus in reverse order, which is the strictest form of the
  promise and the easiest one to break
- vanilla-then-extended versus extended-then-vanilla versus extended alone
- an export with reaction users versus one without, on the totals

The fixtures in `tests/fixtures/` are real exports of the same two days of one channel, taken
with different options. See the README there.

Two things the fixtures are too small to exercise were checked by hand against live exports of
the N++ server, and are worth repeating after any change to the merge policy:

- **Thirty channels, one week, exported twice** — once `--extended --split-users`, once
  `--extended --normal`. 7,084 messages, 37 channels (7 of them parents known only from a
  child's `categoryId`), 135 users. The two databases came out identical apart from the three
  columns above, and importing both in either order converged on the richer result.
- **A 129 MiB export** through the streaming reader: imported at ~2,100 messages/second with a
  peak Python allocation of **9 MiB**, against the ~1.3 GiB that parsing it outright would have
  cost.
