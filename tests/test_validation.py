"""Mechanical checks only, and no false positives on ordinary punctuation."""

from __future__ import annotations

import pytest

from multiagency.config import Constraints
from multiagency.validation import validate

STRICT = Constraints(max_length=100, allow_emojis=False, forbid_em_dash=True)
LOOSE = Constraints(max_length=100, allow_emojis=True, forbid_em_dash=False)


def test_a_clean_post_passes():
    assert validate("We fixed the retry that swallowed a 401.", STRICT).ok


@pytest.mark.parametrize("text", ["a — b", "a – b", "a ― b"])
def test_dashes_are_caught(text):
    result = validate(text, STRICT)
    assert not result.ok
    assert "dash" in result.violations[0].code


def test_emoji_is_caught():
    result = validate("shipped \U0001f680", STRICT)
    assert not result.ok
    assert result.violations[0].code == "emoji"


def test_emoji_is_allowed_when_the_lane_says_so():
    assert validate("shipped \U0001f680", LOOSE).ok


def test_length_is_measured_after_stripping():
    assert validate("  " + "x" * 100 + "  ", STRICT).ok
    result = validate("x" * 101, STRICT)
    assert result.violations[0].code == "length"
    assert "1 over" in result.violations[0].message


def test_empty_is_caught():
    assert validate("   \n ", STRICT).violations[0].code == "empty"


@pytest.mark.parametrize(
    "text",
    [
        "Day-to-day work is 50/50, and that is fine.",
        'She said "no" and meant it; we moved on.',
        "Costs fell 40% (about 3 hours a week).",
        "See the notes: item 1, item 2, item 3...",
        "A well-worn path -- still a path.",
    ],
)
def test_ordinary_punctuation_is_not_a_violation(text):
    assert validate(text, STRICT).ok, text


def test_several_violations_are_all_reported():
    result = validate("a — b \U0001f680 " + "x" * 200, STRICT)
    codes = {v.code for v in result.violations}
    assert codes == {"dash", "emoji", "length"}


def test_judgment_rules_are_not_enforced_here():
    """Funding claims and token names are Hermes's job, not a blocklist."""
    text = "We raised a grant and the token is going to 10x."
    assert validate(text, STRICT).ok
