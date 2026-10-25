import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kb_assistant.core.citations import extract_refs, resolve_citations
from kb_assistant.core.history import to_history
from kb_assistant.core.paging import decode_page_token, encode_page_token
from kb_assistant.core.sse import encode_sse
from kb_assistant.core.uploads import sanitize_filename, validate_chat, validate_upload
from kb_assistant.errors import ValidationError
from kb_assistant.timeutil import iso_ms, message_sk

FIXTURES = Path(__file__).parent / "fixtures" / "legacy"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("case", fixture("sse"))
def test_sse_bytes(case):
    assert encode_sse(case["event"]) == case["frame"]


@pytest.mark.parametrize("case", fixture("paging"))
def test_legacy_cursors(case):
    key = case["key"]
    partition = "gsi1pk" if "gsi1pk" in key else "pk"
    assert encode_page_token(key) == case["token"]
    for token in [case["token"], case["token"] + "=" * (-len(case["token"]) % 4)]:
        assert decode_page_token(token, list(key), partition, key[partition]) == key
    with pytest.raises(ValidationError):
        decode_page_token(case["token"], list(key), partition, "foreign")


@pytest.mark.parametrize("value", [[], None, {}, {"pk": 1}, {"pk": "owner", "extra": "x"}])
def test_malformed_cursor(value):
    import base64

    token = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    with pytest.raises(ValidationError, match="Malformed nextToken"):
        decode_page_token(token, ["pk"], "pk", "owner")


@pytest.mark.parametrize("case", fixture("time"))
def test_timestamp_and_sort_key(case):
    result = iso_ms(datetime.fromtimestamp(case["ms"] / 1000, UTC))
    assert result == case["iso"]
    assert message_sk(result, case["id"]) == case["sk"]


@pytest.mark.parametrize("case", fixture("history"), ids=lambda c: c["name"])
def test_history(case):
    expected = []
    for item in case["output"]:
        text = item["content"][0]["text"]
        expected.append(
            {"role": item["role"], "content": [{"text": text}]}
        )
    assert to_history(case["input"]) == expected


@pytest.mark.parametrize("case", fixture("citations"))
def test_citations(case):
    assert extract_refs(case["text"]) == case["refs"]
    assert resolve_citations(case["text"], dict(case["index"])) == case["citations"]


@pytest.mark.parametrize("case", fixture("filenames"))
def test_filenames(case):
    assert sanitize_filename(case["input"]) == case["output"]


@pytest.mark.parametrize("case", fixture("uploads"))
def test_upload_validation(case):
    if case["status"] == 400:
        with pytest.raises(ValidationError) as exc:
            validate_upload(case["input"], 26214400)
        assert str(exc.value) == case["error"]["message"]
    else:
        assert validate_upload(case["input"], 26214400)["safeName"] == sanitize_filename(
            case["input"]["filename"]
        )


@pytest.mark.parametrize("case", fixture("chat-validation"))
def test_chat_validation(case):
    if case["status"] == 400:
        with pytest.raises(ValidationError) as exc:
            validate_chat(case["body"])
        assert str(exc.value) == case["error"]["message"]
    else:
        assert validate_chat(case["body"])["message"] == json.loads(case["body"])["message"].strip()


def test_js_json_escapes_lone_surrogates_and_preserves_unicode():
    from kb_assistant.core.sse import js_json_dumps

    assert js_json_dumps({"text": "\ud83d"}) == '{"text":"\\ud83d"}'
    assert encode_sse({"type": "text", "delta": "\ud83d"}).encode("utf8")
    assert js_json_dumps("😀") == '"😀"'


@pytest.mark.parametrize("raw", ["1_0", "NaN", "Infinity"])
def test_js_invalid_numeric_limits(raw):
    from kb_assistant.core.paging import parse_limit

    with pytest.raises(ValidationError):
        parse_limit(raw, 25)


@pytest.mark.parametrize("raw", ['{"message":NaN}', '{"message":Infinity}'])
def test_non_json_constants_rejected(raw):
    with pytest.raises(ValidationError, match="Request body must be valid JSON"):
        validate_chat(raw)


def test_javascript_whitespace_trimming():
    assert validate_chat('{"message":"\\ufeffquestion\\ufeff"}')["message"] == "question"
    assert validate_chat('{"message":"\\u0085"}')["message"] == "\u0085"
