import asyncio

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from agent_service.agent.loop import run_streaming
from agent_service.db.pool import get_pool
from agent_service.db import repo
from agent_service.deps import get_openai
from agent_service.schemas import ChatRequest
from agent_service.sse import SSEChannel

router = APIRouter()


@router.post("/chat")
async def chat(req: ChatRequest):
    pool = await get_pool()
    client = get_openai()

    session_id = req.session_id
    if session_id is None:
        async with pool.acquire() as conn:
            session_id = await repo.create_session(conn, title=req.message[:60])

    sse = SSEChannel()

    async def runner():
        try:
            await sse.send("session", {"session_id": str(session_id)})
            await run_streaming(
                client=client,
                pool=pool,
                sse=sse,
                session_id=session_id,
                user_text=req.message,
                document_ids=req.document_ids,
            )
        except Exception as exc:
            await sse.send("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            await sse.close()

    asyncio.create_task(runner())
    return EventSourceResponse(sse.events())
