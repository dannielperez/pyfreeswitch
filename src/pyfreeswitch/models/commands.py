"""Typed reply for FreeSWITCH ``api`` commands that answer ``+OK`` / ``-ERR``.

Every mutating command in ``mod_commands`` and ``mod_callcenter`` replies with a
single status line: ``+OK`` (optionally followed by a message, e.g. ``+OK
Success``), ``-ERR <reason>`` or ``-USAGE: <syntax>``. Consumers must never
string-match those prefixes themselves; they get a :class:`CommandReply`.
"""

from __future__ import annotations

from dataclasses import dataclass

_OK_PREFIX = "+ok"
_ERROR_PREFIXES = ("-err", "-usage")
_MAX_DETAIL = 200


@dataclass(frozen=True, slots=True)
class CommandReply:
    """Outcome of one ``api`` command.

    ``ok`` is ``True`` only for an explicit ``+OK`` line. An empty reply is
    *not* success: a dropped read or a truncated frame looks the same, so the
    caller must not assume the switch acted.
    """

    ok: bool
    #: First reply line, capped, never the command that was sent.
    detail: str = ""

    @property
    def not_found(self) -> bool:
        """The switch could not locate the channel/agent/queue named."""
        lowered = self.detail.lower()
        return (not self.ok) and (
            "no such channel" in lowered
            or "not found" in lowered
            or "cannot locate" in lowered
        )


def parse_command_reply(text: str) -> CommandReply:
    """Classify a ``+OK`` / ``-ERR`` / ``-USAGE`` reply line."""
    stripped = (text or "").strip()
    if not stripped:
        return CommandReply(ok=False, detail="empty reply")
    first = stripped.splitlines()[0].strip()[:_MAX_DETAIL]
    lowered = first.lower()
    if lowered.startswith(_OK_PREFIX):
        return CommandReply(ok=True, detail=first)
    if lowered.startswith(_ERROR_PREFIXES):
        return CommandReply(ok=False, detail=first)
    return CommandReply(ok=False, detail=f"unrecognized reply: {first}")


__all__ = ["CommandReply", "parse_command_reply"]
