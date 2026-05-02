"""Voyage AI embeddings client wrapper (async, batched)."""

from __future__ import annotations

from agent_service.config import settings
from agent_service.deps import get_voyage


EMBED_DIM = 1024
_BATCH = 64


async def embed_documents(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    client = get_voyage()
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        batch = texts[i : i + _BATCH]
        result = await client.embed(
            texts=batch,
            model=settings.EMBED_MODEL,
            input_type="document",
        )
        out.extend(result.embeddings)
    return out


async def embed_query(text: str) -> list[float]:
    client = get_voyage()
    result = await client.embed(
        texts=[text],
        model=settings.EMBED_MODEL,
        input_type="query",
    )
    return result.embeddings[0]
