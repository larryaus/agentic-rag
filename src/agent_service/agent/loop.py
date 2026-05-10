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


def _assistant_text(message: dict[str, Any]) -> str:
    return message.get("content") or ""


def _persisted_assistant(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": message.get("content") or "",
        "tool_calls": message.get("tool_calls") or [],
    }


def _parse_tool_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_invalid_json": raw}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


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
                continue
            if getattr(fn_delta, "name", None):
                acc["function"]["name"] += fn_delta.name
            if getattr(fn_delta, "arguments", None):
                acc["function"]["arguments"] += fn_delta.arguments

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
    new_rows: list[tuple[str, Any, list[dict]]] = [
        ("user", user_text, [])
    ]

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

        # citations referenced in this assistant turn's text
        text = _assistant_text(assistant_message)
        turn_citations: list[dict] = []
        for doc_id, ord_ in find_citation_tokens(text):
            key = (doc_id, ord_)
            if key in citation_index:
                turn_citations.append(citation_index[key])

        new_rows.append(("assistant", _persisted_assistant(assistant_message), turn_citations))

        tool_calls = assistant_message.get("tool_calls") or []
        if finish_reason != "tool_calls" or not tool_calls:
            break

        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            name = function.get("name") or ""
            args = _parse_tool_args(function.get("arguments") or "")
            if name == "search_knowledge_base" and document_ids and "document_ids" not in args:
                args["document_ids"] = document_ids

            await sse.send(
                "tool_use",
                {"name": name, "input": args},
            )
            try:
                if "_invalid_json" in args:
                    result = {"error": f"invalid tool arguments: {args['_invalid_json']}"}
                else:
                    result = await agent_tools.dispatch(name, args)
            except Exception as exc:  # surface errors back to the model
                result = {"error": f"{type(exc).__name__}: {exc}"}

            if name == "search_knowledge_base":
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

            result_json = json.dumps(result, ensure_ascii=False)
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": result_json,
            }
            history.append(tool_message)
            new_rows.append(
                (
                    "tool",
                    {
                        "tool_call_id": tool_call["id"],
                        "name": name,
                        "content": result_json,
                    },
                    [],
                )
            )

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
        {"session_id": str(session_id), "stop_reason": finish_reason},
    )
