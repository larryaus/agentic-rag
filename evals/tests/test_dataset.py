from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_evals.dataset import load_dataset, validate_dataset


DATASET_PATH = Path(__file__).parents[1] / "datasets" / "golden.json"
SAMPLES = Path(__file__).parents[2] / "samples"


def test_golden_dataset_has_real_records() -> None:
    records = load_dataset(DATASET_PATH)
    assert len(records) >= 20
    assert all(record.expected_facts for record in records)
    assert all(record.question.strip() for record in records)
    assert any(not record.answerable for record in records)
    assert any(len(record.expected_sources) > 1 for record in records)


def test_evidence_is_quoted_from_an_expected_source() -> None:
    # Editing a sample document must not silently invalidate the dataset.
    for record in load_dataset(DATASET_PATH):
        texts = [(SAMPLES / source).read_text(encoding="utf-8") for source in record.expected_sources]
        for passage in record.evidence:
            assert any(passage in text for text in texts), (record.id, passage)


def test_rejects_duplicate_ids() -> None:
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    payload.append(dict(payload[0]))
    with pytest.raises(ValueError, match="duplicate id"):
        validate_dataset(payload)


def test_rejects_empty_question_and_invalid_lists() -> None:
    with pytest.raises(ValueError, match="question"):
        validate_dataset(
            [
                {
                    "id": "bad",
                    "question": "",
                    "expected_facts": ["fact"],
                    "expected_sources": [],
                }
            ]
        )
    with pytest.raises(ValueError, match="expected_facts"):
        validate_dataset(
            [
                {
                    "id": "bad",
                    "question": "question",
                    "expected_facts": [],
                    "expected_sources": [],
                }
            ]
        )


def test_evidence_must_match_whether_sources_are_expected() -> None:
    base = {"id": "r", "question": "q", "expected_facts": ["f"]}
    with pytest.raises(ValueError, match="evidence is required"):
        validate_dataset([{**base, "expected_sources": ["a.md"]}])
    with pytest.raises(ValueError, match="evidence must be empty"):
        validate_dataset([{**base, "expected_sources": [], "evidence": ["x"]}])
    [record] = validate_dataset([{**base, "expected_sources": []}])
    assert not record.answerable and record.evidence == []
