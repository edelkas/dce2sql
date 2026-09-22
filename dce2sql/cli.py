"""Command line interface."""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from pathlib import Path

from . import __version__
from .adapters import DEFAULT_ENGINE, ENGINES, create, parse_url
from .documents import Document
from .importer import Importer
from .mentions import ChannelIndex, Unresolver
from .progress import ImportProgress
from .reader import Source, open_document, peek
from .stats import FileStats, Stats, render

#: Extensions considered when a directory is given as a source.
JSON_GLOB = "*.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dce2sql",
        description=(
            "Migrate DiscordChatExporter JSON exports into a SQL database. "
            "Reads vanilla exports and every variant of the extended fork, and can be run "
            "repeatedly over a growing archive: new records are added, changed ones are "
            "updated, and nothing is ever removed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  dce2sql archive.db export.json\n"
            "  dce2sql archive.db 'exports/**/*.json'\n"
            "  dce2sql archive.db exports/ --dry-run\n"
            "  dce2sql archive.db 'exports/*.json' --list\n"
            "  dce2sql archive -e postgres --user me exports/\n"
            "  dce2sql postgresql://me@localhost/archive exports/\n"
            "\n"
            "the password, for a server engine, is taken from --password, else from\n"
            "DCE2SQL_PASSWORD, else asked for at the terminal.\n"
        ),
    )

    parser.add_argument(
        "database",
        help=(
            "database file for SQLite, database name on the server otherwise, "
            "or a full URL such as postgresql://user@host/archive"
        ),
    )
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="SOURCE",
        help=(
            "JSON exports to import: files, directories, or glob patterns. "
            "Member rosters from the fork's 'exportusers' command are accepted too."
        ),
    )

    parser.add_argument(
        "-e",
        "--engine",
        default=None,
        choices=sorted(ENGINES),
        help=f"database engine (default: {DEFAULT_ENGINE}, or taken from a URL)",
    )

    server = parser.add_argument_group(
        "server options", "Ignored by SQLite, which has no server."
    )
    server.add_argument("--host", default=None, help="server host (default: localhost)")
    server.add_argument("--port", type=int, default=None, help="server port")
    server.add_argument("--user", default=None, help="user to connect as")
    server.add_argument(
        "--password",
        default=None,
        help=(
            "password. Prefer the DCE2SQL_PASSWORD environment variable or the prompt: "
            "a password given here is visible to anyone who can list processes"
        ),
    )
    server.add_argument(
        "--no-create",
        action="store_false",
        dest="create",
        help="fail if the database does not exist, instead of creating it",
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        dest="list_only",
        help="list the files that would be processed, then exit",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="parse the exports and report what is in them, without touching the database",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        metavar="N",
        help="messages to buffer before writing a batch (default: 500)",
    )
    content = parser.add_argument_group(
        "message content",
        "DCE resolves mentions into names before writing them, so a renamed channel or "
        "nickname makes old messages look edited. Export with '--markdown false' to avoid "
        "this; use these to repair exports already made.",
    )
    content.add_argument(
        "--unresolve",
        action="store_true",
        help=(
            "put resolved mentions back into their raw '<@123>' form before importing, so "
            "that a later rename cannot make an unchanged message look edited"
        ),
    )
    content.add_argument(
        "--channels",
        default=None,
        metavar="FILE",
        help=(
            "output of DCE's 'channels' command, naming every channel in the server. "
            "Only useful with --unresolve, and the best source it has for channel mentions"
        ),
    )

    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="abort the run on the first file that fails, instead of reporting and going on",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="suppress the status bar",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress the closing report as well"
    )
    parser.add_argument("--version", action="version", version=f"dce2sql {__version__}")

    return parser


def resolve_sources(patterns: list[str]) -> list[Source]:
    """Expand files, directories and glob patterns into a sorted, deduplicated list.

    Globs are expanded here rather than left to the shell, because the shell that matters on
    Windows does not expand them at all, and a quoted pattern is the documented way to reach
    a whole archive in one command.
    """
    found: dict[Path, Source] = {}

    for pattern in patterns:
        path = Path(pattern)
        if path.is_dir():
            matches = _scan(path)
        elif path.exists():
            matches = [Source.of(path)]
        else:
            matches = [
                Source.of(p)
                for p in sorted(glob.glob(pattern, recursive=True))
                if Path(p).is_file()
            ]

        for source in matches:
            found.setdefault(source.path, source)

    return [found[p] for p in sorted(found)]


