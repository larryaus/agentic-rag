from uuid import UUID

from fastapi import APIRouter, HTTPException

from agent_service.db.pool import get_pool
from agent_service.db import repo
from agent_service.schemas import MessageDTO, SessionDetail
from agent_service.transcript import visible_message_for_ui

router = APIRouter()


def _flatten_message(row: dict) -> MessageDTO | None:
    visible = visible_message_for_ui(row)
    return MessageDTO(**visible) if visible else None


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
