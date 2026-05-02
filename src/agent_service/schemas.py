from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class IngestResponse(BaseModel):
    document_id: int
    title: str
    chunk_count: int
    sha256: str
    duplicated: bool = False


class ChatRequest(BaseModel):
    session_id: UUID | None = None
    message: str = Field(min_length=1)
    document_ids: list[int] | None = None
    rerank: bool | None = None


class CitationDTO(BaseModel):
    document_id: int
    chunk_id: int
    ord: int
    title: str
    snippet: str
    score: float | None = None


class DocumentSummary(BaseModel):
    id: int
    title: str
    mime_type: str
    byte_size: int
    chunk_count: int
    created_at: datetime


class MessageDTO(BaseModel):
    role: str
    text: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[CitationDTO] = Field(default_factory=list)
    created_at: datetime


class SessionDetail(BaseModel):
    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageDTO]
