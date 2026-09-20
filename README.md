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
- **A poorer export never erases a richer one.** A field an export could not carry is left
  alone, so a vanilla file and an extended one can be imported in either order.
- **Users and members kept apart.** The global account and its per-server profile go in
  separate tables, the way Discord actually has them — recovered even from exports that merge
  the two.
- **Member rosters.** The fork's `exportusers` documents import too, filling in everyone who
  never posted.
- **Engine-agnostic.** SQLite today, in one self-contained file; the adapter layer exists so
  MySQL and PostgreSQL are a new file rather than a new code path.
- **Streams large files.** Exports past a threshold are read with a constant memory footprint:
  a 129 MiB export imports in a 9 MiB peak, against the ~1.3 GiB that parsing it outright would
  cost.

## Installation

```bash
pip install .
```

Python 3.10 or newer.

## Usage

```
dce2sql DATABASE SOURCE [SOURCE ...]
```

`SOURCE` may be a file, a directory (searched recursively for `.json`), or a glob pattern.
Quote patterns so dce2sql expands them rather than the shell.

| Option | Meaning |
| --- | --- |
| `-e`, `--engine` | Database engine (default: `sqlite`) |
| `-l`, `--list` | List the files that would be processed, then exit |
| `-n`, `--dry-run` | Parse and report what is in the exports, without touching the database |
| `--batch-size N` | Messages buffered before each write (default: 500) |
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

Then query it:

```sql
SELECT u.name, COUNT(*) AS posts
FROM messages m JOIN users u ON u.id = m.user_id
WHERE m.timestamp >= strftime('%s', '2019-01-01')
  AND m.timestamp <  strftime('%s', '2020-01-01')
GROUP BY u.id ORDER BY posts DESC LIMIT 10;
```

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
