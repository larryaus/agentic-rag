import json

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from agent_service.agent.loop import run_streaming
from agent_service.db.pool import get_pool
from agent_service.db import repo
from agent_service.deps import get_openai
from agent_service.schemas import ChatRequest

router = APIRouter()


@router.post("/chat")
async def chat(req: ChatRequest):
    pool = await get_pool()
    client = get_openai()

    session_id = req.session_id
    if session_id is None:
        async with pool.acquire() as conn:
            session_id = await repo.create_session(conn, title=req.message[:60])

    async def event_stream():
        yield {
            "event": "session",
            "data": json.dumps({"session_id": str(session_id)}),
        }
        try:
            async for ev in run_streaming(
                client=client,
                pool=pool,
                session_id=session_id,
                user_text=req.message,
                document_ids=req.document_ids,
            ):
                yield ev
        except Exception as exc:
            yield {
                "event": "error",
                "data": json.dumps({"message": f"{type(exc).__name__}: {exc}"}),
            }

    return EventSourceResponse(event_stream())
