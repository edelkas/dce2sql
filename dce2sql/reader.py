"""Getting a DCE export off disk without having to hold it in memory.

A single channel exported over several years is routinely hundreds of megabytes, and
``json.load`` on one costs roughly ten times the file size in RAM.  Above a threshold this
module switches to streaming with ``ijson`` instead, reading the file twice: once to collect
everything *except* the big array, and once to walk the array itself.

Two passes rather than one because of where DCE puts things.  Under ``--normal`` the lookup
tables that give meaning to ``authorId``, ``emojiKey`` and the rest are written in the
postamble, *after* the messages -- they are filled in as the export progresses, so they cannot
be written any earlier.  A consumer therefore has to reach the end of the file before it can
make sense of the beginning.

Both paths expose the same two things, so nothing downstream knows or cares which one ran.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .util import file_sha1

#: Files at or above this size are streamed.  Below it, parsing the whole thing outright is
#: several times faster and the memory does not matter.
STREAMING_THRESHOLD = 64 * 1024 * 1024

#: The two array keys a DCE document can be built around.
MESSAGES = "messages"
MEMBERS = "members"


@dataclass
class Source:
    """A file queued for import, and what can be known about it before parsing."""

    path: Path
    size: int

    _sha1: str | None = field(default=None, repr=False)

    @classmethod
    def of(cls, path: str | Path) -> "Source":
        path = Path(path)
        return cls(path=path, size=path.stat().st_size)

    @property
    def sha1(self) -> str:
        """Hashed on demand, since ``--list`` has no use for it."""
        if self._sha1 is None:
            self._sha1 = file_sha1(self.path)
        return self._sha1

    def __str__(self) -> str:
        return str(self.path)


class RawDocument:
    """A parsed export: the header, plus an iterator over its one large array.

    ``header`` holds every top-level key except that array -- ``mod``, ``guild``, ``channel``,
    ``dateRange``, ``exportedAt``, ``messageCount``, and under ``--normal`` the ``users``,
    ``members``, ``roles``, ``emojis`` and ``stickers`` lookup tables.
    """

    def __init__(self, source: Source, header: dict, array_key: str, loader) -> None:
        self.source = source
        self.header = header
        self.array_key = array_key
        self._loader = loader

    def items(self) -> Iterator[dict]:
        """Walk the document's main array, one element at a time."""
        return self._loader()


def open_document(
    source: Source, threshold: int = STREAMING_THRESHOLD
) -> RawDocument:
    """Read ``source``, choosing the whole-file or the streaming path by size."""
    if source.size < threshold:
        with source.path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        array_key = array_key_of(document)
        items = document.pop(array_key, [])
        return RawDocument(source, document, array_key, lambda: iter(items))

    array_key = _detect_array_key(source.path)
    header = _stream_header(source.path, array_key)
    return RawDocument(source, header, array_key, lambda: _stream_items(source.path, array_key))


def peek(source: Source, keys=("guild", "channel")) -> dict:
    """Read just the named top-level keys of an export, and stop.

    Used to survey a whole run before importing any of it -- which channels existed and what
    they were called at the time -- without paying to parse every file twice.  Those keys sit
    at the very start of the document, so this costs a few kilobytes per file however large it
    is.
    """
    import ijson

    wanted = set(keys)
    out: dict = {}

    with source.path.open("rb") as handle:
        events = ijson.basic_parse(handle, buf_size=1 << 15)

        event, _ = next(events, ("", None))
        if event != "start_map":
            return out

        key = None
        for event, value in events:
            if event == "map_key":
                key = value
            elif event == "end_map" and key is None:
                break
            elif key in wanted:
                out[key] = _consume_value(events, event, value)
                key = None
                if wanted <= set(out):
                    break
            else:
                _skip_value(events, event)
                key = None

    return out


def array_key_of(document: dict) -> str:
    """Decide which array a document is built around, given its top-level keys.

    A message export has ``channel``; a roster from ``exportusers`` does not.  The order of
    these checks matters: a normalized ``--split-users`` message export *also* has a root
    ``members`` array, but that one is a lookup table rather than the document's subject.
    """
    if "channel" in document or MESSAGES in document:
        return MESSAGES
    if MEMBERS in document:
        return MEMBERS
    # An export of an empty channel still has both keys, so reaching here means the file is
    # not a DCE export at all; the importer reports that properly
    return MESSAGES


def _detect_array_key(path: Path) -> str:
    """The same decision for a file too large to parse, made without parsing it.

    Reading a fixed-size prefix will not do: a guild's role and emoji inventories alone can run
    past any reasonable prefix, pushing the key that settles the question out of view.  This
    walks the top-level keys instead, skipping every value, and stops at the first one that
    decides it -- the third or fourth key, in practice.
    """
    import ijson

    with path.open("rb") as handle:
        events = ijson.basic_parse(handle, buf_size=1 << 16)

        event, _ = next(events, ("", None))
        if event != "start_map":
            return MESSAGES

        for event, value in events:
            if event == "map_key":
                if value in ("channel", MESSAGES):
                    return MESSAGES
                if value == MEMBERS:
                    return MEMBERS
            else:
                _skip_value(events, event)

    return MESSAGES


# ----------------------------------------------------------------------------------------
# Streaming
# ----------------------------------------------------------------------------------------


def _stream_header(path: Path, array_key: str) -> dict:
    """Rebuild the top-level object, skipping the one array that makes the file big."""
    import ijson

    with path.open("rb") as handle:
        events = ijson.basic_parse(handle, buf_size=1 << 20)

        event, _ = next(events)
        if event != "start_map":
            raise ValueError(f"{path}: expected a JSON object at the top level")

        return _consume_map(events, skip=array_key)


def _stream_items(path: Path, array_key: str) -> Iterator[dict]:
    import ijson

    with path.open("rb") as handle:
        yield from ijson.items(handle, f"{array_key}.item", buf_size=1 << 20)


def _consume_map(events, skip: str | None = None) -> dict:
    """Build a dict from an event stream whose ``start_map`` has already been consumed."""
    out: dict[str, Any] = {}
    key: str | None = None

    for event, value in events:
        if event == "map_key":
            key = value
        elif event == "end_map":
            return out
        elif key == skip:
            # The array this document is built around; walked separately on the second pass
            _skip_value(events, event)
            key = None
        else:
            out[key] = _consume_value(events, event, value)
            key = None

    return out


def _consume_value(events, event: str, value: Any) -> Any:
    if event == "start_map":
        return _consume_map(events)
    if event == "start_array":
        return _consume_array(events)
    return value


def _consume_array(events) -> list:
    out: list = []
    for event, value in events:
        if event == "end_array":
            return out
        out.append(_consume_value(events, event, value))
    return out


def _skip_value(events, event: str) -> None:
    """Discard a value without building it, so a huge array costs nothing but the read."""
    if event not in ("start_map", "start_array"):
        return

    depth = 1
    for inner, _ in events:
        if inner in ("start_map", "start_array"):
            depth += 1
        elif inner in ("end_map", "end_array"):
            depth -= 1
            if depth == 0:
                return
