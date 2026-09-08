"""Mechanical constraint checks on what Hermes returns.

Only the checkable rules live here: dashes, emojis, length. The judgment calls
in the constraints block (no funding claims, no token names, no manufactured
urgency) are Hermes's job through the prompt, and deliberately are not
reimplemented as a keyword blocklist, which would fail on both sides.

Nothing in this module rewrites a post. A violation triggers exactly one
regeneration upstream; a second violation flags the post for a human. Text is
never silently dropped or edited.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .config import Constraints

EM_DASH = "—"
HORIZONTAL_BAR = "―"
EN_DASH = "–"
# The en dash is included: it is the same stylistic tic and just as easy to
# avoid, and letting it through would make the rule pointless in practice.
DASHES = {
    EM_DASH: "em dash",
    HORIZONTAL_BAR: "horizontal bar",
    EN_DASH: "en dash",
}

EMOJI_RE = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"  # regional indicators, the flag pairs
    "\U0001f300-\U0001faff"  # pictographs, faces, symbols, extended sets
    "☀-➿"          # misc symbols and dingbats
    "⬀-⯿"          # arrows and shapes with emoji presentation
    "️"                 # variation selector 16, forces emoji rendering
    "‍"                 # zero width joiner, used to build compound emoji
    "]"
)


@dataclass(frozen=True)
class Violation:
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    violations: tuple[Violation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def summary(self) -> str:
        return "; ".join(v.message for v in self.violations)


def validate(text: str, constraints: Constraints) -> ValidationResult:
    """Check one post against the mechanical constraints for its lane."""
    found: list[Violation] = []
    stripped = text.strip()

    if not stripped:
        return ValidationResult((Violation("empty", "the post is empty"),))

    if constraints.forbid_em_dash:
        hits = sorted({name for char, name in DASHES.items() if char in stripped})
        if hits:
            found.append(
                Violation(
                    "dash",
                    "contains {}".format(
                        " and ".join(_article(name) for name in hits)
                    ),
                )
            )

    if not constraints.allow_emojis:
        emojis = _emoji_hits(stripped)
        if emojis:
            found.append(
                Violation(
                    "emoji",
                    "contains {} emoji ({}), which this lane does not allow".format(
                        len(emojis), " ".join(emojis[:5])
                    ),
                )
            )

    length = len(stripped)
    if length > constraints.max_length:
        found.append(
            Violation(
                "length",
                "is {} characters, {} over the {} limit for this lane".format(
                    length, length - constraints.max_length, constraints.max_length
                ),
            )
        )

    return ValidationResult(tuple(found))


def _article(noun: str) -> str:
    return "{} {}".format("an" if noun[0] in "aeiou" else "a", noun)


def _emoji_hits(text: str) -> list[str]:
    """Emoji characters in order of appearance, deduplicated.

    Characters that are plainly punctuation or maths in the swept ranges are
    let through so ordinary text is not mistaken for decoration.
    """
    out: list[str] = []
    for char in text:
        if not EMOJI_RE.match(char):
            continue
        if unicodedata.category(char) in {"Po", "Sm", "Cf"} and char not in "️‍":
            continue
        if char not in out:
            out.append(char)
    return out


def retry_note(result: ValidationResult) -> str:
    """The one line handed back to Hermes on the single allowed retry."""
    return (
        "The previous attempt broke a hard constraint: {}. "
        "Write a fresh post from the same material that does not.".format(result.summary)
    )
