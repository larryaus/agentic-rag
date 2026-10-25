"""Recursive chunking without a tokenizer download, so ingestion works offline.

Every size here is in one estimated-token unit: an ASCII character counts a quarter of
a token and any other character (CJK, for example) a whole one.
"""

import math

SEPARATORS = ["\n## ", "\n### ", "\n\n", "\n", "。", "！", "？", " "]


def _cost(char: str) -> float:
    return 0.25 if ord(char) < 128 else 1.0


def estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return math.ceil(ascii_chars / 4) + (len(text) - ascii_chars)


def _hard_split(text: str, max_tokens: int) -> list[str]:
    pieces, start, used = [], 0, 0.0
    for index, char in enumerate(text):
        if used + _cost(char) > max_tokens and index > start:
            pieces.append(text[start:index])
            start, used = index, 0.0
        used += _cost(char)
    pieces.append(text[start:])
    return pieces


def _split(text: str, separators: list[str], max_tokens: int) -> list[str]:
    if estimate_tokens(text) <= max_tokens:
        return [text]
    if not separators:
        return _hard_split(text, max_tokens)
    separator, rest = separators[0], separators[1:]
    parts = text.split(separator)
    parts = [parts[0], *(separator + part for part in parts[1:])]
    out: list[str] = []
    for part in parts:
        if part:
            out.extend(_split(part, rest, max_tokens) if estimate_tokens(part) > max_tokens else [part])
    return out


def _tail(text: str, budget: int) -> str:
    used, start = 0.0, len(text)
    while start > 0 and used + _cost(text[start - 1]) <= budget:
        start -= 1
        used += _cost(text[start])
    return text[start:]


def chunk_text(text: str, max_tokens: int = 512, overlap: int = 64) -> list[str]:
    text = text.strip()
    if not text:
        return []
    # Leave room for the overlap so no chunk exceeds max_tokens once it is prepended.
    target = max_tokens - overlap
    chunks: list[str] = []
    current = ""
    for piece in _split(text, SEPARATORS, target):
        if current and estimate_tokens(current + piece) > target:
            chunks.append(current)
            current = ""
        current += piece
    if current:
        chunks.append(current)
    return [chunks[0]] + [
        _tail(previous, overlap) + chunk for previous, chunk in zip(chunks, chunks[1:])
    ]
