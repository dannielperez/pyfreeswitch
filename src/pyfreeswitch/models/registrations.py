"""Typed FreeSWITCH ``sofia ... reg`` response model and parser.

``parse_sofia_reg`` returns a bare list, which cannot express *why* the list is
empty. Three very different responses all collapse to ``[]``:

* a profile that genuinely has no registrations (authoritative empty);
* an ``-ERR`` reply (bad profile name, profile not running, ESL refusal);
* output that arrived truncated or in an unrecognized shape.

A consumer that treats all three as "nobody is registered" will happily mark a
whole profile's endpoints unregistered on a transport hiccup.
``parse_sofia_reg_result`` keeps the same parsing but reports the state
alongside the rows, so the caller can decide what is authoritative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from enum import Enum

_USER_RE = re.compile(r"^\s*User:\s*(?P<user>\S+)", re.MULTILINE)
_CONTACT_RE = re.compile(r"^\s*Contact:\s*(?P<contact>.+?)\s*$", re.MULTILINE)
_STATUS_RE = re.compile(r"^\s*Status:\s*(?P<status>.+?)\s*$", re.MULTILINE)

#: FreeSWITCH error replies begin with ``-ERR``; some builds emit ``-USAGE``
#: for a malformed command. Both mean "this is not registration data".
_ERROR_PREFIXES = ("-err", "-usage")

#: A well-formed reply with zero registrations still says so explicitly. Both
#: spellings appear across FreeSWITCH versions.
_EXPLICIT_EMPTY_MARKERS = ("total 0", "0 total")

#: Markers proving the reply really is sofia registration output, even when it
#: lists nothing. Used to tell an authoritative empty from truncated noise.
_REG_SHAPE_MARKERS = ("registrations:", "call-id:", "total")


class SofiaRegState(str, Enum):
    """Why a ``sofia ... reg`` reply looks the way it does."""

    #: Parsed at least one registration.
    COMPLETE = "complete"
    #: Well-formed reply that authoritatively lists no registrations.
    EMPTY = "empty"
    #: FreeSWITCH refused the command (``-ERR``/``-USAGE``).
    ERROR = "error"
    #: Non-empty reply we could not recognize as registration output.
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class SIPRegistration:
    """One normalized SIP registration returned by a FreeSWITCH profile."""

    extension: str
    contact: str = ""
    status_text: str = ""


@dataclass(frozen=True, slots=True)
class SofiaRegResult:
    """Parsed registrations plus whether the reply can be trusted as complete."""

    state: SofiaRegState
    registrations: list[SIPRegistration] = field(default_factory=list)
    #: Short, non-sensitive reason for ERROR/MALFORMED; empty otherwise.
    detail: str = ""

    @property
    def is_authoritative(self) -> bool:
        """True when the reply proves the profile's registration set.

        Only ``COMPLETE`` and ``EMPTY`` qualify. A caller must not treat an
        ``ERROR``/``MALFORMED`` reply as evidence that endpoints went away.
        """
        return self.state in (SofiaRegState.COMPLETE, SofiaRegState.EMPTY)


def parse_sofia_reg(text: str) -> list[SIPRegistration]:
    """Parse ``sofia status profile <name> reg`` output into typed rows."""
    if not text:
        return []

    registrations: list[SIPRegistration] = []
    for block in re.split(r"(?m)^\s*Call-ID:", text):
        user_match = _USER_RE.search(block)
        if not user_match:
            continue
        extension = user_match.group("user").split("@", 1)[0].strip()
        if not extension:
            continue
        contact_match = _CONTACT_RE.search(block)
        status_match = _STATUS_RE.search(block)
        registrations.append(
            SIPRegistration(
                extension=extension,
                contact=(
                    contact_match.group("contact") if contact_match else ""
                ).strip(),
                status_text=(
                    status_match.group("status") if status_match else ""
                ).strip(),
            ),
        )
    return registrations


def parse_sofia_reg_result(text: str) -> SofiaRegResult:
    """Parse ``sofia status profile <name> reg`` and report why it looks this way.

    Same parsing as :func:`parse_sofia_reg`; the difference is that an empty
    result carries its reason. Classification order matters: an ``-ERR`` reply
    is an error even if it happens to contain a ``User:`` line, so the error
    check runs first.
    """
    stripped = (text or "").strip()
    if not stripped:
        # No bytes at all is not proof of an empty profile — a dropped or
        # timed-out read looks identical. Treat it as malformed, not empty.
        return SofiaRegResult(
            state=SofiaRegState.MALFORMED,
            detail="empty response body",
        )

    lowered = stripped.lower()
    if lowered.startswith(_ERROR_PREFIXES):
        return SofiaRegResult(
            state=SofiaRegState.ERROR,
            # First line only: later lines can echo the command, and the command
            # may embed a profile/gateway name we would rather not log.
            detail=stripped.splitlines()[0][:200],
        )

    registrations = parse_sofia_reg(text)
    if registrations:
        return SofiaRegResult(
            state=SofiaRegState.COMPLETE,
            registrations=registrations,
        )

    if any(marker in lowered for marker in _EXPLICIT_EMPTY_MARKERS) or any(
        marker in lowered for marker in _REG_SHAPE_MARKERS
    ):
        return SofiaRegResult(state=SofiaRegState.EMPTY)

    return SofiaRegResult(
        state=SofiaRegState.MALFORMED,
        detail="unrecognized sofia reg output",
    )


__all__ = [
    "SIPRegistration",
    "SofiaRegResult",
    "SofiaRegState",
    "parse_sofia_reg",
    "parse_sofia_reg_result",
]
