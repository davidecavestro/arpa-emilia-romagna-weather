"""Unit tests for the ARPAE bulletin date parser."""

import pytest
from datetime import date

from custom_components.arpa_emilia_romagna_weather.coordinator import (
    _parse_italian_date,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "oggi, giovedì  5 novembre 2026",
            date(2026, 11, 5),
        ),
    ],
)
def test_parse_italian_date(text, expected):
    """Parse the long Italian date used in the bulletin header."""
    assert _parse_italian_date(text) == expected


def test_parse_italian_date_iso():
    """Accept the ISO date the bulletin also publishes."""
    # The bulletin dates itself as ``YYYY-MM-DD``; a reliable parser must accept it.
    assert _parse_italian_date("2026-11-05") == date(2026, 11, 5)


def test_parse_italian_date_none():
    """Return None for empty and unparseable input."""
    assert _parse_italian_date("") is None
    assert _parse_italian_date("not a date at all") is None


def test_parse_italian_date_invalid_month():
    """Return None when the month name is not an Italian month."""
    assert _parse_italian_date("giovedì 5 barbaro 2026") is None
