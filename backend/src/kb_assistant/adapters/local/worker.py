"""In-process ingestion for the local deployment.

Each claim takes a lease and bumps the document's generation. Every later write checks
that generation, and that the stored embedding model is still the one this process is
configured with, so neither a reclaimed lease nor an old process after a model switch
can overwrite newer work. Any number of workers is therefore safe.
"""

import asyncio
import re
import time
from datetime import timedelta
from typing import Any

import asyncpg

from kb_assistant.core.citations import js_slice
from kb_assistant.logging import log
from kb_assistant.timeutil import iso_ms, now

from .chunker import chunk_text
from .fs_blobs import FsBlobs
from .loaders import extract_text

MAX_RECOVERY_ATTEMPTS = 3
UPLOAD_KEY = re.compile(
    r"^uploads/[^/]+/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})/[^/]+$",
    re.IGNORECASE,
)
MODEL_FENCE = """
  (SELECT value FROM kb_meta WHERE key = 'embedding_model') = $M
  AND (SELECT value FROM kb_meta WHERE key = 'embedding_dimension') = $D
"""


class LeaseLost(Exception):
    pass


class Unprocessable(Exception):
    """A document that can never ingest; its message is the stored reason."""


def fenced(query: str, model_param: int) -> str:
    return query.replace("$M", f"${model_param}").replace("$D", f"${model_param + 1}")


