"""Streaming tool-use loop driving ChatGPT with the RAG tools.

The loop:
  1. loads conversation history from db
  2. appends user message
  3. iterates chat.completions streaming until finish_reason != "tool_calls"
     forwarding text deltas + tool_call + citation events to SSE
  4. persists user msg + new assistant/tool turns in one transaction
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
from openai import AsyncOpenAI

from agent_service.agent import tools as agent_tools
from agent_service.agent.citations import find_citation_tokens
from agent_service.agent.prompts import SYSTEM_PROMPT
from agent_service.config import settings
from agent_service.db import repo
from agent_service.sse import SSEChannel
from agent_service.transcript import (
    assistant_for_storage,
    assistant_text,
    parse_tool_args,
    tool_for_storage,
    tool_message,
)


@dataclass
class PendingMessage:
    role: str
    content: Any
    citations: list[dict]


async def _stream_chat_completion(
    *,
    client: AsyncOpenAI,
    messages: list[dict[str, Any]],
    sse: SSEChannel,
    tools_payload: list[dict[str, Any]],
) -> tuple[dict[str, Any], str | None]:
    stream = await client.chat.completions.create(
        model=settings.MAIN_MODEL,
        max_completion_tokens=2048,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
        tools=tools_payload,
        tool_choice="auto",
        stream=True,
    )

    text_parts: list[str] = []
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None

    async for chunk in stream:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        finish_reason = choice.finish_reason or finish_reason
        delta = choice.delta

        content_delta = getattr(delta, "content", None)
        if content_delta:
            text_parts.append(content_delta)
            await sse.send("text", {"delta": content_delta})

        for tool_delta in getattr(delta, "tool_calls", None) or []:
            _accumulate_tool_delta(tool_calls_by_index, tool_delta)

    tool_calls = [
        call for _, call in sorted(tool_calls_by_index.items(), key=lambda item: item[0])
    ]
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    if tool_calls:
        assistant_message["tool_calls"] = tool_calls
    return assistant_message, finish_reason


def _accumulate_tool_delta(
    tool_calls_by_index: dict[int, dict[str, Any]], tool_delta: Any
) -> None:
    index = getattr(tool_delta, "index", 0)
    acc = tool_calls_by_index.setdefault(
        index,
        {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if getattr(tool_delta, "id", None):
        acc["id"] = tool_delta.id
    if getattr(tool_delta, "type", None):
        acc["type"] = tool_delta.type

    fn_delta = getattr(tool_delta, "function", None)
    if fn_delta is None:
        return
    if getattr(fn_delta, "name", None):
        acc["function"]["name"] += fn_delta.name
    if getattr(fn_delta, "arguments", None):
        acc["function"]["arguments"] += fn_delta.arguments


async def run_streaming(
    *,
    client: AsyncOpenAI,
    pool: asyncpg.Pool,
    sse: SSEChannel,
    session_id: UUID,
    user_text: str,
    document_ids: list[int] | None = None,
) -> None:
    async with pool.acquire() as conn:
        history = await repo.load_messages_for_openai(conn, session_id)

    history.append({"role": "user", "content": user_text})
    pending_messages = [PendingMessage("user", user_text, [])]

    citation_index: dict[tuple[int, int], dict] = {}
    tools_payload = agent_tools.tools_for_openai()
    finish_reason: str | None = None

    for _turn in range(settings.MAX_TOOL_TURNS):
        assistant_message, finish_reason = await _stream_chat_completion(
            client=client,
            messages=history,
            sse=sse,
            tools_payload=tools_payload,
        )
        history.append(assistant_message)

        pending_messages.append(
            PendingMessage(
                "assistant",
                assistant_for_storage(assistant_message),
                _citations_for_text(assistant_text(assistant_message), citation_index),
            )
        )

        tool_calls = assistant_message.get("tool_calls") or []
        if finish_reason != "tool_calls" or not tool_calls:
            break

        await _run_tool_calls(
            tool_calls=tool_calls,
            document_ids=document_ids,
            sse=sse,
            history=history,
            pending_messages=pending_messages,
            citation_index=citation_index,
        )

    await _persist_messages(pool, session_id, pending_messages)
    await sse.send(
        "done",
        {"session_id": str(session_id), "stop_reason": finish_reason},
    )


def _citations_for_text(
    text: str, citation_index: dict[tuple[int, int], dict]
) -> list[dict]:
    turn_citations: list[dict] = []
    for doc_id, ord_ in find_citation_tokens(text):
        key = (doc_id, ord_)
        if key in citation_index:
            turn_citations.append(citation_index[key])
    return turn_citations


async def _run_tool_calls(
    *,
    tool_calls: list[dict[str, Any]],
    document_ids: list[int] | None,
    sse: SSEChannel,
    history: list[dict[str, Any]],
    pending_messages: list[PendingMessage],
    citation_index: dict[tuple[int, int], dict],
) -> None:
    for tool_call in tool_calls:
        function = tool_call.get("function") or {}
        name = function.get("name") or ""
        args = parse_tool_args(function.get("arguments") or "")
        if name == "search_knowledge_base" and document_ids and "document_ids" not in args:
            args["document_ids"] = document_ids

        await sse.send("tool_use", {"name": name, "input": args})
        result = await _dispatch_tool(name, args)

        if name == "search_knowledge_base":
            await _index_search_citations(result, citation_index, sse)

        result_json = json.dumps(result, ensure_ascii=False)
        history.append(tool_message(tool_call["id"], result_json))
        pending_messages.append(
            PendingMessage(
                "tool",
                tool_for_storage(tool_call["id"], name, result_json),
                [],
            )
        )


async def _dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if "_invalid_json" in args:
            return {"error": f"invalid tool arguments: {args['_invalid_json']}"}
        return await agent_tools.dispatch(name, args)
    except Exception as exc:  # surface errors back to the model
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _index_search_citations(
    result: dict[str, Any],
    citation_index: dict[tuple[int, int], dict],
    sse: SSEChannel,
) -> None:
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


async def _persist_messages(
    pool: asyncpg.Pool,
    session_id: UUID,
    pending_messages: list[PendingMessage],
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            for message in pending_messages:
                await repo.append_message(
                    conn,
                    session_id=session_id,
                    role=message.role,
                    content=message.content,
                    citations=message.citations,
                )
            await repo.touch_session(conn, session_id)
