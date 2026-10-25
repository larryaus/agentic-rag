from datetime import UTC, datetime
from uuid import uuid4


def now() -> datetime:
    return datetime.now(UTC)


def iso_ms(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def message_sk(created_at: str, id: str | None = None) -> str:
    return f"MSG#{created_at}#{id if id is not None else uuid4()}"