class Worker:
    def __init__(
        self,
        pool: asyncpg.Pool,
        blobs: FsBlobs,
        embedder: Any,
        *,
        model: str,
        dimension: int,
        lease_seconds: float,
        sweep_seconds: float,
        abandoned_upload_minutes: int,
    ) -> None:
        self.pool = pool
        self.blobs = blobs
        self.embedder = embedder
        self.model = model
        self.dimension = str(dimension)
        self.lease_seconds = lease_seconds
        self.sweep_seconds = sweep_seconds
        self.abandoned_upload_minutes = abandoned_upload_minutes
        self.active: dict[str, asyncio.Task[None]] = {}
        self._loop_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        tasks = [*self.active.values(), *([self._loop_task] if self._loop_task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def notify(self, key: str) -> None:
        match = UPLOAD_KEY.match(key)
        if match is not None:
            self.schedule(match.group(1).lower())

    def schedule(self, document_id: str) -> None:
        if document_id in self.active:
            return
        task = asyncio.create_task(self.ingest(document_id))
        self.active[document_id] = task
        task.add_done_callback(lambda _: self.active.pop(document_id, None))

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception as exc:
                log("error", "ingestion sweep failed", errorType=type(exc).__name__)
            await asyncio.sleep(self.sweep_seconds)

    async def sweep(self) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute("DELETE FROM sessions WHERE ttl <= $1", int(time.time()))
            expired = await connection.fetch(
                fenced(
                    f"""SELECT document_id FROM documents
                        WHERE status = 'INGESTING' AND lease_expires_at < now() AND {MODEL_FENCE}""",
                    1,
                ),
                self.model,
                self.dimension,
            )
            uploading = await connection.fetch(
                """SELECT document_id, s3_key FROM documents
                   WHERE status = 'UPLOADING' AND updated_at < $1""",
                iso_ms(now() - timedelta(minutes=self.abandoned_upload_minutes)),
            )
        await asyncio.to_thread(self.blobs.sweep_parts)
        for row in expired:
            self.schedule(str(row["document_id"]))
        for row in uploading:
            document_id = str(row["document_id"])
            if await self.blobs.head(row["s3_key"]) is not None:
                # The upload finished but its notification was lost.
                self.schedule(document_id)
                continue
            async with self.pool.acquire() as connection:
                abandoned = await connection.execute(
                    """UPDATE documents SET status = 'FAILED', error_message = $2, updated_at = $3
                       WHERE document_id = $1 AND status = 'UPLOADING'""",
                    row["document_id"],
                    "Upload was not completed before the presigned URL expired",
                    iso_ms(now()),
                )
            if abandoned == "UPDATE 1":
                await self.blobs.delete(f"{row['s3_key']}.metadata.json")

    async def claim(self, document_id: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as connection:
            return await connection.fetchrow(
                fenced(
                    f"""UPDATE documents
                        SET status = 'INGESTING',
                            ingest_generation = ingest_generation + 1,
                            lease_expires_at = now() + make_interval(secs => $2),
                            updated_at = $3,
                            recovery_attempts = CASE WHEN status = 'INGESTING'
                              THEN COALESCE(recovery_attempts, 0) + 1
                              ELSE recovery_attempts END
                        WHERE document_id = $1
                          AND (status = 'UPLOADING'
                               OR (status = 'INGESTING' AND lease_expires_at < now()))
                          AND {MODEL_FENCE}
                        RETURNING ingest_generation, recovery_attempts, s3_key, content_type""",
                    4,
                ),
                document_id,
                self.lease_seconds,
                iso_ms(now()),
                self.model,
                self.dimension,
            )

    async def _renew(self, document_id: str, generation: int) -> None:
        while True:
            await asyncio.sleep(max(1.0, self.lease_seconds / 5))
            async with self.pool.acquire() as connection:
                renewed = await connection.execute(
                    fenced(
                        f"""UPDATE documents
                            SET lease_expires_at = now() + make_interval(secs => $3)
                            WHERE document_id = $1 AND ingest_generation = $2
                              AND status = 'INGESTING' AND {MODEL_FENCE}""",
                        4,
                    ),
                    document_id,
                    generation,
                    self.lease_seconds,
                    self.model,
                    self.dimension,
                )
            if renewed != "UPDATE 1":
                raise LeaseLost(document_id)

    async def fail(self, document_id: str, generation: int, reason: str) -> bool:
        async with self.pool.acquire() as connection:
            result = await connection.execute(
                """UPDATE documents
                   SET status = 'FAILED', error_message = $3, lease_expires_at = NULL,
                       updated_at = $4
                   WHERE document_id = $1 AND ingest_generation = $2 AND status = 'INGESTING'""",
                document_id,
                generation,
                js_slice(reason, 1000),
                iso_ms(now()),
            )
        return bool(result == "UPDATE 1")

    async def complete(
        self, document_id: str, generation: int, chunks: list[str], vectors: list[list[float]]
    ) -> bool:
        async with self.pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow(
                fenced(
                    f"""SELECT 1 FROM documents
                        WHERE document_id = $1 AND ingest_generation = $2
                          AND status = 'INGESTING' AND {MODEL_FENCE}
                        FOR UPDATE""",
                    3,
                ),
                document_id,
                generation,
                self.model,
                self.dimension,
            )
            if current is None:
                return False
            await connection.execute("DELETE FROM chunks WHERE document_id = $1", document_id)
            await connection.executemany(
                "INSERT INTO chunks (document_id, ord, content, embedding) VALUES ($1, $2, $3, $4)",
                [
                    (document_id, index, chunk, vector)
                    for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
                ],
            )
            await connection.execute(
                """UPDATE documents
                   SET status = 'READY', error_message = NULL, lease_expires_at = NULL,
                       updated_at = $2
                   WHERE document_id = $1""",
                document_id,
                iso_ms(now()),
            )
        return True

    async def ingest(self, document_id: str) -> None:
        claimed = await self.claim(document_id)
        if claimed is None:
            return
        generation = claimed["ingest_generation"]
        if (claimed["recovery_attempts"] or 0) > MAX_RECOVERY_ATTEMPTS:
            await self.fail(
                document_id,
                generation,
                "Ingestion did not complete after three attempts; the uploaded file has been retained",
            )
            return
        renewal = asyncio.create_task(self._renew(document_id, generation))
        work = asyncio.create_task(self._prepare(claimed["s3_key"], claimed["content_type"]))
        try:
            done, _ = await asyncio.wait({renewal, work}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                work.cancel()
                renewal.result()
            chunks, vectors = work.result()
            if not await self.complete(document_id, generation, chunks, vectors):
                log("info", "discarded ingestion for a reclaimed document", documentId=document_id)
            else:
                log("info", "document ingested", documentId=document_id, chunks=len(chunks))
        except LeaseLost:
            log("warn", "ingestion lease lost", documentId=document_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log("warn", "ingestion failed", documentId=document_id, errorType=type(exc).__name__)
            reason = str(exc) if isinstance(exc, Unprocessable) else f"{type(exc).__name__}: {exc}"
            await self.fail(document_id, generation, reason)
        finally:
            renewal.cancel()
            work.cancel()

    async def _prepare(self, key: str, content_type: str) -> tuple[list[str], list[list[float]]]:
        data = await self.blobs.get_bytes(key)
        text = await asyncio.to_thread(extract_text, data, content_type)
        chunks = await asyncio.to_thread(chunk_text, text)
        if not chunks:
            raise Unprocessable("No extractable text")
        # Embedding finishes before any write, so a crash never leaves partial chunks.
        return chunks, await self.embedder.embed(chunks)
