from dataclasses import dataclass
from typing import Any, Protocol

from strands.models import Model

# Chat models are Strands model providers; the agent loop runs on Strands (core/agent.py).
ChatModel = Model


@dataclass
class RawChunk:
    text: str
    source_uri: str
    document_id: str
    title: str
    score: float

    def citation_source(self) -> dict[str, Any]:
        return dict(
            text=self.text,
            sourceUri=self.source_uri,
            documentId=self.document_id,
            title=self.title,
            score=self.score,
        )


@dataclass
class AuthContext:
    sub: str
    username: str


@dataclass
class InputVerdict:
    allowed: bool
    message: str = ""


class Retriever(Protocol):
    async def retrieve(self, query: str, top_k: int) -> list[RawChunk]: ...


class StreamFilter(Protocol):
    def feed(self, text: str) -> str: ...
    def finish(self, success: bool = True) -> str: ...


class Guardrail(Protocol):
    async def check_input(self, text: str) -> InputVerdict: ...
    def output_filter(self) -> StreamFilter: ...


class TokenVerifier(Protocol):
    async def verify(self, authorization_header: str | None) -> AuthContext: ...


class Store(Protocol):
    async def get_session(self, session_id: str) -> dict[str, Any] | None: ...
    async def create_session(self, item: dict[str, Any]) -> None: ...
    async def persist_submitted_message(self, item: dict[str, Any]) -> None: ...
    async def persist_completed_turn(self, item: dict[str, Any]) -> None: ...
    async def list_sessions(
        self, user_sub: str, limit: int, token: str | None
    ) -> dict[str, Any]: ...
    async def list_messages(
        self, session_id: str, limit: int, token: str | None = None
    ) -> dict[str, Any]: ...
    async def get_document(self, document_id: str) -> dict[str, Any] | None: ...
    async def create_document(self, item: dict[str, Any]) -> None: ...
    async def list_documents(self, limit: int, token: str | None) -> dict[str, Any]: ...
    async def all_documents(self) -> list[dict[str, Any]]: ...
    async def delete_failed_document(self, document_id: str) -> bool: ...
    async def transition(
        self,
        document_id: str,
        expected: list[str],
        status: str,
        error: str | None = None,
        recovery_attempt: int | None = None,
    ) -> bool: ...


class BlobStore(Protocol):
    async def presign_put(self, key: str, content_type: str, expires_in: int = 300) -> str: ...
    async def presign_get(self, key: str, expires_in: int = 300) -> str: ...
    async def put_sidecar(self, key: str, body: str) -> None: ...
    async def head(self, key: str) -> int | None: ...
    async def delete(self, key: str) -> None: ...


class Indexer(Protocol):
    async def ingest(self, document_id: str, key: str) -> None: ...
    async def details(self, keys: list[str]) -> list[dict[str, Any]]: ...
