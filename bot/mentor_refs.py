"""Pure parsing of bulk mentor lists; never resolve a username through Telegram."""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_MENTOR_REFS = 1000
MAX_IMPORT_BYTES = 128 * 1024
MAX_TELEGRAM_ID = (1 << 63) - 1
_HANDLE = re.compile(r"[a-zA-Z0-9_]{1,64}\Z")
_LINK = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]+)/?\Z", re.I)


@dataclass(frozen=True)
class MentorRef:
    raw: str
    value: int | str


@dataclass(frozen=True)
class ParsedMentorRefs:
    refs: list[MentorRef]
    invalid: list[str]
    duplicates: int
    total: int


class TooManyMentorRefs(ValueError):
    pass


def parse_mentor_refs(text: str) -> ParsedMentorRefs:
    """Accept IDs, @handles and t.me links; whitespace/comma/semicolon separators.

    A malformed token is reported as a whole rather than extracting a valid-looking
    substring and potentially granting access to the wrong person.
    """
    tokens = [s for s in re.split(r"[\s,;]+", text.lstrip("\ufeff").strip()) if s]
    if len(tokens) > MAX_MENTOR_REFS:
        raise TooManyMentorRefs(f"At most {MAX_MENTOR_REFS} entries are accepted")
    refs, invalid = [], []
    seen: set[int | str] = set()
    duplicates = 0
    for raw in tokens:
        token = raw
        match = _LINK.fullmatch(token)
        if match:
            token = "@" + match.group(1)
        if token.isascii() and token.isdigit():
            if len(token) > 19 or not 0 < int(token) <= MAX_TELEGRAM_ID:
                invalid.append(raw)
                continue
            value: int | str = int(token)
        else:
            handle = token.removeprefix("@")
            if not _HANDLE.fullmatch(handle):
                invalid.append(raw)
                continue
            value = handle.lower()
        if value in seen:
            duplicates += 1
        else:
            seen.add(value)
            refs.append(MentorRef(raw, value))
    return ParsedMentorRefs(refs, invalid, duplicates, len(tokens))
