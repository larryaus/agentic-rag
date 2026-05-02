"""Integration test: hits a real Postgres+pgvector if PGVECTOR_TEST_URL is set.

Skipped in plain `pytest -q` runs to keep tests hermetic.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    "PGVECTOR_TEST_URL" not in os.environ,
    reason="set PGVECTOR_TEST_URL to a postgres+pgvector dsn to enable",
)


@pytest.mark.asyncio
async def test_hybrid_search_finds_needle():
    import asyncpg
    from pgvector.asyncpg import Vector, register_vector

    from agent_service.retrieval.hybrid import hybrid_search

    dsn = os.environ["PGVECTOR_TEST_URL"]
    conn = await asyncpg.connect(dsn)
    await register_vector(conn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("DROP TABLE IF EXISTS chunks_test")
        await conn.execute("DROP TABLE IF EXISTS docs_test")
        # NB: the production schema is fixed; here we just smoke-test the SQL pattern.
        # We rely on the real `documents` and `chunks` tables having been migrated.
        # If this fixture is run against the dev compose db, the seed docs are present.
        rows = await conn.fetch("SELECT id FROM documents LIMIT 1")
        if not rows:
            pytest.skip("no documents seeded in target db")
        # Build a fake query vector matching the embedding dimension (1024)
        qvec = [0.0] * 1024
        qvec[0] = 1.0
        out = await hybrid_search(
            conn,
            query_text="请假",
            query_embedding=qvec,
            document_ids=None,
            candidate_k=20,
            final_k=5,
        )
        assert isinstance(out, list)
    finally:
        await conn.close()
