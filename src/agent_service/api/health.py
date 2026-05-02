from fastapi import APIRouter

from agent_service.db.pool import get_pool

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    db_status = "ok"
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        db_status = f"error: {exc}"
    return {"status": "ok", "db": db_status}
