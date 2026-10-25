import asyncio
import threading
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import EventStreamError
from botocore.stub import Stubber
from botocore.validate import ParamValidator

from kb_assistant.adapters.aws.bedrock_chat import BedrockChat
from kb_assistant.adapters.aws.kb_indexer import KbIndexer
from kb_assistant.adapters.aws.kb_retriever import KbRetriever
from kb_assistant.adapters.aws.s3_blobs import S3Blobs
from kb_assistant.errors import ModelStreamError


async def test_presigned_put_headers_and_get():
    client = boto3.client(
        "s3", config=Config(signature_version="s3v4", request_checksum_calculation="when_required")
    )
    blobs = S3Blobs("documents", client=client)
    query = parse_qs(urlparse(await blobs.presign_put("uploads/key", "text/plain")).query)
    assert query["X-Amz-SignedHeaders"][0].split(";") == ["content-type", "host", "if-none-match"]
    assert query["X-Amz-Expires"] == ["300"]
    assert not any("checksum" in key.lower() for key in query)
    assert parse_qs(urlparse(await blobs.presign_get("uploads/key")).query)["X-Amz-Expires"] == [
        "300"
    ]


@pytest.mark.parametrize("status", [404, 403, 503])
async def test_head_only_definite_404_is_missing(status):
    client = boto3.client("s3")
    with Stubber(client) as stub:
        stub.add_client_error(
            "head_object",
            service_error_code=str(status),
            http_status_code=status,
            expected_params={"Bucket": "documents", "Key": "key"},
        )
        blobs = S3Blobs("documents", client=client)
        if status == 404:
            assert await blobs.head("key") is None
        else:
            with pytest.raises(Exception):
                await blobs.head("key")
        stub.assert_no_pending_responses()


async def test_head_requires_size_and_sidecar_delete():
    client = boto3.client("s3")
    with Stubber(client) as stub:
        stub.add_response("head_object", {}, {"Bucket": "documents", "Key": "key"})
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "documents",
                "Key": "key.metadata.json",
                "Body": "{}",
                "ContentType": "application/json",
            },
        )
        stub.add_response("delete_object", {}, {"Bucket": "documents", "Key": "key"})
        blobs = S3Blobs("documents", client=client)
        with pytest.raises(RuntimeError, match="omitted"):
            await blobs.head("key")
        await blobs.put_sidecar("key", "{}")
        await blobs.delete("key")
        stub.assert_no_pending_responses()


async def test_retrieve_metadata_title_and_top_k():
    did = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    client = boto3.client("bedrock-agent-runtime")
    with Stubber(client) as stub:
        stub.add_response(
            "retrieve",
            {
                "retrievalResults": [
                    {
                        "content": {"text": "passage"},
                        "location": {
                            "type": "S3",
                            "s3Location": {"uri": "s3://bucket/hello%20world.md"},
                        },
                        "metadata": {"documentId": did},
                        "score": 0.8,
                    },
                    {
                        "content": {"text": ""},
                        "location": {"type": "S3", "s3Location": {"uri": "s3://bucket/bad%FF.md"}},
                        "metadata": {"documentId": "bad"},
                    },
                ]
            },
            {
                "knowledgeBaseId": "kb",
                "retrievalQuery": {"text": "question"},
                "retrievalConfiguration": {"vectorSearchConfiguration": {"numberOfResults": 8}},
            },
        )
        result = await KbRetriever("kb", client=client).retrieve("question", 8)
        assert [(r.title, r.document_id, r.score) for r in result] == [
            ("hello world.md", did, 0.8),
            ("bad%FF.md", "", 0),
        ]
        stub.assert_no_pending_responses()


