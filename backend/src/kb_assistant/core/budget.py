"""Keeps a transcript inside a character budget for models with small context windows."""

import json
from copy import deepcopy

from .transcript import AssistantTurn, TextBlock, ToolResults, Turn, UserText

MIN_RESULT_TEXT = 200


def size(turn: Turn) -> int:
    if isinstance(turn, UserText):
        return len(turn.text)
    if isinstance(turn, AssistantTurn):
        return sum(
            len(block.text) if isinstance(block, TextBlock) else len(json.dumps(block.input)) + 64
            for block in turn.blocks
        )
    return sum(
        len(json.dumps(result.json_payload)) if result.json_payload is not None else len(result.text or "")
        for result in turn.results
    )


def fit(system: str, transcript: list[Turn], history_length: int, budget: int) -> list[Turn]:
    """Drops the oldest history pairs, then shortens this turn's oldest search results.

    The current question is never dropped, and shortened results keep their ref, title
    and document ID so citations still resolve.
    """
    total = len(system) + sum(size(turn) for turn in transcript)
    if total <= budget:
        return transcript
    history, current = transcript[:history_length], transcript[history_length:]
    while history and total > budget:
        total -= size(history[0]) + size(history[1])
        history = history[2:]
    if total > budget:
        current = deepcopy(current)
        for turn in current:
            if not isinstance(turn, ToolResults) or total <= budget:
                continue
            for result in turn.results:
                for hit in (result.json_payload or {}).get("results", []):
                    if total <= budget:
                        break
                    text = hit.get("text", "")
                    keep = max(MIN_RESULT_TEXT, len(text) - (total - budget))
                    if keep < len(text):
                        total -= len(text) - keep
                        hit["text"] = text[:keep]
    return [*history, *current]
