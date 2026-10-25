"""Network-isolated Lambda artifact checks; mounted by the gate, never shipped."""

import asyncio
import base64
import importlib
import json
import pkgutil
import sys
import urllib.error
import urllib.request
from pathlib import Path


def request(path, *, port=8080, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers=headers,
        data=json.dumps(data).encode() if data is not None else None,
    )
    try:
        return urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as response:
        return response


def imports():
    from kb_assistant.adapters import aws, oidc
    from kb_assistant.app import create_app

    for module in pkgutil.iter_modules(aws.__path__, aws.__name__ + "."):
        importlib.import_module(module.name)
    assert oidc.OidcVerifier
    app = create_app()
    for name in [
        "store",
        "blobs",
        "model",
        "retriever",
        "indexer",
        "guardrail",
        "verifier",
    ]:
        assert getattr(app.state.container, name) is not None
    assert "boto3" in sys.modules and "jwt" in sys.modules
    import cryptography.hazmat.bindings._rust
    import pydantic_core
    import regex

    assert cryptography.hazmat.bindings._rust and pydantic_core and regex
    print("All AWS/OIDC adapters, container properties and native imports passed")


def auth():
    # The malformed token exercises PyJWT; the shaped token also attempts JWKS.
    parts = [
        base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        for value in [{"alg": "RS256", "kid": "missing"}, {}]
    ]
    for token in ["x", ".".join([*parts, "AA"])]:
        with request("/v1/documents", token=token) as response:
            assert response.status == 401
            assert json.load(response) == {"message": "Unauthorized"}
    errors = [json.loads(line).get("errorType")
              for line in Path("/tmp/app.log").read_text().splitlines() if line.startswith("{")]
    assert "DecodeError" in errors and "PyJWKClientConnectionError" in errors
    print("Malformed JWT and unreachable JWKS: 401 JSON (network disabled)")


def serve():
    import uvicorn

    from kb_assistant.app import create_app
    from kb_assistant.core.ports import AuthContext, Stop, TextDelta, Usage

    release = asyncio.Event()
    messages = []

    class Store:
        async def create_session(self, item):
            pass

        async def persist_submitted_message(self, item):
            messages.append(item)

        async def persist_completed_turn(self, item):
            messages.append(item)

        async def list_documents(self, *args):
            raise RuntimeError("arn:artifact:private-exception-text")

    class Model:
        async def stream(self, *args):
            yield TextDelta(0, "smoke ")
            await release.wait()
            yield TextDelta(0, "answer")
            yield Stop("end_turn")
            yield Usage(1, 2)

    class Verifier:
        async def verify(self, header):
            assert header == "Bearer artifact"
            return AuthContext("artifact", "artifact")

    app = create_app()
    deps = app.state.container
    deps.store, deps.model, deps.verifier = Store(), Model(), Verifier()

    @app.post("/smoke/release")
    async def release_model():
        release.set()
        return {"status": "ok"}

    @app.get("/smoke/state")
    async def state():
        return messages

    uvicorn.run(app, host="127.0.0.1", port=8081, access_log=False)


def stream():
    with request(
        "/", port=8081, data={"message": "smoke"}, token="artifact"
    ) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")

        def frame():
            kind = response.readline().decode().strip().removeprefix("event: ")
            payload = json.loads(response.readline().decode().removeprefix("data: "))
            assert response.readline() == b"\n"
            return kind, payload

        kind, session = frame()
        assert kind == "session" and session["sessionId"]
        assert frame() == ("text", {"delta": "smoke "})
        # The model cannot complete until the client has received the first text frame.
        with request("/smoke/release", port=8081, data={}) as released:
            assert released.status == 200
        assert frame() == ("text", {"delta": "answer"})
        assert frame() == (
            "done",
            {
                "sessionId": session["sessionId"],
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 2},
            },
        )
        assert response.read() == b""
    with request("/smoke/state", port=8081) as response:
        messages = json.load(response)
        assert [row["role"] for row in messages] == ["user", "assistant"]
        assert messages[1]["content"] == "smoke answer"
    with request("/v1/documents", port=8081, token="artifact") as response:
        assert response.status == 500
        assert json.load(response) == {"message": "Internal server error"}
    logs = Path("/tmp/smoke.log").read_text()
    errors = [json.loads(line) for line in logs.splitlines() if line.startswith("{")]
    errors = [row for row in errors if row["level"] == "error"]
    assert len(errors) == 1 and errors[0]["errorType"] == "RuntimeError"
    assert "Traceback" not in logs and "arn:artifact" not in logs
    print(
        "SSE over HTTP: session/text/done, incremental delivery and persisted assistant passed"
    )
    print(
        "Unhandled 500: one structured error, no uvicorn traceback or exception message"
    )


if __name__ == "__main__":
    {"imports": imports, "auth": auth, "serve": serve, "stream": stream}[sys.argv[1]]()
