"""Runs the golden dataset against the real local deployment.

The sample documents are uploaded through the app's own API and ingested by its worker,
so the evaluation covers the shipped extraction, chunking, embedding and hybrid search.
Retrieval then queries the same retriever the search tool uses. Needs the backend
(`kb_assistant`) installed and a Postgres server with pgvector.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from kb_evals.models import GoldenRecord
from kb_evals.retrieval import Retrieved

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
TOKEN = "eval"


class EvalVerifier:
    """Accepts the harness's own token, so no sign-in provider is needed."""

    async def verify(self, header: str | None) -> Any:
        from kb_assistant.core.ports import AuthContext
        from kb_assistant.errors import UnauthorizedError

        if header != f"Bearer {TOKEN}":
            raise UnauthorizedError()
        return AuthContext("eval", "eval")


def database_url(server_url: str, name: str) -> str:
    """The server's URL naming another database, with its user, host and parameters kept."""
    return urlsplit(server_url)._replace(path=f"/{name}").geturl()


@asynccontextmanager
async def temporary_database(server_url: str) -> AsyncIterator[str]:
    """A fresh database on the server, dropped afterwards, so earlier runs cannot leak in."""
    import asyncpg

    name = f"kb_eval_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(server_url)
    await admin.execute(f'CREATE DATABASE "{name}"')
    await admin.close()
    try:
        yield database_url(server_url, name)
    finally:
        admin = await asyncpg.connect(server_url)
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@asynccontextmanager
async def local_app(settings: Any, *, openai_client: Any = None) -> AsyncIterator[tuple[Any, Any]]:
    """The local app with its worker running, and an HTTP client signed in to it."""
    import httpx

    from kb_assistant.app import create_app

    app = create_app(settings)
    container = app.state.container
    container.verifier = EvalVerifier()
    if openai_client is not None:
        container.openai_client = openai_client
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://eval",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=300,
        ) as client:
            yield client, container


async def ingest(client: Any, paths: Sequence[Path], timeout: float = 120) -> None:
    """Uploads each file through the API and waits until every document is READY."""
    for path in paths:
        data = path.read_bytes()
        upload = await client.post(
            "/v1/uploads",
            json={"filename": path.name, "contentType": "text/markdown", "sizeBytes": len(data)},
        )
        upload.raise_for_status()
        url = urlsplit(upload.json()["uploadUrl"])
        put = await client.put(
            f"{url.path}?{url.query}",
            content=data,
            headers={"content-type": "text/markdown", "Authorization": ""},
        )
        put.raise_for_status()
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        items = (await client.get("/v1/documents", params={"limit": 100})).json()["items"]
        statuses = {item["title"]: item["status"] for item in items}
        if any(status == "FAILED" for status in statuses.values()):
            raise RuntimeError(f"ingestion failed: {statuses}")
        if len(items) == len(paths) and all(status == "READY" for status in statuses.values()):
            return
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"documents not ready: {statuses}")
        await asyncio.sleep(0.1)


async def retrieve(
    container: Any, records: Sequence[GoldenRecord], top_k: int
) -> dict[str, list[Retrieved]]:
    """Each record's question through the same retriever the search tool uses."""
    ranked = {}
    for record in records:
        chunks = await container.retriever.retrieve(record.question, top_k)
        ranked[record.id] = [Retrieved(chunk.title, chunk.text) for chunk in chunks]
    return ranked


def parse_sse(body: str) -> list[tuple[str, Any]]:
    events = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in lines and "data" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


async def answer(client: Any, question: str) -> dict[str, Any]:
    """One chat turn: the streamed answer, its stop reason, and the citations it kept."""
    response = await client.post("/v1/chat", json={"message": question})
    response.raise_for_status()
    events = parse_sse(response.text)
    if errors := [data for name, data in events if name == "error"]:
        return {"text": "", "stopReason": "error", "citations": [], "error": errors[0]["message"]}
    session_id = next(data["sessionId"] for name, data in events if name == "session")
    done = next(data for name, data in events if name == "done")
    detail = (await client.get(f"/v1/sessions/{session_id}")).json()
    reply = detail["messages"][-1]
    return {
        "text": reply["content"],
        "stopReason": done["stopReason"],
        "citations": [citation["title"] for citation in reply.get("citations", [])],
    }


async def evaluate_answers(client: Any, records: Sequence[GoldenRecord], judge: Any) -> list[Any]:
    """Asks every question, then has the judge check each answer's expected facts."""
    from kb_evals.answers import score_answer

    scores = []
    for record in records:
        reply = await answer(client, record.question)
        supported = (
            [False] * len(record.expected_facts)
            if reply["stopReason"] == "error"
            else await judge(record.question, reply["text"], record.expected_facts)
        )
        scores.append(score_answer(record, reply, supported))
    return scores
