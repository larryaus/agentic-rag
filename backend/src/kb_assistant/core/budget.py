"""Keeps the messages sent to a model inside a character budget, for small context windows."""

import json
from copy import deepcopy

from strands.types.content import Message, Messages

MIN_RESULT_TEXT = 200


def size(message: Message) -> int:
    total = 0
    for block in message["content"]:
        if "text" in block:
            total += len(block["text"])
        elif "toolUse" in block:
            total += len(json.dumps(block["toolUse"]["input"])) + 64
        elif "toolResult" in block:
            for part in block["toolResult"]["content"]:
                total += len(json.dumps(part["json"])) if "json" in part else len(part.get("text", ""))
    return total


def fit(system: str, messages: Messages, history_length: int, budget: int) -> Messages:
    """Drops the oldest history pairs, then shortens this turn's oldest search results.

    The current question is never dropped, and shortened results keep their ref, title
    and document ID so citations still resolve.
    """
    total = len(system) + sum(size(message) for message in messages)
    if total <= budget:
        return messages
    history, current = messages[:history_length], messages[history_length:]
    while history and total > budget:
        total -= size(history[0]) + size(history[1])
        history = history[2:]
    if total > budget:
        current = deepcopy(current)
        for message in current:
            for block in message["content"]:
                if "toolResult" not in block or total <= budget:
                    continue
                for part in block["toolResult"]["content"]:
                    hits = part["json"].get("results", []) if "json" in part else []
                    for hit in hits:
                        if total <= budget:
                            break
                        text = hit.get("text", "")
                        keep = max(MIN_RESULT_TEXT, len(text) - (total - budget))
                        if keep < len(text):
                            total -= len(text) - keep
                            hit["text"] = text[:keep]
    return [*history, *current]
