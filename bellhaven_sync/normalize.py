"""Normalize names and addresses so website and CRM records can be compared.

Keep this boring and reversible in explanation: lowercase, strip filler words,
collapse USPS abbreviations, keep the house number separate. No geocoding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FILLER_PHRASES = (
    "senior living",
    "assisted living",
    "memory care",
    "skilled nursing",
    "nursing and rehabilitation",
    "nursing & rehabilitation",
    "rehab and nursing",
    "health care center",
    "healthcare",
    "care center",
    "parent account",
    "communities",
    "community",
)

FILLER_WORDS = frozenset({"the", "of", "at", "and", "a", "an"})

STREET_ABBREV = {
    "street": "st",
    "avenue": "ave",
    "boulevard": "blvd",
    "road": "rd",
    "drive": "dr",
    "lane": "ln",
    "court": "ct",
    "place": "pl",
    "terrace": "ter",
    "circle": "cir",
    "highway": "hwy",
    "parkway": "pkwy",
}

DIRECTION_ABBREV = {
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
}

# Designator must be a whole word, and a real separator must precede the unit
# id. Zero-width separators would let "ste" eat the start of "Stevens"/"Steele".
# Optional "#" covers "Suite #200" / "Unit #2" as one unit, not "suite" plus "#200".
UNIT_WORD_RE = re.compile(
    r"\b(?:apartment|suite|unit|apt|ste)\.?\s+#?\s*[a-z0-9-]+\b",
    re.IGNORECASE,
)
# "#2" / " # 2" cannot use \b before "#", so handle the hash form separately.
UNIT_HASH_RE = re.compile(r"#\s*[a-z0-9-]+\b", re.IGNORECASE)
# Same shapes, but captured so field comparison can keep the unit id.
UNIT_WORD_CAPTURE_RE = re.compile(
    r"\b(apartment|suite|unit|apt|ste)\.?\s+#?\s*([a-z0-9-]+)\b",
    re.IGNORECASE,
)
UNIT_HASH_CAPTURE_RE = re.compile(r"#\s*([a-z0-9-]+)\b", re.IGNORECASE)
# Abbreviation families only. "#" is the hash spelling of "unit".
UNIT_DESIGNATOR_CANON = {
    "apartment": "apt",
    "apt": "apt",
    "suite": "ste",
    "ste": "ste",
    "unit": "unit",
}
PAREN_RE = re.compile(r"\([^)]*\)")
NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
MULTI_SPACE_RE = re.compile(r"\s+")
HOUSE_RE = re.compile(r"^(\d+[a-z]?)\s+(.*)$")
PO_BOX_RE = re.compile(r"^p\.?\s*o\.?\s*box\s+(\w+)$", re.IGNORECASE)


@dataclass(frozen=True)
class NormalizedLocation:
    name: str
    street: str
    house_number: str
    city: str
    state: str
    zip5: str

    @property
    def has_address(self) -> bool:
        return bool(self.street and self.state)


def _collapse(text: str) -> str:
    return MULTI_SPACE_RE.sub(" ", text).strip()


def _alnum_collapse(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Shared by names and phrases."""
    return _collapse(NON_ALNUM_RE.sub(" ", text.lower()))


def _filler_phrase_forms(phrase: str) -> set[str]:
    """Forms a filler phrase can take after the same punctuation pipeline as names.

    `nursing & rehabilitation` and `nursing and rehabilitation` must both become
    matchable against text where `&` has already been turned into whitespace.
    """
    base = _alnum_collapse(phrase)
    without_filler_words = _collapse(
        " ".join(token for token in base.split() if token not in FILLER_WORDS)
    )
    return {form for form in (base, without_filler_words) if form}


# Longest first so "nursing rehabilitation" wins over a shorter fragment.
_NORMALIZED_FILLER_PHRASES = tuple(
    sorted(
        {form for phrase in FILLER_PHRASES for form in _filler_phrase_forms(phrase)},
        key=len,
        reverse=True,
    )
)


