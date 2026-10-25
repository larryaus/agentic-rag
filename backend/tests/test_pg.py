import asyncio
import time
from datetime import timedelta
from uuid import uuid4

import asyncpg
import pytest
from pgvector.asyncpg import register_vector

from kb_assistant.adapters.local.fs_blobs import FsBlobs
from kb_assistant.adapters.local.pg_retriever import PgRetriever
from kb_assistant.adapters.local.pg_schema import EmbeddingMismatch, ensure_schema
from kb_assistant.adapters.local.pg_store import ConditionFailed, PgStore
from kb_assistant.adapters.local.reindex import reindex
from kb_assistant.adapters.local.worker import Worker
from kb_assistant.core.history import make_message_item, make_session_item
from kb_assistant.core.paging import encode_page_token
from kb_assistant.errors import ValidationError
from kb_assistant.settings import Settings
from kb_assistant.timeutil import iso_ms, now

pytestmark = pytest.mark.postgres
DIM = 3
FUTURE = int(time.time()) + 86400


async def connect(url, model="m", dim=DIM):
    connection = await asyncpg.connect(url)
    try:
        await ensure_schema(connection, model, dim)
    finally:
        await connection.close()
    return await asyncpg.create_pool(url, init=register_vector, min_size=1, max_size=5)


@pytest.fixture
async def pool(pg_url):
    value = await connect(pg_url)
    yield value
    await value.close()


class Embedder:
    def __init__(self, fail_on_batch=None, delay=0.0):
        self.fail_on_batch = fail_on_batch
        self.delay = delay
        self.batches = 0

    async def embed(self, texts):
        vectors = []
        for start in range(0, len(texts), 64):
            self.batches += 1
            if self.batches == self.fail_on_batch:
                raise RuntimeError("embedding service down")
            await asyncio.sleep(self.delay)
            vectors += [vector(text) for text in texts[start : start + 64]]
        return vectors


def vector(text):
    lowered = text.lower()
    return [
        1.0 + lowered.count("leave"),
        1.0 + lowered.count("refund"),
        1.0 + lowered.count("server"),
    ]


def doc(document_id=None, status="UPLOADING", uploaded_at=None, key=None):
    document_id = document_id or str(uuid4())
    uploaded_at = uploaded_at or iso_ms(now())
    return dict(
        documentId=document_id,
        uploaderSub="owner",
        title="doc.md",
        s3Key=key or f"uploads/owner/{document_id}/doc.md",
        contentType="text/markdown",
        sizeBytes=10,
        status=status,
        uploadedAt=uploaded_at,
        updatedAt=uploaded_at,
    )


def session(user="owner", created_at=None, ttl=FUTURE):
    return make_session_item(
        sessionId=str(uuid4()),
        userSub=user,
        title="t",
        createdAt=created_at or iso_ms(now()),
        ttl=ttl,
    )


def message(item, role="user", created_at=None, **extra):
    return make_message_item(
        sessionId=item["sessionId"],
        userSub=item["userSub"],
        role=role,
        content="hello",
        createdAt=created_at or iso_ms(now()),
        ttl=item["ttl"],
        **extra,
    )


# Schema


async def test_schema_is_idempotent_and_guards_the_embedding_model(pg_url):
    (await connect(pg_url)).terminate()
    (await connect(pg_url)).terminate()
    for model, dim in [("m", 4), ("other", DIM)]:
        with pytest.raises(EmbeddingMismatch, match="reindex"):
            await connect(pg_url, model, dim)


# Store


async def test_session_items_match_the_dynamodb_shape(pool):
    store = PgStore(pool)
    item = session()
    await store.create_session(item)
    assert await store.get_session(item["sessionId"]) == item
    with pytest.raises(ConditionFailed):
        await store.create_session(item)
    assert await store.get_session("not-a-uuid") is None


