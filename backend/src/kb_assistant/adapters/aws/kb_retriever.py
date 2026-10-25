import asyncio
import re
from typing import Any
from urllib.parse import unquote

import boto3

from kb_assistant.core.citations import UUID_V4
from kb_assistant.core.ports import RawChunk


class KbRetriever:
    def __init__(self, knowledge_base_id: str, *, client: Any = None) -> None:
        self.knowledge_base_id = knowledge_base_id
        self.client: Any = client if client is not None else boto3.client("bedrock-agent-runtime")

    async def retrieve(self, query: str, top_k: int) -> list[RawChunk]:
        response = await asyncio.to_thread(
            self.client.retrieve,
            knowledgeBaseId=self.knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k}},
        )
        chunks = []
        for result in response.get("retrievalResults", []):
            uri = result.get("location", {}).get("s3Location", {}).get("uri", "")
            title = uri.rsplit("/", 1)[-1]
            try:
                if re.search(r"%(?![0-9a-fA-F]{2})", title):
                    raise ValueError("Invalid percent escape")
                title = unquote(title, errors="strict")
            except (ValueError, UnicodeError):
                pass
            document_id = result.get("metadata", {}).get("documentId", "")
            if not isinstance(document_id, str) or not UUID_V4.fullmatch(document_id):
                document_id = ""
            chunks.append(
                RawChunk(
                    result.get("content", {}).get("text", ""),
                    uri,
                    document_id,
                    title,
                    result.get("score", 0),
                )
            )
        return chunks
