from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from agent_service.db.pool import get_pool
from agent_service.ingest.pipeline import ingest_bytes
from agent_service.schemas import IngestResponse

router = APIRouter()


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
) -> IngestResponse:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    pool = await get_pool()
    try:
        result = await ingest_bytes(
            pool,
            data=data,
            filename=file.filename or "upload.bin",
            declared_mime=file.content_type,
            title=title,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return IngestResponse(**result)
