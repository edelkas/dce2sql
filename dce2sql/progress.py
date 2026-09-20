"""The status bar shown while importing.

Progress is measured in *bytes of JSON*, not in files or messages.  Files vary enormously in
size, and a message can be a one-word reply or a wall of embeds, but the total number of bytes
to get through is known exactly before anything is parsed -- which makes it the only honest
basis for an estimate.

Within a file the byte count advances per message rather than in one jump at the end, using the
``messageCount`` DCE writes into the document.  That count sits in the postamble, after the
messages, but the header pass has already read past it by the time the messages are walked, so
it is available in time.
"""

from __future__ import annotations

#: The status bar redraws at most this often, per INSTRUCTIONS.md.
REFRESH_PER_SECOND = 1


class ImportProgress:
    """A bar latched to the bottom of the terminal for the duration of the run.

    Disabled -- with ``--no-progress``, or when stderr is not a terminal -- every method
    becomes a no-op, so callers never have to check.
    """

    def __init__(self, total_bytes: int, enabled: bool = True, console=None) -> None:
        self.enabled = enabled and total_bytes > 0
        self.total_bytes = total_bytes
        self.messages = 0
        self._console = console
        self._progress = None
        self._task = None
        self._file_bytes = 0.0
        self._done_bytes = 0.0
        self._completed = 0.0
        self._per_message = 0.0

    def __enter__(self) -> "ImportProgress":
        if not self.enabled:
            return self

        from rich.progress import (
            BarColumn,
            Progress,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )

        self._progress = Progress(
            TextColumn("[bold blue]{task.fields[label]}", justify="left"),
            BarColumn(bar_width=None),
            TextColumn("{task.percentage:>5.1f}%"),
            TextColumn("[green]{task.fields[messages]:>9} msgs"),
            TimeElapsedColumn(),
            TextColumn("eta"),
            TimeRemainingColumn(),
            console=self._console,
            refresh_per_second=REFRESH_PER_SECOND,
            transient=False,
        )
        self._progress.__enter__()
        self._task = self._progress.add_task(
            "import", total=self.total_bytes, label="starting", messages="0"
        )
        return self

    def __exit__(self, *exc) -> None:
        if self._progress is not None:
            self._progress.__exit__(*exc)
            self._progress = None

    # -- per file ----------------------------------------------------------------------

    def start_file(self, label: str, size: int, declared_messages: int | None) -> None:
        self._file_bytes = float(size)
        # A file with no message count -- an empty channel, or a roster -- simply jumps at the
        # end rather than pretending to a smoothness it cannot have
        self._per_message = size / declared_messages if declared_messages else 0.0
        self._update(label=_shorten(label))

    def advance(self, messages: int = 1) -> None:
        self.messages += messages
        if self._per_message:
            self._bump(self._per_message * messages)
        else:
            self._update()

    def finish_file(self) -> None:
        """Snap to the file's true size, undoing any drift from the per-message estimate."""
        self._done_bytes += self._file_bytes
        self._completed = self._done_bytes
        if self._progress is not None:
            self._progress.update(
                self._task, completed=self._completed, messages=f"{self.messages:,}"
            )
        self._file_bytes = 0.0
        self._per_message = 0.0

    def log(self, message: str) -> None:
        """Print above the bar, so the bar stays at the bottom."""
        # Soft wrapping, because these carry file paths that rich would otherwise break
        # across lines mid-path and make unusable
        if self._progress is not None:
            self._progress.console.print(message, soft_wrap=True)
        elif self._console is not None:
            self._console.print(message, soft_wrap=True)

    # -- internals ---------------------------------------------------------------------

    def _bump(self, amount: float) -> None:
        # Never let the estimate run past the end of the file it is inside, however far off
        # the per-message figure turns out to be
        self._completed = min(self._done_bytes + self._file_bytes, self._completed + amount)
        if self._progress is not None:
            self._progress.update(
                self._task, completed=self._completed, messages=f"{self.messages:,}"
            )

    def _update(self, **fields) -> None:
        if self._progress is not None:
            self._progress.update(self._task, messages=f"{self.messages:,}", **fields)


def _shorten(label: str, width: int = 32) -> str:
    if len(label) <= width:
        return label.ljust(width)
    return ("..." + label[-(width - 3) :]).ljust(width)
