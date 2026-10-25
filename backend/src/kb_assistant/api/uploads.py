from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request

from kb_assistant.core.sse import js_json_dumps
from kb_assistant.core.uploads import parse_json, validate_upload
from kb_assistant.timeutil import iso_ms, now

from .deps import Auth, Dependencies

router = APIRouter()


@router.post("/v1/uploads", status_code=201)
async def upload(request: Request, auth: Auth, deps: Dependencies) -> dict[str, Any]:
    upload = validate_upload(parse_json(await request.body()), deps.settings.max_upload_bytes)
    document_id = str(uuid4())
    key = f"uploads/{auth.sub}/{document_id}/{upload['safeName']}"
    timestamp = now()
    uploaded_at = iso_ms(timestamp)
    sidecar = {
        "metadataAttributes": {
            "documentId": {
                "value": {"type": "STRING", "stringValue": document_id},
                "includeForEmbedding": False,
            },
            "uploaderSub": {
                "value": {"type": "STRING", "stringValue": auth.sub},
                "includeForEmbedding": False,
            },
            "uploadedAt": {
                "value": {"type": "NUMBER", "numberValue": int(timestamp.timestamp())},
                "includeForEmbedding": False,
            },
        }
    }
    url = await deps.blobs.presign_put(key, upload["contentType"], 300)
    await deps.store.create_document(
        dict(
            pk=f"DOC#{document_id}",
            sk="META",
            gsi1pk="ORG#DOCUMENT",
            gsi1sk=uploaded_at,
            documentId=document_id,
            uploaderSub=auth.sub,
            title=upload["safeName"],
            s3Key=key,
            contentType=upload["contentType"],
            sizeBytes=upload["sizeBytes"],
            status="UPLOADING",
            uploadedAt=uploaded_at,
            updatedAt=uploaded_at,
        )
    )
    await deps.blobs.put_sidecar(key, js_json_dumps(sidecar))
    return dict(documentId=document_id, uploadUrl=url, key=key, expiresIn=300)
