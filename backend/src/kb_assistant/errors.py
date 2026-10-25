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


def error_message(error: BaseException) -> str:
    name = (
        "SyntaxError"
        if isinstance(error, json.JSONDecodeError)
        else "Error"
        if type(error) in (ValueError, RuntimeError)
        else type(error).__name__
    )
    return f"{name}: {error}"
