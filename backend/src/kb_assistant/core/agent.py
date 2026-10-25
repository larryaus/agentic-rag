from collections.abc import Callable
from typing import Any

from kb_assistant.errors import AgentStreamError, ModelStreamError, error_message

from .budget import fit
from .citations import UUID_V4, js_slice, resolve_citations
from .ports import ChatModel, Guardrail, Retriever, Stop, TextDelta, ToolCall, Usage
from .prompts import SYSTEM_PROMPT
from .text import js_trim
from .tools import SEARCH_TOOL
from .transcript import (
    AssistantTurn,
    TextBlock,
    ToolResult,
    ToolResults,
    ToolUseBlock,
    Turn,
    UserText,
)

# Stop reasons that mean the model finished its answer. Anything else (the token limit, a
# content filter, running out of tool iterations, a reason a server invents) may have cut
# the answer off, so the held masking tail is dropped rather than released.
COMPLETE_STOPS = {"end_turn", "stop_sequence", "guardrail_intervened"}


async def run_agent(
    *,
    model: ChatModel,
    retriever: Retriever,
    guardrail: Guardrail,
    history: list[Turn],
    user_message: str,
    emit: Callable[[dict[str, Any]], None],
    top_k: int,
    max_iterations: int,
    max_context_chars: int | None = None,
) -> dict[str, Any]:
    transcript = [*history, UserText(user_message, guard=True)]
    index: dict[int, dict[str, Any]] = {}
    ref = 0
    text = ""
    stop_reason = "end_turn"
    usage = {"inputTokens": 0, "outputTokens": 0}
    output_filter = guardrail.output_filter()

    def release(delta: str) -> None:
        nonlocal text
        if delta:
            text += delta
            emit({"type": "text", "delta": delta})

    def result() -> dict[str, Any]:
        return dict(
            text=text,
            citations=resolve_citations(text, index),
            usage=usage.copy(),
            stopReason=stop_reason,
        )

    verdict = await guardrail.check_input(user_message)
    if not verdict.allowed:
        release(verdict.message)
        stop_reason = "guardrail_intervened"
        return result()
    try:
        for iteration in range(max_iterations):
            blocks: dict[int, TextBlock | ToolUseBlock] = {}
            stop_reason = "end_turn"
            sent = (
                transcript
                if max_context_chars is None
                else fit(SYSTEM_PROMPT, transcript, len(history), max_context_chars)
            )
            async for event in model.stream(SYSTEM_PROMPT, sent, [SEARCH_TOOL], 2048):
                if isinstance(event, TextDelta):
                    block = blocks.get(event.block_index)
                    if isinstance(block, TextBlock):
                        block.text += event.text
                    else:
                        blocks[event.block_index] = TextBlock(event.block_index, event.text)
                    release(output_filter.feed(event.text))
                elif isinstance(event, ToolCall):
                    blocks[event.block_index] = ToolUseBlock(
                        event.block_index, event.id, event.name, event.input, event.input_error
                    )
                elif isinstance(event, Stop):
                    stop_reason = event.reason
                elif isinstance(event, Usage):
                    usage["inputTokens"] += event.input_tokens
                    usage["outputTokens"] += event.output_tokens
            if blocks:
                transcript.append(AssistantTurn(list(blocks.values())))
            if stop_reason != "tool_use":
                break
            if iteration == max_iterations - 1:
                stop_reason = "max_iterations"
                break
            results: list[ToolResult] = []
            for block in blocks.values():
                if not isinstance(block, ToolUseBlock):
                    continue
                emit(dict(type="tool_use", name=block.name, input=block.input))
                try:
                    if block.input_error is not None:
                        raise ValueError(block.input_error)
                    if block.name != "search_knowledge_base":
                        raise ValueError(f"Unknown tool: {block.name}")
                    query = block.input.get("query")
                    if not isinstance(query, str) or not js_trim(query):
                        raise ValueError("search_knowledge_base requires a non-empty query")
                    chunks = await retriever.retrieve(query, top_k)
                    payload = []
                    for chunk in chunks:
                        ref += 1
                        if UUID_V4.fullmatch(chunk.document_id):
                            index[ref] = chunk.citation_source()
                            emit(
                                dict(
                                    type="citation",
                                    ref=ref,
                                    title=chunk.title,
                                    documentId=chunk.document_id,
                                    score=chunk.score,
                                    snippet=js_slice(chunk.text, 240),
                                )
                            )
                        payload.append(
                            dict(
                                ref=ref,
                                title=chunk.title,
                                documentId=chunk.document_id,
                                score=chunk.score,
                                text=js_slice(chunk.text, 1500),
                            )
                        )
                    results.append(ToolResult(block.id, True, json_payload={"results": payload}))
                except Exception as exc:
                    results.append(ToolResult(block.id, False, text=error_message(exc)))
            transcript.append(ToolResults(results))
    except ModelStreamError as exc:
        release(output_filter.finish(success=False))
        stop_reason = "error"
        raise AgentStreamError(str(exc), result()) from exc
    except BaseException:
        release(output_filter.finish(success=False))
        raise
    answered = text
    # A dropped tail leaves only a truncation mark, so the fallback looks at the answer
    # released before it.
    release(output_filter.finish(success=stop_reason in COMPLETE_STOPS))
    if stop_reason == "max_iterations" and not answered:
        text = "I could not complete this question within the allowed number of search steps. Please try narrowing it."
    return result()
