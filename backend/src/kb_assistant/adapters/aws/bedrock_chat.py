import asyncio
import json
import threading
from collections.abc import AsyncIterator
from typing import Any

import boto3

from kb_assistant.core.ports import ModelEvent, Stop, TextDelta, ToolCall, Usage
from kb_assistant.core.text import js_trim, reject_json_constant
from kb_assistant.core.transcript import AssistantTurn, TextBlock, Turn, UserText
from kb_assistant.errors import ModelStreamError, error_message

EXCEPTIONS = (
    "internalServerException",
    "modelStreamErrorException",
    "validationException",
    "throttlingException",
    "serviceUnavailableException",
)


def bedrock_messages(transcript: list[Turn]) -> list[dict[str, Any]]:
    messages = []
    for turn in transcript:
        content: list[dict[str, Any]]
        if isinstance(turn, UserText):
            content = (
                [{"guardContent": {"text": {"text": turn.text}}}]
                if turn.guard
                else [{"text": turn.text}]
            )
            messages.append({"role": "user", "content": content})
        elif isinstance(turn, AssistantTurn):
            content = []
            for block in turn.blocks:
                content.append(
                    {"text": block.text}
                    if isinstance(block, TextBlock)
                    else {
                        "toolUse": {"toolUseId": block.id, "name": block.name, "input": block.input}
                    }
                )
            messages.append({"role": "assistant", "content": content})
        else:
            content = [
                {
                    "toolResult": {
                        "toolUseId": result.id,
                        "status": "success" if result.ok else "error",
                        "content": [{"json": result.json_payload}]
                        if result.json_payload is not None
                        else [{"text": result.text or ""}],
                    }
                }
                for result in turn.results
            ]
            messages.append({"role": "user", "content": content})
    return messages


class BedrockChat:
    def __init__(
        self, model_id: str, guardrail_id: str, guardrail_version: str, *, client: Any = None
    ) -> None:
        self.model_id = model_id
        self.guardrail_id = guardrail_id
        self.guardrail_version = guardrail_version
        self.client: Any = client if client is not None else boto3.client("bedrock-runtime")

    async def stream(
        self, system: str, transcript: list[Turn], tools: list[dict[str, Any]], max_tokens: int
    ) -> AsyncIterator[ModelEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        cancelled = threading.Event()
        end = object()

        def publish(value: Any) -> None:
            if not cancelled.is_set():
                loop.call_soon_threadsafe(queue.put_nowait, value)

        def consume() -> None:
            stream = None
            try:
                response = self.client.converse_stream(
                    modelId=self.model_id,
                    system=[{"text": system}],
                    messages=bedrock_messages(transcript),
                    toolConfig={
                        "tools": [
                            {
                                "toolSpec": {
                                    "name": tool["name"],
                                    "description": tool["description"],
                                    "inputSchema": {"json": tool["input_schema"]},
                                }
                            }
                            for tool in tools
                        ]
                    },
                    inferenceConfig={"maxTokens": max_tokens},
                    guardrailConfig={
                        "guardrailIdentifier": self.guardrail_id,
                        "guardrailVersion": self.guardrail_version,
                        "streamProcessingMode": "sync",
                    },
                )
                stream = response.get("stream")
                if stream is None:
                    raise RuntimeError("Bedrock returned no response stream")
                try:
                    for event in stream:
                        if cancelled.is_set():
                            break
                        publish(event)
                except Exception as exc:
                    publish(ModelStreamError(error_message(exc)))
            except Exception as exc:
                publish(exc)
            finally:
                try:
                    if stream is not None:
                        stream.close()
                finally:
                    publish(end)

        # A dedicated daemon avoids blocking loop shutdown on a stalled provider iterator.
        worker = threading.Thread(target=consume, daemon=True, name="bedrock-stream")
        worker.start()
        blocks: dict[int, dict[str, str]] = {}
        try:
            while True:
                event = await queue.get()
                if event is end:
                    break
                if isinstance(event, Exception):
                    raise event
                for name in EXCEPTIONS:
                    if name in event:
                        raise ModelStreamError(name)
                start = event.get("contentBlockStart", {})
                if "toolUse" in start.get("start", {}):
                    tool = start["start"]["toolUse"]
                    blocks[self._index(start)] = dict(
                        id=tool.get("toolUseId", ""), name=tool.get("name", ""), raw=""
                    )
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]
                    index = self._index(delta)
                    data = delta.get("delta", {})
                    if "text" in data:
                        yield TextDelta(index, data["text"])
                    if "input" in data.get("toolUse", {}):
                        if index not in blocks:
                            raise RuntimeError("Received tool input before tool start")
                        blocks[index]["raw"] += data["toolUse"]["input"]
                if "contentBlockStop" in event:
                    index = self._index(event["contentBlockStop"])
                    if index in blocks:
                        block = blocks[index]
                        input_error = None
                        try:
                            value = (
                                json.loads(block["raw"], parse_constant=reject_json_constant)
                                if js_trim(block["raw"])
                                else {}
                            )
                            if not isinstance(value, dict):
                                raise ValueError("Tool input must be a JSON object")
                        except ValueError as exc:
                            value = {}
                            input_error = (
                                f"Tool input was not a valid JSON object: {error_message(exc)}"
                            )
                        yield ToolCall(index, block["id"], block["name"], value, input_error)
                if "stopReason" in event.get("messageStop", {}):
                    yield Stop(event["messageStop"]["stopReason"])
                if "usage" in event.get("metadata", {}):
                    usage = event["metadata"]["usage"]
                    yield Usage(usage.get("inputTokens", 0), usage.get("outputTokens", 0))
        finally:
            cancelled.set()

    @staticmethod
    def _index(event: dict[str, Any]) -> int:
        if "contentBlockIndex" not in event:
            raise RuntimeError("Bedrock stream event omitted contentBlockIndex")
        return int(event["contentBlockIndex"])
