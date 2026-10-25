import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import pytest
from strands.models import Model as StrandsModel

from kb_assistant.core.agent import run_agent
from kb_assistant.core.guard_stream import PassThroughGuardrail
from kb_assistant.core.ports import InputVerdict, RawChunk
from kb_assistant.errors import AgentStreamError, ModelStreamError

DID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


# A scripted model turn is a list of these, in stream order.
@dataclass
class TextDelta:
    block_index: int
    text: str


@dataclass
class ToolCall:
    block_index: int
    id: str
    name: str
    input: Any = field(default_factory=dict)
    # Raw argument text, for input that is not valid JSON.
    raw: str | None = None


@dataclass
class Stop:
    reason: str


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


def chunks(script):
    """Converse stream events for a scripted turn, as a Strands model yields them."""
    yield {"messageStart": {"role": "assistant"}}
    text_open = False
    for event in script:
        if isinstance(event, Exception):
            raise event
        if isinstance(event, TextDelta):
            text_open = True
            yield {"contentBlockDelta": {"delta": {"text": event.text}}}
            continue
        if text_open:
            text_open = False
            yield {"contentBlockStop": {}}
        if isinstance(event, ToolCall):
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": event.id, "name": event.name}}
                }
            }
            raw = event.raw if event.raw is not None else json.dumps(event.input)
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": raw}}}}
            yield {"contentBlockStop": {}}
        elif isinstance(event, Stop):
            yield {"messageStop": {"stopReason": event.reason}}
        elif isinstance(event, Usage):
            yield {
                "metadata": {
                    "usage": {
                        "inputTokens": event.input_tokens,
                        "outputTokens": event.output_tokens,
                        "totalTokens": event.input_tokens + event.output_tokens,
                    },
                    "metrics": {"latencyMs": 0},
                }
            }
    if text_open:
        yield {"contentBlockStop": {}}


class Model(StrandsModel):
    """A Strands model that plays back one scripted turn per call."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []

    def update_config(self, **config):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(
            dict(system=system_prompt, messages=deepcopy(messages), tools=deepcopy(tool_specs))
        )
        for chunk in chunks(self.turns.pop(0)):
            yield chunk


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


def results(message):
    return [block["toolResult"] for block in message["content"] if "toolResult" in block]


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


def turn(question, answer):
    return [
        {"role": "user", "content": [{"text": question}]},
        {"role": "assistant", "content": [{"text": answer}]},
    ]


async def test_tool_loop_order_usage_and_history():
    model = Model(
        [TextDelta(0, "Searching."), tool(index=1), Stop("tool_use"), Usage(10, 2)],
        [TextDelta(0, "Answer [ref:1]"), Stop("end_turn"), Usage(3, 4)],
    )
    retriever = Retrieval([chunk("x" * 1600)])
    events = []
    history = turn("old", "old answer")
    result = await run(model, retriever, events, history=deepcopy(history))
    assert [e["type"] for e in events] == ["text", "tool_use", "citation", "text"]
    assert events[1] == {"type": "tool_use", "name": "search_knowledge_base", "input": {"query": "query"}}
    assert result["usage"] == {"inputTokens": 13, "outputTokens": 6}
    assert result["citations"][0]["snippet"] == "x" * 240
    assert result["stopReason"] == "end_turn"
    messages = model.calls[1]["messages"]
    assert messages[:3] == [*history, {"role": "user", "content": [{"text": "question"}]}]
    assert [next(iter(block)) for block in messages[3]["content"]] == ["text", "toolUse"]
    assert results(messages[4])[0]["content"][0]["json"]["results"][0]["text"] == "x" * 1500
    assert retriever.calls == [("query", 8)]
    assert [spec["name"] for spec in model.calls[0]["tools"]] == ["search_knowledge_base"]
    assert "[ref:N]" in model.calls[0]["system"]


@pytest.mark.parametrize(
    "call,error",
    [
        (ToolCall(0, "x", "unknown", {}), "Unknown tool"),
        (tool(""), "non-empty query"),
        (tool(3), "non-empty query"),
        # Input that is not JSON reaches the tool as {}, which still has no query.
        (ToolCall(0, "x", "search_knowledge_base", raw='{"query": '), "non-empty query"),
        (ToolCall(0, "x", "search_knowledge_base", raw="[1]"), "must be a JSON object"),
    ],
)
async def test_tool_errors_are_returned_to_model(call, error):
    model = Model([call, Stop("tool_use")], [TextDelta(0, "recovered"), Stop("end_turn")])
    retriever = Retrieval()
    events = []
    result = await run(model, retriever, events)
    [returned] = results(model.calls[1]["messages"][-1])
    assert returned["status"] == "error" and error in returned["content"][0]["text"]
    assert result["text"] == "recovered" and not retriever.calls
    assert [e["type"] for e in events] == ["tool_use", "text"]


async def test_retrieval_error_then_recovery():
    model = Model(
        [tool(), Stop("tool_use")],
        [tool(id="retry"), Stop("tool_use")],
        [TextDelta(0, "yes [ref:1]")],
    )
    result = await run(model, Retrieval(RuntimeError("outage"), [chunk()]))
    [failed] = results(model.calls[1]["messages"][-1])
    assert failed["status"] == "error" and failed["content"][0]["text"] == "Error: outage"
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
    assert [r["toolUseId"] for r in results(model.calls[1]["messages"][-1])] == ["a", "b"]
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
    assert [e["type"] for e in events].count("tool_use") == 2
    assert not any(
        e["type"] == "text" for e in events
    )  # Legacy fallback is persisted, not streamed.


async def test_bedrock_refusal_is_answer():
    model = Model([TextDelta(0, "refused"), Stop("guardrail_intervened")])
    events = []
    result = await run(model, Retrieval(), events)
    assert result["text"] == "refused" and result["stopReason"] == "guardrail_intervened"
    assert events == [{"type": "text", "delta": "refused"}]


async def test_token_limit_ends_the_turn_without_running_its_tools():
    model = Model([TextDelta(0, "partial"), tool(index=1), Stop("max_tokens")])
    retriever = Retrieval()
    result = await run(model, retriever)
    assert result["stopReason"] == "max_tokens" and result["text"] == "partial"
    assert not retriever.calls and len(model.calls) == 1


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


async def test_other_model_failures_propagate_after_the_filter_finishes():
    finished = []

    class Filter:
        def feed(self, text):
            return ""

        def finish(self, success=True):
            finished.append(success)
            return ""

    class Guard(PassThroughGuardrail):
        def output_filter(self):
            return Filter()

    model = Model([RuntimeError("access denied")])
    with pytest.raises(RuntimeError, match="access denied"):
        await run(model, Retrieval(), guardrail=Guard())
    assert finished == [False]
