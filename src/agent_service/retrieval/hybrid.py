"""Hybrid retrieval: vector cosine + tsvector BM25-ish, fused via RRF."""

from __future__ import annotations

import asyncpg
from pgvector.asyncpg import Vector


_HYBRID_SQL = """
WITH vec AS (
  SELECT id,
         ROW_NUMBER() OVER (ORDER BY embedding <=> $1) AS rnk
  FROM chunks
  WHERE ($3::bigint[] IS NULL OR document_id = ANY($3))
  ORDER BY embedding <=> $1
  LIMIT $4
),
kw AS (
  SELECT id,
         ROW_NUMBER() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS rnk
  FROM chunks, plainto_tsquery('simple', $2) AS q
  WHERE tsv @@ q
    AND ($3::bigint[] IS NULL OR document_id = ANY($3))
  LIMIT $4
),
fused AS (
  SELECT id, SUM(1.0 / (60 + rnk)) AS score
  FROM (
    SELECT id, rnk FROM vec
    UNION ALL
    SELECT id, rnk FROM kw
  ) u
  GROUP BY id
)
SELECT c.id          AS chunk_id,
       c.document_id AS document_id,
       c.ord         AS ord,
       c.content     AS content,
       d.title       AS title,
       f.score       AS score
FROM fused f
  JOIN chunks    c ON c.id = f.id
  JOIN documents d ON d.id = c.document_id
ORDER BY f.score DESC
LIMIT $5;
"""


async def hybrid_search(
    conn: asyncpg.Connection,
    *,
    query_text: str,
    query_embedding: list[float],
    document_ids: list[int] | None = None,
    candidate_k: int = 40,
    final_k: int = 20,
) -> list[dict]:
    rows = await conn.fetch(
        _HYBRID_SQL,
        Vector(query_embedding),
        query_text,
        document_ids,
        candidate_k,
        final_k,
    )
    return [dict(r) for r in rows]
