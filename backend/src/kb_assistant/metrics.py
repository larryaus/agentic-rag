"""One record per chat turn: how it ended, how long it took and what it used.

Both deployments log the record. On AWS it also carries CloudWatch Embedded Metric
Format directives, so the Lambda log stream publishes it as metrics without an API call
or extra permissions.
"""

import time
from collections.abc import Callable, Mapping
from typing import Any

# Published per model. Each turn is also counted per model and stop reason.
TURN_METRICS = {
    "turnMs": "Milliseconds",
    "firstTextMs": "Milliseconds",
    "inputTokens": "Count",
    "outputTokens": "Count",
    "toolCalls": "Count",
}


class TurnMeter:
    """Times a chat turn and counts its searches from the events it streams."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.first_text: float | None = None
        self.tool_calls = 0

    def watch(self, emit: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
        def metered(event: dict[str, Any]) -> None:
            if event["type"] == "text" and self.first_text is None:
                self.first_text = time.monotonic()
            elif event["type"] == "tool_use":
                self.tool_calls += 1
            emit(event)

        return metered

    def record(self, *, model: str, stop_reason: str, usage: Mapping[str, int]) -> dict[str, Any]:
        record: dict[str, Any] = dict(
            model=model,
            stopReason=stop_reason,
            turnMs=_ms(time.monotonic() - self.started),
        )
        # A turn that failed before any text has no time to first text.
        if self.first_text is not None:
            record["firstTextMs"] = _ms(self.first_text - self.started)
        record.update(
            inputTokens=usage.get("inputTokens", 0),
            outputTokens=usage.get("outputTokens", 0),
            toolCalls=self.tool_calls,
        )
        return record


def embedded_metrics(record: Mapping[str, Any], namespace: str) -> dict[str, Any]:
    """The fields that make CloudWatch publish a turn record as metrics."""
    return {
        "turns": 1,
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [["model"]],
                    "Metrics": [
                        {"Name": name, "Unit": unit}
                        for name, unit in TURN_METRICS.items()
                        if name in record
                    ],
                },
                {
                    "Namespace": namespace,
                    "Dimensions": [["model", "stopReason"]],
                    "Metrics": [{"Name": "turns", "Unit": "Count"}],
                },
            ],
        },
    }


def _ms(seconds: float) -> int:
    return round(seconds * 1000)
