import json

import httpx
import pytest
from openai import AsyncOpenAI
from strands.event_loop.streaming import process_stream

from kb_assistant.adapters.local.openai_chat import OpenAIChat
from kb_assistant.adapters.local.openai_embed import EmbeddingError, OpenAIEmbedder
from kb_assistant.core.tools import SEARCH_TOOL
from kb_assistant.errors import ModelStreamError

QUESTION = [{"role": "user", "content": [{"text": "q"}]}]


def sse(*chunks, done=True):
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return body + ("data: [DONE]\n\n" if done else "")


def delta(content=None, tool_calls=None, finish=None):
    value = {}
    if content is not None:
        value["content"] = content
    if tool_calls is not None:
        value["tool_calls"] = tool_calls
    return {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "delta": value, "finish_reason": finish}],
    }


USAGE = {
    "id": "c",
    "object": "chat.completion.chunk",
    "created": 0,
    "model": "m",
    "choices": [],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
}


def client_for(*responses, seen=None):
    queue = list(responses)

    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        response = queue.pop(0)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, text=response, headers={"content-type": "text/event-stream"})

    return AsyncOpenAI(
        api_key="k",
        base_url="http://llm.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


class Answer:
    """The message, stop reason and usage Strands assembles from the adapter's stream."""

    def __init__(self, stop):
        self.reason, message, usage, _ = stop
        self.text = "".join(block["text"] for block in message["content"] if "text" in block)
        self.calls = [
            (block["toolUse"]["toolUseId"], block["toolUse"]["name"], block["toolUse"]["input"])
            for block in message["content"]
            if "toolUse" in block
        ]
        self.usage = (usage["inputTokens"], usage["outputTokens"])


async def answer_from(client, messages=QUESTION):
    stream = OpenAIChat("m", client=client).stream(messages, [SEARCH_TOOL], "system")
    stop = None
    async for event in process_stream(stream):
        if "stop" in event:
            stop = event["stop"]
    return Answer(stop)


async def test_text_only_with_usage():
    seen = []
    answer = await answer_from(
        client_for(sse(delta("Hel"), delta("lo"), delta(finish="stop"), USAGE), seen=seen)
    )
    assert (answer.text, answer.reason, answer.usage, answer.calls) == ("Hello", "end_turn", (12, 5), [])
    request = seen[0]
    assert request["stream"] is True and request["stream_options"] == {"include_usage": True}
    assert request["model"] == "m" and request["max_tokens"] == 2048
    assert request["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "q"},
    ]
    assert request["tools"][0]["function"]["name"] == "search_knowledge_base"


async def test_text_then_parallel_tool_calls_with_split_arguments():
    first = {"index": 0, "id": "a", "type": "function", "function": {"name": "search_knowledge_base", "arguments": '{"qu'}}
    second = {"index": 1, "id": "b", "type": "function", "function": {"name": "search_knowledge_base", "arguments": ""}}
    answer = await answer_from(
        client_for(
            sse(
                delta("Let me look."),
                delta(tool_calls=[first]),
                delta(tool_calls=[second]),
                delta(tool_calls=[{"index": 0, "function": {"arguments": 'ery": "x"}'}}]),
                delta(tool_calls=[{"index": 1, "function": {"arguments": '{"query": "y"}'}}]),
                delta(finish="tool_calls"),
            )
        )
    )
    assert answer.text == "Let me look." and answer.reason == "tool_use"
    assert answer.calls == [
        ("a", "search_knowledge_base", {"query": "x"}),
        ("b", "search_knowledge_base", {"query": "y"}),
    ]
    assert answer.usage == (0, 0)


async def test_ollama_style_stop_with_tool_calls_and_missing_ids():
    call = {"index": 0, "type": "function", "function": {"name": "search_knowledge_base", "arguments": "not json"}}
    answer = await answer_from(client_for(sse(delta(tool_calls=[call]), delta(finish="stop"))))
    # Input that is not JSON reaches the tool as {}; the tool reports the missing query.
    assert answer.calls == [("call_0", "search_knowledge_base", {})]
    assert answer.reason == "tool_use"


@pytest.mark.parametrize(
    "finish,reason",
    [("length", "max_tokens"), ("content_filter", "content_filtered"), ("other", "other")],
)
async def test_finish_reasons(finish, reason):
    answer = await answer_from(client_for(sse(delta("x"), delta(finish=finish))))
    assert answer.reason == reason


async def test_mid_stream_error_raises_model_stream_error():
    broken = sse(delta("partial"), done=False) + "data: {not json\n\n"
    with pytest.raises(ModelStreamError):
        await answer_from(client_for(broken))


async def test_stream_options_rejection_retries_without_it():
    seen = []
    rejected = httpx.Response(
        400, json={"error": {"message": "Unrecognized request argument: stream_options"}}
    )
    answer = await answer_from(
        client_for(rejected, sse(delta("ok"), delta(finish="stop")), seen=seen)
    )
    assert "stream_options" in seen[0] and "stream_options" not in seen[1]
    assert answer.text == "ok" and answer.usage == (0, 0)


async def test_other_request_errors_are_not_retried():
    seen = []
    with pytest.raises(Exception):
        await answer_from(client_for(httpx.Response(401, json={"error": {}}), seen=seen))
    assert len(seen) == 1


async def test_request_for_a_tool_loop_sends_text_as_strings():
    seen = []
    messages = [
        {"role": "user", "content": [{"text": "q"}]},
        {
            "role": "assistant",
            "content": [
                {"text": "a"},
                {"toolUse": {"toolUseId": "t1", "name": "search_knowledge_base", "input": {"query": "x"}}},
                {"text": "b"},
            ],
        },
        {
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"json": {"results": [{"ref": 1}]}}]}},
                {"toolResult": {"toolUseId": "t2", "status": "error", "content": [{"text": "Error: bad"}]}},
            ],
        },
    ]
    await answer_from(client_for(sse(delta("ok"), delta(finish="stop")), seen=seen), messages)
    assert seen[0]["messages"][1:] == [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "ab",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "search_knowledge_base", "arguments": '{"query": "x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": '{"results": [{"ref": 1}]}'},
        {"role": "tool", "tool_call_id": "t2", "content": "Error: bad"},
    ]


