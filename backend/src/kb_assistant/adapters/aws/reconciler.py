import time
from datetime import datetime, timedelta
from typing import Any

from kb_assistant.container import Container
from kb_assistant.errors import error_message
from kb_assistant.logging import log
from kb_assistant.timeutil import now

from .ingest_event import delete_upload_objects

MAX_RECOVERY_ATTEMPTS = 3
TERMINAL_FAILURES = {"FAILED", "NOT_FOUND", "IGNORED", "METADATA_UPDATE_FAILED"}
ACTIVE_STATUSES = {"IN_PROGRESS", "STARTING", "PENDING"}


async def recover_missing_ingestion(
    deps: Container, item: dict[str, Any], counts: dict[str, int], known_missing: bool
) -> None:
    async def transition(
        status: str,
        error: str | None = None,
        attempt: int | None = None,
        expected: str | None = None,
    ) -> bool:
        return await deps.store.transition(
            item["documentId"], [expected or item["status"]], status, error, attempt
        )

    async def cleanup() -> bool:
        return await delete_upload_objects(
            deps, item["s3Key"], "failed to clean up upload object", documentId=item["documentId"]
        )

    size = await deps.blobs.head(item["s3Key"])
    if size is None:
        if await transition("FAILED", "Upload was not completed before the presigned URL expired"):
            counts["abandoned"] += 1
            counts["failed"] += 1
            await cleanup()
        else:
            counts["concurrent"] += 1
        return
    if size > deps.settings.max_upload_bytes:
        if not await cleanup():
            raise RuntimeError("Failed to delete oversized upload objects")
        changed = await transition(
            "FAILED", f"Object exceeds the {deps.settings.max_upload_bytes} byte upload limit"
        )
        counts["failed" if changed else "concurrent"] += 1
        return
    attempts = item.get("ingestionRecoveryAttempts", 0)
    if attempts >= MAX_RECOVERY_ATTEMPTS:
        if not known_missing:
            counts["unchanged"] += 1
            return
        changed = await transition(
            "FAILED",
            "Ingestion did not start after three recovery attempts; the uploaded file has been retained",
        )
        counts["failed" if changed else "concurrent"] += 1
        return
    if not await transition("PENDING", attempt=attempts + 1):
        counts["concurrent"] += 1
        return
    await deps.indexer.ingest(item["documentId"], item["s3Key"])
    counts["retried"] += 1
    if not await transition("INGESTING", expected="PENDING"):
        counts["concurrent"] += 1


async def reconcile_document(
    deps: Container, item: dict[str, Any], detail: dict[str, Any], counts: dict[str, int]
) -> None:
    status = detail.get("status", "")
    expected = [item["status"]]
    if status == "INDEXED":
        changed = await deps.store.transition(item["documentId"], expected, "READY")
        counts["ready" if changed else "concurrent"] += 1
    elif (status == "NOT_FOUND" and item["status"] in ["UPLOADING", "PENDING"]) or (
        status == "" and item["status"] == "UPLOADING"
    ):
        await recover_missing_ingestion(deps, item, counts, status == "NOT_FOUND")
    elif status in TERMINAL_FAILURES or "PARTIAL" in status:
        changed = await deps.store.transition(
            item["documentId"],
            expected,
            "FAILED",
            detail.get("statusReason", f"Bedrock document entered terminal status {status}"),
        )
        counts["failed" if changed else "concurrent"] += 1
    elif item["status"] in ["UPLOADING", "PENDING"] and status in ACTIVE_STATUSES:
        changed = await deps.store.transition(item["documentId"], expected, "INGESTING")
        counts["unchanged" if changed else "concurrent"] += 1
    else:
        counts["unchanged"] += 1


async def reconcile(deps: Container) -> None:
    started = time.monotonic()
    counts = dict(
        examined=0, ready=0, failed=0, abandoned=0, unchanged=0, concurrent=0, retried=0, errored=0
    )
    log("info", "reconciler run started")
    try:
        documents = await deps.store.all_documents()
        counts["examined"] = len(documents)
        cutoff = now() - timedelta(minutes=deps.settings.abandoned_upload_minutes)

        def aged(item: dict[str, Any]) -> bool:
            try:
                return datetime.fromisoformat(item["updatedAt"]) < cutoff
            except ValueError:
                return False

        pollable = [
            d
            for d in documents
            if d["status"] == "INGESTING" or (d["status"] in ["PENDING", "UPLOADING"] and aged(d))
        ]
        for start in range(0, len(pollable), 10):
            batch = pollable[start : start + 10]
            try:
                details = await deps.indexer.details([item["s3Key"] for item in batch])
            except Exception as exc:
                counts["errored"] += len(batch)
                log(
                    "error",
                    "reconciler batch failed",
                    documentIds=[d["documentId"] for d in batch],
                    error=error_message(exc),
                )
                continue
            by_uri = {d.get("identifier", {}).get("s3", {}).get("uri"): d for d in details}
            for item in batch:
                try:
                    await reconcile_document(
                        deps,
                        item,
                        by_uri.get(f"s3://{deps.settings.docs_bucket}/{item['s3Key']}", {}),
                        counts,
                    )
                except Exception as exc:
                    counts["errored"] += 1
                    log(
                        "error",
                        "reconciler document failed",
                        documentId=item["documentId"],
                        error=error_message(exc),
                    )
        log("info", "reconciler summary", **counts)
        if counts["errored"]:
            raise RuntimeError(f"{counts['errored']} document(s) could not be reconciled")
    except Exception as exc:
        log("error", "reconciler run failed", error=error_message(exc))
        raise
    finally:
        log("info", "reconciler run completed", durationMs=int((time.monotonic() - started) * 1000))
