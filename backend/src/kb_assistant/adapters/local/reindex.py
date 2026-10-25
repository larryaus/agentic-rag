"""Rebuilds the vector index for a new embedding model. Run it with the app stopped:

  docker compose stop app
  docker compose run --rm app python -m kb_assistant.adapters.local.reindex
  docker compose up -d app
"""

import asyncio
from pathlib import Path
from typing import Any

import asyncpg

from kb_assistant.settings import Settings
from kb_assistant.timeutil import iso_ms, now

from .fs_blobs import FsBlobs, load_secret
from .pg_schema import ensure_schema, rebuild_chunks


async def reindex(settings: Settings) -> int:
    root = Path(settings.blob_root)
    blobs = FsBlobs(root, settings.public_base_url, load_secret(root, settings.blob_signing_secret))
    connection = await asyncpg.connect(settings.require("database_url"))
    try:
        async with connection.transaction():
            await rebuild_chunks(connection, settings.embedding_model, settings.embedding_dimension)
            rows = await connection.fetch(
                "SELECT document_id, s3_key FROM documents WHERE status IN ('READY', 'INGESTING', 'FAILED')"
            )
            present: list[Any] = []
            missing: list[Any] = []
            for row in rows:
                exists = await blobs.head(row["s3_key"]) is not None
                (present if exists else missing).append(row["document_id"])
            await connection.execute(
                """UPDATE documents
                   SET status = 'FAILED', lease_expires_at = NULL, updated_at = $2,
                       error_message = 'The uploaded file is missing, so it could not be reindexed'
                   WHERE document_id = ANY($1::uuid[])""",
                missing,
                iso_ms(now()),
            )
            # A new generation and an expired lease hand every document to the next worker,
            # and the model fence stops any worker still configured for the old model.
            await connection.execute(
                """UPDATE documents
                   SET status = 'INGESTING', ingest_generation = ingest_generation + 1,
                       lease_expires_at = now() - interval '1 second',
                       recovery_attempts = NULL, error_message = NULL, updated_at = $2
                   WHERE document_id = ANY($1::uuid[])""",
                present,
                iso_ms(now()),
            )
        await ensure_schema(connection, settings.embedding_model, settings.embedding_dimension)
    finally:
        await connection.close()
    return len(present)


def main() -> None:
    settings = Settings()
    count = asyncio.run(reindex(settings))
    print(
        f"Vector index rebuilt for {settings.embedding_model} "
        f"({settings.embedding_dimension} dimensions); {count} documents queued for re-ingestion.\n"
        "Start the app with the new settings: docker compose up -d app"
    )


if __name__ == "__main__":
    main()
