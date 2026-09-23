# dce2sql

Migrate [DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter) JSON exports into a
SQL database.

JSON is a good archival format and a poor query format. Once a few years of a server are on
disk, answering *who posted most in 2019*, *which emoji fell out of use*, or *what did this
message say before it was edited* means re-parsing hundreds of megabytes. dce2sql turns a pile
of exports into one queryable database — and keeps doing it as the archive grows, without ever
losing what an earlier run saw.

## Features

- **Reads every export shape.** Vanilla DiscordChatExporter, and every combination of the
  [extended fork](https://github.com/edelkas/DiscordChatExporter)'s `--extended`, `--normal`,
  `--split-users` and `--reaction-users` options. The same conversation exported six different
  ways produces the same database.
- **Incremental and idempotent.** Run it again over a growing archive: new records are
  inserted, changed ones updated, and re-importing a file you already have changes nothing.
- **Nothing is ever deleted.** An edit is recorded in `message_history` rather than overwriting
  what was there. A message's author survives the account behind it being deleted.
- **A rename is not an edit.** DCE writes `#general` where the message said `<#123>`, so renaming
  a channel — or a nickname, or a custom emoji — changes the body of every message that ever
  used it. `--unresolve` puts those back before importing, so only real edits are recorded as
  edits.
- **A poorer export never erases a richer one.** A field an export could not carry is left
  alone, so a vanilla file and an extended one can be imported in either order.
- **Users and members kept apart.** The global account and its per-server profile go in
  separate tables, the way Discord actually has them — recovered even from exports that merge
  the two.
- **Member rosters.** The fork's `exportusers` documents import too, filling in everyone who
  never posted.
- **Three engines, one archive.** SQLite, MySQL and PostgreSQL. The same exports produce a
  byte-identical archive on all three, which the test suite asserts.
- **Streams large files.** Exports past a threshold are read with a constant memory footprint:
  a 129 MiB export imports in a 9 MiB peak, against the ~1.3 GiB that parsing it outright would
  cost.

## Installation

```bash
pip install .                 # SQLite, which needs no driver
pip install '.[mysql]'        # ...and MySQL
pip install '.[postgres]'     # ...and PostgreSQL
pip install '.[all]'          # ...and both
```

Python 3.10 or newer. MySQL 8.0.13+ or MariaDB 10.8+; PostgreSQL 9.5+.

## Usage

```
dce2sql DATABASE SOURCE [SOURCE ...]
```

`SOURCE` may be a file, a directory (searched recursively for `.json`), or a glob pattern.
Quote patterns so dce2sql expands them rather than the shell.

| Option | Meaning |
| --- | --- |
| `-e`, `--engine` | `sqlite`, `mysql` or `postgres` (default: `sqlite`) |
| `--host`, `--port`, `--user`, `--password` | Server connection details; ignored by SQLite |
| `--no-create` | Fail if the database does not exist, instead of creating it |
| `-l`, `--list` | List the files that would be processed, then exit |
| `-n`, `--dry-run` | Parse and report what is in the exports, without touching the database |
| `--batch-size N` | Messages buffered before each write (default: 500) |
| `--unresolve` | Put resolved mentions and custom emoji back into their raw `<@123>` form |
| `--channels FILE` | Output of DCE's `channels` command, for resolving channel mentions |
| `--stop-on-error` | Abort on the first failing file instead of reporting and going on |
| `--no-progress` | Suppress the status bar |
| `-q`, `--quiet` | Suppress the closing report as well |

## Examples

Import a year of one channel, then add the next year to the same database:

```bash
dce2sql archive.db 'exports/nplusplus-2024-*.json'
dce2sql archive.db 'exports/nplusplus-2025-*.json'
```

Check what a directory of exports contains before committing to importing it:

```bash
dce2sql archive.db exports/ --dry-run
```

Use a server instead of a file, giving the database as a name or a URL:

```bash
dce2sql archive --engine postgres --user me exports/
dce2sql postgresql://me@localhost/archive exports/
```

The database is created if it does not exist. The password comes from `--password`, else from
`DCE2SQL_PASSWORD`, else it is asked for — prefer the last two, since an argument is visible to
anyone who can list processes.

Then query it:

```sql
SELECT u.name, COUNT(*) AS posts
FROM messages m JOIN users u ON u.id = m.user_id
WHERE m.timestamp >= strftime('%s', '2019-01-01')
  AND m.timestamp <  strftime('%s', '2020-01-01')
GROUP BY u.id ORDER BY posts DESC LIMIT 10;
```

## Keeping an archive from drifting

DCE resolves mentions and custom emoji before writing a message body:
`<@197765375503368192>` is written as `@Nickname`, `<#449367560878686208>` as `#channel-name`,
`<:goldheart:711809267547766806>` as `:goldheart:`. All of them are resolved against names that
can change, so re-exporting the same conversation after a rename produces different text for
messages nobody edited — and an importer has no way to tell those from the handful that really
were edited.

**The fix for exports you have yet to make** is to export with markdown off, which leaves the
bodies in their raw form:

```bash
DiscordChatExporter.Cli export -t TOKEN -c CHANNEL -f Json --markdown false
```

**The fix for exports already made** is `--unresolve`, which puts the mentions back on the way
in:

```bash
dce2sql archive.db 'exports/2019/*.json' --unresolve --channels channels-2019.txt
```

Each kind is recovered from whatever the message itself says, wherever possible:

| Kind | Recovered from |
| --- | --- |
| User | the message's own `mentions` array |
| Custom emoji | the message's own `inlineEmojis` array |
| Channel | its own `channelMentions` (`--extended`), else a pooled name-to-ID map |
| Role | its own `roleMentions` (`--extended`), else the server's role list |

So a body is only ever rewritten to name something it already says it used. The pooled channel
map is the one place guesswork enters: it draws on the exports being imported, on a `channels`
listing if you give one, and on the database — in that order, so a name of the right vintage
wins over a later one. An `--extended` export needs none of it. Keeping a `channels` listing
alongside each batch is what makes the rest reliable.

With everything in place this is not an approximation: on the test corpus it reproduces DCE's
own `--markdown false` output byte for byte, for all 230 message bodies.

What cannot be recovered is left exactly as it is. That includes what DCE writes when it could
not resolve something itself (`@Unknown`, `#deleted-channel`), standard Unicode emoji (the
character *is* the raw form), and anything that merely looks like a mention — a bare `#hashtag`,
an `@name` nobody was pinged by, a `:word:` between colons. The matching is deliberately
cautious: it would rather miss one than rewrite something that was never a mention at all.

## Engines

| | SQLite | MySQL | PostgreSQL |
| --- | --- | --- | --- |
| Driver | built in | PyMySQL or mysqlclient | psycopg 3 or psycopg2 |
| Timestamps | Unix seconds | `DATETIME`, UTC | `TIMESTAMPTZ` |
| Booleans | 0 / 1 | `BOOLEAN` | `BOOLEAN` |
| Rich fields | text | `JSON` | `JSONB` |
| Throughput | ~3,100 msg/s | ~1,100 msg/s | ~1,300 msg/s |

SQLite is the default and the fastest, and puts the whole archive in one file you can copy
anywhere. A server is worth it when several people or tools need to query the archive at once,
or when you want the indexing and query planning that `JSONB` and a real timestamp type bring.

Whichever you choose, the archive is the same: the suite imports the same exports into all
three and asserts the resulting databases match, row for row.

## Documentation

- [docs/SQL.md](docs/SQL.md) — the database schema, table by table
- [docs/JSON.md](docs/JSON.md) — the export format this reads
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the tool is put together, and why

## References

- [DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter) — the exporter
- [The extended fork](https://github.com/edelkas/DiscordChatExporter) — extra fields and options
- [Discord API documentation](https://docs.discord.com/developers/docs/intro) — the source of
  the type values and object shapes

## License

MIT. See [LICENSE](LICENSE).
