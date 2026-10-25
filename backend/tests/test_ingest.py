import pytest
from event_fakes import dependencies, document

from kb_assistant.adapters.aws.ingest_event import handle_ingest


def event(key, size=100):
    return {"detail": {"object": {"key": key, "size": size}, "bucket": {"name": "documents"}}}


async def test_decode_and_direct_ingestion():
    doc = document()
    deps = dependencies(doc)
    await handle_ingest(event(f"uploads/user/{doc['documentId']}/My+File%20One.md"), deps)
    assert deps.indexer.ingests == [
        (doc["documentId"], f"uploads/user/{doc['documentId']}/My File One.md")
    ]
    assert deps.store.docs[doc["documentId"]]["status"] == "INGESTING"
    assert deps.store.writes[0][1] == ["UPLOADING", "PENDING"]


@pytest.mark.parametrize(
    "key", ["x.metadata.json", "%FF", "%zz", "outside/key", "uploads/user/not-a-uuid/doc.md"]
)
async def test_skip_invalid_keys_and_sidecars(key):
    deps = dependencies()
    deps.store.get_failure = AssertionError("must not read")
    await handle_ingest(event(key), deps)
    assert not deps.indexer.ingests and not deps.blobs.deletes


async def test_missing_and_already_processed_records():
    deps = dependencies(document(status="READY"))
    for doc in [document(2), document()]:
        await handle_ingest(event(doc["s3Key"], 26_214_401), deps)
    assert not deps.indexer.ingests and not deps.blobs.deletes and not deps.store.writes


@pytest.mark.parametrize("failed_delete", [None, "object", "sidecar"])
async def test_oversize_delete_then_fail_or_retain_retryable(failed_delete):
    doc = document()
    deps = dependencies(doc)
    if failed_delete:
        deps.blobs.fail_delete.add(
            doc["s3Key"] + (".metadata.json" if failed_delete == "sidecar" else "")
        )
    deps.blobs.on_delete = lambda _: assert_status(deps, doc, "UPLOADING")
    await handle_ingest(event(doc["s3Key"], 26_214_401), deps)
    assert deps.blobs.deletes == [doc["s3Key"], doc["s3Key"] + ".metadata.json"]
    assert_status(deps, doc, "UPLOADING" if failed_delete else "FAILED")
    assert not deps.indexer.ingests


def assert_status(deps, doc, status):
    assert deps.store.docs[doc["documentId"]]["status"] == status


@pytest.mark.parametrize("failure", ["read", "ingest"])
async def test_failures_allow_retry_without_failing_document(failure):
    doc = document()
    deps = dependencies(doc)
    if failure == "read":
        deps.store.get_failure = RuntimeError("read failed")
    else:
        deps.indexer.fail_ingest = True
    with pytest.raises(RuntimeError):
        await handle_ingest(event(doc["s3Key"]), deps)
    assert not deps.store.writes


async def test_accepted_ingestion_status_failure_leaves_pending_marker():
    doc = document()
    deps = dependencies(doc)
    deps.store.failures[doc["documentId"]] = RuntimeError("ddb unavailable")
    await handle_ingest(event(doc["s3Key"]), deps)
    await handle_ingest(event(doc["s3Key"]), deps)
    assert [w[2] for w in deps.store.writes] == ["INGESTING", "PENDING", "INGESTING", "PENDING"]
    assert deps.indexer.ingests == [(doc["documentId"], doc["s3Key"])] * 2


def test_event_handlers_reuse_one_loop_and_container(monkeypatch):
    import asyncio

    from kb_assistant.adapters.aws import lambda_events

    lambda_events.runtime.cache_clear()
    seen = []
    lock = asyncio.Lock()

    async def handle(event, container):
        seen.append(container)

        async def hold():
            async with lock:
                await asyncio.sleep(0)

        # Contention binds the lock to the running loop; a second loop would fail here.
        await asyncio.gather(hold(), hold())

    monkeypatch.setattr(lambda_events, "handle_ingest", handle)
    try:
        lambda_events.ingest_handler({}, None)
        lambda_events.ingest_handler({}, None)
    finally:
        lambda_events.runtime.cache_clear()
    assert len(seen) == 2 and seen[0] is seen[1]
