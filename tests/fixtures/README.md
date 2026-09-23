# Test fixtures

Real exports, not synthetic ones. Every file here is the **same two days of the same channel**
— 343 messages of `#nplusplus` in the N++ server, 1–3 January 2025 — written out with different
DiscordChatExporter options. That is what makes the central test possible: import them all and
assert the databases match.

| File | Produced by |
| --- | --- |
| `upstream.json` | Vanilla DiscordChatExporter (see below) |
| `vanilla.json` | The fork at its defaults — the vanilla schema, plus the `mod` block |
| `ext.json` | `--extended` |
| `extnorm.json` | `--extended --normal` |
| `split.json` | `--extended --split-users` |
| `splitnorm.json` | `--extended --normal --split-users` |
| `noreact.json` | `--extended --split-users --reaction-users false` |
| `raw.json` | `--extended --markdown false` |
| `roster.json` | `exportusers`, trimmed (see below) |
| `channels.txt` | `channels --include-threads Active` |

Between them they cover attachments, embeds with thumbnails and video, custom and standard
emoji, a sticker, user and channel mentions, reactions, replies, threads and a parent channel
that is never exported in its own right.

They were all taken on 2026-09-22, which is what matters most about them: several tests assert
that two of these produce the *same* database, and exports of different moments would differ on
nicknames, roles and reaction counts that have nothing to do with the code.

**`raw.json` earns its place twice over.** It is the same 230 messages with `--markdown false`,
so it is both the only fixture in the raw shape *and* the ground truth for the mention
unresolver: whatever `--unresolve` reconstructs from `ext.json` has to come out identical to
what DCE itself wrote here. A test asserts exactly that, for all 230 bodies. `channels.txt`
supplies the channel names it needs, since two of the channels mentioned in those messages are
not themselves exported.

The comparison is byte for byte, with nothing normalized away on either side. That includes
custom emoji: `--unresolve` puts `:goldheart:` back as `<:goldheart:711809267547766806>` from
the message's own `inlineEmojis`, exactly as it does mentions.

## The two derived files

**`upstream.json`** is `vanilla.json` with the `mod` block removed. The fork writes that block
unconditionally, so it has no way to produce a file without one — but its default output was
verified byte-identical to upstream's apart from those lines, which makes stripping them a
faithful stand-in for a genuine upstream export. It is what proves the importer handles a file
with no `mod` block at all.

**`roster.json`** is the full 5,215-member roster of the same server cut down to 54 entries:
everyone who also appears in the message fixtures, plus twenty who do not, with `memberCount`
corrected. The overlap is the point — it is what lets the roster tests check that importing a
roster over a message archive enriches the members without disturbing the users.

## Regenerating them

From a checkout of the fork, with `DISCORD_TOKEN` set:

```bash
EXE=./DiscordChatExporter.Cli/bin/Release/net10.0/DiscordChatExporter.Cli.exe
A=2025-01-01T00:00:00+00:00
B=2025-01-03T00:00:00+00:00
C=197765375503368192   # the #nplusplus channel

$EXE export -t "$DISCORD_TOKEN" -c $C -f Json -o vanilla.json   --after $A --before $B
$EXE export -t "$DISCORD_TOKEN" -c $C -f Json -o ext.json       --after $A --before $B --extended
$EXE export -t "$DISCORD_TOKEN" -c $C -f Json -o extnorm.json   --after $A --before $B --extended --normal
$EXE export -t "$DISCORD_TOKEN" -c $C -f Json -o split.json     --after $A --before $B --extended --split-users
$EXE export -t "$DISCORD_TOKEN" -c $C -f Json -o splitnorm.json --after $A --before $B --extended --normal --split-users
$EXE export -t "$DISCORD_TOKEN" -c $C -f Json -o noreact.json   --after $A --before $B --extended --split-users --reaction-users false
$EXE export -t "$DISCORD_TOKEN" -c $C -f Json -o raw.json       --after $A --before $B --extended --markdown false

$EXE channels -t "$DISCORD_TOKEN" -g 197765375503368192 --include-threads Active > channels.txt
```

Regenerating is rarely worth it. These are an archive of a moment: nicknames, roles, avatars and
the server's boost count have all moved on since, so a fresh set would differ from these in ways
that have nothing to do with the code. Exports of *different* moments must not be mixed into one
set either, or the equality tests start failing on real changes to the server rather than on
bugs.
