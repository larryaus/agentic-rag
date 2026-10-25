from typing import Any

from fastapi import APIRouter, Request

from kb_assistant.core.paging import decode_page_token, parse_limit
from kb_assistant.errors import AppError

from .deps import Auth, Dependencies

router = APIRouter()
SUMMARY_FIELDS = ("sessionId", "title", "createdAt", "updatedAt", "messageCount")


def summary(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in SUMMARY_FIELDS}


@router.get("/v1/sessions")
async def sessions(request: Request, auth: Auth, deps: Dependencies) -> dict[str, Any]:
    token = request.query_params.get("nextToken")
    if token is not None:
        decode_page_token(
            token, ["pk", "sk", "gsi1pk", "gsi1sk"], "gsi1pk", f"USER#{auth.sub}#SESSION"
        )
    page = await deps.store.list_sessions(
        auth.sub,
        parse_limit(request.query_params.get("limit"), 25),
        request.query_params.get("nextToken"),
    )
    return {**page, "items": [summary(item) for item in page["items"]]}


@router.get("/v1/sessions/{session_id}")
async def detail(
    session_id: str, request: Request, auth: Auth, deps: Dependencies
) -> dict[str, Any]:
    meta = await deps.store.get_session(session_id)
    if meta is None:
        raise AppError("Session not found", 404)
    if meta["userSub"] != auth.sub:
        raise AppError("Forbidden", 403)
    token = request.query_params.get("nextToken")
    if token is not None:
        decode_page_token(token, ["pk", "sk"], "pk", f"SESSION#{session_id}")
    page = await deps.store.list_messages(
        session_id,
        parse_limit(request.query_params.get("limit"), 50),
        request.query_params.get("nextToken"),
    )
    messages = [
        dict(
            role=item["role"],
            content=item["content"],
            citations=item.get("citations", []),
            createdAt=item["createdAt"],
        )
        for item in page["items"]
    ]
    return {
        **summary(meta),
        "messages": messages,
        **({"nextToken": page["nextToken"]} if "nextToken" in page else {}),
    }
