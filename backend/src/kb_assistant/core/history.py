from typing import Any

from .citations import CITATION_RE
from .text import js_trim
from .transcript import AssistantTurn, TextBlock, Turn, UserText


def to_history(items: list[dict[str, Any]]) -> list[Turn]:
    answers = {
        item["turnId"]: js_trim(CITATION_RE.sub("", item["content"]))
        for item in items
        if item["role"] == "assistant"
        and "turnId" in item
        and item.get("stopReason") != "guardrail_intervened"
    }
    result: list[Turn] = []
    for item in items:
        if item["role"] != "user" or "turnId" not in item:
            continue
        question = js_trim(item["content"])
        answer = answers.get(item["turnId"])
        if question and answer:
            result.extend([UserText(question), AssistantTurn([TextBlock(0, answer)])])
    return result


def make_session_item(
    *, sessionId: str, userSub: str, title: str, createdAt: str, ttl: int
) -> dict[str, Any]:
    return dict(
        pk=f"SESSION#{sessionId}",
        sk="META",
        gsi1pk=f"USER#{userSub}#SESSION",
        gsi1sk=createdAt,
        sessionId=sessionId,
        userSub=userSub,
        title=title,
        createdAt=createdAt,
        updatedAt=createdAt,
        messageCount=0,
        ttl=ttl,
    )


def make_message_item(
    *,
    sessionId: str,
    userSub: str,
    role: str,
    content: str,
    createdAt: str,
    ttl: int,
    id: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
    stopReason: str | None = None,
    turnId: str | None = None,
) -> dict[str, Any]:
    from kb_assistant.timeutil import message_sk

    item = dict(
        pk=f"SESSION#{sessionId}",
        sk=message_sk(createdAt, id),
        userSub=userSub,
        role=role,
        content=content,
        citations=citations or [],
        createdAt=createdAt,
        ttl=ttl,
    )
    item.update(
        {
            k: v
            for k, v in dict(usage=usage, stopReason=stopReason, turnId=turnId).items()
            if v is not None
        }
    )
    return item
