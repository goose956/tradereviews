"""Robust parser to extract a phone number and customer name from free-text."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedCommand:
    phone: str   # normalised E.164 (e.g. "+447804563456")
    name: str    # customer name as provided


# Matches UK mobile numbers in various formats, or full E.164 international numbers.
# Group 1: optional leading "+"
# Group 2: digits (with optional spaces / dashes / dots)
_PHONE_RE = re.compile(
    r"""
    (\+)?                           # optional leading +
    (
        \d[\d\s\-\.]{7,15}\d        # digits with optional separators (9–17 digits total)
    )
    """,
    re.VERBOSE,
)


def _normalise_uk_number(raw: str) -> str:
    """Convert a UK-style '07…' number to E.164 '+44…'."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0"):
        digits = "44" + digits[1:]
    return f"+{digits}"


def parse_review_command(text: str) -> ParsedCommand | None:
    """Parse a message like '07804563456 John' or '+44 7804 563 456 John Smith'.

    Returns a ``ParsedCommand`` with the normalised E.164 phone and full name,
    or ``None`` if the message could not be parsed.
    """
    text = text.strip()
    if not text:
        return None

    match = _PHONE_RE.search(text)
    if not match:
        return None

    plus_sign = match.group(1) or ""
    raw_number = plus_sign + match.group(2)

    # Everything after the phone number is treated as the customer name.
    remainder = text[match.end() :].strip()

    # If the name appeared *before* the number, try that instead.
    if not remainder:
        remainder = text[: match.start()].strip()

    if not remainder:
        return None

    # Light sanitisation: collapse whitespace, strip non-alpha leading chars.
    name = re.sub(r"\s+", " ", remainder)
    name = re.sub(r"^[^a-zA-Z]+", "", name)
    if not name:
        return None

    # Normalise the phone number.
    if raw_number.startswith("+"):
        phone = "+" + re.sub(r"\D", "", raw_number)
    else:
        phone = _normalise_uk_number(raw_number)

    return ParsedCommand(phone=phone, name=name)
