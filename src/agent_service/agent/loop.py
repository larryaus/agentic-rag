"""Streaming tool-use loop driving ChatGPT with the RAG tools.

`run_streaming` is an async generator that yields SSE event dicts:
  - "text"     : token deltas from the model
  - "tool_use" : tool invocation start
  - "citation" : a search hit referenced by the upcoming text
  - "done"     : final frame with stop_reason

It also persists user / assistant / tool turns to the DB in one
transaction at the end of each request.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator
from uuid import UUID

import asyncpg
from openai import AsyncOpenAI

from agent_service.agent import tools as agent_tools
from agent_service.agent.citations import find_citation_tokens
from agent_service.agent.prompts import SYSTEM_PROMPT
from agent_service.config import settings
from agent_service.db import repo


def _sse(event: str, data: Any) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


def _parse_tool_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_invalid_json": raw}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def _accumulate_tool_delta(
    tool_calls_by_index: dict[int, dict[str, Any]], tool_delta: Any
) -> None:
    index = getattr(tool_delta, "index", 0)
    acc = tool_calls_by_index.setdefault(
        index,
        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
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
    session_id: UUID,
    user_text: str,
    document_ids: list[int] | None = None,
) -> AsyncIterator[dict[str, str]]:
    async with pool.acquire() as conn:
        history = await repo.load_messages_for_openai(conn, session_id)

    history.append({"role": "user", "content": user_text})
    new_rows: list[tuple[str, Any, list[dict]]] = [("user", user_text, [])]

    citation_index: dict[tuple[int, int], dict] = {}
    tools_payload = agent_tools.tools_for_openai()
    finish_reason: str | None = None

    for _turn in range(settings.MAX_TOOL_TURNS):
        stream = await client.chat.completions.create(
            model=settings.MAIN_MODEL,
            max_completion_tokens=2048,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *history],
            tools=tools_payload,
            tool_choice="auto",
            stream=True,
        )
        text_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            finish_reason = choice.finish_reason or finish_reason
            delta = choice.delta
            content_delta = getattr(delta, "content", None)
            if content_delta:
                text_parts.append(content_delta)
                yield _sse("text", {"delta": content_delta})
            for tool_delta in getattr(delta, "tool_calls", None) or []:
                _accumulate_tool_delta(tool_calls_by_index, tool_delta)

        tool_calls = [c for _, c in sorted(tool_calls_by_index.items())]
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts) or None,
        }
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        history.append(assistant_message)

        turn_citations: list[dict] = []
        for doc_id, ord_ in find_citation_tokens(assistant_message.get("content") or ""):
            key = (doc_id, ord_)
            if key in citation_index:
                turn_citations.append(citation_index[key])
        persisted_assistant = {k: v for k, v in assistant_message.items() if k != "role"}
        new_rows.append(("assistant", persisted_assistant, turn_citations))

        if finish_reason != "tool_calls" or not tool_calls:
            break

        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            name = function.get("name") or ""
            args = _parse_tool_args(function.get("arguments") or "")
            if name == "search_knowledge_base" and document_ids and "document_ids" not in args:
                args["document_ids"] = document_ids
            yield _sse("tool_use", {"name": name, "input": args})

            try:
                if "_invalid_json" in args:
                    result = {"error": f"invalid tool arguments: {args['_invalid_json']}"}
                else:
                    result = await agent_tools.dispatch(name, args)
            except Exception as exc:
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
                    yield _sse("citation", citation_index[key])

            result_json = json.dumps(result, ensure_ascii=False)
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": result_json,
                }
            )
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

    yield _sse("done", {"session_id": str(session_id), "stop_reason": finish_reason})
