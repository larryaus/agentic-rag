"""Helpers for tracking citations referenced in assistant text."""

import re

CITATION_RE = re.compile(r"\[doc:(\d+)#chunk:(\d+)\]")


def find_citation_tokens(text: str) -> list[tuple[int, int]]:
    return [(int(m.group(1)), int(m.group(2))) for m in CITATION_RE.finditer(text)]
