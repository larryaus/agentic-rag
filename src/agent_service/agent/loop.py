"""Streaming tool-use loop driving Claude with the RAG tools.

The loop:
  1. loads conversation history from db
  2. appends user message
  3. iterates messages.stream until stop_reason != "tool_use"
     forwarding text deltas + tool_use + citation events to SSE
  4. persists user msg + new assistant/tool turns in one transaction
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import anthropic
import asyncpg

from agent_service.agent import tools as agent_tools
from agent_service.agent.citations import find_citation_tokens
from agent_service.agent.prompts import SYSTEM_PROMPT
from agent_service.config import settings
from agent_service.db import repo
from agent_service.sse import SSEChannel


_SYSTEM_BLOCKS = [
    {
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }
]


def _serialize_blocks(blocks: list[Any]) -> list[dict]:
    """Convert SDK content blocks to plain dicts the API will re-accept."""
    out = []
    for b in blocks:
        if isinstance(b, dict):
            out.append(b)
            continue
        t = getattr(b, "type", None)
        if t == "text":
            out.append({"type": "text", "text": b.text})
        elif t == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": b.id,
                    "name": b.name,
                    "input": b.input,
                }
            )
    return out


def _assistant_text(blocks: list[dict]) -> str:
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


async def run_streaming(
    *,
    client: anthropic.AsyncAnthropic,
    pool: asyncpg.Pool,
    sse: SSEChannel,
    session_id: UUID,
    user_text: str,
    document_ids: list[int] | None = None,
) -> None:
    async with pool.acquire() as conn:
        history = await repo.load_messages_for_anthropic(conn, session_id)

    history.append({"role": "user", "content": user_text})
    new_rows: list[tuple[str, Any, list[dict]]] = [
        ("user", user_text, [])
    ]

    citation_index: dict[tuple[int, int], dict] = {}
    tools_payload = agent_tools.tools_with_cache()

    for _turn in range(settings.MAX_TOOL_TURNS):
        async with client.messages.stream(
            model=settings.MAIN_MODEL,
            max_tokens=2048,
            system=_SYSTEM_BLOCKS,
            tools=tools_payload,
            messages=history,
        ) as stream:
            async for chunk in stream.text_stream:
                if chunk:
                    await sse.send("text", {"delta": chunk})
            final = await stream.get_final_message()

        assistant_blocks = _serialize_blocks(final.content)
        history.append({"role": "assistant", "content": assistant_blocks})

        # citations referenced in this assistant turn's text
        text = _assistant_text(assistant_blocks)
        turn_citations: list[dict] = []
        for doc_id, ord_ in find_citation_tokens(text):
            key = (doc_id, ord_)
            if key in citation_index:
                turn_citations.append(citation_index[key])

        new_rows.append(("assistant", assistant_blocks, turn_citations))

        if final.stop_reason != "tool_use":
            break

        tool_result_blocks: list[dict] = []
        for block in assistant_blocks:
            if block.get("type") != "tool_use":
                continue
            await sse.send(
                "tool_use",
                {"name": block["name"], "input": block.get("input", {})},
            )
            try:
                result = await agent_tools.dispatch(block["name"], block.get("input", {}))
            except Exception as exc:  # surface errors back to the model
                result = {"error": f"{type(exc).__name__}: {exc}"}

            if block["name"] == "search_knowledge_base":
                for hit in result.get("hits", []):
                    key = (hit["document_id"], hit["ord"])
                    citation_index[key] = {
                        "document_id": hit["document_id"],
                        "chunk_id": hit["chunk_id"],
                        "ord": hit["ord"],
                        "title": hit["title"],
                        "snippet": hit["snippet"],
                        "score": hit.get("score"),
                    }
                    await sse.send("citation", citation_index[key])

            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        if tool_result_blocks:
            history.append({"role": "user", "content": tool_result_blocks})
            new_rows.append(("user", tool_result_blocks, []))

    async with pool.acquire() as conn:
        async with conn.transaction():
            for role, content, cits in new_rows:
                await repo.append_message(
                    conn,
                    session_id=session_id,
                    role=role,
                    content=content,
                    citations=cits,
                )
            await repo.touch_session(conn, session_id)

    await sse.send(
        "done",
        {"session_id": str(session_id), "stop_reason": final.stop_reason},
    )
