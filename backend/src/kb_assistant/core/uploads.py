import json
import re
from typing import Any

from kb_assistant.errors import ValidationError

from .citations import UUID_V4
from .text import js_trim, reject_json_constant

CONTENT_TYPES = {
    "application/pdf": (".pdf",),
    "text/plain": (".txt",),
    "text/markdown": (".md",),
    "text/html": (".html", ".htm"),
}


def sanitize_filename(filename: str) -> str:
    base = re.split(r"[\\/]", filename)[-1]
    return re.sub(r"([._-])\1+", r"\1", re.sub(r"[^A-Za-z0-9._-]+", "", base))[:128]


def validate_upload(value: Any, max_upload_bytes: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError()
    if (
        not isinstance(value.get("filename"), str)
        or not isinstance(value.get("contentType"), str)
        or type(value.get("sizeBytes")) not in (int, float)
    ):
        raise ValidationError("filename, contentType, and sizeBytes are required")
    size = value["sizeBytes"]
    if size <= 0 or size > max_upload_bytes or size != int(size):
        raise ValidationError("Invalid file size")
    safe = sanitize_filename(value["filename"])
    if safe in ("", ".", "..") or safe.endswith(".metadata.json"):
        raise ValidationError("Invalid filename")
    if not safe.lower().endswith(CONTENT_TYPES.get(value["contentType"], ())):
        raise ValidationError("File extension does not match content type")
    return {
        "filename": value["filename"],
        "contentType": value["contentType"],
        "sizeBytes": size,
        "safeName": safe,
    }


def parse_json(raw: str | bytes | None) -> Any:
    if raw is None or raw == b"":
        raise ValidationError("Request body is required")
    try:
        return json.loads(raw, parse_constant=reject_json_constant)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError("Request body must be valid JSON") from exc


def validate_chat(raw: str | bytes | None) -> dict[str, Any]:
    value = parse_json(raw)
    if not isinstance(value, dict):
        raise ValidationError()
    message = value.get("message")
    if (
        not isinstance(message, str)
        or not js_trim(message)
        or len(message.encode("utf-16-le", errors="surrogatepass")) // 2 > 8000
    ):
        raise ValidationError("message must be non-empty and no more than 8000 characters")
    if "sessionId" in value and (
        not isinstance(value["sessionId"], str) or not UUID_V4.fullmatch(value["sessionId"])
    ):
        raise ValidationError("sessionId must be a UUID v4")
    return {
        "message": js_trim(message),
        **({"sessionId": value["sessionId"]} if "sessionId" in value else {}),
    }
