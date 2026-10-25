"""Local-deployment pieces that need no database: guardrail, budget, chunking, loaders."""

import json

import pytest
from test_agent import DID, Model, Retrieval, run

from kb_assistant.adapters.local.chunker import chunk_text, estimate_tokens
from kb_assistant.adapters.local.guardrail import REFUSAL, LocalGuardrail
from kb_assistant.adapters.local.loaders import extract_text
from kb_assistant.core.budget import fit, size
from kb_assistant.core.ports import Stop, TextDelta
from kb_assistant.core.transcript import (
    AssistantTurn,
    TextBlock,
    ToolResult,
    ToolResults,
    ToolUseBlock,
    UserText,
)
from kb_assistant.errors import AgentStreamError, ModelStreamError


class Classifier:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def complete(self, system, user, max_tokens=64):
        self.prompts.append(user)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.mark.parametrize(
    "reply,allowed",
    [
        ('{"allowed": true}', True),
        ('Sure: {"allowed": false}', False),
        ('<think>Is {"allowed": true} right? No.</think>\n{"allowed": false}', False),
        ("<think>still thinking about whether to say {", True),
        # Cut off mid-thought after quoting the format: no verdict, so it fails open.
        ('<think>Benign HR question. Format: {"allowed": true} or {"allowed": false}. Check', True),
        (TimeoutError("slow classifier"), True),
        ("not json", True),
        ('{"allowed": "no"}', True),
        (RuntimeError("down"), True),
    ],
)
async def test_input_check_and_fail_open(reply, allowed, capsys):
    verdict = await LocalGuardrail(Classifier(reply), check_input=True).check_input("q")
    assert verdict.allowed is allowed
    assert verdict.message == ("" if allowed else REFUSAL)
    if allowed and reply != '{"allowed": true}':
        assert "input check failed open" in capsys.readouterr().out


async def test_input_check_off_skips_the_model():
    classifier = Classifier('{"allowed": false}')
    assert (await LocalGuardrail(classifier, check_input=False).check_input("q")).allowed
    assert classifier.prompts == []


async def test_refusal_returns_guardrail_stop_without_model_call():
    model, events = Model(), []
    result = await run(
        model,
        Retrieval(),
        events,
        guardrail=LocalGuardrail(Classifier('{"allowed": false}'), check_input=True),
    )
    assert result["text"] == REFUSAL and result["stopReason"] == "guardrail_intervened"
    assert events == [{"type": "text", "delta": REFUSAL}] and model.calls == []


async def test_answers_are_masked_in_stream_and_result():
    secret = "4111 1111 1111 1111"
    model = Model([*(TextDelta(0, c) for c in f"Card {secret} ok"), Stop("end_turn")])
    events = []
    result = await run(
        model, Retrieval(), events, guardrail=LocalGuardrail(Classifier(""), check_input=False)
    )
    streamed = "".join(e["delta"] for e in events if e["type"] == "text")
    assert streamed == result["text"] == "Card {CREDIT_DEBIT_CARD_NUMBER} ok"


async def test_stream_failure_partial_result_is_masked():
    model = Model([TextDelta(0, "password: hunt"), ModelStreamError("cut")])
    events = []
    with pytest.raises(AgentStreamError) as raised:
        await run(
            model, Retrieval(), events, guardrail=LocalGuardrail(Classifier(""), check_input=False)
        )
    partial = raised.value.partial_result["text"]
    assert "hunt" not in partial and "hunt" not in json.dumps(events)


def _history(pairs, size):
    turns = []
    for index in range(pairs):
        turns += [UserText(f"q{index} " + "h" * size), AssistantTurn([TextBlock(0, "a" * size)])]
    return turns


def _round(ref):
    hits = [
        dict(ref=ref + i, title="doc.md", documentId=DID, score=0.5, text="t" * 1500)
        for i in range(8)
    ]
    call = ToolUseBlock(0, f"t{ref}", "search_knowledge_base", {"query": "q"})
    return [
        AssistantTurn([call]),
        ToolResults([ToolResult(f"t{ref}", True, json_payload={"results": hits})]),
    ]


