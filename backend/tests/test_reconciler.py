import json

import pytest
from event_fakes import dependencies, document

from kb_assistant.adapters.aws.reconciler import reconcile


async def test_batches_and_terminal_statuses(capsys):
    docs = [document(i, "INGESTING") for i in range(11)]
    abandoned = document(99)
    deps = dependencies(*docs, abandoned)
    statuses = [
        "INDEXED",
        "FAILED",
        "NOT_FOUND",
        "IGNORED",
        "IN_PROGRESS",
        "METADATA_UPDATE_FAILED",
        "PARTIALLY_INDEXED",
    ] + ["INDEXED"] * 4
    deps.indexer.statuses = {d["s3Key"]: s for d, s in zip(docs, statuses)}
    deps.indexer.statuses[abandoned["s3Key"]] = "NOT_FOUND"
    await reconcile(deps)
    assert [len(b) for b in deps.indexer.batches] == [10, 2]
    assert sum(d["status"] == "READY" for d in deps.store.docs.values()) == 5
    assert sum(d["status"] == "FAILED" for d in deps.store.docs.values()) == 6
    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    counts = next(log for log in logs if log["msg"] == "reconciler summary")
    assert counts["examined"] == 12 and counts["failed"] == 6 and counts["ready"] == 5


@pytest.mark.parametrize("race", [False, True])
async def test_abandoned_marked_before_cleanup_or_race(race):
    doc = document()
    deps = dependencies(doc)
    deps.indexer.statuses[doc["s3Key"]] = "NOT_FOUND"
    if race:
        deps.store.failures[doc["documentId"]] = False

    def verify(_):
        assert deps.store.docs[doc["documentId"]]["status"] == "FAILED"

    deps.blobs.on_delete = verify
    deps.blobs.fail_delete.add(doc["s3Key"])
    await reconcile(deps)
    assert len(deps.blobs.deletes) == (0 if race else 2)


@pytest.mark.parametrize("status", ["UPLOADING", "PENDING"])
@pytest.mark.parametrize("remote", ["INDEXED", "IN_PROGRESS", "STARTING", "PENDING", "NOT_FOUND"])
async def test_aged_pending_uploading_recovery(status, remote):
    doc = document(status=status)
    deps = dependencies(doc)
    deps.indexer.statuses[doc["s3Key"]] = remote
    deps.blobs.sizes[doc["s3Key"]] = 10
    await reconcile(deps)
    assert deps.store.docs[doc["documentId"]]["status"] == (
        "READY" if remote == "INDEXED" else "INGESTING"
    )
    assert len(deps.indexer.ingests) == (1 if remote == "NOT_FOUND" else 0)
    assert not deps.blobs.deletes
    if remote == "NOT_FOUND":
        assert [w[2] for w in deps.store.writes] == ["PENDING", "INGESTING"]
        assert deps.store.docs[doc["documentId"]]["ingestionRecoveryAttempts"] == 1


async def test_transport_failure_leaves_bounded_pending():
    doc = document(status="PENDING", ingestionRecoveryAttempts=1)
    deps = dependencies(doc)
    deps.indexer.statuses[doc["s3Key"]] = "NOT_FOUND"
    deps.blobs.sizes[doc["s3Key"]] = 10
    deps.indexer.fail_ingest = True
    with pytest.raises(RuntimeError, match=r"1 document\(s\)"):
        await reconcile(deps)
    assert deps.store.docs[doc["documentId"]]["status"] == "PENDING"
    assert deps.store.docs[doc["documentId"]]["ingestionRecoveryAttempts"] == 2
    assert not deps.blobs.deletes


@pytest.mark.parametrize(
    "remote,expected", [("NOT_FOUND", "FAILED"), (None, "UPLOADING"), ("IN_PROGRESS", "INGESTING")]
)
async def test_exhausted_recovery_retains_file(remote, expected):
    doc = document(ingestionRecoveryAttempts=3)
    deps = dependencies(doc)
    deps.blobs.sizes[doc["s3Key"]] = 10
    if remote:
        deps.indexer.statuses[doc["s3Key"]] = remote
    await reconcile(deps)
    assert deps.store.docs[doc["documentId"]]["status"] == expected
    assert not deps.indexer.ingests and not deps.blobs.deletes


