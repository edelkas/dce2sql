"""Value conversions between DCE's JSON encoding and the database's.

Everything here is deliberately tolerant.  An archive spanning years of DCE releases will
contain fields that were absent, renamed, or encoded differently at the time, and an importer
that raises on the first surprise is useless for the job.  A value that cannot be understood
becomes ``None``; it is the caller's business to count that as a warning.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

# Discord reassigns every message of a deleted account to this user.  See the 'users' table in
# docs/SQL.md: spotting it is the only way to tell that an archived author has been deleted.
DELETED_USER_ID = 456226577798135808

_SHA1_CHUNK = 1 << 20


def snowflake(value) -> int | None:
    """Parse a Discord ID.  They are 8-byte integers written as strings in the exports."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def discriminator(value) -> int | None:
    """Parse the 4-digit discriminator, written as a zero-padded string ('0001', '0000').

    Discord is retiring these, so in practice almost every account now reports 0.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def color(value) -> int | None:
    """Pack a '#RRGGBB' string into ``R << 16 | G << 8 | B``.

    ``None`` means the default colour, which is not the same as black, so it is preserved.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lstrip("#")
    if not text:
        return None
    # DCE writes 6 digits, but tolerate an alpha channel in case that ever changes
    if len(text) == 8:
        text = text[:6]
    if len(text) != 6:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def timestamp(value) -> int | None:
    """Parse an ISO 8601 instant into a Unix timestamp in UTC.

    DCE always writes an offset -- ``--locale`` only affects the HTML, CSV and plaintext
    writers, while the JSON path hands a ``DateTimeOffset`` straight to ``Utf8JsonWriter``.
    Without ``--utc`` that offset is the exporting machine's local one, so two exports of the
    same message can carry different offsets for the same instant.  Normalizing to UTC here is
    what makes them comparable, and is why re-importing a file exported from another timezone
    does not look like an edit.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    if not text:
        return None

    # 'Z' is valid ISO 8601 but only understood by fromisoformat from 3.11 onwards
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # .NET writes up to 7 fractional digits; Python accepts at most 6
        parsed = _parse_overlong_fraction(text)
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        # No offset at all: the only sane reading is that it was already UTC
        parsed = parsed.replace(tzinfo=timezone.utc)

    return int(parsed.timestamp())


def _parse_overlong_fraction(text: str) -> datetime | None:
    """Retry a timestamp whose fractional second has more digits than Python accepts."""
    dot = text.find(".")
    if dot < 0:
        return None

    end = dot + 1
    while end < len(text) and text[end].isdigit():
        end += 1

    # Sub-second precision is irrelevant once the value becomes a whole-second Unix timestamp,
    # so the fraction is simply truncated rather than rounded
    trimmed = text[: dot + 7] + text[end:] if end - dot > 7 else text
    try:
        return datetime.fromisoformat(trimmed)
    except ValueError:
        return None


def boolean(value, default: bool = False) -> bool:
    if value is None:
        return default
    return bool(value)


def file_sha1(path) -> str:
    """Hash a file without holding it in memory, since exports can be very large."""
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while chunk := handle.read(_SHA1_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


#: Query parameters Discord signs a CDN link with.  They are regenerated every time an export
#: runs and expire within a day, so two exports of the same attachment never agree on them.
#: ``tools/compare_exports.py`` in the exporter's own repository strips the same ones.
SIGNATURE_KEYS = ("ex", "is", "hm")

_CDN_HOSTS = ("discordapp.com", "discordapp.net", "discord.com")


def same_url(left, right) -> bool:
    """Whether two CDN links point at the same thing, ignoring the signature on them.

    Only Discord's own hosts are treated this way, and only those three parameters: an
    arbitrary URL elsewhere in an export may well use ``size`` or ``format`` to mean something,
    and two links that differ in those are genuinely different links.
    """
    if left == right:
        return True
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if not any(host in left for host in _CDN_HOSTS):
        return False
    return unsigned_url(left) == unsigned_url(right)


def unsigned_url(url: str) -> str:
    base, _, query = url.partition("?")
    if not query:
        return url
    kept = [
        part
        for part in query.split("&")
        if part.partition("=")[0] not in SIGNATURE_KEYS
    ]
    return base + ("?" + "&".join(kept) if kept else "")


def chunked(items, size: int):
    """Yield successive chunks, used to keep IN-clauses under each engine's parameter limit."""
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
