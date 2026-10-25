import asyncpg

# Any constant works; it only has to be the same for every process that creates the schema.
SCHEMA_LOCK = 4_207_311_002


class EmbeddingMismatch(RuntimeError):
    pass


def reindex_hint(model: str, dimension: int) -> str:
    return (
        f"The vector index was built with a different embedding model than the configured "
        f"{model} ({dimension} dimensions). Rebuild it offline:\n"
        "  docker compose stop app\n"
        "  docker compose run --rm app python -m kb_assistant.adapters.local.reindex\n"
        "  docker compose up -d app\n"
        "or start over with `docker compose down -v`."
    )


def _tables(dimension: int) -> list[str]:
    # Timestamps are stored as the same millisecond ISO text DynamoDB holds, so items,
    # cursors and ordering stay identical across deployments.
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE TABLE IF NOT EXISTS kb_meta (key text PRIMARY KEY, value text NOT NULL)",
        """CREATE TABLE IF NOT EXISTS sessions (
            session_id uuid PRIMARY KEY,
            user_sub text NOT NULL,
            title text NOT NULL,
            created_at text NOT NULL,
            updated_at text NOT NULL,
            message_count integer NOT NULL DEFAULT 0,
            ttl bigint NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS sessions_by_user
            ON sessions (user_sub, updated_at DESC, session_id DESC)""",
        """CREATE TABLE IF NOT EXISTS messages (
            session_id uuid NOT NULL REFERENCES sessions ON DELETE CASCADE,
            sk text NOT NULL,
            user_sub text NOT NULL,
            role text NOT NULL,
            content text NOT NULL,
            citations jsonb NOT NULL,
            usage jsonb,
            stop_reason text,
            turn_id text,
            created_at text NOT NULL,
            ttl bigint NOT NULL,
            PRIMARY KEY (session_id, sk)
        )""",
        """CREATE TABLE IF NOT EXISTS documents (
            document_id uuid PRIMARY KEY,
            uploader_sub text NOT NULL,
            title text NOT NULL,
            s3_key text NOT NULL UNIQUE,
            content_type text NOT NULL,
            size_bytes bigint NOT NULL,
            status text NOT NULL,
            error_message text,
            recovery_attempts integer,
            ingest_generation integer NOT NULL DEFAULT 0,
            lease_expires_at timestamptz,
            uploaded_at text NOT NULL,
            updated_at text NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS documents_by_upload
            ON documents (uploaded_at DESC, document_id DESC)""",
        f"""CREATE TABLE IF NOT EXISTS chunks (
            document_id uuid NOT NULL REFERENCES documents ON DELETE CASCADE,
            ord integer NOT NULL,
            content text NOT NULL,
            embedding vector({dimension}) NOT NULL,
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
            PRIMARY KEY (document_id, ord)
        )""",
        """CREATE INDEX IF NOT EXISTS chunks_embedding
            ON chunks USING hnsw (embedding vector_cosine_ops)""",
        "CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin (tsv)",
    ]


async def rebuild_chunks(connection: asyncpg.Connection, model: str, dimension: int) -> None:
    """Drops every vector and records the new embedding model; callers re-ingest after."""
    await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK)
    await connection.execute("DROP TABLE IF EXISTS chunks")
    for statement in _tables(dimension)[-3:]:
        await connection.execute(statement)
    await connection.executemany(
        """INSERT INTO kb_meta (key, value) VALUES ($1, $2)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        [("embedding_model", model), ("embedding_dimension", str(dimension))],
    )


async def ensure_schema(connection: asyncpg.Connection, model: str, dimension: int) -> None:
    async with connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK)
        for statement in _tables(dimension):
            await connection.execute(statement)
        rows = await connection.fetch("SELECT key, value FROM kb_meta")
        stored = {row["key"]: row["value"] for row in rows}
        if not stored:
            await connection.executemany(
                "INSERT INTO kb_meta (key, value) VALUES ($1, $2)",
                [("embedding_model", model), ("embedding_dimension", str(dimension))],
            )
            stored = {"embedding_model": model, "embedding_dimension": str(dimension)}
        column = await connection.fetchval(
            """SELECT atttypmod FROM pg_attribute
               WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"""
        )
        if (
            stored.get("embedding_model") != model
            or stored.get("embedding_dimension") != str(dimension)
            or column != dimension
        ):
            raise EmbeddingMismatch(reindex_hint(model, dimension))
