from datetime import datetime, timezone

from agent_service.transcript import messages_for_openai, visible_message_for_ui


def test_messages_for_openai_supports_current_and_previous_assistant_shapes():
    raw = [
        {
            "role": "assistant",
            "content": {
                "text": "new text",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "list_documents", "arguments": "{}"},
                    }
                ],
            },
        },
        {
            "role": "assistant",
            "content": {
                "content": "previous text",
                "tool_calls": [],
            },
        },
    ]

    out = messages_for_openai(raw)

    assert out[0]["content"] == "new text"
    assert out[0]["tool_calls"][0]["id"] == "call_1"
    assert out[1] == {"role": "assistant", "content": "previous text"}


def test_messages_for_openai_converts_legacy_blocks_and_tool_results():
    raw = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "looked it up"},
                {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"ok": true}'},
            ],
        },
    ]

    out = messages_for_openai(raw)

    assert out[0]["content"] == "looked it up"
    assert out[0]["tool_calls"][0]["function"]["name"] == "search"
    assert out[1] == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": '{"ok": true}',
    }


def test_visible_message_for_ui_hides_internal_tool_messages():
    now = datetime.now(timezone.utc)

    assert visible_message_for_ui(
        {"role": "tool", "content": {"content": "{}"}, "citations": [], "created_at": now}
    ) is None
    assert visible_message_for_ui(
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": "{}"}],
            "citations": [],
            "created_at": now,
        }
    ) is None


def test_visible_message_for_ui_flattens_assistant_tool_calls():
    now = datetime.now(timezone.utc)
    row = {
        "role": "assistant",
        "content": {
            "text": "answer",
            "tool_calls": [
                {
                    "function": {
                        "name": "search_knowledge_base",
                        "arguments": '{"query": "leave"}',
                    }
                }
            ],
        },
        "citations": [{"document_id": 1, "chunk_id": 2, "ord": 0, "title": "T", "snippet": "S"}],
        "created_at": now,
    }

    visible = visible_message_for_ui(row)

    assert visible is not None
    assert visible["text"] == "answer"
    assert visible["tool_calls"] == [
        {"name": "search_knowledge_base", "input": {"query": "leave"}}
    ]
    assert visible["citations"][0]["document_id"] == 1
