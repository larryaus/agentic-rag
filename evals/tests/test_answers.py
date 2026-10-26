from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kb_evals.answers import (
    JUDGE_PROMPT,
    LlmJudge,
    parse_verdict,
    report_answers,
    score_answer,
    summarize_answers,
)
from kb_evals.models import GoldenRecord


def test_verdict_takes_the_last_object_after_any_thinking() -> None:
    reply = '<think>maybe {"supported": [false]}</think> Sure: {"supported": [true, false]}'
    assert parse_verdict(reply, 2) == [True, False]


@pytest.mark.parametrize(
    "reply", ["no json", '{"supported": [true]}', '{"supported": [1, 0]}', '{"other": []}']
)
def test_verdict_must_be_one_boolean_per_fact(reply: str) -> None:
    with pytest.raises(ValueError):
        parse_verdict(reply, 2)


def test_judge_sends_numbered_facts_at_temperature_zero() -> None:
    seen = {}

    async def create(**request):
        seen.update(request)
        message = SimpleNamespace(content='{"supported": [true, true]}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    verdict = asyncio.run(LlmJudge(client, "judge")("q?", "an answer", ["one", "two"]))
    assert verdict == [True, True]
    assert seen["model"] == "judge" and seen["temperature"] == 0
    assert seen["messages"][0]["content"] == JUDGE_PROMPT
    assert seen["messages"][1]["content"].endswith("Facts:\n1. one\n2. two")


def covered(id: str = "c") -> GoldenRecord:
    return GoldenRecord(id, "q", ["a", "b"], ["handbook.md"], ["e"])


def uncovered(id: str = "u") -> GoldenRecord:
    return GoldenRecord(id, "q", ["declines"], [], [])


def reply(*cited: str, stop: str = "end_turn") -> dict:
    return {"text": "t", "stopReason": stop, "citations": list(cited)}


def test_scores_facts_citations_and_refusals() -> None:
    scores = [
        score_answer(covered("good"), reply("handbook.md", "handbook.md"), [True, True]),
        score_answer(covered("mixed"), reply("faq.md", "handbook.md"), [True, False]),
        score_answer(covered("uncited"), reply(), [False, False]),
        score_answer(uncovered("declined"), reply(), [True]),
        score_answer(uncovered("answered"), reply("faq.md"), [True]),
        score_answer(covered("broken"), reply(stop="error"), [False, False]),
    ]
    assert scores[0].cited == ["handbook.md"]  # Repeated citations count once.
    assert summarize_answers(scores) == {
        "fact_recall": 0.375,
        "cites_expected_source": 0.5,
        "citation_precision": 0.75,
        "refusal": 0.5,
        "errors": 1.0,
    }
    text = report_answers("Answers", summarize_answers(scores), scores)
    assert "| mixed | 1/2 | faq.md, handbook.md | end_turn |" in text
    assert "| declined | 1/1 | none | end_turn |" in text
