import asyncio
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from kb_assistant.adapters.local.fs_blobs import DOWNLOAD, UPLOAD, BlobTokenError, FsBlobs
from kb_assistant.errors import AppError
from kb_assistant.logging import log

from .deps import Dependencies

# Signed URLs stand in for S3 presigned URLs on the local deployment. They carry their own
# authorization, so these routes never ask for a bearer token.
router = APIRouter()


def _claims(blobs: FsBlobs, token: str | None, op: str) -> dict[str, Any]:
    try:
        return blobs.verify(token or "", op)
    except BlobTokenError as exc:
        raise AppError("Forbidden", 403) from exc


@router.put("/v1/blobs/upload")
async def upload(request: Request, deps: Dependencies) -> Response:
    blobs: FsBlobs = deps.blobs  # type: ignore[assignment]
    claims = _claims(blobs, request.query_params.get("token"), UPLOAD)
    if request.headers.get("content-type") != claims["ct"]:
        raise AppError("Forbidden", 403)
    final = blobs.path(claims["key"])
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.with_name(f"{final.name}.part-{secrets.token_hex(8)}")
    limit = deps.settings.max_upload_bytes
    try:
        size = 0
        with temp.open("xb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise AppError(f"Object exceeds the {limit} byte upload limit", 413)
                await asyncio.to_thread(handle.write, chunk)
        try:
            # A hard link publishes atomically and fails if the key exists, like If-None-Match.
            await asyncio.to_thread(os.link, temp, final)
        except FileExistsError as exc:
            raise AppError("Object already exists", 409) from exc
    finally:
        Path(temp).unlink(missing_ok=True)
    if blobs.on_upload is not None:
        try:
            blobs.on_upload(claims["key"])
        except Exception as exc:
            # The sweep picks up uploads whose notification was lost.
            log("warn", "upload notification failed", errorType=type(exc).__name__)
    return Response(status_code=200)


@router.get("/v1/blobs/download")
async def download(request: Request, deps: Dependencies) -> Response:
    blobs: FsBlobs = deps.blobs  # type: ignore[assignment]
    claims = _claims(blobs, request.query_params.get("token"), DOWNLOAD)
    path = blobs.path(claims["key"])
    if not path.is_file():
        raise AppError("Not found", 404)
    return FileResponse(path, filename=path.name, content_disposition_type="attachment")
