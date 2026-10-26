import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, Status, StatusCode
from test_agent import DID, Model, Retrieval, Stop, TextDelta, Usage, chunk, tool
from test_api import Blobs, Verifier

from kb_assistant.adapters.aws import xray
from kb_assistant.adapters.aws.ddb_store import DdbStore
from kb_assistant.app import create_app
from kb_assistant.errors import AgentStreamError
from kb_assistant.metrics import TURN_METRICS, TurnMeter, embedded_metrics
from kb_assistant.settings import Settings

ROOT = "1-6720f0a1-0123456789abcdef01234567"
PARENT = "89abcdef01234567"
TRACE_HEADER = f"Root={ROOT};Parent={PARENT};Sampled=1;Lineage=a87bd80c:1"


def chat_app(table, **settings):
    app = create_app(Settings(table_name="test-table", chat_model_id="test-model", **settings))
    container = app.state.container
    container.store = DdbStore("test-table", table=table)
    container.blobs = Blobs()
    container.verifier = Verifier()
    container.retriever = Retrieval([chunk("SECRET-CHUNK")])
    container.model = Model(
        [tool("SECRET-QUERY"), Stop("tool_use"), Usage(10, 2)],
        [TextDelta(0, "SECRET-ANSWER [ref:1]"), Stop("end_turn"), Usage(30, 7)],
    )
    return app


async def ask(app, headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": "Bearer owner", **(headers or {})},
    ) as client:
        return await client.post("/v1/chat", json={"message": "SECRET-QUESTION"})


def turn_records(capsys):
    lines = capsys.readouterr().out.splitlines()
    return [r for r in map(json.loads, lines) if r["msg"] == "chat turn completed"]


def test_embedded_metrics_name_only_fields_the_record_has():
    meter = TurnMeter(started=0.0)
    record = meter.record(model="m", stop_reason="error", usage={})
    assert "firstTextMs" not in record
    fields = embedded_metrics(record, "Space")
    assert fields["turns"] == 1
    by_model, by_stop = fields["_aws"]["CloudWatchMetrics"]
    assert by_model["Namespace"] == by_stop["Namespace"] == "Space"
    assert by_model["Dimensions"] == [["model"]]
    assert [m["Name"] for m in by_model["Metrics"]] == [
        name for name in TURN_METRICS if name != "firstTextMs"
    ]
    assert by_stop == {
        "Namespace": "Space",
        "Dimensions": [["model", "stopReason"]],
        "Metrics": [{"Name": "turns", "Unit": "Count"}],
    }
    # CloudWatch drops a record whose metrics or dimensions are missing from it.
    combined = {**record, **fields}
    for directive in fields["_aws"]["CloudWatchMetrics"]:
        for metric in directive["Metrics"]:
            assert isinstance(combined[metric["Name"]], int)
        for dimension in directive["Dimensions"][0]:
            assert isinstance(combined[dimension], str)


