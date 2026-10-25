import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_context: ContextVar[dict[str, str]] = ContextVar("log_context", default={})


@contextmanager
def log_context(**fields: str) -> Iterator[None]:
    token = _context.set(fields)
    try:
        yield
    finally:
        _context.reset(token)


def set_log_user(sub: str) -> None:
    _context.set({**_context.get(), "userSub": sub})


def log(level: str, msg: str, **fields: Any) -> None:
    print(
        json.dumps(dict(level=level, msg=msg, **_context.get(), **fields), ensure_ascii=True),
        flush=True,
    )
