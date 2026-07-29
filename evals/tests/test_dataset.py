from __future__ import annotations

import json
from pathlib import Path

import pytest

from kb_evals.dataset import load_dataset, validate_dataset


DATASET_PATH = Path(__file__).parents[1] / "datasets" / "golden.json"


def test_golden_dataset_has_real_records() -> None:
    records = load_dataset(DATASET_PATH)
    assert len(records) >= 5
    assert all(record.expected_facts for record in records)
    assert all(record.question.strip() for record in records)


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
