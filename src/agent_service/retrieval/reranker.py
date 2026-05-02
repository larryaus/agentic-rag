from agent_service.config import settings
from agent_service.deps import get_voyage


async def rerank(
    query: str, hits: list[dict], top_n: int
) -> list[dict]:
    if not hits or not settings.RERANK_ENABLED:
        return hits[:top_n]
    client = get_voyage()
    docs = [h["content"] for h in hits]
    result = await client.rerank(
        query=query,
        documents=docs,
        model=settings.RERANK_MODEL,
        top_k=top_n,
    )
    out = []
    for r in result.results:
        h = dict(hits[r.index])
        h["score"] = float(r.relevance_score)
        out.append(h)
    return out
