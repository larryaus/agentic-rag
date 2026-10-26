"""Scores retrieval against the golden dataset's evidence passages.

A record's evidence is a set of verbatim passages from its expected sources. Retrieval is
scored on where they appear in the ranked chunks: whether the top chunk holds any of
them, what share of them the top k chunks hold, and the reciprocal rank of the first
chunk that holds one. Questions the documents do not cover have no evidence and are not
scored here; the answer evaluation checks that they are refused.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from kb_evals.models import GoldenRecord

KS = (1, 3, 5)


@dataclass(frozen=True)
class Retrieved:
    """One ranked chunk, as the search tool returns it."""

    title: str
    text: str


@dataclass(frozen=True)
class RecordScore:
    id: str
    # 1-based rank of the first chunk holding any evidence, or None if none does.
    first_rank: int | None
    recall: dict[int, float]


def _normalize(text: str) -> str:
    # Chunking may rewrap lines, so whitespace runs compare equal.
    return re.sub(r"\s+", " ", text).strip()


def score_record(record: GoldenRecord, ranked: Sequence[Retrieved], ks: Iterable[int] = KS) -> RecordScore:
    evidence = [_normalize(passage) for passage in record.evidence]
    chunks = [_normalize(chunk.text) for chunk in ranked]
    first_rank = next(
        (rank for rank, chunk in enumerate(chunks, 1) if any(p in chunk for p in evidence)),
        None,
    )
    recall = {}
    for k in ks:
        top = " \u0000 ".join(chunks[:k])
        recall[k] = sum(p in top for p in evidence) / len(evidence)
    return RecordScore(record.id, first_rank, recall)


def summarize(scores: Sequence[RecordScore], ks: Iterable[int] = KS) -> dict[str, float]:
    if not scores:
        raise ValueError("no answerable records to score")
    count = len(scores)
    summary = {
        "hit@1": sum(score.first_rank == 1 for score in scores) / count,
        "mrr": sum(1 / score.first_rank for score in scores if score.first_rank) / count,
    }
    for k in ks:
        summary[f"recall@{k}"] = sum(score.recall[k] for score in scores) / count
    return {name: round(value, 4) for name, value in summary.items()}


def score_all(
    records: Sequence[GoldenRecord], ranked: Mapping[str, Sequence[Retrieved]]
) -> list[RecordScore]:
    return [score_record(record, ranked[record.id]) for record in records if record.answerable]


def regressions(
    summary: Mapping[str, float], baseline: Mapping[str, float], tolerance: float
) -> list[str]:
    """Metrics that fell more than `tolerance` below the baseline."""
    return [
        f"{name}: {summary.get(name, 0.0):.4f} < baseline {expected:.4f} - {tolerance}"
        for name, expected in baseline.items()
        if summary.get(name, 0.0) < expected - tolerance
    ]


def load_baseline(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        baseline: dict[str, Any] = json.load(handle)
    if not isinstance(baseline.get("metrics"), dict) or not isinstance(
        baseline.get("tolerance"), (int, float)
    ):
        raise ValueError("baseline needs a metrics object and a numeric tolerance")
    return baseline


def report(
    title: str,
    summary: Mapping[str, float],
    scores: Sequence[RecordScore],
    baseline: Mapping[str, float] | None = None,
) -> str:
    """A Markdown report: the summary against the baseline, then each record."""
    lines = [f"### {title}", "", "| Metric | Score | Baseline |", "|---|---|---|"]
    for name, value in summary.items():
        expected = (baseline or {}).get(name)
        lines.append(f"| {name} | {value:.4f} | {'' if expected is None else f'{expected:.4f}'} |")
    lines += ["", "| Record | First evidence rank | Recall@3 |", "|---|---|---|"]
    for score in scores:
        rank = "not retrieved" if score.first_rank is None else str(score.first_rank)
        lines.append(f"| {score.id} | {rank} | {score.recall.get(3, 0.0):.2f} |")
    return "\n".join(lines) + "\n"
