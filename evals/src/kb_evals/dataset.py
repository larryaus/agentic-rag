from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kb_evals.models import GoldenRecord


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_string_list(value: Any, field: str, *, non_empty: bool) -> list[str]:
    if not isinstance(value, list) or (non_empty and not value):
        qualifier = "a non-empty" if non_empty else "a"
        raise ValueError(f"{field} must be {qualifier} list of strings")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must contain only non-empty strings")
    return list(value)


def validate_dataset(payload: Any) -> list[GoldenRecord]:
    if not isinstance(payload, list):
        raise ValueError("dataset must be a list")

    seen: set[str] = set()
    records: list[GoldenRecord] = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, dict):
            raise ValueError(f"record {index} must be an object")
        record_id = _require_string(raw.get("id"), f"record {index}.id")
        if record_id in seen:
            raise ValueError(f"duplicate id: {record_id}")
        seen.add(record_id)
        records.append(
            GoldenRecord(
                id=record_id,
                question=_require_string(
                    raw.get("question"), f"record {index}.question"
                ),
                expected_facts=_require_string_list(
                    raw.get("expected_facts"),
                    f"record {index}.expected_facts",
                    non_empty=True,
                ),
                expected_sources=_require_string_list(
                    raw.get("expected_sources"),
                    f"record {index}.expected_sources",
                    non_empty=False,
                ),
            )
        )
    return records


def load_dataset(path: str | Path) -> list[GoldenRecord]:
    with Path(path).open(encoding="utf-8") as handle:
        return validate_dataset(json.load(handle))
