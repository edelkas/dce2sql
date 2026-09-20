"""Counters gathered during a run, and the report printed at the end of it."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class FileStats:
    """What one file contributed."""

    path: str = ""
    shape: str = ""
    bytes: int = 0
    messages: int = 0
    new_messages: int = 0
    edited_messages: int = 0
    members: int = 0
    first_message_id: int | None = None
    last_message_id: int | None = None
    skipped: bool = False
    error: str | None = None


@dataclass
class Stats:
    """Totals across the whole run."""

    files: int = 0
    files_failed: int = 0
    bytes: int = 0
    seconds: float = 0.0

    #: Rows inserted and rows updated, per table
    inserted: Counter = field(default_factory=Counter)
    updated: Counter = field(default_factory=Counter)

    messages_seen: int = 0
    messages_new: int = 0
    messages_edited: int = 0
    members_seen: int = 0
    users_deleted: int = 0

    #: Shapes encountered, so a mixed archive says so rather than looking uniform
    shapes: Counter = field(default_factory=Counter)

    #: Enum values no version of DCE known to this tool can name
    unknown_types: Counter = field(default_factory=Counter)

    per_file: list[FileStats] = field(default_factory=list)

    #: What the database holds once the run is over, as opposed to what this run contributed.
    #: Filled in by the CLI; see Adapter-side queries in cli.summarize.
    archive: dict = field(default_factory=dict)

    def record(self, file_stats: FileStats) -> None:
        self.per_file.append(file_stats)
        self.files += 1
        self.bytes += file_stats.bytes
        if file_stats.error:
            self.files_failed += 1
            return
        self.shapes[file_stats.shape] += 1
        self.messages_seen += file_stats.messages
        self.messages_new += file_stats.new_messages
        self.messages_edited += file_stats.edited_messages
        self.members_seen += file_stats.members

    def insert(self, table: str, count: int) -> None:
        if count:
            self.inserted[table] += count

    def update(self, table: str, count: int) -> None:
        if count:
            self.updated[table] += count

    @property
    def rows_written(self) -> int:
        return sum(self.inserted.values()) + sum(self.updated.values())


def render(stats: Stats, console=None, dry_run: bool = False) -> None:
    """Print the closing report."""
    from rich.console import Console
    from rich.table import Table

    console = console or Console()

    heading = "Parsed" if dry_run else "Imported"
    failed = f", {stats.files_failed} failed" if stats.files_failed else ""
    console.print()
    console.print(
        f"[bold]{heading}[/bold] {stats.files - stats.files_failed} file(s){failed}"
        f" — {_size(stats.bytes)} in {_duration(stats.seconds)}"
        f" ({_size(stats.bytes / stats.seconds) if stats.seconds else '—'}/s)"
    )

    if stats.shapes:
        shapes = "  ".join(f"{name} ×{n}" for name, n in stats.shapes.most_common())
        console.print(f"  Export shapes: {shapes}", soft_wrap=True)

    console.print(
        f"  Messages: {stats.messages_seen:,} seen, "
        f"{stats.messages_new:,} new, {stats.messages_edited:,} edited"
    )
    if stats.members_seen:
        console.print(f"  Roster entries: {stats.members_seen:,}")
    if stats.users_deleted:
        console.print(
            f"  Users newly found deleted: {stats.users_deleted:,}"
            "  (their messages are reassigned by Discord, so the originals are kept)"
        )

    if stats.unknown_types:
        console.print()
        unknown = ", ".join(f"{k} ×{n}" for k, n in stats.unknown_types.most_common(10))
        console.print(f"  [yellow]Unrecognized type values:[/yellow] {unknown}")
        console.print("  [dim]Stored as NULL. Usually means DCE is newer than this tool.[/dim]")

    if stats.archive:
        console.print()
        console.print("[bold]The archive now holds[/bold]")
        for label, value in stats.archive.items():
            console.print(f"  {label}: {value}")

    tables = sorted(set(stats.inserted) | set(stats.updated))
    if not tables:
        console.print()
        console.print("[dim]Nothing to write; the database already had all of it.[/dim]")
        return

    table = Table(title=None, box=None, pad_edge=False, show_edge=False)
    table.add_column("Table", style="cyan")
    table.add_column("Rows" if dry_run else "Inserted", justify="right")
    table.add_column("Updated", justify="right")
    for name in tables:
        table.add_row(
            name,
            f"{stats.inserted.get(name, 0):,}",
            f"{stats.updated.get(name, 0):,}" if stats.updated.get(name) else "",
        )
    console.print()
    console.print(table)
    if dry_run:
        console.print()
        console.print("[dim]Dry run: nothing was written.[/dim]")


def _size(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s"
