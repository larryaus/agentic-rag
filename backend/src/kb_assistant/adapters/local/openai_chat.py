from collections.abc import AsyncGenerator
from typing import Any

import openai
from openai import AsyncOpenAI
from strands.models.openai import OpenAIModel
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from kb_assistant.errors import ModelStreamError, error_message

MAX_TOKENS = 2048
FINISH_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filtered",
}


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


def plain_text_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sends text-only content as one string, which every compatible server accepts."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and all(part.get("type") == "text" for part in content):
            message["content"] = "".join(part["text"] for part in content)
    return messages


class OpenAIChat(OpenAIModel):
    """Chat Completions streaming against any OpenAI-compatible server (OpenAI, Ollama...).

    Strands formats the request; the stream is assembled here, because compatible servers
    differ in ways its OpenAI provider does not handle: tool calls without an index or a
    first-fragment id, "stop" reported alongside tool calls, a body that ends without a
    finish reason, and servers that reject stream_options.
    """

    def __init__(self, model: str, *, client: Any) -> None:
        super().__init__(client=client, model_id=model, params={"max_tokens": MAX_TOKENS})
        self.openai: AsyncOpenAI = client
        self.model = model

    async def _create(self, **request: Any) -> Any:
        try:
            return await self.openai.chat.completions.create(**request)
        except openai.BadRequestError as exc:
            # Some compatible servers reject stream_options; usage then reports as zero.
            if "stream_options" not in str(exc):
                raise
            request.pop("stream_options", None)
            return await self.openai.chat.completions.create(**request)

    async def complete(
        self, system: str, user: str, max_tokens: int = 512, timeout: float = 20
    ) -> str:
        # A short call of its own: a slow classifier must not hold up the whole turn.
        client = self.openai.with_options(timeout=timeout, max_retries=0)
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
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        request = self.format_request(
            messages,
            tool_specs,
            system_prompt,
            tool_choice,
            system_prompt_content=kwargs.get("system_prompt_content"),
        )
        request["messages"] = plain_text_content(request["messages"])
        stream = await self._create(**request)
        yield {"messageStart": {"role": "assistant"}}
        calls: dict[int, dict[str, str]] = {}
        active = 0
        text_open = False
        finish: str | None = None
        usage = (0, 0)
        try:
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = (chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0)
                for choice in chunk.choices:
                    delta = choice.delta
                    if delta is not None and delta.content:
                        if not text_open:
                            text_open = True
                            yield {"contentBlockStart": {"start": {}}}
                        yield {"contentBlockDelta": {"delta": {"text": delta.content}}}
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
        if text_open:
            yield {"contentBlockStop": {}}
        for index in sorted(calls):
            entry = calls[index]
            yield {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "toolUseId": entry["id"] or f"call_{index}",
                            # A nameless call comes back to the model as an unknown tool.
                            "name": entry["name"] or "unnamed_tool",
                        }
                    }
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": entry["raw"]}}}}
            yield {"contentBlockStop": {}}
        if finish is None:
            # A body that ends without a finish reason was cut off, not completed.
            raise ModelStreamError("Model stream ended without a finish reason")
        reason = FINISH_REASONS.get(finish, finish)
        # Some servers report "stop" even when they returned tool calls; a truncated reply
        # stays truncated, so its possibly cut-off calls are not run.
        if calls and reason not in ("max_tokens", "content_filtered"):
            reason = "tool_use"
        yield {"messageStop": {"stopReason": reason}}  # type: ignore[typeddict-item]
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": usage[0],
                    "outputTokens": usage[1],
                    "totalTokens": usage[0] + usage[1],
                },
                "metrics": {"latencyMs": 0},
            }
        }
