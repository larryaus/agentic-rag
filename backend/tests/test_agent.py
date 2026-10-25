from copy import deepcopy

import pytest

from kb_assistant.core.agent import run_agent
from kb_assistant.core.guard_stream import PassThroughGuardrail
from kb_assistant.core.ports import InputVerdict, RawChunk, Stop, TextDelta, ToolCall, Usage
from kb_assistant.core.transcript import AssistantTurn, TextBlock, ToolResults, UserText
from kb_assistant.errors import AgentStreamError, ModelStreamError

DID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class Model:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []

    async def stream(self, system, transcript, tools, max_tokens):
        self.calls.append((system, deepcopy(transcript), tools, max_tokens))
        for event in self.turns.pop(0):
            if isinstance(event, Exception):
                raise event
            yield event


class Retrieval:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def retrieve(self, query, top_k):
        self.calls.append((query, top_k))
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def chunk(text="text", id=DID):
    return RawChunk(text, "s3://bucket/doc.md", id, "doc.md", 0.875)


def tool(query="query", id="tool", index=0, **kw):
    return ToolCall(index, id, "search_knowledge_base", {"query": query}, **kw)


async def run(model, retrieval, events=None, **kw):
    return await run_agent(
        model=model,
        retriever=retrieval,
        guardrail=kw.pop("guardrail", PassThroughGuardrail()),
        history=kw.pop("history", []),
        user_message="question",
        emit=(events if events is not None else []).append,
        top_k=8,
        max_iterations=kw.pop("max_iterations", 6),
        **kw,
    )


async def test_tool_loop_order_usage_after_stop_and_history_guard():
    model = Model(
        [TextDelta(2, "Searching."), tool(index=1), Stop("tool_use"), Usage(10, 2)],
        [TextDelta(0, "Answer [ref:1]"), Stop("end_turn"), Usage(3, 4)],
    )
    retriever = Retrieval([chunk("x" * 1600)])
    events = []
    history = [UserText("old"), AssistantTurn([TextBlock(0, "old answer")])]
    result = await run(model, retriever, events, history=history)
    assert [e["type"] for e in events] == ["text", "tool_use", "citation", "text"]
    assert result["usage"] == {"inputTokens": 13, "outputTokens": 6}
    assert result["citations"][0]["snippet"] == "x" * 240
    messages = model.calls[1][1]
    assert messages[:3] == history + [UserText("question", True)]
    assert [b.block_index for b in messages[3].blocks] == [1, 2]
    assert messages[4].results[0].json_payload["results"][0]["text"] == "x" * 1500
    assert retriever.calls == [("query", 8)]
    assert all(c[3] == 2048 for c in model.calls)


@pytest.mark.parametrize(
    "call,error",
    [
        (ToolCall(0, "x", "unknown", {}), "Unknown tool"),
        (tool(""), "non-empty query"),
        (tool(3), "non-empty query"),
        (ToolCall(0, "x", "search_knowledge_base", {}, "bad JSON"), "bad JSON"),
    ],
)
async def test_tool_errors_are_returned_to_model(call, error):
    model = Model([call, Stop("tool_use")], [TextDelta(0, "recovered"), Stop("end_turn")])
    retriever = Retrieval()
    result = await run(model, retriever)
    message = model.calls[1][1][-1]
    assert isinstance(message, ToolResults)
    assert not message.results[0].ok and error in message.results[0].text
    assert result["text"] == "recovered" and not retriever.calls


async def test_retrieval_error_then_recovery():
    model = Model(
        [tool(), Stop("tool_use")],
        [tool(id="retry"), Stop("tool_use")],
        [TextDelta(0, "yes [ref:1]")],
    )
    result = await run(model, Retrieval(RuntimeError("outage"), [chunk()]))
    assert not model.calls[1][1][-1].results[0].ok
    assert len(result["citations"]) == 1


async def test_multi_tool_global_refs_and_invalid_document():
    model = Model(
        [tool(id="a"), tool(id="b", index=1), Stop("tool_use")],
        [tool(id="c"), Stop("tool_use")],
        [TextDelta(0, "[ref:4] [ref:1]")],
    )
    events = []
    result = await run(model, Retrieval([chunk(id="bad"), chunk()], [chunk()], [chunk()]), events)
    assert [e["ref"] for e in events if e["type"] == "citation"] == [2, 3, 4]
    assert [r.id for r in model.calls[1][1][-1].results] == ["a", "b"]
    assert [c["ref"] for c in result["citations"]] == [4]


async def test_max_iterations_no_final_dispatch_and_fallback():
    model = Model(*[[tool(), Stop("tool_use")] for _ in range(3)])
    retriever = Retrieval([chunk()], [chunk()])
    events = []
    result = await run(model, retriever, events, max_iterations=3)
    assert len(model.calls) == 3 and len(retriever.calls) == 2
    assert result["stopReason"] == "max_iterations"
    assert (
        result["text"]
        == "I could not complete this question within the allowed number of search steps. Please try narrowing it."
    )
    assert not any(
        e["type"] == "text" for e in events
    )  # Legacy fallback is persisted, not streamed.


async def test_bedrock_refusal_is_answer():
    model = Model([TextDelta(0, "refused"), Stop("guardrail_intervened")])
    events = []
    result = await run(model, Retrieval(), events)
    assert result["text"] == "refused" and result["stopReason"] == "guardrail_intervened"
    assert events == [{"type": "text", "delta": "refused"}]


async def test_input_refusal_never_calls_model():
    class Refusal(PassThroughGuardrail):
        async def check_input(self, text):
            return InputVerdict(False, "refused")

    model = Model()
    result = await run(model, Retrieval(), guardrail=Refusal())
    assert not model.calls and result["stopReason"] == "guardrail_intervened"


@pytest.mark.parametrize("failure", [False, True])
async def test_filter_finish_before_success_or_partial_result(failure):
    class Filter:
        def feed(self, text):
            return ""

        def finish(self, success=True):
            return "[masked]"

    class Guard(PassThroughGuardrail):
        def output_filter(self):
            return Filter()

    model = Model(
        [TextDelta(0, "secret"), Usage(2, 3), ModelStreamError("interrupted")]
        if failure
        else [TextDelta(0, "secret"), Stop("end_turn")]
    )
    events = []
    if failure:
        with pytest.raises(AgentStreamError) as exc:
            await run(model, Retrieval(), events, guardrail=Guard())
        result = exc.value.partial_result
        assert result["usage"] == {"inputTokens": 2, "outputTokens": 3}
        assert result["stopReason"] == "error"
    else:
        result = await run(model, Retrieval(), events, guardrail=Guard())
    assert result["text"] == "[masked]" and events == [{"type": "text", "delta": "[masked]"}]
