import asyncio
import re
import time
from typing import Any
from urllib.parse import unquote

from kb_assistant.container import Container
from kb_assistant.errors import error_message
from kb_assistant.logging import log

DOCUMENT_KEY = re.compile(
    r"^uploads/([^/]+)/([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})/(.+)$",
    re.I,
)


def decode_object_key(raw: str) -> str | None:
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", raw):
            return None
        return unquote(raw.replace("+", " "), errors="strict")
    except UnicodeError:
        return None


async def delete_upload_objects(deps: Container, key: str, message: str, **fields: Any) -> bool:
    keys = [key, f"{key}.metadata.json"]
    results = await asyncio.gather(*(deps.blobs.delete(k) for k in keys), return_exceptions=True)
    deleted = True
    for object_key, result in zip(keys, results, strict=True):
        if isinstance(result, BaseException):
            deleted = False
            log("warn", message, key=object_key, error=error_message(result), **fields)
    return deleted


async def handle_ingest(event: dict[str, Any], deps: Container) -> None:
    started = time.monotonic()
    log("info", "ingest event started")
    try:
        key = decode_object_key(event["detail"]["object"]["key"])
        if key is None:
            log("warn", "ignoring undecodable object key")
            return
        if key.endswith(".metadata.json"):
            log("info", "ignoring metadata sidecar")
            return
        match = DOCUMENT_KEY.fullmatch(key)
        if match is None:
            log("warn", "ignoring object outside upload key layout", key=key)
            return
        document_id = match[2]
        document = await deps.store.get_document(document_id)
        if document is None:
            log(
                "warn",
                "ignoring upload event without a document record",
                documentId=document_id,
                key=key,
            )
            return
        if document["status"] != "UPLOADING":
            log(
                "info",
                "ignoring upload event for document already processed",
                documentId=document_id,
                key=key,
                status=document["status"],
            )
            return

        async def update(expected: list[str], status: str, error: str | None = None) -> None:
            if not await deps.store.transition(document_id, expected, status, error):
                raise RuntimeError("Document status changed concurrently")

        if event["detail"]["object"]["size"] > deps.settings.max_upload_bytes:
            if await delete_upload_objects(deps, key, "failed to delete rejected upload object"):
                await update(
                    ["UPLOADING"],
                    "FAILED",
                    f"Object exceeds the {deps.settings.max_upload_bytes} byte upload limit",
                )
            return
        try:
            await deps.indexer.ingest(document_id, key)
        except Exception as exc:
            log(
                "warn",
                "ingestion request failed; leaving status for reconciliation",
                documentId=document_id,
                error=error_message(exc),
            )
            raise
        try:
            await update(["UPLOADING", "PENDING"], "INGESTING")
        except Exception as exc:
            log(
                "warn",
                "Bedrock accepted ingestion but status update failed",
                documentId=document_id,
                error=error_message(exc),
            )
            try:
                await update(["UPLOADING", "PENDING"], "PENDING")
            except Exception as recovery_error:
                log(
                    "warn",
                    "failed to leave ingestion pending for reconciliation",
                    documentId=document_id,
                    error=error_message(recovery_error),
                )
    except Exception as exc:
        log("error", "ingest event failed; allowing invocation retry", error=error_message(exc))
        raise
    finally:
        log("info", "ingest event completed", durationMs=int((time.monotonic() - started) * 1000))
