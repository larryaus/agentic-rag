from copy import deepcopy
from types import SimpleNamespace

from kb_assistant.settings import Settings


def document(index=1, status="UPLOADING", **values):
    did = f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012x}"
    return dict(
        documentId=did,
        pk=f"DOC#{did}",
        sk="META",
        gsi1pk="ORG#DOCUMENT",
        gsi1sk="2026-01-01T00:00:00.000Z",
        s3Key=f"uploads/user/{did}/doc.md",
        status=status,
        updatedAt="2026-01-01T00:00:00.000Z",
        **values,
    )


class Store:
    def __init__(self, docs):
        self.docs = {d["documentId"]: deepcopy(d) for d in docs}
        self.writes = []
        self.failures = {}
        self.get_failure = None

    async def get_document(self, id):
        if self.get_failure:
            raise self.get_failure
        return deepcopy(self.docs.get(id))

    async def all_documents(self):
        return deepcopy(list(self.docs.values()))

    async def transition(self, id, expected, status, error=None, recovery_attempt=None):
        self.writes.append((id, expected, status, error, recovery_attempt))
        failure = self.failures.get(id)
        if failure is False:
            return False
        if failure:
            raise failure
        doc = self.docs[id]
        if doc["status"] not in expected:
            return False
        doc["status"] = status
        if error is not None:
            doc["errorMessage"] = error[:1000]
        else:
            doc.pop("errorMessage", None)
        if recovery_attempt is not None:
            doc["ingestionRecoveryAttempts"] = recovery_attempt
        return True


class Blobs:
    def __init__(self):
        self.sizes = {}
        self.deletes = []
        self.heads = []
        self.fail_delete = set()
        self.on_delete = None

    async def head(self, key):
        self.heads.append(key)
        value = self.sizes.get(key)
        if isinstance(value, Exception):
            raise value
        return value

    async def delete(self, key):
        self.deletes.append(key)
        if self.on_delete:
            self.on_delete(key)
        if key in self.fail_delete:
            raise RuntimeError("delete failed")


class Indexer:
    def __init__(self):
        self.statuses = {}
        self.batches = []
        self.ingests = []
        self.fail_ingest = False
        self.fail_batches = set()

    async def ingest(self, id, key):
        self.ingests.append((id, key))
        if self.fail_ingest:
            raise RuntimeError("ingest failed")

    async def details(self, keys):
        self.batches.append(keys)
        if len(self.batches) in self.fail_batches:
            raise RuntimeError("poll failed")
        return [
            {
                "identifier": {"s3": {"uri": f"s3://documents/{key}"}},
                "status": self.statuses[key],
                "statusReason": "reason",
            }
            for key in keys
            if key in self.statuses
        ]


def dependencies(*docs):
    return SimpleNamespace(
        settings=Settings(docs_bucket="documents"),
        store=Store(docs),
        blobs=Blobs(),
        indexer=Indexer(),
    )
