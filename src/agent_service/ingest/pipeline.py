"""Document ingestion: load -> chunk -> embed -> persist."""

from __future__ import annotations

import hashlib
from typing import Any

import asyncpg

from agent_service.config import settings
from agent_service.db import repo
from agent_service.ingest.chunker import chunk_text
from agent_service.ingest.loaders import detect_mime, extract_text
from agent_service.retrieval.embeddings import embed_documents


class IngestResult(dict):
    document_id: int
    title: str
    chunk_count: int
    sha256: str
    duplicated: bool


async def ingest_bytes(
    pool: asyncpg.Pool,
    *,
    data: bytes,
    filename: str,
    declared_mime: str | None = None,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    if len(data) > settings.MAX_INGEST_BYTES:
        raise ValueError(
            f"file exceeds size limit ({len(data)} > {settings.MAX_INGEST_BYTES})"
        )

    sha = hashlib.sha256(data).hexdigest()
    mime = detect_mime(filename, declared_mime)

    async with pool.acquire() as conn:
        existing = await repo.find_document_by_sha(conn, sha)
        if existing:
            return {
                "document_id": existing["id"],
                "title": existing["title"],
                "chunk_count": 0,
                "sha256": sha,
                "duplicated": True,
            }

    text = extract_text(data, mime).strip()
    if not text:
        raise ValueError("could not extract any text from file")

    chunks = chunk_text(text, settings.CHUNK_TOKENS, settings.CHUNK_OVERLAP)
    if not chunks:
        raise ValueError("chunker produced no chunks")

    embeddings = await embed_documents([c["content"] for c in chunks])
    if len(embeddings) != len(chunks):
        raise RuntimeError("embedding count mismatch")

    for c, e in zip(chunks, embeddings):
        c["embedding"] = e

    resolved_title = title or filename

    async with pool.acquire() as conn:
        async with conn.transaction():
            doc_id = await repo.insert_document(
                conn,
                title=resolved_title,
                mime_type=mime,
                byte_size=len(data),
                sha256=sha,
                source_uri=filename,
                metadata=metadata,
            )
            inserted = await repo.insert_chunks_bulk(
                conn, document_id=doc_id, chunks=chunks
            )

    return {
        "document_id": doc_id,
        "title": resolved_title,
        "chunk_count": inserted,
        "sha256": sha,
        "duplicated": False,
    }
