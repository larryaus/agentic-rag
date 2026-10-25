import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kb_assistant.api import chat, documents, health, local_blobs, sessions, uploads
from kb_assistant.api.spa import spa_router
from kb_assistant.container import Container
from kb_assistant.core.sse import js_json_dumps
from kb_assistant.errors import AppError
from kb_assistant.logging import log, log_context
from kb_assistant.settings import Settings


class LegacyJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return js_json_dumps(content).encode("utf-8")


class RequestLogging:
    def __init__(self, app: ASGIApp, trust_lambda_context: bool = False) -> None:
        self.app = app
        # Only the Lambda Web Adapter sets this header; elsewhere a client could.
        self.trust_lambda_context = trust_lambda_context

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        response_started = False
        response_finished = False

        async def track_send(message: Message) -> None:
            nonlocal response_started, response_finished
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_finished = True
            await send(message)

        with log_context(requestId=request_id(scope, self.trust_lambda_context)):
            log("info", "http request started", method=scope["method"], path=scope["path"])
            try:
                await self.app(scope, receive, track_send)
            except Exception as exc:
                # Handle inside Starlette's error middleware so uvicorn never receives
                # the exception and prints a second, potentially sensitive traceback.
                log("error", "http request failed", errorType=type(exc).__name__)
                try:
                    if not response_started:
                        await LegacyJSONResponse(
                            {"message": "Internal server error"}, status_code=500
                        )(scope, receive, send)
                    elif not response_finished:
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                except OSError:
                    pass
            finally:
                log(
                    "info",
                    "http request completed",
                    durationMs=int((time.monotonic() - started) * 1000),
                )


def request_id(scope: Scope, trust_lambda_context: bool) -> str:
    if trust_lambda_context:
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-amzn-lambda-context":
                try:
                    return str(UUID(json.loads(value)["request_id"]))
                except Exception:
                    # Any malformed value, including pathologically nested JSON, is ignored.
                    break
    return str(uuid4())


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = app.state.container
        await container.start()
        try:
            container.initialize_http()
            yield
        finally:
            await container.stop()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
        default_response_class=LegacyJSONResponse,
        lifespan=lifespan,
    )
    app.state.container = Container(settings)
    app.add_middleware(RequestLogging, trust_lambda_context=settings.deploy_target == "aws")
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[s.strip() for s in settings.cors_allow_origins.split(",")],
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["authorization", "content-type"],
        )

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        return LegacyJSONResponse({"message": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return LegacyJSONResponse({"message": "Invalid request"}, status_code=400)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        message = (
            "Method not allowed"
            if exc.status_code == 405
            else "Not found"
            if exc.status_code == 404
            else str(exc.detail)
        )
        return LegacyJSONResponse({"message": message}, status_code=exc.status_code)

    app.include_router(health.router)
    for name, router in {
        "uploads": uploads.router,
        "documents": documents.router,
        "sessions": sessions.router,
        "chat": chat.router,
    }.items():
        if name in settings.route_names:
            app.include_router(router)
    if settings.deploy_target == "local":
        app.include_router(local_blobs.router)
        if settings.frontend_dist_dir:
            # Last, so every API route takes precedence over the frontend's fallback.
            app.include_router(spa_router(Path(settings.frontend_dist_dir)))
    return app
