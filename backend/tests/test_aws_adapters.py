import asyncio
import threading
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from kb_assistant.adapters.aws.bedrock_chat import BedrockChat, bedrock_messages
from kb_assistant.adapters.aws.kb_indexer import KbIndexer
from kb_assistant.adapters.aws.kb_retriever import KbRetriever
from kb_assistant.adapters.aws.s3_blobs import S3Blobs
from kb_assistant.core.ports import Stop, TextDelta, ToolCall, Usage
from kb_assistant.core.transcript import (
    AssistantTurn,
    TextBlock,
    ToolResult,
    ToolResults,
    ToolUseBlock,
    UserText,
)
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
    def __init__(self, events):
        self.events = events
        self.closed = threading.Event()
        self.thread = None

    def __iter__(self):
        self.thread = threading.current_thread()
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event

    def close(self):
        self.closed.set()


class Client:
    def __init__(self, events):
        self.stream = EventStream(events)
        self.calls = []

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        return {"stream": self.stream}


def tool_events(raw):
    return [
        {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"toolUse": {"toolUseId": "tool", "name": "search_knowledge_base"}},
            }
        },
        *[
            {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": s}}}}
            for s in raw
        ],
        {"contentBlockStop": {"contentBlockIndex": 1}},
    ]


@pytest.mark.parametrize(
    "fragments,input,error",
    [
        (['{"que', 'ry":"q"}'], {"query": "q"}, False),
        ([], {}, False),
        (["{"], {}, True),
        (["[]"], {}, True),
    ],
)
async def test_bedrock_fragmented_tools_and_usage_after_stop(fragments, input, error):
    client = Client(
        [
            *tool_events(fragments),
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hello"}}},
            {"messageStop": {"stopReason": "tool_use"}},
            {"metadata": {"usage": {"inputTokens": 4, "outputTokens": 3}}},
        ]
    )
    model = BedrockChat("model", "guard", "1", client=client)
    result = [
        e
        async for e in model.stream(
            "system",
            [UserText("old"), UserText("new", True)],
            [{"name": "search", "description": "desc", "input_schema": {}}],
            2048,
        )
    ]
    assert result[0] == ToolCall(1, "tool", "search_knowledge_base", input, result[0].input_error)
    assert bool(result[0].input_error) is error
    assert result[1:] == [TextDelta(0, "hello"), Stop("tool_use"), Usage(4, 3)]
    call = client.calls[0]
    assert call["guardrailConfig"] == {
        "guardrailIdentifier": "guard",
        "guardrailVersion": "1",
        "streamProcessingMode": "sync",
    }
    assert call["messages"] == [
        {"role": "user", "content": [{"text": "old"}]},
        {"role": "user", "content": [{"guardContent": {"text": {"text": "new"}}}]},
    ]
    assert call["inferenceConfig"] == {"maxTokens": 2048}
    assert call["system"] == [{"text": "system"}]
    assert client.stream.thread is not threading.current_thread() and client.stream.closed.is_set()


def test_bedrock_transcript_order_and_results():
    turns = [
        AssistantTurn(
            [TextBlock(2, "after"), ToolUseBlock(1, "t", "search", {}), TextBlock(0, "before")]
        ),
        ToolResults(
            [
                ToolResult("t", True, json_payload={"results": []}),
                ToolResult("e", False, text="error"),
            ]
        ),
    ]
    messages = bedrock_messages(turns)
    assert messages[0]["content"] == [
        {"text": "before"},
        {"toolUse": {"toolUseId": "t", "name": "search", "input": {}}},
        {"text": "after"},
    ]
    assert messages[1]["content"] == [
        {
            "toolResult": {
                "toolUseId": "t",
                "status": "success",
                "content": [{"json": {"results": []}}],
            }
        },
        {"toolResult": {"toolUseId": "e", "status": "error", "content": [{"text": "error"}]}},
    ]


@pytest.mark.parametrize(
    "failure",
    [
        "internalServerException",
        "modelStreamErrorException",
        "validationException",
        "throttlingException",
        "serviceUnavailableException",
        RuntimeError("socket broke"),
    ],
)
async def test_stream_failures(failure):
    client = Client(
        [
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "partial"}}},
            {failure: {}} if isinstance(failure, str) else failure,
        ]
    )
    stream = BedrockChat("m", "g", "1", client=client).stream("", [], [], 2048)
    assert await anext(stream) == TextDelta(0, "partial")
    with pytest.raises(ModelStreamError):
        await anext(stream)
    assert client.stream.closed.is_set()


async def test_stream_consumer_early_close():
    client = Client([{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "x"}}}] * 100)
    stream = BedrockChat("m", "g", "1", client=client).stream("", [], [], 2048)
    await anext(stream)
    await stream.aclose()
    assert await asyncio.to_thread(client.stream.closed.wait, 1)


@pytest.mark.parametrize("raw", ['{"query":', ""])
async def test_bedrock_agent_corrects_bad_tool_and_returns_citation(raw):
    from test_agent import Retrieval, chunk, run

    class Turns:
        def __init__(self):
            self.calls = []
            self.turns = [
                [*tool_events([raw] if raw else []), {"messageStop": {"stopReason": "tool_use"}}],
                [
                    *tool_events(['{"query":"question"}']),
                    {"messageStop": {"stopReason": "tool_use"}},
                ],
                [
                    {
                        "contentBlockDelta": {
                            "contentBlockIndex": 0,
                            "delta": {"text": "answer [ref:1]"},
                        }
                    },
                    {"messageStop": {"stopReason": "end_turn"}},
                ],
            ]

        def converse_stream(self, **kwargs):
            self.calls.append(kwargs)
            return {"stream": EventStream(self.turns.pop(0))}

    client = Turns()
    model = BedrockChat("model", "guard", "1", client=client)
    result = await run(model, Retrieval([chunk()]))
    assert result["citations"][0]["ref"] == 1
    error = client.calls[1]["messages"][-1]["content"][0]["toolResult"]
    assert error["status"] == "error"
    assert error["content"][0]["text"].startswith(
        "Error: Tool input was not a valid JSON object: SyntaxError"
        if raw
        else "Error: search_knowledge_base requires a non-empty query"
    )
    assert all(call["guardrailConfig"]["streamProcessingMode"] == "sync" for call in client.calls)
