"""Tool definitions and dispatch for the agent loop."""

from __future__ import annotations

from typing import Any

import asyncpg

from agent_service.config import settings
from agent_service.db import repo
from agent_service.db.pool import get_pool
from agent_service.retrieval.embeddings import embed_query
from agent_service.retrieval.hybrid import hybrid_search
from agent_service.retrieval.query_rewrite import rewrite_query
from agent_service.retrieval.reranker import rerank


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge_base",
        "description": (
            "在企业知识库中进行混合检索（向量 + 关键词 + RRF 融合 + 重排）。"
            "回答任何事实性问题前必须调用本工具。"
            "返回的每个 hit 都带有 `citation_token`，请把它原样贴在你的回答中。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用于检索的查询短语（可在用户问题基础上精炼）",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "返回的命中数量，默认 8",
                },
                "document_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "可选：将检索范围限定在这些文档 ID",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_document",
        "description": "按 ID 拉取整篇文档的元数据与正文（截断至约 8k tokens）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "integer"},
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "list_documents",
        "description": "列出知识库中可用的文档（支持按标题模糊匹配）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "query": {"type": "string", "description": "标题模糊匹配关键词"},
            },
        },
    },
]


def tools_for_openai() -> list[dict[str, Any]]:
    """Return tool definitions in OpenAI Chat Completions function format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in TOOLS
    ]


# ---------- dispatch ----------

async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    pool = await get_pool()
    if name == "search_knowledge_base":
        return await _tool_search(pool, args)
    if name == "fetch_document":
        return await _tool_fetch(pool, args)
    if name == "list_documents":
        return await _tool_list(pool, args)
    return {"error": f"unknown tool: {name}"}


def _truncate(text: str, n: int = 280) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= n else text[:n] + "…"


async def _tool_search(pool: asyncpg.Pool, args: dict) -> dict:
    user_query = args["query"]
    top_k = int(args.get("top_k") or settings.RETRIEVAL_FINAL_K)
    doc_ids = args.get("document_ids") or None

    rewritten = await rewrite_query(user_query)
    qvec = await embed_query(rewritten)

    async with pool.acquire() as conn:
        candidates = await hybrid_search(
            conn,
            query_text=rewritten,
            query_embedding=qvec,
            document_ids=doc_ids,
            candidate_k=settings.RETRIEVAL_TOP_K,
            final_k=max(top_k * 3, 20),
        )

    final = await rerank(rewritten, candidates, top_n=top_k)

    hits = []
    for h in final:
        hits.append(
            {
                "citation_token": f"[doc:{h['document_id']}#chunk:{h['ord']}]",
                "document_id": h["document_id"],
                "chunk_id": h["chunk_id"],
                "ord": h["ord"],
                "title": h["title"],
                "snippet": _truncate(h["content"], 400),
                "score": float(h.get("score") or 0.0),
            }
        )
    return {"query": rewritten, "hits": hits}


async def _tool_fetch(pool: asyncpg.Pool, args: dict) -> dict:
    doc_id = int(args["document_id"])
    async with pool.acquire() as conn:
        meta = await repo.get_document(conn, doc_id)
        if not meta:
            return {"error": f"document {doc_id} not found"}
        text = await repo.get_document_full_text(conn, doc_id, char_limit=32000)
    return {
        "document_id": doc_id,
        "title": meta["title"],
        "mime_type": meta["mime_type"],
        "content": text,
    }


async def _tool_list(pool: asyncpg.Pool, args: dict) -> dict:
    limit = int(args.get("limit") or 50)
    query = args.get("query")
    async with pool.acquire() as conn:
        docs = await repo.list_documents(conn, limit=limit, query=query)
    return {
        "documents": [
            {
                "id": d["id"],
                "title": d["title"],
                "mime_type": d["mime_type"],
                "chunk_count": d["chunk_count"],
            }
            for d in docs
        ]
    }
