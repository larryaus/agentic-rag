import json
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from test_api import api, client  # noqa: F401

from kb_assistant.app import create_app
from kb_assistant.settings import Settings


@pytest.mark.parametrize(
    "routes,properties",
    [
        ("chat", {"store", "model", "retriever", "guardrail", "verifier"}),
        ("uploads", {"store", "blobs", "verifier"}),
        ("documents", {"store", "blobs", "verifier"}),
        ("sessions", {"store", "verifier"}),
    ],
)
async def test_route_scoped_lifespan_initializes_only_needed_adapters(
    routes, properties, monkeypatch
):
    settings = Settings(
        kb_routes=routes,
        table_name="test-table",
        docs_bucket="docs",
        knowledge_base_id="kb",
        chat_model_id="model",
        guardrail_id="guard",
        guardrail_version="1",
        oidc_issuer="https://issuer.invalid",
        oidc_client_id="client",
    )

    # Constructors must not fetch JWKS or contact any service during Lambda init.
    def forbidden(*args, **kwargs):
        raise AssertionError("Network attempted during startup")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert set(vars(app.state.container)) - {"settings", "local", "_pool"} == properties
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.get("/healthz")).status_code == 200
            for group, path, method in [
                ("chat", "/", "POST"),
                ("chat", "/v1/chat", "POST"),
                ("uploads", "/v1/uploads", "POST"),
                ("documents", "/v1/documents", "GET"),
                ("sessions", "/v1/sessions", "GET"),
            ]:
                response = await client.request(method, path)
                assert response.status_code == (401 if group == routes else 404)


@pytest.mark.parametrize(
    "header", [None, "{", '{"request_id":"not-an-id"}', "[" * 10000, '{"request_id": 7}']
)
async def test_request_id_fallback_is_uuid(header, client, capsys):  # noqa: F811
    response = await client.get(
        "/healthz", headers={"x-amzn-lambda-context": header} if header else {}
    )
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert UUID(records[0]["requestId"]) and not captured.err


async def test_unhandled_error_logged_once_without_escape(api, capsys):  # noqa: F811
    app, deps = api
    deps.store.list_sessions = AsyncMock(side_effect=RuntimeError("arn:secret:must-not-log"))
    request_id = "18cf2d3d-61ba-44c0-bb17-2f22b1050c45"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=True), base_url="http://test"
    ) as session:
        response = await session.get(
            "/v1/sessions",
            headers={
                "Authorization": "Bearer owner",
                "x-amzn-lambda-context": json.dumps(
                    {"request_id": request_id, "invoked_function_arn": "arn:hidden"}
                ),
                "x-private": "never-log-header",
            },
        )
    assert response.status_code == 500 and response.json() == {"message": "Internal server error"}
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    errors = [row for row in records if row["level"] == "error"]
    assert len(errors) == 1 and errors[0]["errorType"] == "RuntimeError"
    assert all(row["requestId"] == request_id for row in records)
    assert not captured.err
    assert all(secret not in captured.out for secret in ["arn:", "Traceback", "never-log-header"])
