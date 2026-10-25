import re
from typing import Any

CITATION_RE = re.compile(r"\[ref:([0-9]+)\]")
UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)


def js_slice(text: str, length: int) -> str:
    """JavaScript string lengths and slices count UTF-16 code units."""
    return text.encode("utf-16-le", errors="surrogatepass")[: length * 2].decode(
        "utf-16-le", errors="surrogatepass"
    )


def extract_refs(text: str) -> list[int]:
    return list(dict.fromkeys(int(m) for m in CITATION_RE.findall(text)))


def resolve_citations(text: str, index: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(
            ref=ref,
            title=c["title"],
            documentId=c["documentId"],
            score=c["score"],
            snippet=js_slice(c["text"], 240),
        )
        for ref in extract_refs(text)
        if (c := index.get(ref)) is not None
    ]
