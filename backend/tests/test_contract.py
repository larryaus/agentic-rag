import json
from pathlib import Path

from pydantic import TypeAdapter

from kb_assistant import contract
from kb_assistant.core.sse import encode_sse


def test_contract_literals_round_trip_with_omitted_optionals(legacy):
    data = json.loads((Path(__file__).parents[2] / "shared/fixtures/contract.json").read_text())
    models = {
        "documentStatuses": contract.DocumentStatus,
        "documentSummaries": contract.DocumentSummary,
        "sessionSummaries": contract.SessionSummary,
        "messageViews": contract.MessageView,
        "sessionDetails": contract.SessionDetail,
        "documentPages": contract.Page[contract.DocumentSummary],
        "sessionPages": contract.Page[contract.SessionSummary],
        "uploadRequests": contract.UploadRequest,
        "uploadResponses": contract.UploadResponse,
        "citations": contract.Citation,
        "usages": contract.Usage,
        "sseEvents": contract.SseEvent,
    }
    assert set(data) == set(models)
    for name, model in models.items():
        adapter = TypeAdapter(model)
        for entry in data[name]:
            value = adapter.validate_python(entry)
            assert adapter.dump_python(value, by_alias=True, exclude_none=True) == entry
    assert data["sseEvents"] == [case["event"] for case in legacy("sse")]
    assert [encode_sse(e) for e in data["sseEvents"]] == [case["frame"] for case in legacy("sse")]
