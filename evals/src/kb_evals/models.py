from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GoldenRecord:
    id: str
    question: str
    expected_facts: list[str]
    expected_sources: list[str]
    # Verbatim passages from the expected sources that answer the question. Retrieval is
    # scored on whether the retrieved chunks contain them; an empty list with no expected
    # sources marks a question the documents do not cover.
    evidence: list[str] = field(default_factory=list)

    @property
    def answerable(self) -> bool:
        return bool(self.expected_sources)
