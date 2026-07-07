"""Tests for the LIKE-wildcard escape helper.

The helper sits between agent-supplied filter values (`city`,
`neighborhood`, `place_name` suffixes) and SQLAlchemy's `ilike(...)`.
SQL is parameter-bound regardless, so this is a DoS-shape fix — `%`
or `_` characters in user input must not blow up Postgres' LIKE
backtracking on JSONB `astext` columns.
"""

from __future__ import annotations

from kebi.core.places._place_utils import escape_like


def test_escapes_percent() -> None:
    assert escape_like("%a%b%") == "\\%a\\%b\\%"


def test_escapes_underscore() -> None:
    assert escape_like("a_b") == "a\\_b"


def test_escapes_backslash_first() -> None:
    """Backslashes are escaped before `%` / `_` so the escape char itself
    is consistent. (`\\%` means `%` literally; `\\\\%` means `\\` then `%`.)"""
    assert escape_like("a\\b") == "a\\\\b"


def test_plain_string_passes_through(self_unused: object = None) -> None:
    assert escape_like("Tokyo") == "Tokyo"


def test_unicode_passes_through() -> None:
    assert escape_like("กรุงเทพ") == "กรุงเทพ"


def test_combined_wildcards_and_letters() -> None:
    """The classic DoS payload — should be neutered to literal characters."""
    payload = "%_%_%_%_%_%_%_%_%"
    escaped = escape_like(payload)
    assert escaped.count("\\%") == 9
    assert escaped.count("\\_") == 8
