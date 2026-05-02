from fastapi import APIRouter, Query

from agent_service.db.pool import get_pool
from agent_service.db import repo
from agent_service.schemas import DocumentSummary

router = APIRouter()


@router.get("/documents", response_model=list[DocumentSummary])
async def list_documents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None),
) -> list[DocumentSummary]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await repo.list_documents(conn, limit=limit, offset=offset, query=q)
    return [DocumentSummary(**r) for r in rows]
