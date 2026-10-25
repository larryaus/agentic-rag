"""Streaming masking of sensitive data in answers, for deployments without Bedrock.

Every detector matches at most MAX_LENGTH characters and decides from that span plus at
most two characters on either side. Matches are found at every start position, so whether
one exists never depends on text further back. The stream therefore always holds back the
last HOLD characters and never releases a cut through a detected span: anything released
can no longer become part of a match, so no fragment of a secret leaves before it is
masked, and the streamed text equals masking the whole answer at once.
"""

from collections.abc import Callable
from dataclasses import dataclass

import regex


def luhn(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char) * (2 if position % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def iban_checksum(value: str) -> bool:
    compact = value.replace(" ", "").upper()
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


@dataclass(frozen=True)
class Detector:
    name: str
    pattern: "regex.Pattern[str]"
    # Longest possible match, label included. Patterns must not match more than this.
    max_length: int
    valid: Callable[[str], bool] = lambda value: True

    @property
    def placeholder(self) -> str:
        return "{" + self.name + "}"

    def value_span(self, match: "regex.Match[str]") -> tuple[int, int]:
        # Labelled detectors mask only the value and leave the label readable.
        if "value" in self.pattern.groupindex:
            return match.span("value")
        return match.span()


# Digit runs are taken whole (maximal), so a long number is never cut into a fake card.
DETECTORS = [
    Detector(
        "CREDIT_DEBIT_CARD_NUMBER",
        regex.compile(r"(?<![\d])(?<!\d[ -])\d(?:[ -]?\d){12,18}(?![\d])(?![ -]\d)"),
        37,
        lambda value: luhn(_digits(value)),
    ),
    Detector(
        "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
        regex.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}(?![A-Za-z0-9])"),
        64,
        iban_checksum,
    ),
    Detector(
        "AWS_ACCESS_KEY",
        regex.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"),
        20,
    ),
    Detector(
        "AWS_SECRET_KEY",
        regex.compile(
            r"(?i:secret)[^\n]{0,40}?(?<![A-Za-z0-9/+])(?P<value>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])"
        ),
        86,
    ),
    Detector(
        "PASSWORD",
        regex.compile(
            r"(?i:(?<![A-Za-z])(?:password|passwd|pwd)(?![A-Za-z]))[ \t]{0,3}"
            r"(?:[:=]|(?i:is)(?![A-Za-z]))[ \t]{0,3}(?P<value>[^\s]{1,64})"
        ),
        80,
    ),
    Detector(
        "US_SOCIAL_SECURITY_NUMBER", regex.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])"), 11
    ),
    Detector(
        "UK_NATIONAL_INSURANCE_NUMBER",
        regex.compile(
            r"(?<![A-Za-z0-9])[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D](?![A-Za-z0-9])"
        ),
        13,
    ),
    Detector(
        "CA_SOCIAL_INSURANCE_NUMBER",
        regex.compile(r"(?<![\d-])(?<!\d )\d{3}[ -]?\d{3}[ -]?\d{3}(?![\d-])(?! \d)"),
        11,
        lambda value: luhn(_digits(value)),
    ),
]
MAX_LENGTH = max(detector.max_length for detector in DETECTORS)
# One extra character covers the lookahead that decides where a match ends.
HOLD = MAX_LENGTH + 1
# Released text kept for lookbehinds; longer than any single match, so a match that starts
# in it also ends in it and was already masked when released. (A chain of overlapping
# matches can be longer; holding one back long enough ends in the forced overflow cut.)
CONTEXT = 2 * HOLD
# Only a pathological chain of overlapping matches can hold this much back.
OVERFLOW = 4096
TRUNCATED = " …"

Span = tuple[int, int, str]


def complete_spans(text: str) -> list[Span]:
    """Validated matches as (start, end, placeholder), overlaps merged under the longest."""
    found: list[Span] = []
    for detector in DETECTORS:
        # Overlapped: a non-overlapping scan resumes where its last match ended, so where it
        # finds a match would depend on text arbitrarily far back.
        for match in detector.pattern.finditer(text, overlapped=True):
            start, end = detector.value_span(match)
            if detector.valid(text[start:end]):
                found.append((start, end, detector.placeholder))
    return merge(found)


def merge(found: list[Span]) -> list[Span]:
    found = sorted(found, key=lambda span: (span[0], -(span[1] - span[0])))
    merged: list[Span] = []
    for start, end, name in found:
        if merged and start < merged[-1][1]:
            previous = merged[-1]
            longest = name if end - start > previous[1] - previous[0] else previous[2]
            merged[-1] = (previous[0], max(previous[1], end), longest)
        else:
            merged.append((start, end, name))
    return merged


def render(text: str, start: int, end: int, spans: list[Span]) -> str:
    out, cursor = [], start
    for span_start, span_end, name in spans:
        if span_end <= start or span_start >= end:
            continue
        out.append(text[cursor : max(cursor, span_start)])
        out.append(name)
        cursor = min(span_end, end)
    out.append(text[cursor:end])
    return "".join(out)


def mask(text: str) -> str:
    """Masks a whole text; every split of a successful stream releases exactly this."""
    return render(text, 0, len(text), complete_spans(text))


class MaskingFilter:
    def __init__(self) -> None:
        self.context = ""
        self.pending = ""

    def feed(self, text: str) -> str:
        self.pending += text
        released = self._release(final=False)
        if len(self.pending) > OVERFLOW:
            released += self._release(final=False, force=True)
        return released

    def finish(self, success: bool = True) -> str:
        if success:
            return self._release(final=True)
        # A failed stream may have stopped mid-secret, and the held tail cannot be told
        # apart from the start of one, so it is dropped rather than released.
        released = self._release(final=False)
        dropped = self.pending.strip()
        self.pending = ""
        return released + (TRUNCATED if dropped else "")

    def _release(self, *, final: bool, force: bool = False) -> str:
        text = self.context + self.pending
        offset = len(self.context)
        # The context keeps released text raw, so a span cut by a forced release is found
        # again here and the rest of it is masked too.
        spans = complete_spans(text)
        point = len(text) if final else max(offset, len(text) - HOLD)
        if not force:
            # Never cut a span; each step moves the point strictly left, so this ends.
            moved = True
            while moved:
                moved = False
                for start, end, _ in spans:
                    if offset <= start < point < end:
                        point = start
                        moved = True
        released = render(text, offset, point, spans)
        self.context = text[:point][-CONTEXT:]
        self.pending = text[point:]
        return released