async def test_persist_is_atomic_and_owner_conditional(pool):
    store = PgStore(pool)
    item = session()
    await store.create_session(item)
    first = message(item, citations=[{"ref": 1, "score": 0.30000000000000004}], turnId="t")
    await store.persist_submitted_message(first)
    with pytest.raises(ConditionFailed):
        await store.persist_submitted_message(first)
    stranger = dict(message(item), userSub="intruder")
    with pytest.raises(ConditionFailed):
        await store.persist_completed_turn(stranger)
    page = await store.list_messages(item["sessionId"], 50)
    assert page == {"items": [first]}
    meta = await store.get_session(item["sessionId"])
    assert meta["messageCount"] == 1 and meta["updatedAt"] == first["createdAt"]
    assert meta["gsi1sk"] == first["createdAt"]


async def test_equal_timestamps_page_exactly_once(pool):
    store = PgStore(pool)
    stamp = iso_ms(now())
    ids = set()
    for _ in range(5):
        item = session(created_at=stamp)
        ids.add(item["sessionId"])
        await store.create_session(item)
    await store.create_session(session(user="someone-else"))
    seen, token = [], None
    while True:
        page = await store.list_sessions("owner", 2, token)
        seen += [item["sessionId"] for item in page["items"]]
        token = page.get("nextToken")
        if token is None:
            break
    assert sorted(seen) == sorted(ids) and len(seen) == 5


async def test_foreign_and_tampered_cursors_are_rejected(pool):
    store = PgStore(pool)
    for _ in range(3):
        await store.create_session(session())
    token = (await store.list_sessions("owner", 1, None))["nextToken"]
    with pytest.raises(ValidationError, match="Malformed nextToken"):
        await store.list_sessions("someone-else", 1, token)
    forged = encode_page_token(
        dict(pk="SESSION#not-a-uuid", sk="META", gsi1pk="USER#owner#SESSION", gsi1sk="x")
    )
    with pytest.raises(ValidationError):
        await store.list_sessions("owner", 1, forged)
    with pytest.raises(ValidationError):
        await store.list_documents(1, token)


async def test_expired_sessions_disappear_even_mid_paging(pool):
    store = PgStore(pool)
    live, expiring = session(), session()
    await store.create_session(live)
    await store.create_session(expiring)
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE sessions SET ttl = $1 WHERE session_id = $2",
            int(time.time()) - 1,
            expiring["sessionId"],
        )
    assert await store.get_session(expiring["sessionId"]) is None
    page = await store.list_sessions("owner", 10, None)
    assert [item["sessionId"] for item in page["items"]] == [live["sessionId"]]


async def test_messages_page_newest_first_in_chronological_pages(pool):
    store = PgStore(pool)
    item = session()
    await store.create_session(item)
    start = now()
    rows = [message(item, created_at=iso_ms(start + timedelta(milliseconds=i))) for i in range(5)]
    for row in rows:
        await store.persist_submitted_message(row)
    first = await store.list_messages(item["sessionId"], 2)
    assert first["items"] == rows[3:]
    second = await store.list_messages(item["sessionId"], 2, first["nextToken"])
    assert second["items"] == rows[1:3]


async def test_document_transitions_are_conditional_under_races(pool):
    store = PgStore(pool)
    item = doc()
    await store.create_document(item)
    results = await asyncio.gather(
        *(store.transition(item["documentId"], ["UPLOADING"], "FAILED", "x" * 2000) for _ in range(5))
    )
    assert results.count(True) == 1
    stored = await store.get_document(item["documentId"])
    assert stored["status"] == "FAILED" and len(stored["errorMessage"]) == 1000
    assert await store.delete_failed_document(item["documentId"])
    assert not await store.delete_failed_document(item["documentId"])


# Retrieval


async def insert_ready(pool, text, status="READY"):
    item = doc(status=status)
    await PgStore(pool).create_document(item)
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO chunks (document_id, ord, content, embedding) VALUES ($1, 0, $2, $3)",
            item["documentId"],
            text,
            vector(text),
        )
    return item["documentId"]