@pytest.mark.parametrize("which", [None, "object", "sidecar"])
async def test_oversize_cleanup_precedes_failed_transition(which):
    doc = document()
    deps = dependencies(doc)
    deps.indexer.statuses[doc["s3Key"]] = "NOT_FOUND"
    deps.blobs.sizes[doc["s3Key"]] = 26_214_401
    if which:
        deps.blobs.fail_delete.add(doc["s3Key"] + (".metadata.json" if which == "sidecar" else ""))

    def verify(_):
        assert deps.store.docs[doc["documentId"]]["status"] == "UPLOADING"

    deps.blobs.on_delete = verify
    if which:
        with pytest.raises(RuntimeError, match=r"1 document\(s\)"):
            await reconcile(deps)
        assert not deps.store.writes
    else:
        await reconcile(deps)
        assert deps.store.docs[doc["documentId"]]["status"] == "FAILED"
    assert not deps.indexer.ingests and len(deps.blobs.deletes) == 2


@pytest.mark.parametrize("status", ["PENDING", "INGESTING"])
async def test_omitted_pending_ingesting_untouched(status):
    deps = dependencies(document(status=status))
    await reconcile(deps)
    assert not deps.blobs.heads and not deps.store.writes


@pytest.mark.parametrize("size", [None, 10])
async def test_omitted_uploading_checked_and_retried(size):
    doc = document()
    deps = dependencies(doc)
    deps.blobs.sizes[doc["s3Key"]] = size
    await reconcile(deps)
    assert deps.blobs.heads == [doc["s3Key"]]
    assert deps.store.docs[doc["documentId"]]["status"] == (
        "FAILED" if size is None else "INGESTING"
    )


@pytest.mark.parametrize("failure", ["head403", "head503", "write", "batch"])
async def test_errors_isolated_within_and_across_batches(failure, capsys):
    docs = [document(i, "INGESTING") for i in range(12)]
    if failure.startswith("head"):
        docs[0]["status"] = "UPLOADING"
    deps = dependencies(*docs)
    deps.indexer.statuses = {d["s3Key"]: "INDEXED" for d in docs}
    if failure.startswith("head"):
        deps.indexer.statuses[docs[0]["s3Key"]] = "NOT_FOUND"
        deps.blobs.sizes[docs[0]["s3Key"]] = RuntimeError(failure)
    elif failure == "write":
        deps.store.failures[docs[0]["documentId"]] = RuntimeError("ddb unavailable")
    else:
        deps.indexer.fail_batches.add(1)
    failed = 10 if failure == "batch" else 1
    with pytest.raises(RuntimeError, match=f"{failed} document"):
        await reconcile(deps)
    assert sum(d["status"] == "READY" for d in deps.store.docs.values()) == 12 - failed
    assert not deps.blobs.deletes and len(deps.indexer.batches) == 2
    summary = next(
        json.loads(s) for s in capsys.readouterr().out.splitlines() if "reconciler summary" in s
    )
    assert summary["errored"] == failed and summary["ready"] == 12 - failed


@pytest.mark.parametrize("failure", [False, RuntimeError("ddb failed")])
async def test_recovery_claim_race_or_error_does_not_ingest(failure):
    doc = document()
    deps = dependencies(doc)
    deps.indexer.statuses[doc["s3Key"]] = "NOT_FOUND"
    deps.blobs.sizes[doc["s3Key"]] = 10
    deps.store.failures[doc["documentId"]] = failure
    if failure:
        with pytest.raises(RuntimeError, match="1 document"):
            await reconcile(deps)
    else:
        await reconcile(deps)
    assert not deps.indexer.ingests and not deps.blobs.deletes


async def test_fresh_uploading_and_pending_not_polled():
    docs = [document(1), document(2, "PENDING")]
    for doc in docs:
        doc["updatedAt"] = "2099-01-01T00:00:00.000Z"
    deps = dependencies(*docs)
    await reconcile(deps)
    assert not deps.indexer.batches
