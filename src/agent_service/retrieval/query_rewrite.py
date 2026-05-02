"""Cheap query rewrite using Haiku — turn conversational query into a
retrieval-friendly phrase. Fails open: returns original on any error.
"""

from __future__ import annotations

from agent_service.config import settings
from agent_service.deps import get_anthropic


_PROMPT = (
    "你是检索查询改写器。把用户问题改写为更适合关键词+向量混合检索的简短查询，"
    "保留关键实体和限定词，去掉寒暄与代词。只输出改写后的查询，不要解释。"
)


async def rewrite_query(user_text: str) -> str:
    try:
        client = get_anthropic()
        msg = await client.messages.create(
            model=settings.CHEAP_MODEL,
            max_tokens=128,
            system=_PROMPT,
            messages=[{"role": "user", "content": user_text}],
        )
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                rewritten = block.text.strip()
                return rewritten or user_text
        return user_text
    except Exception:
        return user_text
