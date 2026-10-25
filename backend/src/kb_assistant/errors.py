import json
from typing import Any


class AppError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ValidationError(AppError):
    def __init__(self, message: str = "Invalid request") -> None:
        super().__init__(message, 400)


class UnauthorizedError(AppError):
    def __init__(self) -> None:
        super().__init__("Unauthorized", 401)


class ModelStreamError(Exception):
    pass


class AgentStreamError(Exception):
    def __init__(self, name: str, partial_result: dict[str, Any]) -> None:
        super().__init__(name)
        self.partial_result = partial_result


def error_fields(error: BaseException) -> dict[str, str]:
    """Log fields naming an error. An AWS error is named by its service code, since every
    botocore error is a ClientError; its message names the action or parameter at fault."""
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, dict) else None
    code = details.get("Code") if isinstance(details, dict) else None
    if not isinstance(code, str) or not code:
        return {"errorType": type(error).__name__}
    message = details.get("Message") if isinstance(details, dict) else None
    fields = {"errorType": code}
    if isinstance(message, str) and message:
        fields["errorMessage"] = message[:500]
    return fields


def error_message(error: BaseException) -> str:
    name = (
        "SyntaxError"
        if isinstance(error, json.JSONDecodeError)
        else "Error"
        if type(error) in (ValueError, RuntimeError)
        else type(error).__name__
    )
    return f"{name}: {error}"
