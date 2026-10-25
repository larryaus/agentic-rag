import asyncio
from typing import Any

from fastapi import APIRouter, Request, Response

from kb_assistant.core.paging import parse_limit
from kb_assistant.errors import AppError, error_message
from kb_assistant.logging import log

from .deps import Auth, Dependencies

router = APIRouter()
SUMMARY_FIELDS = ("documentId", "title", "contentType", "sizeBytes", "status", "uploadedAt")


@router.get("/v1/documents")
async def documents(request: Request, auth: Auth, deps: Dependencies) -> dict[str, Any]:
    page = await deps.store.list_documents(
        parse_limit(request.query_params.get("limit"), 25), request.query_params.get("nextToken")
    )
    return {**page, "items": [{key: item[key] for key in SUMMARY_FIELDS} for item in page["items"]]}


@router.get("/v1/documents/{document_id}/download")
async def download(document_id: str, auth: Auth, deps: Dependencies) -> dict[str, str]:
    item = await deps.store.get_document(document_id)
    if item is None:
        raise AppError("Document not found", 404)
    if item["status"] != "READY":
        raise AppError("Document is not ready", 409)
    return {"url": await deps.blobs.presign_get(item["s3Key"], 300)}


@router.delete("/v1/documents/{document_id}", status_code=204)
async def remove(document_id: str, auth: Auth, deps: Dependencies) -> Response:
    item = await deps.store.get_document(document_id)
    if item is None:
        raise AppError("Document not found", 404)
    if item["status"] != "FAILED" or not await deps.store.delete_failed_document(document_id):
        raise AppError("Only failed documents can be removed", 409)
    keys = [item["s3Key"], f"{item['s3Key']}.metadata.json"]
    results = await asyncio.gather(
        *(deps.blobs.delete(key) for key in keys), return_exceptions=True
    )
    for key, result in zip(keys, results, strict=True):
        if isinstance(result, BaseException):
            log(
                "warn",
                "failed to delete removed document object",
                key=key,
                error=error_message(result),
            )
    return Response(status_code=204)
