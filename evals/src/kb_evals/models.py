from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoldenRecord:
    id: str
    question: str
    expected_facts: list[str]
    expected_sources: list[str]
