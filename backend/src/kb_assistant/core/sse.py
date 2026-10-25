"""JSON and frames compatible with JSON.stringify and the deployed SSE contract."""

import json
import math
from typing import Any


def js_json_dumps(value: Any) -> str:
    if isinstance(value, float):
        if not math.isfinite(value):
            return "null"
        if value == 0:
            return "0"
        raw = repr(value).lower()
        if 1e-6 <= abs(value) < 1e21:
            from decimal import Decimal

            return (
                format(Decimal(raw), "f").rstrip("0").rstrip(".")
                if "." in format(Decimal(raw), "f")
                else format(Decimal(raw), "f")
            )
        mantissa, exponent = raw.split("e")
        mantissa = mantissa.removesuffix(".0")
        exp = int(exponent)
        return f"{mantissa}e{'+' if exp >= 0 else ''}{exp}"
    if isinstance(value, dict):
        # JS enumerates canonical integer keys first, then other keys in insertion order.
        def order(key: str) -> tuple[int, int]:
            numeric = (
                key.isascii() and key.isdecimal() and str(int(key)) == key and int(key) < 2**32 - 1
            )
            return (0, int(key)) if numeric else (1, 0)

        return (
            "{"
            + ",".join(
                js_json_dumps(str(k)) + ":" + js_json_dumps(value[k])
                for k in sorted(value, key=lambda k: order(str(k)))
            )
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(js_json_dumps(v) for v in value) + "]"
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return "".join(
        f"\\u{ord(char):04x}" if 0xD800 <= ord(char) <= 0xDFFF else char for char in encoded
    )


FIELDS = {
    "session": ("sessionId",),
    "tool_use": ("name", "input"),
    "citation": ("ref", "title", "documentId", "score", "snippet"),
    "text": ("delta",),
    "done": ("sessionId", "stopReason", "usage"),
    "error": ("message",),
}


def encode_sse(event: dict[str, Any]) -> str:
    kind = event["type"]
    payload = {key: event[key] for key in FIELDS[kind] if key in event}
    return f"event: {kind}\ndata: {js_json_dumps(payload)}\n\n"