async def test_indexer_idempotency_sidecar_and_poll():
    client = boto3.client("bedrock-agent")
    uri = "s3://documents/key"
    did = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    with Stubber(client) as stub:
        expected = {
            "knowledgeBaseId": "kb",
            "dataSourceId": "ds",
            "clientToken": f"ingest-{did}",
            "documents": [
                {
                    "content": {"dataSourceType": "S3", "s3": {"s3Location": {"uri": uri}}},
                    "metadata": {
                        "type": "S3_LOCATION",
                        "s3Location": {"uri": uri + ".metadata.json"},
                    },
                }
            ],
        }
        stub.add_response("ingest_knowledge_base_documents", {}, expected)
        stub.add_response(
            "get_knowledge_base_documents",
            {},
            {
                "knowledgeBaseId": "kb",
                "dataSourceId": "ds",
                "documentIdentifiers": [{"dataSourceType": "S3", "s3": {"uri": uri}}],
            },
        )
        indexer = KbIndexer("kb", "ds", "documents", client=client)
        await indexer.ingest(did, "key")
        assert await indexer.details(["key"]) == []
        stub.assert_no_pending_responses()


class EventStream:
    """A Converse event stream. Like botocore, it raises in-band exception events."""

    def __init__(self, events, gate=None):
        self.events = events
        self.closed = threading.Event()
        self.thread = None
        # When set, each event after the first waits for it, so a test can act mid-stream.
        self.gate = gate

    def __iter__(self):
        self.thread = threading.current_thread()
        for position, event in enumerate(self.events):
            if position and self.gate is not None:
                self.gate.wait(1)
            if isinstance(event, Exception):
                raise event
            yield event

    def close(self):
        self.closed.set()


class Client:
    """A bedrock-runtime client that streams one scripted answer per call."""

    def __init__(self, *turns, gate=None):
        self.turns = list(turns)
        self.calls = []
        self.streams = []
        self.gate = gate
        self.meta = boto3.client("bedrock-runtime", region_name="us-east-1").meta

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        self.streams.append(EventStream(self.turns.pop(0), self.gate))
        return {"stream": self.streams[-1]}


def tool_events(raw, id="tool", block=1):
    return [
        {
            "contentBlockStart": {
                "contentBlockIndex": block,
                "start": {"toolUse": {"toolUseId": id, "name": "search_knowledge_base"}},
            }
        },
        *[
            {"contentBlockDelta": {"contentBlockIndex": block, "delta": {"toolUse": {"input": s}}}}
            for s in raw
        ],
        {"contentBlockStop": {"contentBlockIndex": block}},
    ]


def text_events(text, stop="end_turn", usage=(4, 3)):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": stop}},
        {"metadata": {"usage": {"inputTokens": usage[0], "outputTokens": usage[1], "totalTokens": sum(usage)}}},
    ]


def stream_error(code):
    return EventStreamError({"Error": {"Code": code, "Message": "failed"}}, "ConverseStream")


async def test_bedrock_request_guards_only_the_new_question_through_the_tool_loop():
    from test_agent import Retrieval, chunk, run, turn

    client = Client(
        [*tool_events(['{"que', 'ry":"q"}']), {"messageStop": {"stopReason": "tool_use"}}],
        text_events("answer [ref:1]"),
    )
    model = BedrockChat("model", "guard", "1", client=client)
    retriever = Retrieval([chunk()])
    result = await run(model, retriever, history=turn("old", "old answer"))
    assert result["text"] == "answer [ref:1]" and result["citations"][0]["ref"] == 1
    assert result["usage"] == {"inputTokens": 4, "outputTokens": 3}
    assert retriever.calls == [("q", 8)]
    shape = client.meta.service_model.operation_model("ConverseStream").input_shape
    for call in client.calls:
        # The fake client accepts anything, so check each request against the real API.
        report = ParamValidator().validate(call, shape)
        assert not report.has_errors(), report.generate_report()
        assert call["modelId"] == "model"
        assert call["guardrailConfig"] == {
            "guardrailIdentifier": "guard",
            "guardrailVersion": "1",
            "trace": "disabled",
            "streamProcessingMode": "sync",
        }
        assert call["inferenceConfig"] == {"maxTokens": 2048}
        assert "[ref:N]" in call["system"][0]["text"]
        assert [t["toolSpec"]["name"] for t in call["toolConfig"]["tools"]] == [
            "search_knowledge_base"
        ]
        assert call["messages"][:3] == [
            {"role": "user", "content": [{"text": "old"}]},
            {"role": "assistant", "content": [{"text": "old answer"}]},
            {"role": "user", "content": [{"guardContent": {"text": {"text": "question"}}}]},
        ]
    tool_use, tool_result = client.calls[1]["messages"][3:]
    assert tool_use["content"] == [
        {"toolUse": {"toolUseId": "tool", "name": "search_knowledge_base", "input": {"query": "q"}}}
    ]
    [result_block] = tool_result["content"]
    assert result_block["toolResult"]["toolUseId"] == "tool"
    assert result_block["toolResult"]["content"][0]["json"]["results"][0]["ref"] == 1
    # The Bedrock stream is read on a worker thread, never on the event loop.
    assert client.streams[0].thread is not threading.current_thread()


