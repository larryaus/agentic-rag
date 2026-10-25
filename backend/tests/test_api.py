import json
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from test_agent import Model, Retrieval

from kb_assistant.adapters.aws.ddb_codec import to_ddb
from kb_assistant.adapters.aws.ddb_store import DdbStore
from kb_assistant.app import create_app
from kb_assistant.core.ports import AuthContext, Stop, TextDelta
from kb_assistant.errors import AgentStreamError, UnauthorizedError
from kb_assistant.settings import Settings


class Blobs:
    def __init__(self):
        self.sidecars = {}
        self.deleted = []
        self.signed = []

    async def presign_put(self, key, content_type, expires_in=300):
        self.signed.append((key, content_type, expires_in))
        return "https://signed.example/put"

    async def presign_get(self, key, expires_in=300):
        self.signed.append((key, expires_in))
        return "https://signed.example/get"

    async def put_sidecar(self, key, body):
        self.sidecars[key] = body

    async def delete(self, key):
        self.deleted.append(key)


class Verifier:
    async def verify(self, header):
        if header != "Bearer owner":
            raise UnauthorizedError()
        return AuthContext("owner", "owner")


@pytest.fixture
def api(table):
    app = create_app(Settings(table_name="test-table"))
    container = app.state.container
    container.store = DdbStore("test-table", table=table)
    container.blobs = Blobs()
    container.verifier = Verifier()
    container.model = Model([TextDelta(0, "answer"), Stop("end_turn")])
    container.retriever = Retrieval()
    return app, container


@pytest.fixture
def client(api):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api[0], raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": "Bearer owner"},
    )


def seed(table, legacy):
    data = legacy("items")
    table.put_item(Item=to_ddb(data["session"]))
    for row in data["messages"]:
        table.put_item(Item=to_ddb(row["item"]))
    table.put_item(Item=to_ddb(legacy("document")["document"]))
    return data


async def test_health_auth_errors_methods_cors(client):
    assert (await client.get("/healthz", headers={"Authorization": ""})).json() == {"status": "ok"}
    for path in ["/v1/documents", "/v1/sessions"]:
        response = await client.get(path, headers={"Authorization": ""})
        assert response.status_code == 401 and response.json() == {"message": "Unauthorized"}
        assert "access-control-allow-origin" not in response.headers
    assert (await client.get("/unknown")).json() == {"message": "Not found"}
    for path in ["/", "/v1/chat"]:
        response = await client.get(path)
        assert response.status_code == 405 and response.json() == {"message": "Method not allowed"}
        assert (
            await client.post(path, content="{", headers={"Authorization": ""})
        ).status_code == 401


async def test_chat_body_validation_from_legacy(client, legacy):
    for case in legacy("chat-validation"):
        if case["status"] == 400:
            response = await client.post("/", content=case["body"] or b"")
            assert response.status_code == 400 and response.json() == case["error"]