def _scan(directory: Path) -> list[Source]:
    """Walk a directory for exports, taking each file's size from the scan itself.

    Deliberately not ``rglob`` plus ``is_file``.  On Windows a path over 259 characters -- which
    DCE reaches easily, since it names a file after the server, the category, the channel, its
    ID and the date range -- can still be *listed* but no longer stat'ed by path, so ``is_file``
    answers False for a file that is plainly there and the export gets skipped without a word.
    Reading it will fail later, loudly and with an explanation, which is the right outcome; what
    must not happen is it quietly going missing.
    """
    out: list[Source] = []
    stack = [directory]

    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue

        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.name.lower().endswith(".json"):
                out.append(Source(path=Path(entry.path), size=entry.stat().st_size))

    return sorted(out, key=lambda s: s.path)


def connection_options(args, console) -> dict:
    """Work out which engine to use and how to reach it.

    A URL settles everything at once; otherwise the flags do, with the engine defaulting to
    SQLite.  Explicit flags win over a URL, so a stored URL can be reused with one part changed.
    """
    options = {"engine": args.engine or DEFAULT_ENGINE, "database": args.database}

    from_url = parse_url(args.database)
    if from_url is not None:
        options = {**from_url, **({"engine": args.engine} if args.engine else {})}

    for name in ("host", "port", "user", "password"):
        given = getattr(args, name, None)
        if given is not None:
            options[name] = given

    options["create"] = args.create
    options.setdefault("host", "localhost")

    if options["engine"] != "sqlite" and options.get("password") is None:
        options["password"] = _password(options.get("user"), console)

    return options


def _password(user, console) -> str | None:
    """A password from the environment, or asked for, but never guessed.

    Kept off the command line where it can be: an argument is visible in the process list and
    ends up in shell history, which for a long-lived archive credential is worse than it
    sounds.
    """
    from_env = os.environ.get("DCE2SQL_PASSWORD")
    if from_env is not None:
        return from_env

    if not sys.stdin.isatty():
        return None

    import getpass

    who = f" for {user}" if user else ""
    try:
        return getpass.getpass(f"Password{who} (blank for none): ") or None
    except (EOFError, KeyboardInterrupt):
        console.print()
        return None


def main(argv: list[str] | None = None) -> int:
    from rich.console import Console

    args = build_parser().parse_args(argv)
    console = Console(stderr=True)

    sources = resolve_sources(args.sources)
    if not sources:
        console.print("[red]No files matched.[/red]")
        return 1

    total = sum(s.size for s in sources)

    if args.list_only:
        for source in sources:
            console.print(f"{_size(source.size):>10}  {source.path}", soft_wrap=True)
        console.print(f"\n{len(sources)} file(s), {_size(total)}")
        return 0

    if args.channels and not args.unresolve:
        console.print("[red]--channels only does something with --unresolve.[/red]")
        return 1

    if args.batch_size:
        from . import importer as importer_module

        importer_module.BATCH_SIZE = max(1, args.batch_size)

    stats = Stats()
    started = time.time()

    connection = None if args.dry_run else connection_options(args, console)

    show_progress = not args.no_progress and console.is_terminal
    with ImportProgress(total, enabled=show_progress, console=console) as progress:
        if args.dry_run:
            failed = _dry_run(sources, stats, progress)
        else:
            failed = _import(args, connection, sources, stats, progress)

    stats.unresolver = _UNRESOLVER[0]

    stats.seconds = time.time() - started

    if not args.quiet:
        render(stats, console=Console(), dry_run=args.dry_run)

    return 1 if failed else 0


#: Set by the run so the closing report can say what the unresolver managed. A module global
#: because it is built inside _import and read after it, and threading it back out through the
#: return value would mean changing what every other caller expects.
_UNRESOLVER: list = [None]


