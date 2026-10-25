import json
from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI

from kb_assistant.core.ports import ModelEvent, Stop, TextDelta, ToolCall, Usage
from kb_assistant.core.text import js_trim, reject_json_constant
from kb_assistant.core.transcript import AssistantTurn, TextBlock, ToolUseBlock, Turn, UserText
from kb_assistant.errors import ModelStreamError, error_message

FINISH_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filtered",
}


def openai_messages(system: str, transcript: list[Turn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in transcript:
        if isinstance(turn, UserText):
            messages.append({"role": "user", "content": turn.text})
        elif isinstance(turn, AssistantTurn):
            # Chat Completions cannot interleave text and tool calls in one message, so
            # text blocks are joined in order and tool calls follow in block order.
            text = "".join(block.text for block in turn.blocks if isinstance(block, TextBlock))
            calls = [block for block in turn.blocks if isinstance(block, ToolUseBlock)]
            message: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.input)},
                    }
                    for call in calls
                ]
            messages.append(message)
        else:
            for result in turn.results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.id,
                        "content": json.dumps(result.json_payload, ensure_ascii=False)
                        if result.json_payload is not None
                        else result.text or "",
                    }
                )
    return messages


def openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


def parse_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    try:
        value = json.loads(raw, parse_constant=reject_json_constant) if js_trim(raw) else {}
        if not isinstance(value, dict):
            raise ValueError("Tool input must be a JSON object")
        return value, None
    except ValueError as exc:
        return {}, f"Tool input was not a valid JSON object: {error_message(exc)}"


def implied_index(calls: dict[int, dict[str, str]], call_id: str | None, active: int) -> int:
    """For servers that omit the index: an id names its call; no id continues the active one."""
    if not calls:
        return 0
    if call_id:
        known = next((index for index, call in calls.items() if call["id"] == call_id), None)
        if known is not None:
            return known
        # An id arriving for a call that has none yet names that call.
        return active if not calls[active]["id"] else max(calls) + 1
    return active


class OpenAIChat:
    """Chat Completions streaming against any OpenAI-compatible server (OpenAI, Ollama...)."""

    def __init__(self, model: str, *, client: Any) -> None:
        self.model = model
        self.client: AsyncOpenAI = client

    async def _create(self, **request: Any) -> Any:
        try:
            return await self.client.chat.completions.create(**request)
        except openai.BadRequestError as exc:
            # Some compatible servers reject stream_options; usage then reports as zero.
            if "stream_options" not in str(exc):
                raise
            request.pop("stream_options", None)
            return await self.client.chat.completions.create(**request)

    async def complete(
        self, system: str, user: str, max_tokens: int = 512, timeout: float = 20
    ) -> str:
        # A short call of its own: a slow classifier must not hold up the whole turn.
        client = self.client.with_options(timeout=timeout, max_retries=0)
        response = await client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0,
        )
        choice = response.choices[0]
        if choice.finish_reason == "length":
            # A truncated reply may hold a quoted example rather than the verdict.
            raise RuntimeError("Classifier reply was cut off")
        return choice.message.content or ""

    async def stream(
        self, system: str, transcript: list[Turn], tools: list[dict[str, Any]], max_tokens: int
    ) -> AsyncIterator[ModelEvent]:
        stream = await self._create(
            model=self.model,
            messages=openai_messages(system, transcript),
            tools=openai_tools(tools),
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        calls: dict[int, dict[str, str]] = {}
        active = 0
        finish: str | None = None
        usage = (0, 0)
        try:
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = (chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0)
                for choice in chunk.choices:
                    delta = choice.delta
                    if delta is not None and delta.content:
                        yield TextDelta(0, delta.content)
                    for call in (delta.tool_calls or []) if delta is not None else []:
                        index = (
                            call.index
                            if call.index is not None
                            else implied_index(calls, call.id, active)
                        )
                        active = index
                        entry = calls.setdefault(index, dict(id="", name="", raw=""))
                        if call.id and not entry["id"]:
                            entry["id"] = call.id
                        if call.function is not None:
                            if call.function.name and not entry["name"]:
                                entry["name"] = call.function.name
                            entry["raw"] += call.function.arguments or ""
                    if choice.finish_reason:
                        finish = choice.finish_reason
        except Exception as exc:
            raise ModelStreamError(error_message(exc)) from exc
        finally:
            # Stops the HTTP response when the turn ends early or is cancelled.
            await stream.close()
        for index in sorted(calls):
            entry = calls[index]
            value, input_error = parse_arguments(entry["raw"])
            yield ToolCall(
                1 + index, entry["id"] or f"call_{index}", entry["name"], value, input_error
            )
        if finish is None:
            # A body that ends without a finish reason was cut off, not completed.
            raise ModelStreamError("Model stream ended without a finish reason")
        reason = FINISH_REASONS.get(finish, finish)
        # Some servers report "stop" even when they returned tool calls; a truncated reply
        # stays truncated, so its possibly cut-off calls are not run.
        if calls and reason not in ("max_tokens", "content_filtered"):
            reason = "tool_use"
        yield Stop(reason)
        yield Usage(*usage)
