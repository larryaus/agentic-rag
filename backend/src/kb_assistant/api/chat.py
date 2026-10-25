import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request, Response
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from kb_assistant.core.agent import run_agent
from kb_assistant.core.citations import js_slice
from kb_assistant.core.history import make_message_item, make_session_item, to_history
from kb_assistant.core.sse import encode_sse
from kb_assistant.core.text import JS_SPACES
from kb_assistant.core.uploads import validate_chat
from kb_assistant.errors import AgentStreamError, AppError
from kb_assistant.logging import log
from kb_assistant.timeutil import iso_ms, now

from .deps import Auth, Dependencies

router = APIRouter()
GENERIC_FAILURE = "The request could not be completed. Please try again."
STREAM_FAILURE = "The model response stream ended unexpectedly. Please try again."


class ChatResponse(Response):
    media_type = "text/event-stream"

    def __init__(
        self,
        turn: Callable[[Callable[[dict[str, Any]], None]], Awaitable[None]],
        deadline_seconds: float,
    ) -> None:
        super().__init__(status_code=200, headers={"cache-control": "no-cache"})
        self.raw_headers = [
            (key, value) for key, value in self.raw_headers if key != b"content-length"
        ]
        self.turn = turn
        self.deadline_seconds = deadline_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        disconnected = False
        cancellation: asyncio.CancelledError | None = None
        deadline = asyncio.get_running_loop().time() + self.deadline_seconds

        def emit(event: dict[str, Any]) -> None:
            if not disconnected:
                queue.put_nowait(encode_sse(event).encode("utf-8"))

        async def run() -> None:
            try:
                await self.turn(emit)
            finally:
                queue.put_nowait(None)

        async def pump() -> None:
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            while True:
                frame = await queue.get()
                await send(
                    {
                        "type": "http.response.body",
                        "body": frame or b"",
                        "more_body": frame is not None,
                    }
                )
                if frame is None:
                    return

        async def listen() -> None:
            while (await receive())["type"] != "http.disconnect":
                pass

        turn = asyncio.create_task(run())
        sender = asyncio.create_task(pump())
        listener = asyncio.create_task(listen())
        try:
            await asyncio.wait(
                {sender, listener},
                timeout=max(0, deadline - asyncio.get_running_loop().time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError as exc:
            cancellation = exc
        finally:
            disconnected = True
            sender.cancel()
            listener.cancel()
            # Cancellation belongs to the transport, not the durable turn. Repeated
            # cancellation must not detach the work from the Lambda invocation.
            while not turn.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    turn.cancel()
                    break
                try:
                    await asyncio.wait({turn}, timeout=remaining)
                except asyncio.CancelledError as exc:
                    cancellation = exc
            for task in (sender, listener, turn):
                if task.done():
                    try:
                        task.result()
                    except (asyncio.CancelledError, OSError, ClientDisconnect):
                        pass
                    except Exception as exc:
                        log("error", "chat response task failed", errorType=type(exc).__name__)
                else:
                    # Consume a cancelled task's result without extending the hard deadline.
                    task.add_done_callback(_consume_task)
        if cancellation is not None:
            raise cancellation


def _consume_task(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


@router.post("/")
@router.post("/v1/chat")
async def chat(request: Request, auth: Auth, deps: Dependencies) -> Response:
    body = validate_chat(await request.body())
    is_new = "sessionId" not in body
    session_id = body.get("sessionId", str(uuid4()))
    store = deps.store
    if not is_new:
        meta = await store.get_session(session_id)
        if meta is None:
            raise AppError("Session not found", 404)
        if meta["userSub"] != auth.sub:
            raise AppError("Forbidden", 403)
    started = now()
    created_at = iso_ms(started)
    ttl = int((started + timedelta(days=deps.settings.session_ttl_days)).timestamp())
    if is_new:
        await store.create_session(
            make_session_item(
                sessionId=session_id,
                userSub=auth.sub,
                title=js_slice(JS_SPACES.sub(" ", body["message"]), 60),
                createdAt=created_at,
                ttl=ttl,
            )
        )

    async def turn(emit: Callable[[dict[str, Any]], None]) -> None:
        model_deadline = (
            asyncio.get_running_loop().time()
            + deps.settings.chat_turn_deadline_seconds
            - min(1.0, deps.settings.chat_turn_deadline_seconds / 10)
        )

        async def persist(result: dict[str, Any], turn_id: str) -> None:
            updated_at = iso_ms(max(now(), started + timedelta(milliseconds=1)))
            await store.persist_completed_turn(
                make_message_item(
                    sessionId=session_id,
                    userSub=auth.sub,
                    role="assistant",
                    turnId=turn_id,
                    content=result["text"],
                    citations=result["citations"],
                    usage=result["usage"],
                    stopReason=result["stopReason"],
                    createdAt=updated_at,
                    ttl=ttl,
                )
            )

        try:
            emit(dict(type="session", sessionId=session_id))
            history = (
                []
                if is_new
                else to_history(
                    (await store.list_messages(session_id, deps.settings.max_history_messages))[
                        "items"
                    ]
                )
            )
            turn_id = str(uuid4())
            await store.persist_submitted_message(
                make_message_item(
                    sessionId=session_id,
                    userSub=auth.sub,
                    role="user",
                    content=body["message"],
                    turnId=turn_id,
                    createdAt=created_at,
                    ttl=ttl,
                )
            )
            try:
                # Reserve up to one second inside the response deadline for a failure row.
                async with asyncio.timeout_at(model_deadline):
                    result = await run_agent(
                        model=deps.model,
                        retriever=deps.retriever,
                        guardrail=deps.guardrail,
                        history=history,
                        user_message=body["message"],
                        emit=emit,
                        top_k=deps.settings.retrieval_top_k,
                        max_iterations=deps.settings.max_tool_iterations,
                        max_context_chars=deps.settings.llm_max_context_chars,
                    )
                await persist(result, turn_id)
                emit(
                    dict(
                        type="done",
                        sessionId=session_id,
                        stopReason=result["stopReason"],
                        usage=result["usage"],
                    )
                )
            except Exception as exc:
                result = (
                    exc.partial_result
                    if isinstance(exc, AgentStreamError)
                    else dict(
                        text=GENERIC_FAILURE,
                        citations=[],
                        usage={"inputTokens": 0, "outputTokens": 0},
                        stopReason="error",
                    )
                )
                try:
                    await persist(result, turn_id)
                except Exception as persistence_error:
                    log(
                        "error",
                        "failed to persist failure assistant row",
                        errorType=type(persistence_error).__name__,
                    )
                raise
        except Exception as exc:
            log("error", "chat request failed after stream commit", errorType=type(exc).__name__)
            emit(
                dict(
                    type="error",
                    message=STREAM_FAILURE
                    if isinstance(exc, AgentStreamError)
                    else GENERIC_FAILURE,
                )
            )

    return ChatResponse(turn, deps.settings.chat_turn_deadline_seconds)