def build_unresolver(args, sources, adapter, progress) -> Unresolver:
    """Survey every channel the run could possibly be talking about, before importing any of it.

    A message body says ``#general``; only a name-to-ID map turns that back into ``<#123>``.
    No single source has all of them, so three are pooled, in the order their names are most
    likely to be the ones in force when the bodies were written:

    1. the exports being imported -- each knows its own channel and its parent, named as they
       were at the time, which is exactly the vintage wanted;
    2. a ``channels`` listing, which knows every channel in the server but only as of whenever
       it was run;
    3. the database, which knows everything ever seen.

    The first to claim a name keeps it. Reading the exports costs a few kilobytes each rather
    than a full parse, because the channel sits at the very top of the document.
    """
    index = ChannelIndex()

    for source in sources:
        try:
            index.add_export_header(peek(source))
        except Exception:  # noqa: BLE001 -- a file that cannot be peeked will fail properly later
            continue

    if args.channels:
        try:
            index.add_listing(args.channels)
        except OSError as exc:
            progress.log(f"[yellow]could not read {args.channels}:[/yellow] {exc}")

    try:
        index.add_database(
            adapter.fetchall(
                f"SELECT {adapter.quote('id')}, {adapter.quote('name')}, "
                f"{adapter.quote('type')} FROM {adapter.quote('channels')}"
            )
        )
    except Exception:  # noqa: BLE001 -- an empty or brand new database simply has none
        pass

    unresolver = Unresolver(index)
    try:
        unresolver.add_roles(
            {"id": i, "name": n}
            for i, n in adapter.fetchall(
                f"SELECT {adapter.quote('id')}, {adapter.quote('name')} "
                f"FROM {adapter.quote('roles')}"
            )
        )
    except Exception:  # noqa: BLE001
        pass

    return unresolver


def _import(
    args, connection: dict, sources: list[Source], stats: Stats, progress: ImportProgress
) -> bool:
    engine = connection.pop("engine")
    database = connection.pop("database")
    adapter = create(engine, database, **connection)

    try:
        adapter.connect()
    except Exception as exc:  # noqa: BLE001
        where = adapter.describe() if hasattr(adapter, "describe") else database
        progress.log(f"[red]cannot open {where}:[/red] {exc}")
        return True

    try:
        adapter.create_schema()
        importer = Importer(adapter, stats, progress)

        unresolver = None
        if args.unresolve:
            unresolver = build_unresolver(args, sources, adapter, progress)
            _UNRESOLVER[0] = unresolver
            # Roles are pooled as each file is read, so only the channel survey -- the part
            # that had to happen up front -- can be reported here
            sources_of = ", ".join(
                f"{n:,} from {where}" for where, n in unresolver.channels.sources.most_common()
            )
            progress.log(
                f"[dim]Unresolving against {len(unresolver.channels):,} channel name(s): "
                f"{sources_of or 'none found'}.[/dim]"
            )

        for source in sources:
            doc = _open(source, stats, progress, unresolver)
            if doc is None:
                if args.stop_on_error:
                    return True
                continue

            progress.start_file(source.path.name, source.size, doc.declared_count)
            result = importer.import_document(doc, source)
            progress.finish_file()

            if result.error:
                progress.log(f"[red]failed[/red] {source.path}: {result.error}")
                if args.stop_on_error:
                    return True

        summarize(adapter, stats)
        return stats.files_failed > 0
    finally:
        adapter.close()


def summarize(adapter, stats: Stats) -> None:
    """Read back a few totals, so the report says what the archive is, not just what changed.

    These are the numbers worth knowing after a long run over a growing archive: how much is in
    there now, how many people it covers, and what span of time it reaches across. Counting is
    cheap next to the import that produced it.
    """
    counts = {
        "Guilds": "guilds",
        "Channels": "channels",
        "Messages": "messages",
        "Users": "users",
        "Members": "members",
        "Attachments": "attachments",
        "Embeds": "embeds",
        "Reactions": "reactions",
        "Emoji": "emojis",
        "Edits kept": "message_history",
    }

    try:
        for label, table in counts.items():
            stats.archive[label] = f"{adapter.count(table):,}"

        deleted = adapter.fetchall(
            f"SELECT COUNT(*) FROM {adapter.quote('users')} "
            f"WHERE {adapter.quote('deleted')} = 1"
        )[0][0]
        if deleted:
            stats.archive["Deleted accounts"] = f"{deleted:,}"

        span = adapter.fetchall(
            f"SELECT MIN({adapter.quote('timestamp')}), MAX({adapter.quote('timestamp')}) "
            f"FROM {adapter.quote('messages')}"
        )[0]
        if span[0] is not None:
            stats.archive["Spanning"] = f"{_date(span[0])} to {_date(span[1])}"

        imports = adapter.count("imports")
        stats.archive["Files imported"] = f"{imports:,}"
    except Exception:  # noqa: BLE001 -- a report is never worth failing a good import over
        stats.archive.clear()


