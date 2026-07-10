# ruff: noqa: INP001
"""Public configuration safety contracts."""

from __future__ import annotations

import pytest
from pyfreeswitch.config import ESLConfig
from pyfreeswitch.config import normalize_sip_profiles

_AUTH_VALUE = "test-" + "value"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 10.0),
        ("invalid", 10.0),
        ("nan", 10.0),
        ("inf", 10.0),
        (-5, 1.0),
        (0, 1.0),
        (5, 5.0),
        (999, 30.0),
    ],
)
def test_esl_timeout_is_finite_and_bounded(value, expected):
    config = ESLConfig(host="core.example", password=_AUTH_VALUE, timeout=value)

    assert config.timeout == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ("wg", "vpc")),
        ("wg", ()),
        (["wg", "wg", "vpc"], ("wg", "vpc")),
        (["wg; status", "safe_name"], ("safe_name",)),
        (["a", "b", "c", "d", "e"], ("a", "b", "c", "d")),
    ],
)
def test_sip_profiles_are_command_safe_unique_and_bounded(value, expected):
    assert normalize_sip_profiles(value) == expected
