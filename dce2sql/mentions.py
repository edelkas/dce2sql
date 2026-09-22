"""Putting resolved mentions back the way Discord stores them.

DCE writes a message body with its mentions already resolved: ``<@197765375503368192>`` becomes
``@Nickname``, ``<#449367560878686208>`` becomes ``#channel-name``, ``<@&198374136001593344>``
becomes ``@Role Name``.  Readable, and fatal for an archive kept up to date by re-exporting --
rename a channel and every message that ever mentioned it now has different content, although
nobody edited anything.  The importer cannot tell that from a real edit, so it dutifully records
hundreds of revisions in ``message_history`` that never happened.

Exporting with ``--markdown false`` avoids the whole problem and is what an archive should use.
This module is for the exports already made: it turns the resolved text back into the raw form,
so that what lands in the database is stable whatever anybody renames afterwards.

What can be inverted, and from what:

============  ==========================================================================
user          the message's own ``mentions`` array -- the only names it actually mentions
role          the guild's role inventory, or the roles carried on any person in the file
channel       every channel the run has heard of: this run's exports, a ``channels``
              listing, and the database, in that order
============  ==========================================================================

Some mentions cannot be inverted and that is expected, not an error.  DCE writes
``@Unknown``, ``#deleted-channel`` and ``@deleted-role`` when it cannot resolve one, and those
carry no ID to recover.  A mention of somebody absent from the ``mentions`` array is likewise
beyond reach.  The reverse mistake matters more, so the matching below is deliberately timid:
text like ``@everyone`` or a bare ``#hashtag`` is left alone, and a name is only substituted
where it cannot be the prefix of a longer one.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: What DCE writes when it cannot resolve a mention. There is no ID behind these, so they stay.
UNRESOLVABLE = {"user": "Unknown", "channel": "deleted-channel", "role": "deleted-role"}

#: Appended to a channel mention when the channel is a voice one.
VOICE_SUFFIX = " [voice]"

#: Role names that are really the global mentions, and must not be rewritten: Discord's
#: everyone role is literally named "@everyone", so treating it as a role would turn the text
#: "@everyone" into "@@everyone" on the way out.
GLOBAL_ROLES = {"@everyone", "@here", "everyone", "here"}

#: Discord channel types that are voice, and so carry the suffix above.
VOICE_TYPES = {2, 13}
VOICE_KINDS = {"GuildVoiceChat", "GuildStageVoice"}

#: A character that could be part of a longer name of the same kind.  A match followed by one
#: of these is not a whole name, so '@bob' must not be pulled out of '@bobby'.  Channel names
#: use hyphens, which is why they get their own set.
_NAME_TAIL = r"\w"
_CHANNEL_TAIL = r"[\w-]"


@dataclass
class ChannelIndex:
    """Every channel this run has heard of, by name.

    Pooled from several sources because no single one is complete.  The exports being imported
    know their own channel and its parent, and know them *as they were named at the time*,
    which is exactly what a body exported then refers to.  A ``channels`` listing knows every
    channel but only as of whenever it was run.  The database knows everything ever seen.  The
    first source to claim a name keeps it, so a contemporaneous name beats a later one.
    """

    by_name: dict[str, str] = field(default_factory=dict)
    voice: set[str] = field(default_factory=set)
    sources: Counter = field(default_factory=Counter)
    collisions: int = 0

    def add(self, channel_id, name: str | None, source: str, is_voice: bool = False) -> None:
        if not name or channel_id is None:
            return
        name = name.strip()
        if not name or name == UNRESOLVABLE["channel"]:
            return

        channel_id = str(channel_id)
        if is_voice:
            self.voice.add(channel_id)

        existing = self.by_name.get(name)
        if existing is not None:
            if existing != channel_id:
                self.collisions += 1
            return

        self.by_name[name] = channel_id
        self.sources[source] += 1

    def add_export_header(self, header: dict, source: str = "exports") -> None:
        """Take the channel an export covers, and the parent it names."""
        channel = header.get("channel") or {}
        self.add(
            channel.get("id"),
            channel.get("name"),
            source,
            is_voice=_is_voice(channel.get("type")),
        )
        # 'category' is the parent's name whatever the parent actually is -- a category, a
        # forum, or an ordinary channel holding threads
        self.add(channel.get("categoryId"), channel.get("category"), source)

    def add_listing(self, path: str | Path, source: str = "channels file") -> int:
        """Read the output of DCE's ``channels`` command.

        Its lines look like ``197765375503368192 | Chat / nplusplus`` for a channel and
        `` * 941808645174333500 | Thread / number | Active`` for a thread.  The parts are split
        off from the ends rather than the middle, because a thread may be named anything at all
        -- including something with a ``/`` or a ``|`` in it.
        """
        added = 0
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            parsed = _parse_listing_line(line)
            if parsed is None:
                continue
            channel_id, name = parsed
            before = len(self.by_name)
            self.add(channel_id, name, source)
            added += len(self.by_name) - before
        return added

    def add_database(self, rows, source: str = "database") -> None:
        for channel_id, name, kind in rows:
            self.add(channel_id, name, source, is_voice=_is_voice(kind))

    def is_voice(self, channel_id: str) -> bool:
        return channel_id in self.voice

    def __len__(self) -> int:
        return len(self.by_name)


@dataclass
class Unresolver:
    """Rewrites resolved mentions in a message body back to their raw form."""

    channels: ChannelIndex
    roles: dict[str, str] = field(default_factory=dict)

    #: What was put back, by kind
    recovered: Counter = field(default_factory=Counter)

    #: Names a message says it mentions that appear nowhere in its body.  Mostly *not* a
    #: failure: Discord puts the parent author into a reply's mentions although the reply's
    #: text never names them, which accounts for nearly all of these.  Kept because it is the
    #: only handle on what could not be put back, but too noisy to report as a shortfall.
    not_in_body: Counter = field(default_factory=Counter)

    _channel_pattern: re.Pattern | None = field(default=None, repr=False)
    _channel_map: dict[str, str] = field(default_factory=dict, repr=False)
    _role_pattern: re.Pattern | None = field(default=None, repr=False)
    _role_map: dict[str, str] = field(default_factory=dict, repr=False)

    def add_roles(self, roles) -> None:
        """Pool role names. Any source will do -- they all carry the ID alongside the name."""
        for role in roles:
            name, role_id = role.get("name"), role.get("id")
            if not name or role_id is None or name in GLOBAL_ROLES:
                continue
            self.roles.setdefault(name.strip(), str(role_id))
        self._role_pattern = None

    def unresolve(self, content: str, people=()) -> str:
        """Return ``content`` with every mention it can identify put back in its raw form.

        ``people`` is what the message says it mentions.  Restricting user matching to that
        list is what keeps this safe: a message that does not mention anyone called Bob will
        never have the words "@Bob" in it rewritten, however many Bobs the server has.
        """
        if not content:
            return content

        content = self._unresolve_people(content, people)
        content = self._unresolve_roles(content)
        content = self._unresolve_channels(content)
        return content

    # -- the three kinds -----------------------------------------------------------------

    def _unresolve_people(self, content: str, people) -> str:
        names: dict[str, str] = {}
        for person in people:
            rendered, user_id = getattr(person, "rendered", None), getattr(person, "id", None)
            if not rendered or user_id is None or rendered == UNRESOLVABLE["user"]:
                continue
            names.setdefault(rendered, f"<@{user_id}>")

        if not names:
            return content

        # Counted before the substitution, while the resolved text is still there: a name the
        # message says it mentions but that appears nowhere in the body. Usually the mention was
        # edited away, or it lives in a reply or a forward rather than in this body.
        absent = sum(1 for name in names if f"@{name}" not in content)

        pattern = _compile(names, "@", _NAME_TAIL)
        content, hits = _apply(pattern, names, content)

        self.recovered["user"] += hits
        self.not_in_body["user"] += absent
        return content

    def _unresolve_roles(self, content: str) -> str:
        if not self.roles:
            return content
        if self._role_pattern is None:
            self._role_map = {name: f"<@&{i}>" for name, i in self.roles.items()}
            self._role_pattern = _compile(self._role_map, "@", _NAME_TAIL)

        content, hits = _apply(self._role_pattern, self._role_map, content)
        self.recovered["role"] += hits
        return content

    def _unresolve_channels(self, content: str) -> str:
        if not self.channels:
            return content
        if self._channel_pattern is None:
            mapping = {}
            for name, channel_id in self.channels.by_name.items():
                # A voice channel is written with a marker after it; the longer form has to be
                # offered first so that it wins, and the bare form kept for the channel that
                # stopped being a voice channel since
                if self.channels.is_voice(channel_id):
                    mapping[name + VOICE_SUFFIX] = f"<#{channel_id}>"
                mapping[name] = f"<#{channel_id}>"
            self._channel_map = mapping
            self._channel_pattern = _compile(mapping, "#", _CHANNEL_TAIL)

        content, hits = _apply(self._channel_pattern, self._channel_map, content)
        self.recovered["channel"] += hits
        return content

    # -- reporting -----------------------------------------------------------------------

    @property
    def total(self) -> int:
        return sum(self.recovered.values())

    def summary(self) -> str:
        if not self.total:
            return "none recognized"
        parts = ", ".join(
            f"{n:,} {kind}" for kind, n in self.recovered.most_common() if n
        )
        return f"{self.total:,} ({parts})"


# ----------------------------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------------------------


def _compile(mapping: dict[str, str], sigil: str, tail: str) -> re.Pattern:
    """Build one pattern matching any of these names after ``sigil``.

    Longest first, because Python's alternation takes the first branch that matches at a
    position rather than the longest -- without the ordering, a server with both "bob" and
    "bobby" would have every "@bobby" turned into "<@bob-id>by".

    The guards on either side do the rest of the work.  Nothing may precede the sigil that
    would make it part of a word (an e-mail address, say), and nothing may follow the name that
    could have continued it -- so a channel called "general" is not pulled out of a mention of
    "#general-chat" that this run has never heard of.
    """
    names = sorted(mapping, key=len, reverse=True)
    branches = "|".join(re.escape(name) for name in names)
    return re.compile(rf"(?<![\w]){re.escape(sigil)}(?:{branches})(?!{tail})")


def _apply(pattern: re.Pattern, mapping: dict[str, str], content: str) -> tuple[str, int]:
    hits = 0

    def swap(match: re.Match) -> str:
        nonlocal hits
        name = match.group(0)[1:]
        replacement = mapping.get(name)
        if replacement is None:  # pragma: no cover -- the pattern is built from the mapping
            return match.group(0)
        hits += 1
        return replacement

    return pattern.sub(swap, content), hits


def _parse_listing_line(line: str) -> tuple[str, str] | None:
    """One line of a ``channels`` listing, as (id, name), or None if it is not one."""
    line = line.rstrip()
    if not line:
        return None

    is_thread = line.lstrip().startswith("*")
    head, separator, rest = line.lstrip().lstrip("*").strip().partition(" | ")
    if not separator:
        return None

    channel_id = head.strip()
    if not channel_id.isdigit():
        return None

    if is_thread:
        # ' * <id> | Thread / <name> | Active'. Trimmed from the ends, since the name in the
        # middle can contain anything a person can type into a thread title.
        for status in (" | Active", " | Archived"):
            if rest.endswith(status):
                rest = rest[: -len(status)]
                break
        name = rest.partition(" / ")[2] or rest
    else:
        # '<id> | Category / channel-name'. A category name may contain a slash; the channel
        # name may not, so the last segment is the one wanted.
        name = rest.rpartition(" / ")[2] or rest

    return channel_id, name.strip()


def _is_voice(kind) -> bool:
    """Whether a channel type means voice, given either the number or DCE's name for it."""
    if isinstance(kind, bool) or kind is None:
        return False
    if isinstance(kind, int):
        return kind in VOICE_TYPES
    text = str(kind).strip()
    if text.isdigit():
        return int(text) in VOICE_TYPES
    return text in VOICE_KINDS
