"""Recursive token-aware text chunker.

Hierarchical separator preference: heading > paragraph > line > Chinese
punctuation > space > hard token slice. Chunks merge greedily up to
(max_tokens - overlap), then we prepend `overlap` tokens of the previous
chunk to provide context.
"""

from __future__ import annotations

SEPARATORS = ["\n## ", "\n### ", "\n\n", "\n", "。", "！", "？", " "]

_ENC = None
_ENC_TRIED = False


def _enc():
    """Return a tiktoken encoding if available + downloadable, else None.

    tiktoken needs to fetch its BPE file the first time; if the network is
    blocked we transparently fall back to a char-based heuristic so the
    chunker still works (with slightly less precise token counts).
    """
    global _ENC, _ENC_TRIED
    if _ENC_TRIED:
        return _ENC
    _ENC_TRIED = True
    try:
        import tiktoken
        _ENC = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENC = None
    return _ENC


def count_tokens(text: str) -> int:
    enc = _enc()
    if enc is not None:
        return len(enc.encode(text))
    # Fallback heuristic: Chinese ~1 token/char, ASCII ~1 token / 4 chars.
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    other = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + other) if text else 0


def _decode(tokens) -> str:
    enc = _enc()
    if enc is not None:
        return enc.decode(tokens)
    return "".join(tokens)


def _encode(text: str):
    enc = _enc()
    if enc is not None:
        return enc.encode(text)
    # In fallback mode "tokens" are characters.
    return list(text)


def chunk_text(text: str, max_tokens: int = 512, overlap: int = 64) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    pieces = _recursive_split(text, SEPARATORS, max_tokens)
    merged = _merge(pieces, max_tokens, overlap)
    return [
        {"ord": i, "content": c, "token_count": count_tokens(c)}
        for i, c in enumerate(merged)
    ]


def _recursive_split(text: str, seps: list[str], max_tokens: int) -> list[str]:
    if count_tokens(text) <= max_tokens:
        return [text]
    if not seps:
        toks = _encode(text)
        return [
            _decode(toks[i : i + max_tokens])
            for i in range(0, len(toks), max_tokens)
        ]
    sep, rest = seps[0], seps[1:]
    parts = text.split(sep)
    if len(parts) > 1:
        parts = [parts[0]] + [sep + p for p in parts[1:]]
    parts = [p for p in parts if p]
    out: list[str] = []
    for p in parts:
        if count_tokens(p) <= max_tokens:
            out.append(p)
        else:
            out.extend(_recursive_split(p, rest, max_tokens))
    return out


def _merge(pieces: list[str], max_tokens: int, overlap: int) -> list[str]:
    target = max(max_tokens - overlap, max_tokens // 2)
    chunks: list[str] = []
    cur = ""
    cur_toks = 0
    for p in pieces:
        pt = count_tokens(p)
        if cur and cur_toks + pt > target:
            chunks.append(cur)
            cur = ""
            cur_toks = 0
        cur += p
        cur_toks += pt
    if cur:
        chunks.append(cur)

    if overlap <= 0 or len(chunks) <= 1:
        return chunks

    out = [chunks[0]]
    for i in range(1, len(chunks)):
        prev_toks = _encode(chunks[i - 1])
        if len(prev_toks) > overlap:
            tail = _decode(prev_toks[-overlap:])
        else:
            tail = chunks[i - 1]
        out.append(tail + chunks[i])
    return out
