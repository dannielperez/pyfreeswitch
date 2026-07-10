"""Typed FreeSWITCH ``sofia ... reg`` response model and parser."""

from __future__ import annotations

import re
from dataclasses import dataclass

_USER_RE = re.compile(r"^\s*User:\s*(?P<user>\S+)", re.MULTILINE)
_CONTACT_RE = re.compile(r"^\s*Contact:\s*(?P<contact>.+?)\s*$", re.MULTILINE)
_STATUS_RE = re.compile(r"^\s*Status:\s*(?P<status>.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SIPRegistration:
    """One normalized SIP registration returned by a FreeSWITCH profile."""

    extension: str
    contact: str = ""
    status_text: str = ""


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


__all__ = ["SIPRegistration", "parse_sofia_reg"]
