from uuid import UUID

from fastapi import APIRouter, HTTPException

from agent_service.db.pool import get_pool
from agent_service.db import repo
from agent_service.schemas import CitationDTO, MessageDTO, SessionDetail

router = APIRouter()


def _flatten_message(row: dict) -> MessageDTO | None:
    role = row["role"]
    content = row["content"]
    text_parts: list[str] = []
    tool_calls: list[dict] = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        # may be assistant blocks (text/tool_use) or user tool_result blocks
        is_tool_result_only = all(
            (b.get("type") == "tool_result") for b in content if isinstance(b, dict)
        )
        if is_tool_result_only and role == "user":
            return None  # hide internal tool results from UI
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                text_parts.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                tool_calls.append({"name": b.get("name"), "input": b.get("input", {})})

    citations = [CitationDTO(**c) for c in (row.get("citations") or [])]
    return MessageDTO(
        role=role,
        text="".join(text_parts),
        tool_calls=tool_calls,
        citations=citations,
        created_at=row["created_at"],
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: UUID) -> SessionDetail:
    pool = await get_pool()
    async with pool.acquire() as conn:
        meta = await repo.get_session(conn, session_id)
        if not meta:
            raise HTTPException(status_code=404, detail="session not found")
        rows = await repo.load_messages(conn, session_id)

    msgs = [m for m in (_flatten_message(r) for r in rows) if m is not None]
    return SessionDetail(
        id=meta["id"],
        title=meta["title"],
        created_at=meta["created_at"],
        updated_at=meta["updated_at"],
        messages=msgs,
    )
