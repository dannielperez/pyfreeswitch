# ruff: noqa: INP001
"""Public configuration safety contracts."""

from __future__ import annotations

import pytest
from pyfreeswitch.config import DEFAULT_ESL_MAX_BODY_BYTES
from pyfreeswitch.config import DEFAULT_ESL_MAX_FRAME_BYTES
from pyfreeswitch.config import DEFAULT_ESL_MAX_HEADER_BYTES
from pyfreeswitch.config import MAX_ESL_MAX_BODY_BYTES
from pyfreeswitch.config import MAX_ESL_MAX_FRAME_BYTES
from pyfreeswitch.config import MAX_ESL_MAX_HEADER_BYTES
from pyfreeswitch.config import MIN_ESL_MAX_BODY_BYTES
from pyfreeswitch.config import MIN_ESL_MAX_FRAME_BYTES
from pyfreeswitch.config import MIN_ESL_MAX_HEADER_BYTES
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


@pytest.mark.parametrize(
    "cap",
    [
        (
            "max_header_bytes",
            DEFAULT_ESL_MAX_HEADER_BYTES,
            MIN_ESL_MAX_HEADER_BYTES,
            MAX_ESL_MAX_HEADER_BYTES,
        ),
        (
            "max_body_bytes",
            DEFAULT_ESL_MAX_BODY_BYTES,
            MIN_ESL_MAX_BODY_BYTES,
            MAX_ESL_MAX_BODY_BYTES,
        ),
        (
            "max_frame_bytes",
            DEFAULT_ESL_MAX_FRAME_BYTES,
            MIN_ESL_MAX_FRAME_BYTES,
            MAX_ESL_MAX_FRAME_BYTES,
        ),
    ],
)
@pytest.mark.parametrize(
    ("value_kind", "value"),
    [
        ("below", -1),
        ("above", float("1e100")),
        ("invalid", "not-a-number"),
        ("nonfinite", float("nan")),
        ("nonfinite", float("inf")),
    ],
)
def test_esl_size_caps_are_finite_and_bounded(
    cap,
    value_kind,
    value,
):
    field, default, minimum, maximum = cap
    config = ESLConfig(
        host="core.example",
        password=_AUTH_VALUE,
        **{field: value},
    )

    expected = {
        "below": minimum,
        "above": maximum,
        "invalid": default,
        "nonfinite": default,
    }[value_kind]
    assert getattr(config, field) == expected
