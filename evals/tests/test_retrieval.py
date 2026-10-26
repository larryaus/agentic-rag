from __future__ import annotations

import json

import pytest

from kb_evals.models import GoldenRecord
from kb_evals.retrieval import (
    Retrieved,
    load_baseline,
    regressions,
    report,
    score_all,
    score_record,
    summarize,
)


def record(id: str, evidence: list[str], sources: list[str] | None = None) -> GoldenRecord:
    return GoldenRecord(id, "q", ["f"], ["a.md"] if sources is None else sources, evidence)


def chunk(text: str) -> Retrieved:
    return Retrieved("a.md", text)


def test_scores_rank_and_recall_from_evidence() -> None:
    score = score_record(
        record("r", ["10 days", "15 days"]),
        [chunk("nothing"), chunk("after 1 year,\n10 days of leave"), chunk("then 15 days")],
    )
    assert score.first_rank == 2
    assert score.recall == {1: 0.0, 3: 1.0, 5: 1.0}


def test_evidence_matches_across_rewrapped_lines_but_not_across_chunks() -> None:
    wrapped = score_record(record("r", ["paid annual leave"]), [chunk("paid\n  annual leave")])
    assert wrapped.first_rank == 1
    split = score_record(record("r", ["paid annual leave"]), [chunk("paid"), chunk("annual leave")])
    assert split.first_rank is None and split.recall[3] == 0.0


def test_summary_skips_unanswerable_records_and_averages() -> None:
    records = [record("hit", ["x"]), record("miss", ["y"]), record("oos", [], sources=[])]
    ranked = {"hit": [chunk("x")], "miss": [chunk("z"), chunk("y")], "oos": [chunk("x")]}
    scores = score_all(records, ranked)
    assert [score.id for score in scores] == ["hit", "miss"]
    assert summarize(scores) == {
        "hit@1": 0.5,
        "mrr": 0.75,
        "recall@1": 0.5,
        "recall@3": 1.0,
        "recall@5": 1.0,
    }
    with pytest.raises(ValueError, match="no answerable records"):
        summarize([])


def test_regressions_respect_the_tolerance() -> None:
    baseline = {"hit@1": 0.8, "mrr": 0.9}
    assert regressions({"hit@1": 0.76, "mrr": 0.95}, baseline, 0.05) == []
    [failure] = regressions({"hit@1": 0.7, "mrr": 0.95}, baseline, 0.05)
    assert failure.startswith("hit@1: 0.7000 < baseline 0.8000")
    assert regressions({}, {"mrr": 0.1}, 0.0)  # A missing metric counts as zero.


def test_baseline_file_shape(tmp_path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"metrics": {"mrr": 0.9}, "tolerance": 0.02}))
    assert load_baseline(path)["tolerance"] == 0.02
    path.write_text(json.dumps({"metrics": {"mrr": 0.9}}))
    with pytest.raises(ValueError, match="numeric tolerance"):
        load_baseline(path)


def test_report_lists_summary_and_records() -> None:
    scores = score_all([record("r", ["x"])], {"r": [chunk("y")]})
    text = report("Retrieval", summarize(scores), scores, {"mrr": 1.0})
    assert "| mrr | 0.0000 | 1.0000 |" in text
    assert "| r | not retrieved | 0.00 |" in text
