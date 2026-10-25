from typing import Any

import asyncpg

from kb_assistant.core.ports import RawChunk

CANDIDATES = 40

# Vector and keyword rankings fused with reciprocal rank fusion (k = 60), so neither
# channel alone decides; only ready documents are searchable.
HYBRID_SQL = """
WITH vec AS (
  SELECT c.document_id, c.ord,
         ROW_NUMBER() OVER (ORDER BY c.embedding <=> $1) AS rnk
  FROM chunks c JOIN documents d ON d.document_id = c.document_id
  WHERE d.status = 'READY'
  ORDER BY c.embedding <=> $1
  LIMIT $3
),
kw AS (
  SELECT c.document_id, c.ord,
         ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS rnk
  FROM chunks c JOIN documents d ON d.document_id = c.document_id,
       plainto_tsquery('simple', $2) AS q
  WHERE d.status = 'READY' AND c.tsv @@ q
  ORDER BY ts_rank_cd(c.tsv, q) DESC
  LIMIT $3
),
fused AS (
  SELECT document_id, ord, SUM(1.0 / (60 + rnk)) AS score
  FROM (SELECT * FROM vec UNION ALL SELECT * FROM kw) ranked
  GROUP BY document_id, ord
)
SELECT c.content, d.s3_key, d.document_id, d.title, f.score
FROM fused f
JOIN chunks c ON c.document_id = f.document_id AND c.ord = f.ord
JOIN documents d ON d.document_id = f.document_id
ORDER BY f.score DESC, c.document_id, c.ord
LIMIT $4
"""


class PgRetriever:
    def __init__(self, pool: asyncpg.Pool, embedder: Any) -> None:
        self.pool = pool
        self.embedder = embedder

    async def retrieve(self, query: str, top_k: int) -> list[RawChunk]:
        [vector] = await self.embedder.embed([query])
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(HYBRID_SQL, vector, query, CANDIDATES, top_k)
        return [
            RawChunk(
                text=row["content"],
                source_uri=row["s3_key"],
                document_id=str(row["document_id"]),
                title=row["title"],
                score=float(row["score"]),
            )
            for row in rows
        ]
