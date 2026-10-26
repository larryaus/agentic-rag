"""Scores full answers: expected facts, citations, and refusals.

Citations are checked exactly: an answer to a covered question should cite one of its
expected sources, and an answer to an uncovered question should cite nothing. Whether an
answer states each expected fact is judged by a model, since wording varies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from kb_evals.models import GoldenRecord

JUDGE_PROMPT = """You grade answers from a company knowledge-base assistant.
For each numbered fact, decide whether the answer states it or clearly implies it. Wording
may differ, but numbers, durations, amounts and names must match the fact.
Reply with JSON only, one boolean per fact in order: {"supported": [true, false]}"""

Judge = Callable[[str, str, Sequence[str]], Awaitable[list[bool]]]


def parse_verdict(reply: str, count: int) -> list[bool]:
    """The judge's booleans; reasoning models may think aloud first, so the last object wins."""
    reply = re.sub(r"(?s)<think>.*?(?:</think>|$)", "", reply)
    objects = re.findall(r"\{[^{}]*\}", reply)
    if not objects:
        raise ValueError("judge reply has no JSON object")
    supported = json.loads(objects[-1]).get("supported")
    if (
        not isinstance(supported, list)
        or len(supported) != count
        or not all(isinstance(value, bool) for value in supported)
    ):
        raise ValueError(f"judge must return {count} booleans")
    return supported


class LlmJudge:
    """A judge on any OpenAI-compatible chat model."""

    def __init__(self, client: Any, model: str) -> None:
        self.client = client
        self.model = model

    async def __call__(self, question: str, answer: str, facts: Sequence[str]) -> list[bool]:
        numbered = "\n".join(f"{index}. {fact}" for index, fact in enumerate(facts, 1))
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": JUDGE_PROMPT},
                {
                    "role": "user",
                    "content": f"Question: {question}\n\nAnswer:\n{answer}\n\nFacts:\n{numbered}",
                },
            ],
            temperature=0,
            max_tokens=512,
        )
        return parse_verdict(response.choices[0].message.content or "", len(facts))


@dataclass(frozen=True)
class AnswerScore:
    id: str
    answerable: bool
    supported: list[bool]
    cited: list[str]
    expected_sources: list[str]
    stop_reason: str

    @property
    def fact_recall(self) -> float:
        return sum(self.supported) / len(self.supported) if self.supported else 0.0

    @property
    def cites_expected(self) -> bool:
        return any(title in self.expected_sources for title in self.cited)

    @property
    def refused(self) -> bool:
        # An uncovered question's fact says the assistant declined; it must cite nothing.
        return all(self.supported) and not self.cited


def score_answer(record: GoldenRecord, reply: dict[str, Any], supported: list[bool]) -> AnswerScore:
    return AnswerScore(
        id=record.id,
        answerable=record.answerable,
        supported=supported,
        cited=list(dict.fromkeys(reply["citations"])),
        expected_sources=list(record.expected_sources),
        stop_reason=reply["stopReason"],
    )


def summarize_answers(scores: Sequence[AnswerScore]) -> dict[str, float]:
    covered = [score for score in scores if score.answerable]
    uncovered = [score for score in scores if not score.answerable]
    cited = [score for score in covered if score.cited]
    summary = {
        "fact_recall": sum(score.fact_recall for score in covered) / len(covered) if covered else 0.0,
        "cites_expected_source": sum(score.cites_expected for score in covered) / len(covered)
        if covered
        else 0.0,
        "citation_precision": sum(
            sum(title in score.expected_sources for title in score.cited) / len(score.cited)
            for score in cited
        )
        / len(cited)
        if cited
        else 0.0,
        "refusal": sum(score.refused for score in uncovered) / len(uncovered) if uncovered else 0.0,
        "errors": float(sum(score.stop_reason == "error" for score in scores)),
    }
    return {name: round(value, 4) for name, value in summary.items()}


def report_answers(title: str, summary: dict[str, float], scores: Sequence[AnswerScore]) -> str:
    lines = [f"### {title}", "", "| Metric | Score |", "|---|---|"]
    lines += [f"| {name} | {value:.4f} |" for name, value in summary.items()]
    lines += ["", "| Record | Facts supported | Cited | Stop reason |", "|---|---|---|---|"]
    for score in scores:
        facts = f"{sum(score.supported)}/{len(score.supported)}"
        cited = ", ".join(score.cited) or "none"
        lines.append(f"| {score.id} | {facts} | {cited} | {score.stop_reason} |")
    return "\n".join(lines) + "\n"
