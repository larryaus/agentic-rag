import json
from uuid import UUID

from fastapi import APIRouter, HTTPException

from agent_service.db.pool import get_pool
from agent_service.db import repo
from agent_service.schemas import CitationDTO, MessageDTO, SessionDetail

router = APIRouter()


def _flatten_message(row: dict) -> MessageDTO | None:
    role = row["role"]
    content = row["content"]
    if role == "tool":
        return None

    text = ""
    tool_calls: list[dict] = []
    if isinstance(content, str):
        text = content
    elif isinstance(content, dict):
        text = content.get("content") or ""
        for call in content.get("tool_calls") or []:
            function = call.get("function") or {}
            args_raw = function.get("arguments") or "{}"
            try:
                parsed_args = json.loads(args_raw)
            except json.JSONDecodeError:
                parsed_args = {"_raw": args_raw}
            tool_calls.append({"name": function.get("name"), "input": parsed_args})

    citations = [CitationDTO(**c) for c in (row.get("citations") or [])]
    return MessageDTO(
        role=role,
        text=text,
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