def test_budget_drops_oldest_history_first_and_keeps_refs():
    history = _history(10, 4000)
    current = [UserText("current question", guard=True)]
    for round_index in range(5):
        current += _round(1 + round_index * 8)
    transcript = history + current
    fitted = fit("system", transcript, len(history), 90_000)
    assert len("system") + sum(size(t) for t in fitted) <= 90_000
    assert fitted[-len(current)].text == "current question"
    kept_history = fitted[: len(fitted) - len(current)]
    # Whole pairs, newest kept.
    assert len(kept_history) % 2 == 0
    if kept_history:
        assert kept_history[-2].text.startswith("q9")
    refs = [
        hit["ref"]
        for t in fitted
        if isinstance(t, ToolResults)
        for hit in t.results[0].json_payload["results"]
    ]
    assert refs == list(range(1, 41))


def test_budget_truncates_tool_text_when_history_is_not_enough():
    current = [UserText("q", guard=True)]
    for round_index in range(5):
        current += _round(1 + round_index * 8)
    fitted = fit("s", current, 0, 30_000)
    assert sum(size(t) for t in fitted) + 1 <= 30_000
    hits = fitted[2].results[0].json_payload["results"]
    assert all(set(hit) == {"ref", "title", "documentId", "score", "text"} for hit in hits)
    # The original transcript is untouched.
    assert current[2].results[0].json_payload["results"][0]["text"] == "t" * 1500


async def test_agent_applies_budget_only_when_configured():
    model = Model([TextDelta(0, "ok"), Stop("end_turn")], [TextDelta(0, "ok"), Stop("end_turn")])
    history = _history(3, 1000)
    await run(model, Retrieval(), history=history)
    await run(model, Retrieval(), history=history, max_context_chars=1500)
    assert len(model.calls[0][1]) == 7 and len(model.calls[1][1]) == 1


def test_chunker_units_and_overlap():
    assert chunk_text("   ") == []
    for text in [
        "word " * 3000,
        "这是一个测试。" * 400,
        "mixed 混合 text 文本 " * 500,
        "x" * 20000,
        "# Title\n\n## Section\n\n" + "para\n\n" * 400,
    ]:
        chunks = chunk_text(text)
        assert chunks and all(estimate_tokens(c) <= 512 for c in chunks)
        for previous, current in zip(chunks, chunks[1:]):
            # Each chunk opens with the end of the one before it.
            assert any(current.startswith(previous[-n:]) for n in range(1, len(previous) + 1))


def test_loaders(tmp_path):
    html = b"<html><style>x{}</style><script>bad()</script><p>Hello</p><p>World</p></html>"
    assert extract_text(html, "text/html").split() == ["Hello", "World"]
    assert extract_text("caf\xe9".encode("latin-1"), "text/plain") == "caf�"
    assert extract_text("# Hi".encode(), "text/markdown") == "# Hi"
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(72, 72)
    path = tmp_path / "blank.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    assert extract_text(path.read_bytes(), "application/pdf").strip() == ""


@pytest.mark.parametrize("reason", ["max_tokens", "content_filtered", "abort"])
async def test_truncated_answers_drop_the_held_tail(reason):
    prefix = "The key is wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKE"  # 39 of 40 characters
    model = Model([*(TextDelta(0, c) for c in "secret " + prefix), Stop(reason)])
    events = []
    result = await run(
        model, Retrieval(), events, guardrail=LocalGuardrail(Classifier(""), check_input=False)
    )
    streamed = "".join(e["delta"] for e in events if e["type"] == "text")
    assert "wJalrXUtnFEMI" not in streamed and "wJalrXUtnFEMI" not in result["text"]
    assert result["stopReason"] == reason


async def test_running_out_of_tool_iterations_drops_the_held_tail():
    from test_agent import tool

    prefix = "secret wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKE"
    model = Model([*(TextDelta(0, c) for c in prefix), tool(index=1), Stop("tool_use")])
    events = []
    result = await run(
        model,
        Retrieval(),
        events,
        guardrail=LocalGuardrail(Classifier(""), check_input=False),
        max_iterations=1,
    )
    assert result["stopReason"] == "max_iterations"
    assert "wJalrXUtnFEMI" not in json.dumps(events) and "wJalrXUtnFEMI" not in result["text"]


async def test_running_out_of_tool_iterations_with_only_held_text_uses_the_fallback():
    from test_agent import tool

    model = Model(*[[TextDelta(0, "Let me search."), tool(index=1), Stop("tool_use")]] * 2)
    events = []
    result = await run(
        model,
        Retrieval([]),
        events,
        guardrail=LocalGuardrail(Classifier(""), check_input=False),
        max_iterations=2,
    )
    assert result["stopReason"] == "max_iterations"
    # The dropped tail leaves only the truncation mark, which is not an answer.
    assert result["text"].startswith("I could not complete this question")
