import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from typing import Any, cast

from strands import Agent
from strands.agent.conversation_manager import NullConversationManager
from strands.hooks import (
    AfterToolsEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    BeforeToolsEvent,
)
from strands.middleware import InvokeModelContext, InvokeModelStage
from strands.tools import PythonAgentTool
from strands.tools.executors import SequentialToolExecutor
from strands.types.content import Messages
from strands.types.exceptions import MaxTokensReachedException
from strands.types.tools import ToolResult, ToolUse

from kb_assistant.errors import AgentStreamError, ModelStreamError, error_message

from .budget import fit
from .citations import UUID_V4, js_slice, resolve_citations
from .ports import ChatModel, Guardrail, Retriever
from .prompts import SYSTEM_PROMPT
from .text import js_trim
from .tools import SEARCH_TOOL

# Stop reasons that mean the model finished its answer. Anything else (the token limit, a
# content filter, running out of tool iterations, a reason a server invents) may have cut
# the answer off, so the held masking tail is dropped rather than released.
COMPLETE_STOPS = {"end_turn", "stop_sequence", "guardrail_intervened"}
# Strands logs tool input and model output at warning level; neither may reach the logs.
logging.getLogger("strands").setLevel(logging.ERROR)


async def run_agent(
    *,
    model: ChatModel,
    retriever: Retriever,
    guardrail: Guardrail,
    history: Messages,
    user_message: str,
    emit: Callable[[dict[str, Any]], None],
    top_k: int,
    max_iterations: int,
    max_context_chars: int | None = None,
) -> dict[str, Any]:
    index: dict[int, dict[str, Any]] = {}
    ref = 0
    text = ""
    stop_reason = "end_turn"
    usage = {"inputTokens": 0, "outputTokens": 0}
    output_filter = guardrail.output_filter()
    model_calls = 0
    out_of_steps = False
    # Strands appends this turn's messages to the history list it is given.
    history_length = len(history)

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

    async def search(tool_use: ToolUse, **_: Any) -> ToolResult:
        nonlocal ref
        try:
            arguments = tool_use["input"]
            if not isinstance(arguments, dict):
                raise ValueError("Tool input must be a JSON object")
            query = arguments.get("query")
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
            return {
                "toolUseId": tool_use["toolUseId"],
                "status": "success",
                "content": [{"json": {"results": payload}}],
            }
        except Exception as exc:
            return {
                "toolUseId": tool_use["toolUseId"],
                "status": "error",
                "content": [{"text": error_message(exc)}],
            }

    def count_model_call(event: BeforeModelCallEvent) -> None:
        nonlocal model_calls
        model_calls += 1

    def skip_tools_on_last_call(event: BeforeToolsEvent) -> None:
        # The final unusable tool request is never dispatched.
        nonlocal out_of_steps
        if model_calls >= max_iterations:
            out_of_steps = True
            event.cancel = True

    def end_turn_when_out_of_steps(event: AfterToolsEvent) -> None:
        if out_of_steps:
            event.end_turn = True

    def announce_tool(event: BeforeToolCallEvent) -> None:
        emit(dict(type="tool_use", name=event.tool_use["name"], input=event.tool_use["input"]))

    def fit_budget(context: InvokeModelContext) -> InvokeModelContext:
        # Each call gets its own copy of the messages, so history stays whole for the next.
        assert max_context_chars is not None
        return context.replace(
            messages=fit(SYSTEM_PROMPT, context.messages, history_length, max_context_chars)
        )

    agent = Agent(
        model=model,
        messages=history,
        tools=[PythonAgentTool(SEARCH_TOOL["name"], SEARCH_TOOL, search)],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
        conversation_manager=NullConversationManager(),
        # Retries would outlive the response deadline; a failed turn is retried by the user.
        retry_strategy=None,
        # Refs are numbered in tool-call order, so tools run one at a time.
        tool_executor=SequentialToolExecutor(),
    )
    agent.hooks.add_callback(BeforeModelCallEvent, count_model_call)
    agent.hooks.add_callback(BeforeToolsEvent, skip_tools_on_last_call)
    agent.hooks.add_callback(AfterToolsEvent, end_turn_when_out_of_steps)
    agent.hooks.add_callback(BeforeToolCallEvent, announce_tool)
    if max_context_chars is not None:
        agent.add_middleware(InvokeModelStage.Input, fit_budget)

    try:
        stream = cast(AsyncGenerator[Any, None], agent.stream_async(user_message))
        async with aclosing(stream) as events:
            async for event in events:
                if "data" in event:
                    release(output_filter.feed(event["data"]))
                chunk = event.get("event") or {}
                if "metadata" in chunk:
                    reported = chunk["metadata"].get("usage", {})
                    usage["inputTokens"] += reported.get("inputTokens", 0)
                    usage["outputTokens"] += reported.get("outputTokens", 0)
                if "result" in event:
                    stop_reason = event["result"].stop_reason
    except MaxTokensReachedException:
        stop_reason = "max_tokens"
    except ModelStreamError as exc:
        release(output_filter.finish(success=False))
        stop_reason = "error"
        raise AgentStreamError(str(exc), result()) from exc
    except BaseException:
        release(output_filter.finish(success=False))
        raise
    if out_of_steps:
        stop_reason = "max_iterations"
    answered = text
    # A dropped tail leaves only a truncation mark, so the fallback looks at the answer
    # released before it.
    release(output_filter.finish(success=stop_reason in COMPLETE_STOPS))
    if stop_reason == "max_iterations" and not answered:
        text = "I could not complete this question within the allowed number of search steps. Please try narrowing it."
    return result()