@pytest.mark.parametrize(
    "failure",
    [
        stream_error("internalServerException"),
        stream_error("modelStreamErrorException"),
        stream_error("validationException"),
        stream_error("throttlingException"),
        stream_error("serviceUnavailableException"),
        RuntimeError("socket broke"),
    ],
)
async def test_failure_after_the_stream_starts_keeps_the_partial_answer(failure):
    from test_agent import Retrieval, run

    from kb_assistant.errors import AgentStreamError

    client = Client(
        [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "partial"}}},
            failure,
        ]
    )
    with pytest.raises(AgentStreamError) as raised:
        await run(BedrockChat("m", "g", "1", client=client), Retrieval())
    assert raised.value.partial_result["text"] == "partial"
    assert raised.value.partial_result["stopReason"] == "error"
    assert isinstance(raised.value.__cause__, ModelStreamError)
    # The broken stream still holds its connection, so it is closed rather than left to GC.
    assert client.streams[0].closed.is_set()


@pytest.mark.parametrize("code", ["AccessDeniedException", "ThrottlingException"])
async def test_a_refused_call_is_not_a_stream_failure(code):
    from botocore.exceptions import ClientError
    from test_agent import Retrieval, run

    from kb_assistant.errors import AgentStreamError

    class Refusing(Client):
        def converse_stream(self, **kwargs):
            self.calls.append(kwargs)
            raise ClientError({"Error": {"Code": code, "Message": "no"}}, "ConverseStream")

    client = Refusing()
    with pytest.raises(Exception) as raised:
        await run(BedrockChat("m", "g", "1", client=client), Retrieval())
    assert not isinstance(raised.value, AgentStreamError)
    # Throttling is not retried: a retry would outlive the response deadline.
    assert len(client.calls) == 1


async def test_stream_consumer_early_close():
    gate = threading.Event()
    client = Client(text_events("x") * 20, gate=gate)
    stream = BedrockChat("m", "g", "1", client=client).stream(
        [{"role": "user", "content": [{"text": "q"}]}], None, "system"
    )
    await anext(stream)
    await stream.aclose()
    gate.set()
    # The worker sees the turn has stopped at its next chunk and closes the stream.
    assert await asyncio.to_thread(client.streams[0].closed.wait, 1)


@pytest.mark.parametrize("raw", ['{"query":', ""])
async def test_bedrock_agent_corrects_bad_tool_and_returns_citation(raw):
    from test_agent import Retrieval, chunk, run

    client = Client(
        [*tool_events([raw] if raw else []), {"messageStop": {"stopReason": "tool_use"}}],
        [*tool_events(['{"query":"question"}']), {"messageStop": {"stopReason": "tool_use"}}],
        text_events("answer [ref:1]"),
    )
    # Bedrock accepts a tool result status from Claude models only; Strands sends it to them.
    model = BedrockChat("au.anthropic.claude-sonnet-4-6", "guard", "1", client=client)
    result = await run(model, Retrieval([chunk()]))
    assert result["citations"][0]["ref"] == 1
    error = client.calls[1]["messages"][-1]["content"][0]["toolResult"]
    assert error["status"] == "error"
    # Input that is not JSON reaches the tool as {}, which still has no query.
    assert error["content"][0]["text"] == "Error: search_knowledge_base requires a non-empty query"