def normalize_name(value: str | None) -> str:
    if not value:
        return ""
    text = value.lower()
    text = PAREN_RE.sub(" ", text)  # strips "(Parent Account)" and similar
    text = _alnum_collapse(text)
    for phrase in _NORMALIZED_FILLER_PHRASES:
        text = text.replace(phrase, " ")
    text = _collapse(text)
    tokens = [t for t in text.split() if t not in FILLER_WORDS]
    return _collapse(" ".join(tokens))


def normalize_name_for_comparison(value: str | None) -> str:
    """Formatting-only name comparison for proposal field diffs.

    Matching keeps using lossy ``normalize_name``, which strips care-type
    phrases and parenthetical text. This helper keeps those words. Parentheses
    themselves are dropped; the words inside them stay.
    """
    if not value:
        return ""
    text = value.replace("&", " and ")
    return _alnum_collapse(text)


def normalize_city(value: str | None) -> str:
    if not value:
        return ""
    text = NON_ALNUM_RE.sub(" ", value.lower())
    return _collapse(text)


def normalize_state(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().upper()[:2]


def normalize_zip(value: str | None) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    return digits[:5]


def _finish_street(text: str) -> tuple[str, str]:
    """Punctuation, house number, and suffix/direction abbreviations.

    Callers decide what to do with suite/unit text before this step.
    """
    text = NON_ALNUM_RE.sub(" ", text)
    text = _collapse(text)

    po = PO_BOX_RE.match(text)
    if po:
        return "", f"po box {po.group(1)}"

    house = ""
    rest = text
    match = HOUSE_RE.match(text)
    if match:
        house, rest = match.group(1), match.group(2)

    tokens = []
    for token in rest.split():
        if token in DIRECTION_ABBREV:
            tokens.append(DIRECTION_ABBREV[token])
        elif token in STREET_ABBREV:
            tokens.append(STREET_ABBREV[token])
        else:
            tokens.append(token)
    return house, _collapse(" ".join(tokens))


def _canonicalize_unit_designators(text: str) -> str:
    """Fold equivalent unit words and keep the identifier."""

    def repl_word(match: re.Match[str]) -> str:
        kind = match.group(1).lower()
        ident = match.group(2).lower()
        return f" {UNIT_DESIGNATOR_CANON[kind]} {ident} "

    def repl_hash(match: re.Match[str]) -> str:
        return f" unit {match.group(1).lower()} "

    text = UNIT_WORD_CAPTURE_RE.sub(repl_word, text)
    return UNIT_HASH_CAPTURE_RE.sub(repl_hash, text)


def normalize_street(value: str | None) -> tuple[str, str]:
    """Return (house_number, normalized_street_without_house_number).

    Suite and unit identifiers are removed. Matching uses this so formatting
    of a suite does not block a facility/account match.
    """
    if not value:
        return "", ""
    text = value.lower().strip()
    text = UNIT_WORD_RE.sub(" ", text)
    text = UNIT_HASH_RE.sub(" ", text)
    return _finish_street(text)


def normalize_street_for_comparison(value: str | None) -> tuple[str, str]:
    """Like ``normalize_street``, but keep a canonical unit identifier.

    Field diffs use this. ``Suite``/``Ste.``/``Suite #`` and ``Apt.``/``Apartment``
    compare equal, and ``#2`` compares equal to ``Unit 2`` and ``Unit #2``.
    ``Suite 200`` does not compare equal to ``Suite 100``. Matching must keep
    using ``normalize_street``.
    """
    if not value:
        return "", ""
    text = _canonicalize_unit_designators(value.lower().strip())
    return _finish_street(text)


def normalize_location(
    *,
    name: str | None = None,
    street: str | None = None,
    city: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
) -> NormalizedLocation:
    house, street_norm = normalize_street(street)
    return NormalizedLocation(
        name=normalize_name(name),
        street=street_norm,
        house_number=house,
        city=normalize_city(city),
        state=normalize_state(state),
        zip5=normalize_zip(zip_code),
    )
