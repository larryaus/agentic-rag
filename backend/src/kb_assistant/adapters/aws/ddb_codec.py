from decimal import Decimal
from typing import Any


def to_ddb(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: to_ddb(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [to_ddb(item) for item in value if item is not None]
    return value


def from_ddb(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: from_ddb(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_ddb(item) for item in value]
    return value
