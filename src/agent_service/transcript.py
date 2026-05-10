"""Adapters for persisted chat transcripts and OpenAI chat messages."""

from __future__ import annotations

import json
from typing import Any


def assistant_text(message: dict[str, Any]) -> str:
    return message.get("content") or ""


def assistant_for_storage(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": message.get("content") or "",
        "tool_calls": message.get("tool_calls") or [],
    }


def parse_tool_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_invalid_json": raw}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def tool_message(tool_call_id: str, content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    }


def tool_for_storage(tool_call_id: str, name: str, content: str) -> dict[str, Any]:
    return {
        "tool_call_id": tool_call_id,
        "name": name,
        "content": content,
    }


def messages_for_openai(raw_messages: list[dict]) -> list[dict[str, Any]]:
    """Return persisted messages in OpenAI Chat Completions format."""
    out: list[dict[str, Any]] = []
    for message in raw_messages:
        converted = _message_for_openai(message["role"], message["content"])
        if converted is None:
            continue
        if isinstance(converted, list):
            out.extend(converted)
        else:
            out.append(converted)
    return out


def visible_message_for_ui(row: dict) -> dict[str, Any] | None:
    role = row["role"]
    content = row["content"]
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, dict):
        if role == "tool":
            return None
        text_parts.append(_assistant_stored_text(content))
        tool_calls.extend(_tool_calls_for_ui(content.get("tool_calls", []) or []))
    elif isinstance(content, list):
        if role == "user" and _is_tool_result_only(content):
            return None
        text, calls = _content_blocks_for_ui(content)
        text_parts.extend(text)
        tool_calls.extend(calls)

    return {
        "role": role,
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
        "citations": row.get("citations") or [],
        "created_at": row["created_at"],
    }


def _message_for_openai(role: str, content: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
    if role == "tool":
        if isinstance(content, dict):
            return tool_message(
                tool_call_id=content.get("tool_call_id", ""),
                content=content.get("content", ""),
            )
        return None

    if role == "assistant":
        if isinstance(content, dict) and (
            "text" in content or "content" in content or "tool_calls" in content
        ):
            msg = {
                "role": "assistant",
                "content": _assistant_stored_text(content) or None,
            }
            if content.get("tool_calls"):
                msg["tool_calls"] = content["tool_calls"]
            return msg

        if isinstance(content, list):
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in _dict_blocks(content):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(_legacy_tool_call_for_openai(block))
            msg = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            return msg

    if role == "user" and isinstance(content, list):
        tool_messages = []
        for block in _dict_blocks(content):
            if block.get("type") != "tool_result":
                continue
            tool_messages.append(
                tool_message(
                    tool_call_id=block.get("tool_use_id", ""),
                    content=block.get("content", ""),
                )
            )
        return tool_messages or None

    return {"role": role, "content": content}


def _assistant_stored_text(content: dict[str, Any]) -> str:
    return content.get("text") or content.get("content") or ""


def _legacy_tool_call_for_openai(block: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": block.get("id", ""),
        "type": "function",
        "function": {
            "name": block.get("name", ""),
            "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
        },
    }


def _tool_calls_for_ui(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for call in calls:
        function = call.get("function") or {}
        args = function.get("arguments") or "{}"
        try:
            parsed_args = json.loads(args)
        except Exception:
            parsed_args = {"_raw": args}
        out.append({"name": function.get("name"), "input": parsed_args})
    return out


def _content_blocks_for_ui(content: list[Any]) -> tuple[list[str], list[dict[str, Any]]]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in _dict_blocks(content):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({"name": block.get("name"), "input": block.get("input", {})})
    return text_parts, tool_calls


def _is_tool_result_only(content: list[Any]) -> bool:
    blocks = _dict_blocks(content)
    return bool(blocks) and all(block.get("type") == "tool_result" for block in blocks)


def _dict_blocks(content: list[Any]) -> list[dict[str, Any]]:
    return [block for block in content if isinstance(block, dict)]