async def test_upload_legacy_document_sidecar_and_partial_write(client, api, legacy, monkeypatch):
    from kb_assistant.api import uploads

    data = legacy("document")
    monkeypatch.setattr(uploads, "uuid4", lambda: data["document"]["documentId"])
    monkeypatch.setattr(
        uploads, "now", lambda: datetime.fromisoformat(data["document"]["uploadedAt"])
    )
    response = await client.post(
        "/v1/uploads", json={"filename": "a.txt", "contentType": "text/plain", "sizeBytes": 123}
    )
    assert response.status_code == 201
    doc = await api[1].store.get_document(response.json()["documentId"])
    assert doc == data["document"]
    assert api[1].blobs.sidecars[doc["s3Key"]] == data["sidecarBody"]
    assert response.json() == {
        "documentId": doc["documentId"],
        "key": doc["s3Key"],
        "uploadUrl": "https://signed.example/put",
        "expiresIn": 300,
    }
    monkeypatch.setattr(uploads, "uuid4", lambda: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    api[1].blobs.put_sidecar = AsyncMock(side_effect=RuntimeError("sidecar failed"))
    response = await client.post(
        "/v1/uploads", json={"filename": "a.txt", "contentType": "text/plain", "sizeBytes": 123}
    )
    assert response.status_code == 500
    assert (await api[1].store.get_document("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))[
        "status"
    ] == "UPLOADING"


async def test_upload_validation_routes(client, legacy):
    for case in legacy("uploads"):
        if case["status"] == 400:
            response = (
                await client.post("/v1/uploads", json=case["input"])
                if case["input"] is not None
                else await client.post("/v1/uploads", content="null")
            )
            assert response.status_code == 400 and response.json() == case["error"]


async def test_legacy_routes_and_continue_session(client, api, table, legacy):
    data = seed(table, legacy)
    sid = data["session"]["sessionId"]
    sessions = (await client.get("/v1/sessions")).json()
    assert sessions["items"][0]["sessionId"] == sid
    detail = (await client.get(f"/v1/sessions/{sid}?limit=1")).json()
    assert len(detail["messages"]) == 1 and "nextToken" in detail
    older = (
        await client.get(f"/v1/sessions/{sid}", params={"nextToken": detail["nextToken"]})
    ).json()
    assert len(older["messages"]) == 2
    assert len((await client.get("/v1/documents")).json()["items"]) == 1
    tokens = legacy("paging")
    for path, token in [
        ("/v1/sessions", tokens[0]["token"]),
        (f"/v1/sessions/{sid}", tokens[1]["token"]),
        ("/v1/documents", tokens[2]["token"]),
    ]:
        assert (await client.get(path, params={"nextToken": token})).status_code == 200
    response = await client.post("/", json={"message": "next", "sessionId": sid})
    assert (
        response.status_code == 200
        and response.text.startswith("event: session\n")
        and "event: done\n" in response.text
    )
    detail = (await client.get(f"/v1/sessions/{sid}")).json()
    assert detail["messageCount"] == 2  # Legacy fixture starts with count zero.
    assert [r["content"] for r in detail["messages"]][-2:] == ["next", "answer"]
    assert len(api[1].model.calls[0][1]) == 3  # Old complete pair, then the question.


async def test_session_json_preserves_utf16_slices(client, api, legacy):
    from kb_assistant.core.citations import js_slice

    meta = dict(legacy("items")["session"], title=js_slice("a" * 59 + "😀", 60))
    message = dict(legacy("items")["messages"][1]["item"])
    message["citations"] = [
        dict(message["citations"][0], snippet=js_slice("b" * 239 + "😀", 240))
    ]
    api[1].store.list_sessions = AsyncMock(return_value={"items": [meta]})
    api[1].store.get_session = AsyncMock(return_value=meta)
    api[1].store.list_messages = AsyncMock(return_value={"items": [message]})

    listing = await client.get("/v1/sessions")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["title"] == meta["title"]
    detail = await client.get(f"/v1/sessions/{meta['sessionId']}")
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["citations"] == message["citations"]
    assert b"\\ud83d" in listing.content and b"\\ud83d" in detail.content


async def test_session_ownership_precedes_query_and_validation(client, api, table, legacy):
    data = seed(table, legacy)
    sid = data["session"]["sessionId"]
    table.put_item(Item=to_ddb(dict(data["session"], userSub="foreign")))
    api[1].store.list_messages = AsyncMock(side_effect=AssertionError("must not query"))
    response = await client.get(f"/v1/sessions/{sid}?limit=bad")
    assert response.status_code == 403 and response.json() == {"message": "Forbidden"}
    assert (await client.post("/", json={"sessionId": sid, "message": "q"})).status_code == 403
    assert (await client.get("/v1/sessions/missing")).status_code == 404
    assert (
        await client.post(
            "/", json={"sessionId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "message": "q"}
        )
    ).status_code == 404


@pytest.mark.parametrize("limit", ["0", "101", "1.5", "bad", "", "١", "１２", "1e٢", "0x１２"])
async def test_bad_limits_and_tokens(client, limit):
    for path in ["/v1/documents", "/v1/sessions"]:
        assert (await client.get(path, params={"limit": limit})).status_code == 400
        assert (await client.get(path, params={"nextToken": "bad"})).json() == {
            "message": "Malformed nextToken"
        }


@pytest.mark.parametrize("status", ["UPLOADING", "PENDING", "INGESTING", "READY", "FAILED"])
async def test_document_download_and_removal_states(client, api, table, legacy, status):
    doc = dict(legacy("document")["document"], status=status)
    table.put_item(Item=to_ddb(doc))
    path = "/v1/documents/" + doc["documentId"]
    response = await client.get(path + "/download")
    assert response.status_code == (200 if status == "READY" else 409)
    if status == "READY":
        assert response.json() == {"url": "https://signed.example/get"}
    response = await client.delete(path)
    assert response.status_code == (204 if status == "FAILED" else 409)
    if status == "FAILED":
        assert response.content == b""
        assert set(api[1].blobs.deleted) == {doc["s3Key"], doc["s3Key"] + ".metadata.json"}
    else:
        assert api[1].blobs.deleted == []


async def test_document_missing_race_and_best_effort_delete(client, api, table, legacy):
    assert (await client.get("/v1/documents/missing/download")).status_code == 404
    assert (await client.delete("/v1/documents/missing")).status_code == 404
    doc = dict(legacy("document")["document"], status="FAILED")
    table.put_item(Item=to_ddb(doc))
    path = "/v1/documents/" + doc["documentId"]
    original = api[1].store.delete_failed_document
    api[1].store.delete_failed_document = AsyncMock(return_value=False)
    assert (await client.delete(path)).status_code == 409 and api[1].blobs.deleted == []
    api[1].store.delete_failed_document = original
    api[1].blobs.delete = AsyncMock(side_effect=RuntimeError("outage"))
    assert (await client.delete(path)).status_code == 204
    assert await api[1].store.get_document(doc["documentId"]) is None


@pytest.mark.parametrize(
    "stage", ["create", "history", "user", "agent", "partial", "completion", "failure_row"]
)
async def test_chat_failure_matrix(client, api, table, legacy, monkeypatch, stage):
    from kb_assistant.api import chat

    data = seed(table, legacy)
    store = api[1].store
    sid = data["session"]["sessionId"]
    payload = {"message": "q", "sessionId": sid}
    boom = RuntimeError("private details")
    if stage == "create":
        payload.pop("sessionId")
        store.create_session = AsyncMock(side_effect=boom)
    elif stage == "history":
        store.list_messages = AsyncMock(side_effect=boom)
    elif stage == "user":
        store.persist_submitted_message = AsyncMock(side_effect=boom)
    elif stage == "completion":
        original = store.persist_completed_turn
        attempts = []

        async def persist(item):
            attempts.append(item)
            if len(attempts) == 1:
                raise boom
            await original(item)

        store.persist_completed_turn = persist
    else:
        error = (
            AgentStreamError(
                "modelStreamErrorException",
                dict(
                    text="partial",
                    citations=[],
                    usage={"inputTokens": 2, "outputTokens": 3},
                    stopReason="error",
                ),
            )
            if stage == "partial"
            else boom
        )
        monkeypatch.setattr(chat, "run_agent", AsyncMock(side_effect=error))
        if stage == "failure_row":
            store.persist_completed_turn = AsyncMock(side_effect=boom)
    response = await client.post("/v1/chat", json=payload)
    if stage == "create":
        assert response.status_code == 500 and response.json() == {
            "message": "Internal server error"
        }
        return
    assert response.status_code == 200 and response.text.startswith("event: session\n")
    assert (
        "event: error\n" in response.text
        and "event: done\n" not in response.text
        and "private details" not in response.text
    )
    rows = [
        r
        for r in table.scan()["Items"]
        if r.get("role") == "assistant" and r.get("turnId") not in ["turn1", None]
    ]
    assert len(rows) == (0 if stage in ["history", "user", "failure_row"] else 1)
    if rows:
        assert rows[0]["stopReason"] == "error"
        assert rows[0]["content"] == (
            "partial"
            if stage == "partial"
            else "The request could not be completed. Please try again."
        )
    if stage == "partial":
        assert "The model response stream ended unexpectedly. Please try again." in response.text


async def test_new_chat_session_first_done_last_and_timestamp(client, api, monkeypatch):
    from kb_assistant.api import chat

    fixed = datetime.fromisoformat("2026-01-01T00:00:00.123+00:00")
    monkeypatch.setattr(chat, "now", lambda: fixed)
    response = await client.post("/", json={"message": " hello  world "})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert [frame.splitlines()[0] for frame in response.text.strip().split("\n\n")] == [
        "event: session",
        "event: text",
        "event: done",
    ]
    page = await api[1].store.list_sessions("owner", 25, None)
    session = page["items"][0]
    assert session["title"] == "hello world" and session["messageCount"] == 2
    rows = (await api[1].store.list_messages(session["sessionId"], 50))["items"]
    assert rows[0]["turnId"] == rows[1]["turnId"]
    assert rows[1]["createdAt"] == "2026-01-01T00:00:00.124Z"


async def test_new_session_readable_after_agent_failure(client, api, monkeypatch):
    from kb_assistant.api import chat

    monkeypatch.setattr(chat, "run_agent", AsyncMock(side_effect=RuntimeError("private failure")))
    response = await client.post("/", json={"message": "question"})
    assert response.status_code == 200 and "event: error" in response.text
    session = (await client.get("/v1/sessions")).json()["items"][0]
    detail = (await client.get("/v1/sessions/" + session["sessionId"])).json()
    assert detail["messageCount"] == 2
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert (
        detail["messages"][1]["content"] == "The request could not be completed. Please try again."
    )


async def test_cursor_validation_order_matches_legacy(client, table, legacy):
    data = seed(table, legacy)
    for path in ["/v1/sessions", "/v1/sessions/" + data["session"]["sessionId"]]:
        response = await client.get(path, params={"limit": "bad", "nextToken": "bad"})
        assert response.json() == {"message": "Malformed nextToken"}
    response = await client.get("/v1/documents", params={"limit": "bad", "nextToken": "bad"})
    assert response.json() == {"message": "limit must be between 1 and 100"}


async def test_cors_only_when_configured():
    app = create_app(Settings(cors_allow_origins="https://frontend.example"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.options(
            "/v1/chat",
            headers={
                "Origin": "https://frontend.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://frontend.example"


async def test_aws_failure_is_logged_by_its_service_code(client, monkeypatch, capsys):
    from botocore.exceptions import ClientError

    from kb_assistant.api import chat

    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized to ApplyGuardrail"}},
        "ConverseStream",
    )
    monkeypatch.setattr(chat, "run_agent", AsyncMock(side_effect=denied))
    response = await client.post("/", json={"message": "question"})
    assert "event: error" in response.text and "ApplyGuardrail" not in response.text
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    failure = next(r for r in records if r["msg"] == "chat request failed after stream commit")
    assert failure["errorType"] == "AccessDeniedException"
    assert failure["errorMessage"] == "not authorized to ApplyGuardrail"