def embedding_client(vectors):
    def handler(request):
        body = json.loads(request.content)
        data = [
            {"object": "embedding", "index": i, "embedding": vector}
            for i, vector in enumerate(vectors(len(body["input"])))
        ]
        return httpx.Response(200, json={"object": "list", "data": data, "model": "m",
                                         "usage": {"prompt_tokens": 1, "total_tokens": 1}})

    return AsyncOpenAI(
        api_key="k",
        base_url="http://llm.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


async def test_embeddings_batch_and_validate():
    embedder = OpenAIEmbedder("m", 3, client=embedding_client(lambda n: [[0.1, 0.2, 0.3]] * n))
    assert len(await embedder.embed(["x"] * 130)) == 130
    with pytest.raises(EmbeddingError, match="returned 2 dimensions"):
        await OpenAIEmbedder("m", 3, client=embedding_client(lambda n: [[0.1, 0.2]] * n)).embed(["x"])
    with pytest.raises(EmbeddingError, match="returned 1 vectors for 2 inputs"):
        await OpenAIEmbedder("m", 3, client=embedding_client(lambda n: [[0.1, 0.2, 0.3]])).embed(
            ["x", "y"]
        )


async def test_tool_calls_without_an_index_are_assembled():
    call = {"id": "a", "type": "function", "function": {"name": "search_knowledge_base", "arguments": '{"query": "x"}'}}
    answer = await answer_from(client_for(sse(delta(tool_calls=[call]), delta(finish="tool_calls"))))
    assert answer.calls == [("a", "search_knowledge_base", {"query": "x"})]


class RecordingStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self.chunks:
            yield chunk

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("stop_at", ["messageStart", "contentBlockDelta"])
async def test_stream_is_closed_when_the_consumer_stops_early(stop_at):
    from types import SimpleNamespace

    from openai.types.chat import ChatCompletionChunk

    chunks = [ChatCompletionChunk.model_validate(delta(f"part {i} ")) for i in range(5)]
    stream = RecordingStream(chunks)

    async def create(**request):
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    events = OpenAIChat("m", client=client).stream(QUESTION, [SEARCH_TOOL], "s")
    while stop_at not in await anext(events):
        pass
    await events.aclose()
    assert stream.closed


async def test_classifier_call_has_its_own_short_timeout_and_no_retries():
    from types import SimpleNamespace

    seen = {}

    async def create(**request):
        seen["request"] = request
        message = SimpleNamespace(content='{"allowed": true}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    def with_options(**options):
        seen["options"] = options
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    client = SimpleNamespace(with_options=with_options)
    assert await OpenAIChat("m", client=client).complete("system", "question") == '{"allowed": true}'
    assert seen["options"] == {"timeout": 20, "max_retries": 0}
    assert seen["request"]["max_tokens"] == 512 and seen["request"]["temperature"] == 0


async def test_index_less_tool_calls_split_on_a_new_id():
    first = {"id": "a", "type": "function", "function": {"name": "search_knowledge_base", "arguments": '{"query":'}}
    rest = {"function": {"arguments": ' "x"}'}}
    second = {"id": "b", "type": "function", "function": {"name": "search_knowledge_base", "arguments": '{"query": "y"}'}}
    answer = await answer_from(
        client_for(sse(delta(tool_calls=[first]), delta(tool_calls=[rest]), delta(tool_calls=[second]), delta(finish="tool_calls")))
    )
    assert answer.calls == [
        ("a", "search_knowledge_base", {"query": "x"}),
        ("b", "search_knowledge_base", {"query": "y"}),
    ]


async def test_truncated_classifier_reply_raises_so_the_check_fails_open():
    from types import SimpleNamespace

    async def create(**request):
        message = SimpleNamespace(content='Format: {"allowed": false}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="length")])

    client = SimpleNamespace(
        with_options=lambda **options: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
    )
    with pytest.raises(RuntimeError, match="cut off"):
        await OpenAIChat("m", client=client).complete("system", "question")


async def test_body_ending_without_a_finish_reason_is_a_stream_error():
    with pytest.raises(ModelStreamError, match="without a finish reason"):
        await answer_from(client_for(sse(delta("The key is wJalrXUtnFEMI"), done=True)))


async def test_interleaved_index_less_fragments_keep_their_ids():
    def call(id_, arguments, name=None):
        function = {"arguments": arguments}
        if name:
            function["name"] = name
        return {"id": id_, "type": "function", "function": function}

    fragments = [
        call("a", '{"query": ', "search_knowledge_base"),
        call("b", '{"query": ', "search_knowledge_base"),
        call("a", '"x"}'),
        call("b", '"y"}'),
    ]
    answer = await answer_from(
        client_for(sse(*(delta(tool_calls=[f]) for f in fragments), delta(finish="tool_calls")))
    )
    assert answer.calls == [
        ("a", "search_knowledge_base", {"query": "x"}),
        ("b", "search_knowledge_base", {"query": "y"}),
    ]


@pytest.mark.parametrize("finish,reason", [("length", "max_tokens"), ("content_filter", "content_filtered")])
async def test_truncation_with_tool_calls_stays_truncated(finish, reason):
    call = {"index": 0, "id": "a", "type": "function", "function": {"name": "search_knowledge_base", "arguments": '{"qu'}}
    answer = await answer_from(client_for(sse(delta("key wJal"), delta(tool_calls=[call]), delta(finish=finish))))
    assert answer.reason == reason


async def test_anonymous_fragments_continue_the_most_recent_call():
    def call(id_, arguments, name=None):
        function = {"arguments": arguments}
        if name:
            function["name"] = name
        value = {"type": "function", "function": function}
        if id_:
            value["id"] = id_
        return value

    fragments = [
        call("a", '{"query"', "search_knowledge_base"),
        call("b", '{"query": "y"}', "search_knowledge_base"),
        call("a", ': '),
        call(None, '"x"}'),
    ]
    answer = await answer_from(
        client_for(sse(*(delta(tool_calls=[f]) for f in fragments), delta(finish="tool_calls")))
    )
    assert answer.calls == [
        ("a", "search_knowledge_base", {"query": "x"}),
        ("b", "search_knowledge_base", {"query": "y"}),
    ]