async def test_hybrid_retrieval_ranks_and_skips_unready_documents(pool):
    leave = await insert_ready(pool, "Annual leave is twenty days of leave per year.")
    await insert_ready(pool, "Refunds are processed within five days.")
    await insert_ready(pool, "Server restarts happen on Sundays.")
    hidden = await insert_ready(pool, "Secret leave leave leave policy.", status="INGESTING")
    hits = await PgRetriever(pool, Embedder()).retrieve("how much annual leave", 8)
    assert hits[0].document_id == leave and hidden not in [hit.document_id for hit in hits]
    assert hits[0].title == "doc.md" and hits[0].source_uri.startswith("uploads/owner/")
    assert all(hit.score > 0 for hit in hits)


# Worker


@pytest.fixture
def blobs(tmp_path):
    return FsBlobs(tmp_path, "http://local", b"secret")


def worker(pool, blobs, embedder=None, model="m", lease=300.0):
    return Worker(
        pool,
        blobs,
        embedder or Embedder(),
        model=model,
        dimension=DIM,
        lease_seconds=lease,
        sweep_seconds=60,
        abandoned_upload_minutes=10,
    )


async def uploaded(pool, blobs, text="Annual leave policy.\n\n" * 3, **kwargs):
    item = doc(**kwargs)
    await PgStore(pool).create_document(item)
    path = blobs.path(item["s3Key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return item


async def status(pool, document_id):
    async with pool.acquire() as connection:
        return await connection.fetchrow(
            """SELECT status, error_message, ingest_generation, recovery_attempts,
                      (SELECT count(*) FROM chunks WHERE document_id = $1) AS chunks
               FROM documents WHERE document_id = $1""",
            document_id,
        )


async def test_concurrent_claims_have_one_winner(pool, blobs):
    item = await uploaded(pool, blobs)
    claims = await asyncio.gather(*(worker(pool, blobs).claim(item["documentId"]) for _ in range(5)))
    assert sum(claim is not None for claim in claims) == 1


async def test_ingest_and_notification(pool, blobs):
    item = await uploaded(pool, blobs)
    runner = worker(pool, blobs)
    runner.notify(item["s3Key"])
    runner.notify("not/an/upload")
    await asyncio.gather(*runner.active.values())
    row = await status(pool, item["documentId"])
    assert row["status"] == "READY" and row["chunks"] >= 1 and row["error_message"] is None


async def test_stale_completion_is_rejected_after_reclaim(pool, blobs):
    item = await uploaded(pool, blobs)
    first = worker(pool, blobs)
    old = await first.claim(item["documentId"])
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE documents SET lease_expires_at = now() - interval '1 second'"
        )
    new = await worker(pool, blobs).claim(item["documentId"])
    assert new["ingest_generation"] == old["ingest_generation"] + 1
    assert new["recovery_attempts"] == 1
    assert not await first.complete(item["documentId"], old["ingest_generation"], ["x"], [[1, 1, 1]])
    assert await first.complete(item["documentId"], new["ingest_generation"], ["x"], [[1, 1, 1]])


async def test_failure_between_batches_leaves_no_chunks(pool, blobs):
    item = await uploaded(pool, blobs, text="word " * 40000)
    await worker(pool, blobs, Embedder(fail_on_batch=2)).ingest(item["documentId"])
    row = await status(pool, item["documentId"])
    assert row["status"] == "FAILED" and row["chunks"] == 0
    assert "embedding service down" in row["error_message"]


async def test_cancelled_ingestion_leaves_no_chunks_and_stays_reclaimable(pool, blobs):
    item = await uploaded(pool, blobs)
    runner = worker(pool, blobs, Embedder(delay=5))
    task = asyncio.create_task(runner.ingest(item["documentId"]))
    await asyncio.sleep(0.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    row = await status(pool, item["documentId"])
    assert row["status"] == "INGESTING" and row["chunks"] == 0


async def test_long_ingestion_renews_its_lease(pool, blobs):
    item = await uploaded(pool, blobs)
    runner = worker(pool, blobs, Embedder(delay=3.5), lease=2.0)
    task = asyncio.create_task(runner.ingest(item["documentId"]))
    await asyncio.sleep(2.5)
    # Past the original lease, but renewal keeps another worker from reclaiming it.
    assert await worker(pool, blobs).claim(item["documentId"]) is None
    await task
    assert (await status(pool, item["documentId"]))["status"] == "READY"


async def test_recovery_attempts_are_capped(pool, blobs):
    item = await uploaded(pool, blobs)
    async with pool.acquire() as connection:
        await connection.execute(
            """UPDATE documents SET status = 'INGESTING', recovery_attempts = 3,
               lease_expires_at = now() - interval '1 second'"""
        )
    await worker(pool, blobs).ingest(item["documentId"])
    row = await status(pool, item["documentId"])
    assert row["status"] == "FAILED" and "three attempts" in row["error_message"]


async def test_unreadable_document_fails_with_a_plain_reason(pool, blobs):
    item = await uploaded(pool, blobs, text="   \n  ")
    await worker(pool, blobs).ingest(item["documentId"])
    assert (await status(pool, item["documentId"]))["error_message"] == "No extractable text"


async def test_sweep_fails_abandoned_uploads_and_recovers_missed_notifications(pool, blobs):
    old = iso_ms(now() - timedelta(minutes=20))
    abandoned = doc(uploaded_at=old)
    await PgStore(pool).create_document(abandoned)
    await blobs.put_sidecar(abandoned["s3Key"], "{}")
    missed = await uploaded(pool, blobs, uploaded_at=old)
    recent = doc()
    await PgStore(pool).create_document(recent)
    expired = session(ttl=int(time.time()) - 1)
    await PgStore(pool).create_session(expired)
    runner = worker(pool, blobs)
    await runner.sweep()
    await asyncio.gather(*runner.active.values())
    assert (await status(pool, abandoned["documentId"]))["status"] == "FAILED"
    assert await blobs.head(f"{abandoned['s3Key']}.metadata.json") is None
    assert (await status(pool, missed["documentId"]))["status"] == "READY"
    assert (await status(pool, recent["documentId"]))["status"] == "UPLOADING"
    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM sessions") == 0


async def test_reindex_fences_out_workers_on_the_old_model(pg_url, pool, blobs, tmp_path):
    item = await uploaded(pool, blobs)
    await worker(pool, blobs).ingest(item["documentId"])
    settings = Settings(
        database_url=pg_url,
        embedding_model="new-model",
        embedding_dimension=DIM,
        blob_root=str(tmp_path),
    )
    assert await reindex(settings) == 1
    stale = worker(pool, blobs, model="m")
    assert await stale.claim(item["documentId"]) is None
    # The current generation, so only the model fence can refuse the write.
    generation = (await status(pool, item["documentId"]))["ingest_generation"]
    assert not await stale.complete(item["documentId"], generation, ["x"], [[1, 1, 1]])
    await worker(pool, blobs, model="new-model").ingest(item["documentId"])
    row = await status(pool, item["documentId"])
    assert row["status"] == "READY" and row["chunks"] >= 1
    async with pool.acquire() as connection:
        meta = dict(await connection.fetch("SELECT key, value FROM kb_meta"))
    assert meta == {"embedding_model": "new-model", "embedding_dimension": str(DIM)}


async def test_reindex_fails_documents_whose_file_is_missing(pg_url, pool, blobs, tmp_path):
    kept = await uploaded(pool, blobs)
    lost = await uploaded(pool, blobs)
    for item in (kept, lost):
        await worker(pool, blobs).ingest(item["documentId"])
    blobs.path(lost["s3Key"]).unlink()
    settings = Settings(
        database_url=pg_url, embedding_model="m", embedding_dimension=DIM, blob_root=str(tmp_path)
    )
    assert await reindex(settings) == 1
    row = await status(pool, lost["documentId"])
    assert row["status"] == "FAILED" and "missing" in row["error_message"]
    assert (await status(pool, kept["documentId"]))["status"] == "INGESTING"