def _date(instant) -> str:
    """Format a stored timestamp, whichever way the engine gave it back.

    SQLite keeps Unix seconds; MySQL and PostgreSQL have native types and hand back a datetime.
    """
    from datetime import datetime, timezone

    if isinstance(instant, datetime):
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(instant, timezone.utc).strftime("%Y-%m-%d")


def _dry_run(sources: list[Source], stats: Stats, progress: ImportProgress) -> bool:
    """Parse everything and report what is in it, without opening a database.

    Useful on its own -- it validates a whole archive and says which DCE options produced it --
    and as a rehearsal before committing to a long import.
    """
    for source in sources:
        doc = _open(source, stats, progress)
        if doc is None:
            continue

        progress.start_file(source.path.name, source.size, doc.declared_count)
        fs = FileStats(path=str(source.path), shape=doc.describe(), bytes=source.size)

        try:
            _count(doc, fs, stats, progress)
        except Exception as exc:  # noqa: BLE001
            fs.error = f"{type(exc).__name__}: {exc}"
            progress.log(f"[red]failed[/red] {source.path}: {fs.error}")

        progress.finish_file()
        stats.record(fs)

    return stats.files_failed > 0


def _count(doc: Document, fs: FileStats, stats: Stats, progress: ImportProgress) -> None:
    """Tally what a document holds, mirroring the tables an import would fill."""
    from .importer import emojis_in, people_in, stickers_in

    doc.validate()

    if doc.guild:
        stats.insert("guilds", 1)
    if doc.channel:
        stats.insert("channels", 1)
    stats.insert("roles", len(doc.guild_roles()))

    users: set = set()
    members: set = set()

    for person in doc.owners():
        users.add(person.id)

    if doc.is_roster:
        for person in doc.members():
            fs.members += 1
            users.add(person.id)
            members.add(person.id)
            progress.advance()
    else:
        for message in doc.messages():
            fs.messages += 1
            progress.advance()

            for person in people_in(message):
                users.add(person.id)
                if person.member is not None:
                    members.add(person.id)

            stats.insert("attachments", len(message.get("attachments") or []))
            stats.insert("embeds", len(message.get("embeds") or []))
            stats.insert("mentions", len(message.get("mentions") or []))
            stats.insert("message_stickers", len(message.get("stickers") or []))
            stats.insert("message_emojis", len(message.get("inlineEmojis") or []))
            for reaction in message.get("reactions") or []:
                stats.insert("reactions", max(1, len(reaction.get("users") or [])))
            if message.get("interaction"):
                stats.insert("interactions", 1)

            stats.insert("emojis", len(list(emojis_in([message]))))
            stats.insert("stickers", len(list(stickers_in([message]))))

    stats.insert("messages", fs.messages)
    stats.insert("users", len(users))
    stats.insert("members", len(members))


def _open(
    source: Source, stats: Stats, progress: ImportProgress, unresolver=None
) -> Document | None:
    try:
        return Document(open_document(source), unresolver)
    except Exception as exc:  # noqa: BLE001
        progress.log(
            f"[red]unreadable[/red] {source.path}: {type(exc).__name__}: {exc}"
            + _long_path_hint(source.path)
        )
        stats.record(
            FileStats(path=str(source.path), bytes=source.size, error=str(exc))
        )
        return None


#: Windows refuses path-based access beyond this, counting the terminating NUL.
MAX_PATH = 259


def _long_path_hint(path: Path) -> str:
    """Explain the one failure whose error message gives no clue what went wrong."""
    if os.name != "nt" or len(str(path)) <= MAX_PATH:
        return ""
    return (
        f"\n  [yellow]The path is {len(str(path))} characters, over Windows' limit of "
        f"{MAX_PATH}.[/yellow] Move the exports somewhere shorter, or turn on long path "
        f"support (LongPathsEnabled)."
    )


def _size(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


if __name__ == "__main__":
    sys.exit(main())
