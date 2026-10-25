import asyncio
from collections.abc import Coroutine
from functools import cache
from typing import Any

from kb_assistant.container import Container
from kb_assistant.logging import log_context
from kb_assistant.settings import Settings

from .ingest_event import handle_ingest
from .reconciler import reconcile


@cache
def runtime() -> tuple[asyncio.AbstractEventLoop, Container]:
    # One loop and container per Lambda instance: warm invocations reuse boto3 clients,
    # and loop-bound primitives inside the adapters never see a second loop.
    return asyncio.new_event_loop(), Container(Settings())


def run(work: Coroutine[Any, Any, None]) -> None:
    loop, _ = runtime()
    loop.run_until_complete(work)


def ingest_handler(event: dict[str, Any], context: Any) -> None:
    with log_context(requestId=getattr(context, "aws_request_id", "")):
        run(handle_ingest(event, runtime()[1]))


def reconciler_handler(event: dict[str, Any], context: Any) -> None:
    with log_context(requestId=getattr(context, "aws_request_id", "")):
        run(reconcile(runtime()[1]))
