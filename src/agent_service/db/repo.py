import json
from typing import Any
from uuid import UUID

import asyncpg


# ---------- documents ----------

async def insert_document(
    conn: asyncpg.Connection,
    *,
    title: str,
    mime_type: str,
    byte_size: int,
    sha256: str,
    source_uri: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO documents (title, source_uri, mime_type, byte_size, sha256, metadata)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        title,
        source_uri,
        mime_type,
        byte_size,
        sha256,
        json.dumps(metadata or {}),
    )
    return row["id"]


async def find_document_by_sha(conn: asyncpg.Connection, sha256: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, title, mime_type, byte_size, sha256 FROM documents WHERE sha256 = $1",
        sha256,
    )
    return dict(row) if row else None


async def list_documents(
    conn: asyncpg.Connection, *, limit: int = 50, offset: int = 0, query: str | None = None
) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT d.id, d.title, d.mime_type, d.byte_size, d.created_at,
               COALESCE(c.cnt, 0) AS chunk_count
        FROM documents d
        LEFT JOIN (SELECT document_id, COUNT(*) AS cnt FROM chunks GROUP BY document_id) c
               ON c.document_id = d.id
        WHERE $1::text IS NULL OR d.title ILIKE '%' || $1 || '%'
        ORDER BY d.created_at DESC
        LIMIT $2 OFFSET $3
        """,
        query,
        limit,
        offset,
    )
    return [dict(r) for r in rows]


async def get_document(conn: asyncpg.Connection, document_id: int) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, title, mime_type, byte_size, sha256, created_at FROM documents WHERE id = $1",
        document_id,
    )
    return dict(row) if row else None


async def get_document_full_text(
    conn: asyncpg.Connection, document_id: int, char_limit: int = 32000
) -> str:
    rows = await conn.fetch(
        "SELECT content FROM chunks WHERE document_id = $1 ORDER BY ord ASC",
        document_id,
    )
    text = "\n\n".join(r["content"] for r in rows)
    return text[:char_limit]


# ---------- chunks ----------

async def insert_chunks_bulk(
    conn: asyncpg.Connection,
    *,
    document_id: int,
    chunks: list[dict[str, Any]],
) -> int:
    if not chunks:
        return 0
    rows = [
        (
            document_id,
            c["ord"],
            c["content"],
            c["token_count"],
            c["embedding"],
            json.dumps(c.get("metadata", {})),
        )
        for c in chunks
    ]
    await conn.executemany(
        """
        INSERT INTO chunks (document_id, ord, content, token_count, embedding, metadata)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (document_id, ord) DO NOTHING
        """,
        rows,
    )
    return len(rows)


async def get_chunk(conn: asyncpg.Connection, chunk_id: int) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, document_id, ord, content FROM chunks WHERE id = $1",
        chunk_id,
    )
    return dict(row) if row else None


# ---------- sessions / messages ----------

async def create_session(
    conn: asyncpg.Connection, title: str | None = None
) -> UUID:
    row = await conn.fetchrow(
        "INSERT INTO sessions (title) VALUES ($1) RETURNING id",
        title,
    )
    return row["id"]


async def get_session(conn: asyncpg.Connection, session_id: UUID) -> dict | None:
    row = await conn.fetchrow(
        "SELECT id, title, created_at, updated_at FROM sessions WHERE id = $1",
        session_id,
    )
    return dict(row) if row else None


async def touch_session(conn: asyncpg.Connection, session_id: UUID) -> None:
    await conn.execute(
        "UPDATE sessions SET updated_at = now() WHERE id = $1", session_id
    )


async def append_message(
    conn: asyncpg.Connection,
    *,
    session_id: UUID,
    role: str,
    content: Any,
    citations: list[dict] | None = None,
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO messages (session_id, role, content, citations)
        VALUES ($1, $2, $3::jsonb, $4::jsonb)
        RETURNING id
        """,
        session_id,
        role,
        json.dumps(content, ensure_ascii=False),
        json.dumps(citations or [], ensure_ascii=False),
    )
    return row["id"]


async def load_messages(
    conn: asyncpg.Connection, session_id: UUID
) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT id, role, content, citations, created_at
        FROM messages
        WHERE session_id = $1
        ORDER BY created_at ASC, id ASC
        """,
        session_id,
    )
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "role": r["role"],
                "content": json.loads(r["content"]) if isinstance(r["content"], str) else r["content"],
                "citations": json.loads(r["citations"]) if isinstance(r["citations"], str) else r["citations"],
                "created_at": r["created_at"],
            }
        )
    return out


async def load_messages_for_openai(
    conn: asyncpg.Connection, session_id: UUID
) -> list[dict]:
    """Return persisted messages in OpenAI Chat Completions shape."""
    raw = await load_messages(conn, session_id)
    out: list[dict] = []
    for m in raw:
        role = m["role"]
        content = m["content"]
        if role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": content.get("content")}
            if content.get("tool_calls"):
                msg["tool_calls"] = content["tool_calls"]
            out.append(msg)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": content["tool_call_id"],
                    "content": content["content"],
                }
            )
        else:
            out.append({"role": role, "content": content})
    return out