async def test_each_turn_logs_its_metrics_as_embedded_metric_format(table, capsys):
    response = await ask(chat_app(table))
    assert "event: done" in response.text
    [record] = turn_records(capsys)
    assert record["model"] == "test-model" and record["stopReason"] == "end_turn"
    assert record["userSub"] == "owner" and record["requestId"]
    assert record["inputTokens"] == 40 and record["outputTokens"] == 9
    assert record["toolCalls"] == 1
    assert 0 <= record["firstTextMs"] <= record["turnMs"]
    assert record["turns"] == 1
    assert record["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "KbAssistant"
    assert "SECRET" not in json.dumps(record)


@pytest.mark.parametrize("failure", ["agent", "partial", "persist"])
async def test_failed_turns_are_recorded_as_errors(table, capsys, monkeypatch, failure):
    from kb_assistant.api import chat

    app = chat_app(table)
    if failure == "agent":
        monkeypatch.setattr(chat, "run_agent", AsyncMock(side_effect=RuntimeError("down")))
    elif failure == "partial":
        partial = dict(
            text="partial",
            citations=[],
            usage={"inputTokens": 5, "outputTokens": 1},
            stopReason="error",
        )
        error = AgentStreamError("modelStreamErrorException", partial)
        monkeypatch.setattr(chat, "run_agent", AsyncMock(side_effect=error))
    else:
        app.state.container.store.persist_completed_turn = AsyncMock(side_effect=RuntimeError())
    response = await ask(app)
    assert "event: error" in response.text
    [record] = turn_records(capsys)
    assert record["stopReason"] == "error"
    # Tokens a failed turn used still count toward its cost.
    assert (record["inputTokens"], record["outputTokens"]) == {
        "agent": (0, 0),
        "partial": (5, 1),
        "persist": (40, 9),
    }[failure]
    assert ("firstTextMs" in record) == (failure == "persist")


async def test_local_turn_records_carry_no_embedded_metrics(table, capsys):
    app = chat_app(table)
    app.state.container.settings = Settings(deploy_target="local", llm_model="local-model")
    await ask(app)
    [record] = turn_records(capsys)
    assert record["model"] == "local-model" and "_aws" not in record and "turns" not in record


@pytest.mark.parametrize(
    "header,sampled",
    [
        (TRACE_HEADER, True),
        (f"Root={ROOT};Parent={PARENT};Sampled=0", False),
        (f" Root={ROOT}; Parent={PARENT}", False),
        (f"Root=1-bad;Parent={PARENT};Sampled=1", None),
        (f"Root={ROOT};Sampled=1", None),
        ("", None),
    ],
)
def test_trace_header_names_the_parent_segment(header, sampled):
    parent = xray.parse_trace_header(header)
    if sampled is None:
        assert parent is None
        return
    assert parent is not None and parent.is_remote and parent.trace_flags.sampled == sampled
    assert parent.trace_id == 0x6720F0A10123456789ABCDEF01234567
    assert parent.span_id == 0x89ABCDEF01234567


def test_daemon_address_forms():
    assert xray.daemon_destination("169.254.79.129:2000") == ("169.254.79.129", 2000)
    assert xray.daemon_destination("tcp:127.0.0.1:2000 udp:127.0.0.2:2001") == ("127.0.0.2", 2001)


def test_subsegments_keep_timing_and_allowed_attributes_only():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    parent = xray.parse_trace_header(TRACE_HEADER)
    assert parent is not None
    with trace.use_span(NonRecordingSpan(parent)):
        with provider.get_tracer("test").start_as_current_span("execute_tool search?<>") as span:
            span.set_attributes(
                {
                    "gen_ai.tool.name": "search_knowledge_base",
                    "gen_ai.usage.input_tokens": 12,
                    "gen_ai.tool.call.arguments": '{"query": "SECRET"}',
                    "gen_ai.tool.call.result": "SECRET",
                    "gen_ai.usage.details": ["not", "scalar"],
                }
            )
            span.add_event("gen_ai.tool.message", {"content": "SECRET"})
            span.set_status(Status(StatusCode.ERROR, "SECRET"))
    [finished] = exporter.get_finished_spans()
    document = xray.subsegment(finished)
    assert document is not None
    assert document["name"] == "execute_tool search___"
    assert document["trace_id"] == ROOT and document["parent_id"] == PARENT
    assert document["type"] == "subsegment" and len(document["id"]) == 16
    assert document["start_time"] <= document["end_time"]
    assert document["metadata"] == {
        "strands": {"gen_ai.tool.name": "search_knowledge_base", "gen_ai.usage.input_tokens": 12}
    }
    assert document["fault"] is True and "SECRET" not in json.dumps(document)


@pytest.fixture
def daemon(monkeypatch):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.setblocking(False)
    address = "127.0.0.1:%d" % receiver.getsockname()[1]
    # The tracer provider is process-wide, so an earlier test may have installed it.
    monkeypatch.setattr(xray.configure_tracing(address), "destination", receiver.getsockname())

    def received():
        documents = []
        while True:
            try:
                datagram = receiver.recv(65536)
            except BlockingIOError:
                return documents
            header, _, body = datagram.partition(b"\n")
            assert json.loads(header) == {"format": "json", "version": 1}
            documents.append(json.loads(body))

    yield address, received
    receiver.close()


async def test_sampled_turn_sends_its_agent_spans_to_xray_without_content(table, daemon):
    address, received = daemon
    app = chat_app(table, aws_xray_daemon_address=address)
    context = json.dumps({"request_id": DID, "xray_trace_id": TRACE_HEADER})
    response = await ask(app, {"x-amzn-lambda-context": context})
    assert "event: done" in response.text
    documents = received()
    assert "SECRET" not in json.dumps(documents)
    assert {d["trace_id"] for d in documents} == {ROOT}
    by_name = {}
    for document in documents:
        by_name.setdefault(document["name"], []).append(document)
    # Every subsegment hangs off the Lambda function's segment or another subsegment.
    assert all(d["parent_id"] in {d["id"] for d in documents} | {PARENT} for d in documents)
    [agent] = by_name["invoke_agent Strands Agents"]
    assert agent["parent_id"] == PARENT
    cycles = by_name["execute_event_loop_cycle"]
    assert [d["parent_id"] for d in cycles] == [agent["id"]] * 2
    [search] = by_name["execute_tool search_knowledge_base"]
    for document in [*by_name["chat"], search]:
        assert document["parent_id"] in {cycle["id"] for cycle in cycles}
    assert len(by_name["chat"]) == 2
    assert search["metadata"]["strands"]["gen_ai.tool.status"] == "success"
    usage = [d["metadata"]["strands"]["gen_ai.usage.input_tokens"] for d in by_name["chat"]]
    assert sorted(usage) == [10, 30]


@pytest.mark.parametrize("header", [TRACE_HEADER.replace("Sampled=1", "Sampled=0"), None])
async def test_unsampled_or_untraced_turns_send_nothing(table, daemon, header):
    address, received = daemon
    app = chat_app(table, aws_xray_daemon_address=address)
    headers = {}
    if header is not None:
        headers["x-amzn-lambda-context"] = json.dumps({"request_id": DID, "xray_trace_id": header})
    response = await ask(app, headers)
    assert "event: done" in response.text and received() == []


def test_only_the_aws_chat_function_traces(monkeypatch):
    calls = []
    monkeypatch.setattr(xray, "configure_tracing", calls.append)
    create_app(Settings(kb_routes="sessions", aws_xray_daemon_address="127.0.0.1:2000"))
    create_app(Settings(kb_routes="chat"))
    assert calls == []
    create_app(Settings(kb_routes="chat", aws_xray_daemon_address="127.0.0.1:2000"))
    assert calls == ["127.0.0.1:2000"]
