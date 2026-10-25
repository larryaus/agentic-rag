from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True
    )


DocumentStatus = Literal["UPLOADING", "PENDING", "INGESTING", "READY", "FAILED"]


class Citation(Contract):
    ref: int
    title: str
    document_id: str
    score: float
    snippet: str


class Usage(Contract):
    input_tokens: int
    output_tokens: int


class DocumentSummary(Contract):
    document_id: str
    title: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    uploaded_at: str


class SessionSummary(Contract):
    session_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class MessageView(Contract):
    role: Literal["user", "assistant"]
    content: str
    citations: list[Citation]
    created_at: str


class SessionDetail(SessionSummary):
    messages: list[MessageView]
    next_token: str | None = None


T = TypeVar("T")


class Page(Contract, Generic[T]):
    items: list[T]
    next_token: str | None = None


class UploadRequest(Contract):
    filename: str
    content_type: str
    size_bytes: int


class UploadResponse(Contract):
    document_id: str
    upload_url: str
    key: str
    expires_in: int


class SessionEvent(Contract):
    type: Literal["session"]
    session_id: str


class ToolUseEvent(Contract):
    type: Literal["tool_use"]
    name: str
    input: dict[str, Any]


class CitationEvent(Citation):
    type: Literal["citation"]


class TextEvent(Contract):
    type: Literal["text"]
    delta: str


class DoneEvent(Contract):
    type: Literal["done"]
    session_id: str
    stop_reason: str
    usage: Usage


class ErrorEvent(Contract):
    type: Literal["error"]
    message: str


SseEvent = Annotated[
    SessionEvent | ToolUseEvent | CitationEvent | TextEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]
