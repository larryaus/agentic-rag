import base64
import json
import math

from kb_assistant.errors import ValidationError

from .sse import js_json_dumps
from .text import js_trim


def encode_page_token(key: dict[str, str]) -> str:
    return base64.urlsafe_b64encode(js_json_dumps(key).encode()).decode().rstrip("=")


def decode_page_token(
    token: str, expected_keys: list[str], partition_key: str, partition: str
) -> dict[str, str]:
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if not isinstance(data, dict) or set(data) != set(expected_keys):
            raise ValueError()
        if any(not isinstance(v, str) for v in data.values()) or data[partition_key] != partition:
            raise ValueError()
        return dict(data)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValidationError("Malformed nextToken") from exc


def parse_limit(raw: str | None, fallback: int) -> int:
    if raw is None:
        return fallback
    try:
        raw = js_trim(raw)
        if "_" in raw or not raw.isascii():
            raise ValueError()
        # JavaScript Number also accepts whitespace, exponent and hexadecimal notation.
        value = (
            float(int(raw, 0))
            if raw.strip().lower().startswith(("0x", "0o", "0b"))
            else float(raw.strip() or "0")
        )
        if not math.isfinite(value) or not value.is_integer() or not 1 <= value <= 100:
            raise ValueError()
        return int(value)
    except ValueError as exc:
        raise ValidationError("limit must be between 1 and 100") from exc
